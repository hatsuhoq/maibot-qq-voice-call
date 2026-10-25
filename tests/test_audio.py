import asyncio
import pytest

from qq_voice_call.audio import AudioBridge


def test_overflow_is_reported_and_never_silently_drops_frames():
    audio = AudioBridge(1, 2, 24000)
    for _ in range(101):
        audio._capture(bytes(640), 320, None, None)
    assert audio.incoming.qsize() == 100
    with pytest.raises(RuntimeError, match="积压"):
        audio.check()


async def test_bounded_playback_backpressure_and_clear():
    audio = AudioBridge(1, 2, 24000)
    task = asyncio.create_task(audio.write(bytes(24000 * 8)))
    await asyncio.sleep(0.02)
    assert not task.done()
    assert len(audio.pending) <= 24000 * 4
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    audio.interrupt()
    assert not audio.pending
    output = bytearray(960)
    audio._play(output, 480, None, None)
    assert output == bytes(960)


async def test_output_callback_pads_silence_without_replaying_old_bytes():
    audio = AudioBridge(1, 2, 24000)
    await audio.write(b"\x01\x00" * 100)
    output = bytearray(960)
    audio._play(output, 480, None, None)
    assert output[:200] == b"\x01\x00" * 100
    assert output[200:] == bytes(760)
    audio._play(output, 480, None, None)
    assert output == bytes(960)


def test_device_name_prefers_mme_exact_match(monkeypatch):
    from qq_voice_call import audio as audio_module

    hosts = [{"name": "MME"}, {"name": "Windows WASAPI"}]
    devices = [
        {"name": "VIRTUAL REC 3/4 (ROUTIST R2)", "hostapi": 1, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "PLAYBACK 5/6 (ROUTIST R2)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "VIRTUAL REC 3/4 (ROUTIST R2)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
    ]
    monkeypatch.setattr(audio_module.sd, "query_hostapis", lambda: hosts)
    monkeypatch.setattr(audio_module.sd, "query_devices", lambda: devices)
    assert audio_module.resolve_device("VIRTUAL REC 3/4 (ROUTIST R2)", -1, "input") == 2
    assert audio_module.resolve_device("playback 5/6", -1, "output") == 1
    assert audio_module.resolve_device("", 7, "input") == 7
    import pytest
    with pytest.raises(ValueError, match="找不到"):
        audio_module.resolve_device("PLAYBACK 5/6", -1, "input")


def test_has_devices_accepts_names_or_indexes():
    from qq_voice_call.settings import AudioSettings

    assert not AudioSettings().has_devices()
    assert AudioSettings(input_device_name="VIRTUAL REC 3/4", output_device=3).has_devices()
    assert AudioSettings(input_device=1, output_device=2).has_devices()


async def test_read_wakes_on_capture_from_audio_thread():
    import threading

    audio = AudioBridge(1, 2, 24000)
    reader = asyncio.create_task(audio.read())
    await asyncio.sleep(0.01)
    assert not reader.done()
    threading.Thread(target=audio._capture, args=(b"\x02\x00" * 320, 320, None, None)).start()
    async with asyncio.timeout(1):
        assert await reader == b"\x02\x00" * 320


async def test_close_wakes_blocked_reader_and_writer():
    audio = AudioBridge(1, 2, 24000)
    reader = asyncio.create_task(audio.read())
    writer = asyncio.create_task(audio.write(bytes(24000 * 8)))
    await asyncio.sleep(0.01)
    await asyncio.to_thread(audio.close)
    for task in (reader, writer):
        with pytest.raises(RuntimeError, match="已关闭"):
            async with asyncio.timeout(1):
                await task


async def test_drain_returns_after_callback_consumes_buffer():
    audio = AudioBridge(1, 2, 24000)
    await audio.write(b"\x01\x00" * 100)
    drain = asyncio.create_task(audio.drain())
    await asyncio.sleep(0.01)
    assert not drain.done()
    audio._play(bytearray(960), 480, None, None)
    async with asyncio.timeout(1):
        await drain


def test_xruns_are_counted_not_fatal():
    audio = AudioBridge(1, 2, 24000)
    audio._capture(b"\x03\x00" * 320, 320, None, "input overflow")
    audio._play(bytearray(960), 480, None, "output underflow")
    audio.check()
    assert (audio.input_xruns, audio.output_xruns) == (1, 1)
    assert audio.incoming.get_nowait() == b"\x03\x00" * 320
