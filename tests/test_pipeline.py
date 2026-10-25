from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import logging
import pytest

from qq_voice_call.dialogue import Dialogue, Reply, parse_reply
from qq_voice_call.session import CallSession
from qq_voice_call.settings import VoiceCallConfig
from qq_voice_call.vad import TurnDetector


class FakeAudio:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.pending = []
        self.closed = False
        self.drained = asyncio.Event()
        self.input_xruns = 0
        self.output_xruns = 0

    def buffered(self):
        return sum(len(chunk) for chunk in self.pending)

    def open(self):
        pass

    async def read(self):
        return await self.frames.get()

    async def write(self, data):
        self.pending.append(data)

    async def drain(self):
        await self.drained.wait()

    def interrupt(self):
        self.pending.clear()

    def close(self):
        self.closed = True


class FakeSpeech:
    def __init__(self):
        self.client = SimpleNamespace(aclose=AsyncMock())
        self.transcribe = AsyncMock(return_value="你好")

    async def synthesize(self, text, emotion):
        yield b"\x01\x00" * 200


def make_session():
    config = VoiceCallConfig()
    ctx = SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock(return_value={
        "success": True, "response": '{"text":"你好呀！","emotion":"happy"}'
    })))
    dialogue = Dialogue(ctx, config.chat, "系统提示")
    audio = FakeAudio()
    speech = FakeSpeech()
    return CallSession(config, audio, speech, dialogue, logging.getLogger("test"), "测试的私聊", "real-session")


async def wait_state(session, expected):
    async with asyncio.timeout(2):
        while session.state != expected:
            await asyncio.sleep(0.001)


def test_vad_preserves_start_and_finishes_only_after_silence():
    detector = TurnDetector(VoiceCallConfig().audio)
    frame = b"\x01\x00" * 320
    events = [detector.feed(frame, True) for _ in range(20)]
    assert sum(began for began, _ in events) == 1
    assert all(pcm is None for _, pcm in events)
    assert all(detector.feed(bytes(640), False)[1] is None for _ in range(29))
    _, audio = detector.feed(bytes(640), False)
    assert audio[:len(frame) * 20] == frame * 20
    assert not detector.active


def test_vad_rejects_short_noise_and_bounds_continuous_speech():
    config = VoiceCallConfig().audio
    config.max_utterance_seconds = 2
    detector = TurnDetector(config)
    for _ in range(5):
        detector.feed(bytes(640), True)
    assert all(detector.feed(bytes(640), False)[1] is None for _ in range(30))
    result = [detector.feed(bytes(640), True)[1] for _ in range(110)]
    segments = [data for data in result if data]
    assert len(segments) == 1
    assert len(segments[0]) <= 64000


async def test_barge_in_clears_audio_and_does_not_commit_unheard_reply():
    session = make_session()
    session.turn = asyncio.create_task(session.respond(bytes(6400)))
    await wait_state(session, "正在说话")
    assert session.audio.pending
    await session.interrupt()
    assert not session.audio.pending
    assert session.interruptions == 1
    assert [row["role"] for row in session.dialogue.history] == ["user"]
    session.audio.drained.set()
    await session.respond(bytes(6400))
    assert session.completed_turns == 1
    assert session.dialogue.history[-1]["role"] == "assistant"


async def test_cancel_during_llm_never_plays_late_audio():
    session = make_session()
    gate = asyncio.Event()

    async def generate(**kwargs):
        await gate.wait()
        return {"success": True, "response": '{"text":"过期回复","emotion":"neutral"}'}

    session.dialogue.ctx.llm.generate = generate
    session.turn = asyncio.create_task(session.respond(bytes(6400)))
    await wait_state(session, "正在思考")
    await session.interrupt()
    gate.set()
    await asyncio.sleep(0)
    assert not session.audio.pending
    assert session.completed_turns == 0


async def test_shutdown_closes_audio_http_and_history():
    session = make_session()
    session.dialogue.history.append({"role": "user", "content": "私密电话"})
    session.task = asyncio.create_task(session.run())
    await asyncio.sleep(0)
    session.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session.task
    assert session.audio.closed
    session.speech.client.aclose.assert_awaited_once()
    assert not session.dialogue.history
    assert session.state == "已停止"


