from queue import Empty, Full, Queue
from threading import Lock
from typing import Any, Dict, List, Optional

import asyncio
import sounddevice as sd


def list_devices() -> List[Dict[str, Any]]:
    hosts = sd.query_hostapis()
    return [
        {"id": i, "name": d["name"], "host": hosts[d["hostapi"]]["name"],
         "input_channels": d["max_input_channels"], "output_channels": d["max_output_channels"]}
        for i, d in enumerate(sd.query_devices())
    ]


def resolve_device(name: str, index: int, kind: str) -> int:
    """按名称找设备；名称为空时用编号。优先 MME（共享模式，最容易协商采样率），其次其他接口。"""
    name = name.strip()
    if not name:
        return index
    channel_key = "max_input_channels" if kind == "input" else "max_output_channels"
    hosts = sd.query_hostapis()
    candidates = [(i, d, hosts[d["hostapi"]]["name"]) for i, d in enumerate(sd.query_devices()) if d[channel_key] > 0]
    for exact in (True, False):
        for preferred in ("MME", None):
            found = [i for i, d, host in candidates
                     if (preferred is None or host == preferred)
                     and (d["name"].strip() == name if exact else name.lower() in d["name"].lower())]
            if found:
                return found[0]
    label = "录音" if kind == "input" else "播放"
    raise ValueError(f"找不到名称为“{name}”的{label}设备，请检查 Windows 声音设置里该设备是否已启用")


class AudioBridge:
    """PortAudio 回调仅搬运有界缓冲区；模型请求始终在异步任务中运行。"""

    def __init__(self, input_device: int, output_device: int, sample_rate: int) -> None:
        self.input_device = input_device
        self.output_device = output_device
        self.sample_rate = sample_rate
        self.incoming: Queue[bytes] = Queue(maxsize=100)
        self.pending = bytearray()
        self.lock = Lock()
        self.error: Optional[str] = None
        self.closed = False
        self.input_stream: Optional[sd.RawInputStream] = None
        self.output_stream: Optional[sd.RawOutputStream] = None
        # PortAudio 回调在音频线程里，通过 call_soon_threadsafe 唤醒事件循环里的等待者，而不是定时轮询。
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.readable = asyncio.Event()  # 收到新帧、出错或关闭
        self.played = asyncio.Event()  # 播放回调取走了缓冲、出错或关闭
        # 偶发欠载/溢出（系统卡顿一下）只计数，由会话记日志；不当作设备故障挂断整通电话。
        self.input_xruns = 0
        self.output_xruns = 0
        self.silence = bytes(sample_rate // 50 * 2)  # 预分配的一块静音，播放回调里不再新建对象

    def open(self) -> None:
        if self.input_device < 0 or self.output_device < 0 or self.input_device == self.output_device:
            raise ValueError("必须显式选择两个不同的音频设备，不能使用默认麦克风")
        sd.check_input_settings(device=self.input_device, channels=1, dtype="int16", samplerate=16000)
        sd.check_output_settings(device=self.output_device, channels=1, dtype="int16", samplerate=self.sample_rate)
        try:
            self.input_stream = sd.RawInputStream(device=self.input_device, samplerate=16000,
                                                 channels=1, dtype="int16", blocksize=320, callback=self._capture)
            self.output_stream = sd.RawOutputStream(device=self.output_device, samplerate=self.sample_rate,
                                                   channels=1, dtype="int16", blocksize=self.sample_rate // 50,
                                                   callback=self._play)
            self.output_stream.start()
            self.input_stream.start()
        except BaseException:
            self.close()
            raise

    def _notify(self, event: asyncio.Event) -> None:
        loop = self.loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # 事件循环已关闭：没有等待者需要唤醒

    def _capture(self, data: Any, frames: int, timing: Any, status: Any) -> None:
        if self.closed:
            return
        if status:
            self.input_xruns += 1  # 溢出时这一帧数据仍然有效，照常送出
        try:
            self.incoming.put_nowait(bytes(data))
        except Full:
            self.error = "输入音频积压超过 2 秒，已停止以避免播放过期回复"
        self._notify(self.readable)

    def _play(self, data: Any, frames: int, timing: Any, status: Any) -> None:
        if status:
            self.output_xruns += 1
        count = len(data)
        with self.lock:
            available = min(count, len(self.pending))
            if available:
                # memoryview 直接拷贝，不生成中间 bytes；视图释放后才能缩短 bytearray。
                with memoryview(self.pending) as view:
                    data[:available] = view[:available]
                del self.pending[:available]
        if available < count:
            if count - available > len(self.silence):
                self.silence = bytes(count - available)
            data[available:] = self.silence[:count - available]
        if available or self.error:
            self._notify(self.played)

    def check(self) -> None:
        if self.error:
            raise RuntimeError(self.error)
        if self.closed:
            raise RuntimeError("音频桥接已关闭")

    async def read(self) -> bytes:
        self.loop = asyncio.get_running_loop()
        while True:
            self.check()
            try:
                return self.incoming.get_nowait()
            except Empty:
                pass
            self.readable.clear()
            # 清除标志后再看一次队列，避免回调恰好在两步之间放入帧而错过唤醒。
            if self.incoming.empty() and not self.error and not self.closed:
                await self.readable.wait()

    async def write(self, pcm: bytes) -> None:
        if len(pcm) % 2:
            raise ValueError("PCM16 音频长度必须为偶数")
        # 最多缓存 2 秒音频，HTTP 消费端施加背压；打断可以立即清空。
        self.loop = asyncio.get_running_loop()
        for offset in range(0, len(pcm), 4096):
            chunk = pcm[offset:offset + 4096]
            while True:
                self.check()
                with self.lock:
                    if len(self.pending) + len(chunk) <= self.sample_rate * 4:
                        self.pending.extend(chunk)
                        break
                    # 在锁内清除：回调取走数据也要持锁，之后一定会再置位，不会漏掉唤醒。
                    self.played.clear()
                await self.played.wait()

    async def drain(self) -> None:
        self.loop = asyncio.get_running_loop()
        while True:
            self.check()
            with self.lock:
                if not self.pending:
                    break
                self.played.clear()
            await self.played.wait()
        # 缓冲已消费不等于扬声器已播完；等待 PortAudio 报告的输出延迟。
        if self.output_stream is not None:
            await asyncio.sleep(float(self.output_stream.latency) + 0.02)

    def buffered(self) -> int:
        """还没交给声卡的待播字节数。"""
        with self.lock:
            return len(self.pending)

    def interrupt(self) -> None:
        with self.lock:
            self.pending.clear()

    def close(self) -> None:
        self.closed = True
        self.interrupt()
        self._notify(self.readable)
        self._notify(self.played)
        try:
            if self.input_stream is not None:
                self.input_stream.close()
        finally:
            if self.output_stream is not None:
                self.output_stream.close()
