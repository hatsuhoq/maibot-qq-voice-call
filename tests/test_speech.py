import httpx
import io
import pytest
import wave

from qq_voice_call.settings import SpeechSettings
from qq_voice_call.speech import LocalSpeech, parse_wav_header, wav_bytes

COSY = SpeechSettings(tts_backend="cosyvoice", tts_url="http://127.0.0.1:8092", tts_sample_rate=24000)


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"\x01"
        yield b"\x00\x02"
        yield b"\x00"


def test_wav_uses_exact_input_format():
    with wave.open(io.BytesIO(wav_bytes(bytes(640))), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) == (16000, 1, 2, 320)


async def test_asr_multipart_and_pcm_stream():
    def respond(request):
        if request.url.path == "/v1/audio/transcriptions":
            assert b"RIFF" in request.content and b"speech.wav" in request.content
            return httpx.Response(200, json={"text": "  你好  "})
        assert b'"emotion":"happy"' in request.content
        return httpx.Response(200, stream=Chunks(), headers={
            "x-audio-format": "pcm_s16le_mono", "x-audio-sample-rate": "24000",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        speech = LocalSpeech(COSY, client)
        assert await speech.transcribe(bytes(640)) == "你好"
        assert b"".join([chunk async for chunk in speech.synthesize("你好", "happy")]) == b"\x01\x00\x02\x00"


@pytest.mark.parametrize("headers,body", [
    ({}, b"RIFFWAVE"),
    ({"x-audio-format": "pcm_s16le_mono", "x-audio-sample-rate": "48000"}, b"\0\0"),
    ({"x-audio-format": "pcm_s16le_mono", "x-audio-sample-rate": "24000"}, b"\0"),
    ({"x-audio-format": "pcm_s16le_mono", "x-audio-sample-rate": "24000"}, b""),
])
async def test_bad_tts_audio_fails_explicitly(headers, body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, content=body))) as client:
        with pytest.raises(ValueError):
            _ = [chunk async for chunk in LocalSpeech(COSY, client).synthesize("你好", "neutral")]


async def test_non_200_is_not_played():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="服务出错"))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            _ = [chunk async for chunk in LocalSpeech(COSY, client).synthesize("你好", "neutral")]


def gsv_settings(**extra):
    styles = [
        {"emotion": "neutral", "ref_audio_path": "D:/voices/neutral.wav", "prompt_text": "平静的原文", "prompt_lang": "zh"},
        {"emotion": "happy", "ref_audio_path": "D:/voices/happy.wav", "prompt_text": "开心的原文", "prompt_lang": "zh"},
    ]
    return SpeechSettings(voice_styles=styles, **extra)


def stream_wav(rate=32000, channels=1, frames=b"\x01\x00\x02\x00\x03\x00"):
    # 模拟 api_v2 的流式输出：先发长度为 0 的 WAV 头，再发裸 PCM，并且分块边界不对齐。
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
    header = buffer.getvalue()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield header[:20]
            yield header[20:] + frames[:1]
            yield frames[1:]

    return Body()


async def test_gpt_sovits_picks_emotion_reference_and_strips_header():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, stream=stream_wav(), headers={"content-type": "audio/wav"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        speech = LocalSpeech(gsv_settings(), client)
        audio = b"".join([chunk async for chunk in speech.synthesize("你好呀", "happy")])
        assert audio == b"\x01\x00\x02\x00\x03\x00"
        # 没有单独配置 sad 时退回 neutral
        _ = [chunk async for chunk in speech.synthesize("唉", "sad")]
    import json
    happy, sad = (json.loads(r.content) for r in seen)
    assert seen[0].url.path == "/tts" and seen[0].url.port == 9880
    assert happy["ref_audio_path"] == "D:/voices/happy.wav" and happy["prompt_text"] == "开心的原文"
    assert happy["text_lang"] == "zh" and happy["media_type"] == "wav" and happy["streaming_mode"] == 1
    assert sad["ref_audio_path"] == "D:/voices/neutral.wav"


async def test_gpt_sovits_sample_rate_mismatch_tells_how_to_fix():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=stream_wav(rate=48000)))) as client:
        with pytest.raises(ValueError, match="48000"):
            _ = [chunk async for chunk in LocalSpeech(gsv_settings(), client).synthesize("你好", "neutral")]


async def test_gpt_sovits_error_body_is_reported():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"message": "ref_audio_path is required"}))) as client:
        with pytest.raises(RuntimeError, match="ref_audio_path"):
            _ = [chunk async for chunk in LocalSpeech(gsv_settings(), client).synthesize("你好", "neutral")]


async def test_gpt_sovits_requires_neutral_reference():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        with pytest.raises(ValueError, match="neutral"):
            _ = [chunk async for chunk in LocalSpeech(SpeechSettings(), client).synthesize("你好", "neutral")]


def test_duplicate_emotion_rejected():
    with pytest.raises(ValueError, match="每种情绪"):
        SpeechSettings(voice_styles=[{"emotion": "neutral", "ref_audio_path": "a.wav"},
                                     {"emotion": "neutral", "ref_audio_path": "b.wav"}])


def test_wav_header_parser_handles_partial_and_extra_chunks():
    full = wav_bytes(bytes(4))
    assert parse_wav_header(full[:30]) is None
    rate, channels, bits, offset = parse_wav_header(full)
    assert (rate, channels, bits) == (16000, 1, 16) and full[offset:] == bytes(4)
    with pytest.raises(ValueError):
        parse_wav_header(b"ID3\x00" + bytes(20))


async def test_gpt_sovits_check_only_probes_asr_health_then_synthesizes():
    paths = []

    def respond(request):
        paths.append(request.url.path)
        if request.url.path == "/health":
            assert request.url.port == 8091
            return httpx.Response(200, json={"ready": True})
        return httpx.Response(200, stream=stream_wav(frames=bytes(6400)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await LocalSpeech(gsv_settings(), client).check()
    assert paths == ["/health", "/tts"]