async def test_single_turn_error_is_skipped_but_three_in_a_row_stop_session():
    session = make_session()
    session.speech.transcribe.side_effect = RuntimeError("ASR 离线")
    await session.respond(bytes(6400))
    await session.respond(bytes(6400))
    assert session.failed_turns == 2 and "ASR 离线" in session.last_turn_error
    assert session.state == "等待说话" and not session.audio.closed
    session.turn = asyncio.create_task(session.respond(bytes(6400)))
    await asyncio.sleep(0)
    await session.run()
    assert session.audio.closed
    assert session.state == "发生错误"
    assert "ASR 离线" in session.error


async def test_success_resets_failure_count():
    session = make_session()
    session.speech.transcribe.side_effect = [RuntimeError("偶发"), "你好"]
    session.audio.drained.set()
    await session.respond(bytes(6400))
    await session.respond(bytes(6400))
    assert session.failed_turns == 0 and session.completed_turns == 1


async def test_failed_model_or_unparseable_reply_is_skipped_without_playing():
    session = make_session()
    session.dialogue.ctx.llm.generate.return_value = {"success": False}
    await session.respond(bytes(6400))
    assert "模型调用失败" in session.last_turn_error
    session.dialogue.ctx.llm.generate.return_value = {"success": True, "response": '{"text": "半截'}
    await session.respond(bytes(6400))
    assert "无法解析" in session.last_turn_error
    assert not session.audio.pending


def test_parse_reply_tolerates_trailing_text_fences_and_plain_text():
    reply = parse_reply('{"text":"嗯……那就早点睡吧。","emotion":"gentle"}\n\n（补充说明）', 180)
    assert (reply.text, reply.emotion) == ("嗯……那就早点睡吧。", "gentle")
    assert parse_reply('```json\n{"text":"好呀","emotion":"happy"}\n```', 180).emotion == "happy"
    assert parse_reply("好的，我在听。", 180).emotion == "neutral"
    assert parse_reply('{"text":"今天好累。我们明天再聊吧，晚安啦","emotion":"sad"}', 14).text == "今天好累。我们明天再聊吧，"
    assert parse_reply('{"text":"好累。真的好累啊今天","emotion":"sad"}', 8).text == "好累。真的好累啊"
    with pytest.raises(ValueError):
        parse_reply('{"text":"x","emotion":"injected"}', 180)
    with pytest.raises(ValueError):
        parse_reply("", 180)


def test_reply_emotion_is_closed_set():
    with pytest.raises(ValueError):
        Reply(text="你好", emotion="injected instruction")


def filler_session(text="今天上班特别累你呢", llm_delay=0.2):
    session = make_session()
    session.fillers = [b"\x07\x00" * 10]
    session.config.chat.filler_delay_seconds = 0.05
    session.config.chat.filler_probability = 1.0
    session.speech.transcribe = AsyncMock(return_value=text)
    original_generate = session.dialogue.ctx.llm.generate

    async def slow_generate(**kwargs):
        await asyncio.sleep(llm_delay)
        return await original_generate(**kwargs)

    session.dialogue.ctx.llm.generate = slow_generate
    session.audio.drained.set()
    return session


async def test_filler_only_when_reply_is_slow_and_never_twice_in_a_row():
    session = filler_session()
    await session.respond(bytes(6400))
    assert session.audio.pending[0] == b"\x07\x00" * 10 and "filler_seconds" in session.metrics
    assert all("嗯" not in item["content"] for item in session.dialogue.history)
    session.audio.pending.clear()
    await session.respond(bytes(6400))
    assert b"\x07\x00" * 10 not in session.audio.pending  # 上一句刚用过
    session.audio.pending.clear()
    await session.respond(bytes(6400))
    assert session.audio.pending[0] == b"\x07\x00" * 10


async def test_no_filler_for_short_utterance_or_fast_reply():
    short = filler_session(text="好的")
    await short.respond(bytes(6400))
    assert b"\x07\x00" * 10 not in short.audio.pending
    fast = filler_session(llm_delay=0)
    await fast.respond(bytes(6400))
    assert b"\x07\x00" * 10 not in fast.audio.pending


