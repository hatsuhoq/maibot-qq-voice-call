from typing import AsyncIterator, Optional, Tuple

import httpx
import io
import struct
import wave

from .settings import SpeechSettings

# GPT-SoVITS 的 text_lang；中文模式本身支持中英混读。
TEXT_LANG = {"zh-CN": "zh", "en-US": "en", "ja-JP": "ja"}
MAX_HEADER_BYTES = 4096


def wav_bytes(pcm: bytes) -> bytes:
    result = io.BytesIO()
    with wave.open(result, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(pcm)
    return result.getvalue()


def parse_wav_header(buffer: bytes) -> Optional[Tuple[int, int, int, int]]:
    """解析流式 WAV 头，返回 (采样率, 声道, 位深, data 起始偏移)；头还不完整时返回 None。

    GPT-SoVITS 流式输出的 WAV 头里长度字段为 0，所以只读取 fmt 和 data 的位置，不信任长度。
    """
    if len(buffer) < 12:
        return None
    if buffer[:4] != b"RIFF" or buffer[8:12] != b"WAVE":
        raise ValueError("GPT-SoVITS 没有返回 WAV 音频，请检查 media_type 和服务版本")
    offset = 12
    fmt: Optional[Tuple[int, int, int]] = None
    while offset + 8 <= len(buffer):
        chunk_id = buffer[offset:offset + 4]
        size = struct.unpack("<I", buffer[offset + 4:offset + 8])[0]
        body = offset + 8
        if chunk_id == b"fmt ":
            if body + 16 > len(buffer):
                return None
            audio_format, channels, rate, _, _, bits = struct.unpack("<HHIIHH", buffer[body:body + 16])
            if audio_format != 1:
                raise ValueError("GPT-SoVITS 返回的不是 PCM 音频")
            fmt = (rate, channels, bits)
        elif chunk_id == b"data":
            if fmt is None:
                raise ValueError("WAV 音频缺少格式信息")
            return fmt[0], fmt[1], fmt[2], body
        offset = body + size + (size & 1)
    if len(buffer) > MAX_HEADER_BYTES:
        raise ValueError("WAV 头过长或格式异常")
    return None


class LocalSpeech:
    def __init__(self, config: SpeechSettings, client: httpx.AsyncClient, language: str = "zh-CN") -> None:
        self.config = config
        self.client = client
        self.text_lang = TEXT_LANG.get(language, "zh")

    async def check(self, synthesize: bool = True) -> None:
        """检查服务健康；synthesize=False 时跳过 GPT-SoVITS 试合成（等待来电前已验证过，接通时不再白等一次合成）。"""
        urls = [self.config.asr_url] if self.config.tts_backend == "gpt_sovits" else [self.config.asr_url, self.config.tts_url]
        for url in urls:
            response = await self.client.get(url.rstrip("/") + "/health")
            response.raise_for_status()
            info = response.json()
            if info.get("ready") is not True:
                raise RuntimeError(f"本地语音服务尚未就绪：{url}")
            if url == self.config.tts_url and info.get("sample_rate") != self.config.tts_sample_rate:
                raise ValueError("TTS 服务采样率与插件配置不一致")
        if self.config.tts_backend == "gpt_sovits" and synthesize:
            # api_v2 没有健康检查接口；合成一个短句，同时验证参考音频和采样率。
            total = 0
            async for chunk in self.synthesize("嗯，好的。", "neutral"):
                total += len(chunk)
            if total < self.config.tts_sample_rate // 10:
                raise ValueError("GPT-SoVITS 试合成的音频过短，请检查参考音频")

    async def transcribe(self, pcm: bytes) -> str:
        response = await self.client.post(
            self.config.asr_url.rstrip("/") + "/v1/audio/transcriptions",
            data={"model": self.config.asr_model, "language": "zh"},
            files={"file": ("speech.wav", wav_bytes(pcm), "audio/wav")},
        )
        response.raise_for_status()
        text = response.json()["text"]
        if not isinstance(text, str):
            raise ValueError("ASR 服务的 text 必须是字符串")
        if len(text) > 4000:
            raise ValueError("ASR 返回文本过长")
        return text.strip()

    async def synthesize(self, text: str, emotion: str) -> AsyncIterator[bytes]:
        if self.config.tts_backend == "gpt_sovits":
            source = self._gpt_sovits(text, emotion)
        else:
            source = self._cosyvoice(text, emotion)
        pending = b""
        total = 0
        async for chunk in source:
            total += len(chunk)
            if total > self.config.tts_sample_rate * 2 * 120:
                raise ValueError("TTS 音频超过两分钟上限")
            pending += chunk
            even = len(pending) // 2 * 2
            if even:
                yield pending[:even]
                pending = pending[even:]
        if pending or not total:
            raise ValueError("TTS 返回空音频或不完整的 PCM16 数据")

    async def _cosyvoice(self, text: str, emotion: str) -> AsyncIterator[bytes]:
        async with self.client.stream(
            "POST", self.config.tts_url.rstrip("/") + "/tts", json={"text": text, "emotion": emotion}
        ) as response:
            response.raise_for_status()
            if response.headers.get("x-audio-format") != "pcm_s16le_mono":
                raise ValueError("TTS 必须返回单声道 PCM16LE；不能把 WAV/MP3 文件头当作音频播放")
            if response.headers.get("x-audio-sample-rate") != str(self.config.tts_sample_rate):
                raise ValueError("TTS 响应采样率不匹配")
            async for chunk in response.aiter_bytes(chunk_size=4096):
                yield chunk

    async def _gpt_sovits(self, text: str, emotion: str) -> AsyncIterator[bytes]:
        style = self.config.style_for(emotion)
        payload = {
            "text": text,
            "text_lang": self.text_lang,
            "ref_audio_path": style.ref_audio_path,
            "prompt_text": style.prompt_text,
            "prompt_lang": style.prompt_lang,
            "text_split_method": "cut5",
            "batch_size": 1,
            "speed_factor": self.config.gpt_sovits_speed,
            "media_type": "wav",
            "streaming_mode": self.config.gpt_sovits_streaming_mode,
        }
        async with self.client.stream("POST", self.config.tts_url.rstrip("/") + "/tts", json=payload) as response:
            if response.status_code != 200:
                detail = (await response.aread())[:300].decode("utf-8", "replace")
                raise RuntimeError(f"GPT-SoVITS 合成失败（HTTP {response.status_code}）：{detail}")
            header = b""
            started = False
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if started:
                    yield chunk
                    continue
                header += chunk
                parsed = parse_wav_header(header)
                if parsed is None:
                    continue
                rate, channels, bits, data_offset = parsed
                if channels != 1 or bits != 16:
                    raise ValueError("GPT-SoVITS 必须输出单声道 16 位音频")
                if rate != self.config.tts_sample_rate:
                    raise ValueError(
                        f"GPT-SoVITS 实际采样率为 {rate}，请把插件配置 speech.tts_sample_rate 改为 {rate}")
                started = True
                if len(header) > data_offset:
                    yield header[data_offset:]
            if not started:
                raise ValueError("GPT-SoVITS 返回的音频不完整")
