from typing import Any, Dict, List, Optional, Tuple

import asyncio
import logging
import math
import random
import time
import webrtcvad

from .audio import AudioBridge
from .dialogue import Dialogue, Reply
from .settings import VoiceCallConfig
from .speech import LocalSpeech
from .vad import TurnDetector


def _dbfs(peak: int) -> str:
    return f"{20 * math.log10(peak / 32768):.0f}" if peak > 0 else "-∞"


async def prepare_fillers(config: VoiceCallConfig, speech: LocalSpeech, logger: logging.Logger,
                          out: List[bytes]) -> None:
    """预先合成短应答，逐条追加到 out（边合成边可用）；失败只记录，不影响通话。"""
    for text in config.chat.fillers:
        try:
            async with asyncio.timeout(config.speech.timeout_seconds):
                pcm = b"".join([chunk async for chunk in speech.synthesize(text, "neutral")])
            # 最多 1.5 秒，避免应答本身拖慢正式回复。
            out.append(pcm[: config.speech.tts_sample_rate * 3])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"预合成短应答“{text}”失败：{type(exc).__name__}: {exc}")


class CallSession:
    def __init__(self, config: VoiceCallConfig, audio: AudioBridge, speech: LocalSpeech,
                 dialogue: Dialogue, logger: logging.Logger, name: str, stream_id: str,
                 fillers: Optional[List[bytes]] = None) -> None:
        self.config = config
        self.audio = audio
        self.speech = speech
        self.dialogue = dialogue
        self.logger = logger
        self.name = name
        self.stream_id = stream_id
        self.state = "等待说话"
        self.error = ""
        self.reason = ""
        self.metrics: Dict[str, float] = {}
        self.turn: Optional[asyncio.Task[None]] = None
        self.task: Optional[asyncio.Task[None]] = None
        self.completed_turns = 0
        self.interruptions = 0
        self.failed_turns = 0
        self.last_turn_error = ""
        # 插件在等待来电时已合成好的短应答（可能仍在后台追加）；没有时接通后自己合成。
        self.shared_fillers = fillers is not None
        self.fillers: List[bytes] = fillers if fillers is not None else []
        self.last_filler: Optional[int] = None
        self.filler_used_last_turn = False
        self.pending_barge = False  # 小睦说话/思考时听到对方出声，还没达到打断时长
        self.ignored_sounds = 0
        # 停顿达到 early_asr_ms 时提前发起的识别：(语音切片, 识别任务)
        self.early: Optional[Tuple[bytes, asyncio.Task[str]]] = None
        self.early_hits = 0

    async def interrupt(self) -> None:
        if self.turn is not None:
            if not self.turn.done():
                # 先让本句自己处理取消（统计已播出多少、清空播放缓冲），再兜底清空一次。
                self.turn.cancel()
                self.interruptions += 1
                try:
                    await self.turn
                except asyncio.CancelledError:
                    pass
                self.audio.interrupt()
            else:
                self.turn.result()  # 传播后台异常，不把失败当作正常结束。
            self.turn = None

    def _busy(self) -> bool:
        return self.turn is not None and not self.turn.done()

    async def _on_frame(self, detector: TurnDetector, frame: bytes, voiced: bool) -> None:
        """小睦说话或思考时，对方的短促声音（嗯、咳嗽、回声、杂音）不打断；持续说话超过 barge_in_ms 才打断。"""
        began, utterance = detector.feed(frame, voiced)
        early = detector.take_early()
        if voiced and self.early is not None:
            self._drop_early()  # 对方停顿后又接着说，提前识别的切片作废
        if early is not None and not (self.pending_barge and self._busy()):
            self.early = (early, asyncio.create_task(self._transcribe(early), name="qq_voice_early_asr"))
            # 被丢弃的提前识别若已失败，也要取走异常，避免“Task exception was never retrieved”。
            self.early[1].add_done_callback(lambda task: task.cancelled() or task.exception())
        if began:
            if self._busy():
                self.pending_barge = True
            else:
                self.state = "正在听"
        if self.pending_barge and detector.active and detector.voiced * 20 >= self.config.audio.barge_in_ms:
            self.pending_barge = False
            if self._busy():
                self.logger.info(f"小睦说话时被打断：对方持续说话超过 {self.config.audio.barge_in_ms}ms")
                await self.interrupt()
            self.state = "正在听"
        if utterance is not None:
            if self.pending_barge and self._busy():
                # 没达到打断时长的短声音：小睦继续说，这段不回应。
                self.pending_barge = False
                self.ignored_sounds += 1
                self.logger.info(f"小睦说话时听到 {len(utterance) / 32000:.1f} 秒短声音，没有打断")
                self._drop_early()
                return
            self.pending_barge = False
            transcript = None
            if self.early is not None:
                if self.early[0] == utterance:
                    transcript = self.early[1]
                    self.early = None
                else:
                    self._drop_early()
            await self.interrupt()
            self.turn = asyncio.create_task(self.respond(utterance, transcript), name="qq_voice_turn")
        elif not detector.active:
            self.pending_barge = False
            self._drop_early()

    def _drop_early(self) -> None:
        if self.early is not None:
            self.early[1].cancel()
            self.early = None

    async def _transcribe(self, pcm: bytes) -> str:
        async with asyncio.timeout(self.config.speech.timeout_seconds):
            return await self.speech.transcribe(pcm)

    async def run(self) -> None:
        detector = TurnDetector(self.config.audio)
        vad = webrtcvad.Vad(self.config.audio.vad_mode)
        start = last_voice = time.monotonic()
        frames = voiced_frames = 0
        peak = 0
        silence_warned = False
        xruns_logged = 0
        xrun_log_at = 0.0
        filler_task = (asyncio.create_task(self._prepare_fillers())
                       if self.config.chat.fillers and not self.shared_fillers else None)
        try:
            while True:
                if self.turn is not None and self.turn.done():
                    self.turn.result()
                    self.turn = None
                now = time.monotonic()
                if now - start >= self.config.audio.max_call_minutes * 60:
                    self.reason = "达到单次桥接时长上限"
                    break
                if now - last_voice >= self.config.audio.idle_seconds:
                    self.reason = "长时间没有用户讲话，已停止桥接"
                    break
                # 音频驱动异常停产时也必须可超时，不能永远等待一个回调。
                frame = await asyncio.wait_for(self.audio.read(), timeout=3)
                voiced = vad.is_speech(frame, 16000)
                frames += 1
                voiced_frames += voiced
                peak = max(peak, max(abs(v) for v in memoryview(frame).cast("h")))
                if not silence_warned and voiced_frames == 0 and now - start >= 10:
                    silence_warned = True
                    self.logger.warning(
                        f"接通 10 秒仍没听到对方说话（输入峰值 {_dbfs(peak)} dBFS）。"
                        "若对方一直在说话，请检查 Studio One 路由工程是否打开、QQ 的输出设备是否为 PLAYBACK 3/4")
                xruns = self.audio.input_xruns + self.audio.output_xruns
                if xruns > xruns_logged and now - xrun_log_at >= 10:
                    self.logger.warning(f"音频设备累计出现 {xruns} 次欠载/溢出；偶发不影响通话，频繁出现请检查 CPU 占用或声卡缓冲设置")
                    xruns_logged, xrun_log_at = xruns, now
                if voiced:
                    last_voice = now
                await self._on_frame(detector, frame, voiced)
        except asyncio.CancelledError:
            self.reason = "已手动停止或配置已更新"
            raise
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.logger.exception("QQ 语音桥接失败，已停止录音和播放")
        finally:
            if filler_task is not None and not filler_task.done():
                filler_task.cancel()
                await asyncio.gather(filler_task, return_exceptions=True)
            self._drop_early()
            if self.turn is not None:
                self.turn.cancel()
                await asyncio.gather(self.turn, return_exceptions=True)
                self.turn = None
            self.audio.interrupt()
            self.logger.info(
                f"通话音频已停止：{self.error or self.reason or '对方挂断'}；收到音频 {frames * 0.02:.0f} 秒，"
                f"其中检测到说话 {voiced_frames * 0.02:.1f} 秒，输入峰值 {_dbfs(peak)} dBFS，"
                f"完成回复 {self.completed_turns} 句（其中提前识别命中 {self.early_hits} 句），"
                f"被打断 {self.interruptions} 次，忽略短声音 {self.ignored_sounds} 次，"
                f"失败 {self.failed_turns} 句，音频欠载/溢出 {self.audio.input_xruns + self.audio.output_xruns} 次")
            try:
                await asyncio.to_thread(self.audio.close)
            finally:
                await self.speech.client.aclose()
                self.dialogue.history.clear()
                self.state = "发生错误" if self.error else "已停止"

    async def _prepare_fillers(self) -> None:
        """接通后预先合成短应答；失败只记录，不影响通话。"""
        await prepare_fillers(self.config, self.speech, self.logger, self.fillers)

    def _pick_filler(self) -> Optional[bytes]:
        if not self.fillers:
            return None
        choices = [i for i in range(len(self.fillers)) if i != self.last_filler] or [0]
        self.last_filler = random.choice(choices)
        return self.fillers[self.last_filler]

    async def respond(self, pcm: bytes, transcript: Optional[asyncio.Task[str]] = None) -> None:
        # 单句失败（模型格式、偶发超时等）只跳过这一句；连续 3 句失败才停止整通电话。
        try:
            await self._respond(pcm, transcript)
            self.failed_turns = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.audio.interrupt()
            self.failed_turns += 1
            self.last_turn_error = f"{type(exc).__name__}: {exc}"
            self.logger.warning(f"电话本句处理失败（连续第 {self.failed_turns} 次），已跳过：{self.last_turn_error}")
            self.state = "等待说话"
            if self.failed_turns >= 3:
                raise

    async def _respond(self, pcm: bytes, transcript: Optional[asyncio.Task[str]] = None) -> None:
        """transcript 为停顿期间已提前发起的识别；此时 asr_seconds 只统计停顿判定结束后还需等待的时间。"""
        began = time.monotonic()
        reply: Optional[Reply] = None
        written = 0  # 已放入播放缓冲的正式回复字节数
        try:
            self.state = "正在识别"
            mark = time.monotonic()
            if transcript is None:
                text = await self._transcribe(pcm)
            else:
                self.early_hits += 1
                try:
                    text = await transcript
                finally:
                    transcript.cancel()
            self.metrics["asr_seconds"] = round(time.monotonic() - mark, 3)
            if not text:
                self.state = "等待说话"
                return
            self.state = "正在思考"
            mark = time.monotonic()
            filler = None
            thinking = asyncio.create_task(self.dialogue.reply(text), name="qq_voice_reply")
            try:
                async with asyncio.timeout(self.config.speech.timeout_seconds):
                    chat = self.config.chat
                    # 像真人一样偶尔“嗯”一下：长句、回复迟迟没好、按概率、且不连续两句都用。
                    if (self.fillers and len(text) >= chat.filler_min_chars and not self.filler_used_last_turn
                            and random.random() < chat.filler_probability):
                        done, _ = await asyncio.wait({thinking}, timeout=chat.filler_delay_seconds)
                        if not done:
                            filler = self._pick_filler()
                            if filler is not None:
                                # 只放入播放缓冲，不等待播完；正式回复的音频排在它后面。
                                await self.audio.write(filler)
                                self.metrics["filler_seconds"] = round(time.monotonic() - began, 3)
                    reply = await thinking
            finally:
                if not thinking.done():
                    thinking.cancel()
                    await asyncio.gather(thinking, return_exceptions=True)
            self.filler_used_last_turn = filler is not None
            self.metrics["llm_seconds"] = round(time.monotonic() - mark, 3)
            self.state = "正在合成"
            mark = time.monotonic()
            first = True
            async with asyncio.timeout(self.config.speech.timeout_seconds):
                async for pcm_chunk in self.speech.synthesize(reply.text, reply.emotion):
                    if first:
                        self.metrics["tts_first_chunk_seconds"] = round(time.monotonic() - mark, 3)
                        self.metrics["speech_end_to_audio_seconds"] = round(time.monotonic() - began, 3)
                        first = False
                        self.logger.info(
                            f"电话耗时：识别 {self.metrics['asr_seconds']}s{'（提前识别）' if transcript is not None else ''}，回复 {self.metrics['llm_seconds']}s，"
                            f"合成首包 {self.metrics['tts_first_chunk_seconds']}s，说完到出声共 "
                            f"{self.metrics['speech_end_to_audio_seconds']}s（另有停顿判定 {self.config.audio.end_ms}ms）"
                            f"{'，短应答 ' + str(self.metrics['filler_seconds']) + 's 出声' if filler is not None else ''}"
                            f"；听到「{text[:30]}」{len(text)} 字 → 回复 {len(reply.text)} 字，情绪 {reply.emotion}")
                    self.state = "正在说话"
                    await self.audio.write(pcm_chunk)
                    written += len(pcm_chunk)
                await self.audio.drain()
            self.dialogue.played(reply)
            self.completed_turns += 1
            self.state = "等待说话"
        except asyncio.CancelledError:
            if reply is not None and written:
                # 合成未结束时 written 小于整句，估出的比例偏大，只是近似。
                played = written - min(self.audio.buffered(), written)
                if played > 0:
                    self.dialogue.interrupted(reply, played / written)
            self.audio.interrupt()
            raise
        except BaseException:
            self.audio.interrupt()
            raise

    def snapshot(self) -> Dict[str, Any]:
        return {"state": self.state, "chat_name": self.name, "active": self.task is not None and not self.task.done(),
                "completed_turns": self.completed_turns, "interruptions": self.interruptions,
                "failed_turns": self.failed_turns, "last_turn_error": self.last_turn_error,
                "metrics": dict(self.metrics), "error": self.error, "reason": self.reason}