async def test_fillers_are_prepared_and_failures_do_not_stop_call():
    session = make_session()
    await session._prepare_fillers()
    assert len(session.fillers) == len(session.config.chat.fillers)

    async def broken(text, emotion):
        raise RuntimeError("TTS 离线")
        yield b""

    session.fillers = []
    session.speech.synthesize = broken
    await session._prepare_fillers()
    assert session.fillers == []


async def _feed(session, detector, voiced_frames, silent_frames=30):
    for _ in range(voiced_frames):
        await session._on_frame(detector, bytes(640), True)
    for _ in range(silent_frames):
        await session._on_frame(detector, bytes(640), False)


async def test_short_sound_while_speaking_does_not_interrupt():
    session = make_session()
    detector = TurnDetector(session.config.audio)
    speaking = asyncio.Event()
    session.turn = asyncio.create_task(speaking.wait())
    await _feed(session, detector, 15)  # 0.3 秒“嗯”
    assert not session.turn.done() and session.interruptions == 0 and session.ignored_sounds == 1
    assert not session.pending_barge
    speaking.set()
    await session.turn


async def test_sustained_speech_while_speaking_interrupts_and_is_answered():
    session = make_session()
    detector = TurnDetector(session.config.audio)
    session.turn = asyncio.create_task(asyncio.Event().wait())
    old = session.turn
    await _feed(session, detector, 35, silent_frames=0)  # 0.7 秒 > 600ms
    assert old.cancelled() and session.interruptions == 1
    await _feed(session, detector, 0)  # 说完
    assert session.turn is not None and session.turn is not old
    session.audio.drained.set()
    await session.turn
    assert session.completed_turns == 1


async def test_short_sound_when_idle_is_still_answered():
    session = make_session()
    detector = TurnDetector(session.config.audio)
    await _feed(session, detector, 15)
    assert session.turn is not None and session.ignored_sounds == 0
    session.audio.drained.set()
    await session.turn
    assert session.completed_turns == 1


def test_vad_early_slice_equals_final_utterance_after_pause():
    detector = TurnDetector(VoiceCallConfig().audio)
    frame = b"\x01\x00" * 320
    for _ in range(20):
        detector.feed(frame, True)
    for _ in range(14):
        detector.feed(bytes(640), False)
        assert detector.take_early() is None
    detector.feed(bytes(640), False)
    early = detector.take_early()
    assert early is not None and detector.take_early() is None
    final = None
    for _ in range(15):
        _, final = detector.feed(bytes(640), False)
    assert final == early


async def test_early_asr_is_reused_when_caller_stays_silent():
    session = make_session()
    session.audio.drained.set()
    detector = TurnDetector(session.config.audio)
    await _feed(session, detector, 20)
    await session.turn
    assert session.speech.transcribe.await_count == 1 and session.early_hits == 1
    assert session.completed_turns == 1


async def test_early_asr_is_dropped_when_caller_keeps_talking():
    session = make_session()
    session.audio.drained.set()
    detector = TurnDetector(session.config.audio)
    await _feed(session, detector, 20, silent_frames=16)  # 停顿 320ms，已提前识别
    assert session.early is not None
    early_task = session.early[1]
    await _feed(session, detector, 20)  # 又接着说，然后说完
    await asyncio.gather(early_task, return_exceptions=True)
    assert early_task.cancelled()
    await session.turn
    assert session.early_hits == 1 and session.completed_turns == 1
    # 送去回复的是完整的整段话，而不是停顿前那一半
    assert len(session.speech.transcribe.await_args.args[0]) > 640 * 40


async def test_interrupted_reply_records_only_what_was_played():
    session = make_session()

    class PlayedAudio(FakeAudio):
        async def write(self, data):
            pass  # 立即被声卡播放

    session.audio = PlayedAudio()
    session.turn = asyncio.create_task(session.respond(bytes(6400)))
    await wait_state(session, "正在说话")
    await session.interrupt()
    assert session.dialogue.history[-1]["role"] == "assistant"
    assert "被对方打断" in session.dialogue.history[-1]["content"]


async def test_reply_token_budget_follows_max_reply_chars():
    session = make_session()
    session.config.chat.max_reply_chars = 500
    await session.dialogue.reply("你好")
    assert session.dialogue.ctx.llm.generate.await_args.kwargs["max_tokens"] >= 1000
