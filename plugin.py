from typing import Any, Dict, List, Optional, Tuple, cast

from maibot_sdk import API, Command, MaiBotPlugin

from pathlib import Path

import asyncio
import httpx
import json

from .audio import AudioBridge, list_devices, resolve_device
from .auto_calls import AutoCalls
from .call_routing import route, svcl_path
from .dialogue import Dialogue
from .invite import Inviter
from .session import CallSession, prepare_fillers
from .settings import VoiceCallConfig
from .speech import LocalSpeech


class QQVoiceCallPlugin(MaiBotPlugin):
    config_model = VoiceCallConfig

    def __init__(self) -> None:
        super().__init__()
        self._ready = False
        self._lock = asyncio.Lock()
        self._session: Optional[CallSession] = None
        self._bridge: Optional[AutoCalls] = None
        self._supervisor: Optional[asyncio.Task[None]] = None
        self._paused = False  # 用户在控制页点了停止：守护任务不自动重开
        self._last_listen_error = ""
        self._routing: Optional[asyncio.Task[None]] = None
        self._inviter: Optional[Inviter] = None
        self._invite_task: Optional[asyncio.Task[None]] = None
        self._route_again = False
        # 等待来电时预合成的短应答，按语音与短应答配置缓存，跨通话复用。
        self._fillers: List[bytes] = []
        self._fillers_key = ""
        self._filler_task: Optional[asyncio.Task[None]] = None

    async def on_load(self) -> None:
        self._ready = True
        self.ctx.logger.info("QQ 语音插件已加载")
        await self._auto_listen()
        self._supervisor = asyncio.create_task(self._supervise(), name="qq_call_supervisor")
        self._inviter = Inviter(self.ctx, lambda: self.config.invite, lambda: self.config.plugin.allowed_qq_ids,
                                self._in_call, self.ctx.logger,
                                Path(__file__).resolve().parent / "data" / "invite_state.json")
        self._invite_task = asyncio.create_task(self._inviter.run(), name="qq_call_invite")

    async def on_unload(self) -> None:
        self._ready = False
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
            self._supervisor = None
        if self._invite_task is not None:
            self._invite_task.cancel()
            await asyncio.gather(self._invite_task, return_exceptions=True)
            self._invite_task = None
        if self._routing is not None:
            self._routing.cancel()
            await asyncio.gather(self._routing, return_exceptions=True)
        if self._filler_task is not None:
            self._filler_task.cancel()
            await asyncio.gather(self._filler_task, return_exceptions=True)
            self._filler_task = None
        await self._stop_watcher()
        async with self._lock:
            await self._stop()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope == "self":
            await self._stop_watcher()
            async with self._lock:
                await self._stop()
            self._paused = False
            await self._auto_listen()
            self.ctx.logger.info("语音插件配置已更新，旧通话音频已停止")

    async def _auto_listen(self) -> None:
        # 语音服务或通话桥没就绪时不让插件加载失败；控制页可稍后点“等待 QQ 来电”重试。
        if not (self.config.plugin.enabled and self.config.bridge.enabled):
            return
        try:
            await self.listen()
            if self._last_listen_error:
                self.ctx.logger.info("自动接听已恢复")
            self._last_listen_error = ""
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != self._last_listen_error:  # 同样的原因只记一次，避免刷屏
                self.ctx.logger.warning(f"自动接听未启动，将每 15 秒重试：{message}")
            self._last_listen_error = message

    async def _supervise(self, interval: float = 15) -> None:
        """语音服务、NapCat 通话桥晚于小睦启动或中途重启时，自动重新开始等待来电。"""
        while True:
            await asyncio.sleep(interval)
            if self._ready and not self._paused and not self._watching() and not self._in_call():
                await self._auto_listen()

    async def _stop_watcher(self) -> None:
        if self._bridge is not None:
            await self._bridge.stop()

    async def _end_auto_call(self) -> None:
        async with self._lock:
            await self._stop()

    @API("voice_listen", description="开启自动接听和通话跟踪", public=False)
    async def listen(self) -> Dict[str, Any]:
        self._paused = False
        async with self._lock:
            config = cast(VoiceCallConfig, self.config).model_copy(deep=True)
            if not self._ready or not config.plugin.enabled or not config.bridge.enabled:
                raise ValueError("请先启用插件和自动接听桥")
            if not config.plugin.allowed_qq_ids or not config.bridge.token_file:
                raise ValueError("请配置允许通话的 QQ 号及桥接 Token 文件")
            if not config.audio.has_devices():
                raise ValueError("请完成一次性的音频设备配置")
            if self._bridge is not None and self._watching():
                return self._bridge.snapshot()
            if self._in_call():
                raise ValueError("请先停止当前手动桥接")
            # 模型服务未准备好时不自动接电话。
            async with httpx.AsyncClient(timeout=httpx.Timeout(config.speech.timeout_seconds, connect=5),
                                         trust_env=False) as client:
                await LocalSpeech(config.speech, client, config.chat.prompt_language).check()
            self._bridge = AutoCalls(config.bridge, config.plugin.allowed_qq_ids, self._start,
                                     self._end_auto_call, self.ctx.logger, self._on_call_phase)
            self._warm_fillers(config)
            self._schedule_routing("开始等待来电")
            self._bridge.task = asyncio.create_task(self._bridge.run(), name="qq_call_watcher")
            await asyncio.sleep(0)
            return self._bridge.snapshot()

    @staticmethod
    def _filler_key(config: VoiceCallConfig) -> str:
        return json.dumps([config.speech.model_dump(mode="json"), config.chat.prompt_language, config.chat.fillers],
                          ensure_ascii=False, sort_keys=True)

    def _warm_fillers(self, config: VoiceCallConfig) -> None:
        key = self._filler_key(config)
        if not config.chat.fillers or key == self._fillers_key:
            return
        if self._filler_task is not None and not self._filler_task.done():
            self._filler_task.cancel()
        self._fillers_key = key
        self._fillers = []
        self._filler_task = asyncio.create_task(self._prepare_fillers(config, self._fillers),
                                                name="qq_call_fillers")

    async def _prepare_fillers(self, config: VoiceCallConfig, out: List[bytes]) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(config.speech.timeout_seconds, connect=5),
                                     trust_env=False, follow_redirects=False) as client:
            await prepare_fillers(config, LocalSpeech(config.speech, client, config.chat.prompt_language),
                                  self.ctx.logger, out)
        if len(out) < len(config.chat.fillers) and out is self._fillers:
            self._fillers_key = ""  # 有短应答没合成成功：下次开始等待来电时重新合成

    def _in_call(self) -> bool:
        return self._session is not None and self._session.task is not None and not self._session.task.done()

    def _watching(self) -> bool:
        return self._bridge is not None and self._bridge.task is not None and not self._bridge.task.done()

    @Command("voice_call_invite_test", pattern=r"^/约电话$", description="立即让小睦判断现在要不要约你打电话（测试用）")
    async def invite_command(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        if self._inviter is None:
            return False, "插件尚未加载完成", 2
        qq = self._inviter.target()
        stream = await self.ctx.chat.get_stream_by_user_id(qq, platform="qq") if qq else None
        if not isinstance(stream, dict) or stream.get("session_id") != stream_id:
            return False, "只能在约电话对象的私聊里使用", 2
        try:
            decision = await self._inviter.check(force=True)
        except Exception as exc:
            await self.ctx.send.text(f"约电话判断失败：{type(exc).__name__}: {exc}", stream_id)
            return False, "约电话判断失败", 2
        if not decision.invite:
            await self.ctx.send.text(f"（测试）小睦这次决定先不约：{decision.reason}", stream_id)
        return True, "已完成约电话判断", 2

    def _on_call_phase(self, phase: str, peer: Optional[str]) -> None:
        # 响铃时就切好，接通后再切一次（通话时可能新开了 QQ 子进程）。
        if phase in {"ringing", "connected"} and peer in self.config.plugin.allowed_qq_ids:
            self._schedule_routing("来电响铃" if phase == "ringing" else "电话接通")

    def _schedule_routing(self, reason: str) -> None:
        if not self.config.audio.route_call_audio:
            return
        if self._routing is not None and not self._routing.done():
            self._route_again = True  # 正在切换，结束后再切一次
            return
        self._route_again = False
        self._routing = asyncio.create_task(self._route_call_audio(reason), name="qq_call_audio_route")

    async def _route_call_audio(self, reason: str) -> None:
        audio = self.config.audio
        while True:
            try:
                message = await asyncio.to_thread(
                    route, svcl_path(audio.svcl_path), audio.call_render_device, audio.call_capture_device)
                self.ctx.logger.info(f"{reason}：{message}")
            except Exception as exc:
                self.ctx.logger.warning(f"{reason}：自动切换 QQ 通话设备失败（{type(exc).__name__}: {exc}）。"
                                        "可在音量合成器里手动把通话中的 QQ 改为 PLAYBACK 3/4 和 VIRTUAL REC 5/6")
            if not self._route_again:
                return
            self._route_again = False

    async def _stop(self) -> None:
        if self._session is not None and self._session.task is not None:
            task = self._session.task
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @API("voice_devices", description="列出本机音频设备，不打开设备", public=False)
    async def devices(self) -> Dict[str, Any]:
        return {"devices": await asyncio.to_thread(list_devices)}

    @API("voice_status", description="查看音频桥接状态和耗时", public=False)
    async def status(self) -> Dict[str, Any]:
        if self._session is None:
            result = {"state": "未启动", "active": False, "chat_name": "", "error": "", "metrics": {}, "reason": ""}
        else:
            result = self._session.snapshot()
        result["bridge"] = self._bridge.snapshot() if self._bridge is not None else {"state": "未启用自动接听", "active": False, "error": ""}
        return result

    @API("voice_start", description="手动接听 QQ 后，绑定指定私聊并开始音频桥接", public=False)
    async def start(self, qq_user_id: str, confirmed_connected: bool = False) -> Dict[str, Any]:
        if self.config.bridge.enabled:
            raise ValueError("已选择自动接听，请使用“等待 QQ 来电”，无需手动填写来电者")
        return await self._start(qq_user_id, confirmed_connected)

    async def _start(self, qq_user_id: str, confirmed_connected: bool) -> Dict[str, Any]:
        async with self._lock:
            config = cast(VoiceCallConfig, self.config).model_copy(deep=True)
            if not self._ready or not config.plugin.enabled:
                raise ValueError("请先启用 QQ 语音插件")
            if confirmed_connected is not True:
                raise ValueError("请先手动接通 QQ 并确认来电者与填写的 QQ 号一致")
            if not qq_user_id.isdigit() or qq_user_id not in config.plugin.allowed_qq_ids:
                raise ValueError("该 QQ 号不在插件允许列表中")
            if self._in_call():
                raise ValueError("已有音频桥接正在运行，请先停止")
            if not config.audio.has_devices():
                raise ValueError("请先选择通话输入和输出设备")
            async with asyncio.timeout(config.speech.timeout_seconds):
                stream = await self.ctx.chat.get_stream_by_user_id(qq_user_id, platform="qq")
                if not isinstance(stream, dict) or not stream.get("session_id"):
                    raise ValueError("找不到该用户的真实私聊，请先向小睦发送一条 QQ 私聊消息")
                if (stream["platform"] != "qq" or stream["is_group_session"]
                        or str(stream["user_id"]) != qq_user_id):
                    raise ValueError("聊天流身份校验失败")
                stream_id = stream["session_id"]
                name = f"{stream['user_nickname'] or qq_user_id}的私聊"
                dialogue = await Dialogue.create(self.ctx, config.chat, stream_id, name)
            client = httpx.AsyncClient(timeout=httpx.Timeout(config.speech.timeout_seconds, connect=5),
                                       trust_env=False, follow_redirects=False)
            speech = LocalSpeech(config.speech, client, config.chat.prompt_language)
            input_device, output_device = await asyncio.to_thread(
                lambda: (resolve_device(config.audio.input_device_name, config.audio.input_device, "input"),
                         resolve_device(config.audio.output_device_name, config.audio.output_device, "output")))
            audio = AudioBridge(input_device, output_device, config.speech.tts_sample_rate)
            try:
                # 自动接听时 listen() 已试合成过；接通后只查健康，不再让对方多等一次合成。
                await speech.check(synthesize=not config.bridge.enabled)
                # 开设备发生在线程中。若请求取消，必须等开设备结束再清理，避免后台继续录音。
                opening = asyncio.create_task(asyncio.to_thread(audio.open))
                try:
                    await asyncio.shield(opening)
                except asyncio.CancelledError:
                    await opening
                    raise
                fillers = self._fillers if self._fillers_key == self._filler_key(config) else None
                session = CallSession(config, audio, speech, dialogue, self.ctx.logger, name, stream_id, fillers)
                self._session = session
                if self._inviter is not None:
                    self._inviter.call_started()
                session.task = asyncio.create_task(session.run(), name="qq_voice_call")
                self.ctx.logger.info(f"来电已接通：{name}，开始收发音频（录音设备 {input_device}，播放设备 {output_device}）")
                # 让 run() 进入 try/finally，保证紧接着的 stop/unload 也会关闭设备。
                await asyncio.sleep(0)
            except BaseException:
                await self._stop()
                await asyncio.to_thread(audio.close)
                await client.aclose()
                raise
            return session.snapshot()

    @API("voice_stop", description="停止音频收发；QQ 电话需在客户端手动挂断", public=False)
    async def stop(self) -> Dict[str, Any]:
        self._paused = True
        await self._stop_watcher()
        async with self._lock:
            await self._stop()
            return await self.status()

    @Command("voice_call_stop", pattern=r"^/电话停止$", description="当前通话用户停止音频桥接")
    async def stop_command(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        if self._session is None or stream_id != self._session.stream_id:
            return False, "当前私聊没有绑定语音桥接", 2
        await self.stop()
        await self.ctx.send.text("语音桥接已停止，请在 QQ 中挂断电话。", stream_id)
        return True, "已停止音频桥接", 2


def create_plugin() -> QQVoiceCallPlugin:
    return QQVoiceCallPlugin()
