"""虚拟声卡回环测试：往每个虚拟播放通道放一段很轻的 1 kHz 提示音，看它从哪个虚拟录音通道出来。

只使用 MME 接口下名称含 PLAYBACK / VIRTUAL REC 的设备（ROUTIST R2 的虚拟通道），
不碰真实麦克风和扬声器。结果用于确定 QQ 扬声器、QQ 麦克风和插件 input/output_device 的接法。
"""

from array import array
from typing import Dict, List

import math
import threading
import time

RATE = 48000
SECONDS = 0.8
AMPLITUDE = 0.05  # 约 -26 dBFS，很轻


def tone() -> bytes:
    frames = array("h")
    total = int(RATE * SECONDS)
    for index in range(total):
        fade = min(1.0, index / 2400, (total - index) / 2400)
        value = int(32767 * AMPLITUDE * fade * math.sin(2 * math.pi * 1000 * index / RATE))
        frames.extend((value, value))
    return frames.tobytes()


def level_db(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return -120.0
    mean = sum(value * value for value in samples) / len(samples)
    return 20 * math.log10(max(math.sqrt(mean) / 32768, 1e-6))


def run() -> Dict[str, Dict[str, float]]:
    import sounddevice as sd

    hosts = sd.query_hostapis()
    devices = list(enumerate(sd.query_devices()))
    mme = [(i, d) for i, d in devices if hosts[d["hostapi"]]["name"] == "MME"]
    outputs = [(i, d["name"]) for i, d in mme if d["max_output_channels"] >= 2 and "PLAYBACK" in d["name"]]
    inputs = [(i, d["name"]) for i, d in mme if d["max_input_channels"] >= 2 and "VIRTUAL REC" in d["name"]]
    signal = tone()
    results: Dict[str, Dict[str, float]] = {}
    for out_id, out_name in outputs:
        captured: Dict[int, List[bytes]] = {in_id: [] for in_id, _ in inputs}
        lock = threading.Lock()
        streams = []

        def make_callback(in_id: int):
            def callback(data, frames, timing, status):
                with lock:
                    captured[in_id].append(bytes(data))
            return callback

        try:
            for in_id, _ in inputs:
                stream = sd.RawInputStream(device=in_id, samplerate=RATE, channels=2, dtype="int16",
                                           callback=make_callback(in_id))
                stream.start()
                streams.append(stream)
            time.sleep(0.3)
            with lock:
                for chunks in captured.values():
                    chunks.clear()
            with sd.RawOutputStream(device=out_id, samplerate=RATE, channels=2, dtype="int16") as player:
                player.write(signal)
            time.sleep(0.3)
        finally:
            for stream in streams:
                stream.close()
        results[f"[{out_id}] {out_name}"] = {
            f"[{in_id}] {in_name}": level_db(b"".join(captured[in_id])) for in_id, in_name in inputs
        }
    return results


def report() -> None:
    print("\n== 虚拟声卡回环测试（会播放约 1 秒很轻的提示音） ==")
    try:
        results = run()
    except Exception as exc:
        print(f"  ★回环测试失败★ {type(exc).__name__}: {exc}")
        return
    if not results:
        print("  没有找到名称含 PLAYBACK / VIRTUAL REC 的 MME 设备。")
        return
    for output, levels in results.items():
        heard = [name for name, level in levels.items() if level > -45]
        print(f"  播放到 {output}：")
        for name, level in levels.items():
            mark = "  ← 收到" if level > -45 else ""
            print(f"      {name}：{level:6.1f} dBFS{mark}")
        if not heard:
            print("      （所有虚拟录音通道都没收到）")
