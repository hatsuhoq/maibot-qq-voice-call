from types import SimpleNamespace
from unittest.mock import AsyncMock

from maibot_sdk.context import PluginContext

import asyncio
import pytest

from qq_voice_call.plugin import create_plugin
from qq_voice_call.settings import VoiceCallConfig
from qq_voice_call import plugin as plugin_module
from test_pipeline import FakeAudio, FakeSpeech


def make_plugin():
    plugin = create_plugin()
    plugin._ctx = PluginContext("hatsuhoq.maibot-qq-voice-call")
    config = VoiceCallConfig()
    config.plugin.enabled = True
    config.plugin.allowed_qq_ids = ["123456"]
    config.audio.input_device = 3
    config.audio.output_device = 11
    plugin.set_plugin_config(config.model_dump())
    return plugin


async def test_sdk_components_and_required_lifecycle():
    plugin = make_plugin()
    assert len(plugin.get_components()) == 7
    await plugin.on_load()
    assert (await plugin.status())["state"] == "未启动"
    await plugin.on_config_update("self", {}, "next")
    await plugin.on_unload()
    assert not plugin._ready


@pytest.mark.parametrize("qq,confirmed,error", [
    ("123456", False, "手动接通"), ("999999", True, "允许列表"), ("", True, "允许列表"),
])
async def test_start_authorization_before_any_io(qq, confirmed, error):
    plugin = make_plugin()
    await plugin.on_load()
    with pytest.raises(ValueError, match=error):
        await plugin.start(qq, confirmed)


async def test_missing_real_session_cannot_be_replaced_by_hash():
    plugin = make_plugin()
    plugin.ctx.chat.get_stream_by_user_id = AsyncMock(return_value=None)
    await plugin.on_load()
    with pytest.raises(ValueError, match="真实私聊"):
        await plugin.start("123456", True)


@pytest.mark.parametrize("operation", ["reload", "unload", "stop"])
async def test_lifecycle_cancels_active_session(operation):
    plugin = make_plugin()
    cleanup = asyncio.Event()

    async def running():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()

    task = asyncio.create_task(running())
    await asyncio.sleep(0)
    plugin._session = SimpleNamespace(task=task, stream_id="real", snapshot=lambda: {"active": False})
    if operation == "reload":
        await plugin.on_config_update("self", {}, "new")
    elif operation == "unload":
        await plugin.on_unload()
    else:
        await plugin.stop()
    assert cleanup.is_set() and task.cancelled()


async def test_other_chat_cannot_stop_current_call():
    plugin = make_plugin()
    plugin._session = SimpleNamespace(stream_id="private-session")
    success, _, _ = await plugin.stop_command(stream_id="other-session")
    assert not success


def wire_start(monkeypatch, plugin, audio):
    plugin.ctx.chat.get_stream_by_user_id = AsyncMock(return_value={
        "session_id": "actual-session", "platform": "qq", "is_group_session": False,
        "user_id": "123456", "user_nickname": "来电者",
    })
    dialogue = SimpleNamespace(history=[], reply=AsyncMock())
    monkeypatch.setattr(plugin_module.Dialogue, "create", AsyncMock(return_value=dialogue))
    speech = FakeSpeech()
    speech.check = AsyncMock()

    def speech_factory(config, client, language="zh-CN"):
        speech.client = client
        return speech

    monkeypatch.setattr(plugin_module, "LocalSpeech", speech_factory)
    monkeypatch.setattr(plugin_module, "AudioBridge", lambda *a: audio)
    return speech


async def test_actual_start_stop_and_duplicate_start(monkeypatch):
    plugin = make_plugin()
    audio = FakeAudio()
    speech = wire_start(monkeypatch, plugin, audio)
    await plugin.on_load()
    result = await plugin.start("123456", True)
    assert result["active"] and result["chat_name"] == "来电者的私聊"
    with pytest.raises(ValueError, match="已有音频桥接"):
        await plugin.start("123456", True)
    await plugin.stop()
    assert audio.closed and speech.client.is_closed
    assert not (await plugin.status())["active"]


async def test_start_failure_closes_resources(monkeypatch):
    plugin = make_plugin()
    audio = FakeAudio()
    speech = wire_start(monkeypatch, plugin, audio)
    speech.check.side_effect = RuntimeError("TTS 未就绪")
    await plugin.on_load()
    with pytest.raises(RuntimeError, match="TTS 未就绪"):
        await plugin.start("123456", True)
    assert audio.closed and speech.client.is_closed


async def test_cancel_while_device_opens_closes_after_thread_finishes(monkeypatch):
    from threading import Event

    plugin = make_plugin()
    entered, release = Event(), Event()

    class SlowAudio(FakeAudio):
        def open(self):
            entered.set()
            assert release.wait(2)

    audio = SlowAudio()
    speech = wire_start(monkeypatch, plugin, audio)
    await plugin.on_load()
    task = asyncio.create_task(plugin.start("123456", True))
    async with asyncio.timeout(2):
        while not entered.is_set():
            await asyncio.sleep(0.001)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert audio.closed and speech.client.is_closed


async def test_supervisor_retries_listen_until_services_are_up(monkeypatch):
    plugin = make_plugin()
    config = VoiceCallConfig.model_validate(plugin.config.model_dump())
    config.bridge.enabled = True
    config.bridge.token_file = "D:/token"
    plugin.set_plugin_config(config.model_dump())
    plugin._ready = True
    calls = []

    async def fake_listen():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("语音服务未就绪")
        plugin._bridge = SimpleNamespace(task=asyncio.create_task(asyncio.Event().wait()), stop=AsyncMock())
        return {}

    monkeypatch.setattr(plugin, "listen", fake_listen)
    task = asyncio.create_task(plugin._supervise(interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) == 3 and plugin._last_listen_error == ""
    plugin._bridge.task.cancel()


async def test_supervisor_respects_manual_stop(monkeypatch):
    plugin = make_plugin()
    plugin._ready = True
    plugin._paused = True
    listen = AsyncMock()
    monkeypatch.setattr(plugin, "_auto_listen", listen)
    task = asyncio.create_task(plugin._supervise(interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    listen.assert_not_awaited()


async def test_call_audio_routed_on_ring_and_connect_only_for_allowed(monkeypatch):
    plugin = make_plugin()
    seen = []

    def fake_route(svcl, render, capture):
        seen.append((render, capture))
        return "ok"

    monkeypatch.setattr(plugin_module, "route", fake_route)
    plugin._on_call_phase("ringing", "999")  # 不在允许名单
    plugin._on_call_phase("accepting", "123456")
    assert plugin._routing is None
    plugin._on_call_phase("ringing", "123456")
    plugin._on_call_phase("connected", "123456")  # 正在切换时再来一次：结束后补切
    await plugin._routing
    assert len(seen) == 2
    assert seen[0] == (r"ROUTIST*\Device\PLAYBACK 3/4\Render", r"ROUTIST*\Device\VIRTUAL REC 5/6\Capture")


async def test_call_audio_routing_failure_only_warns(monkeypatch):
    plugin = make_plugin()

    def broken(*args):
        raise FileNotFoundError("没找到 svcl.exe")

    monkeypatch.setattr(plugin_module, "route", broken)
    plugin._schedule_routing("电话接通")
    await plugin._routing  # 不抛出

    config = VoiceCallConfig.model_validate(plugin.config.model_dump())
    config.audio.route_call_audio = False
    plugin.set_plugin_config(config.model_dump())
    plugin._routing = None
    plugin._schedule_routing("电话接通")
    assert plugin._routing is None
