"""语音链路自检：不接电话、不打开录音，只检查本地服务、参考音频和音频设备。

用插件自己的 .venv 运行（selfcheck.bat 会自动处理），结果写入 scripts/logs/。
1. 列出音频设备，方便填写 input_device / output_device；
2. 用插件配置里每种情绪的参考音频试合成一句话，保存 wav 并记录首包延迟；
3. 把合成出的语音降采样到 16kHz 后送给语音识别，检查能否听写回原文。
"""

from array import array
from pathlib import Path

import asyncio
import io
import sys
import time
import tomllib
import traceback
import wave

PLUGIN_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = PLUGIN_DIR / "scripts" / "logs"
sys.path.insert(0, str(PLUGIN_DIR.parent))

SAMPLES = {
    "neutral": "你好，我在听，你慢慢说就好。",
    "happy": "太好啦，听到你的声音我好开心！",
    "sad": "唉，这件事真的让人有点难过。",
    "angry": "你怎么又熬夜了，我真的要生气了！",
    "gentle": "没关系的，累了就早点休息吧。",
    "surprised": "诶？真的吗，这也太意外了吧！",
}


def resample_to_16k(pcm: bytes, rate: int) -> bytes:
    source = array("h")
    source.frombytes(pcm)
    if rate == 16000 or not source:
        return pcm
    step = rate / 16000
    out = array("h")
    position = 0.0
    last = len(source) - 1
    while position < last:
        index = int(position)
        frac = position - index
        out.append(int(source[index] * (1 - frac) + source[index + 1] * frac))
        position += step
    return out.tobytes()


def save_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm)


def list_audio_devices() -> None:
    print("\n== 音频设备 ==")
    try:
        from qq_voice_call.audio import list_devices
        for device in list_devices():
            kind = []
            if device["input_channels"]:
                kind.append(f"输入{device['input_channels']}声道")
            if device["output_channels"]:
                kind.append(f"输出{device['output_channels']}声道")
            print(f"  [{device['id']:>3}] {device['name']}  ({device['host']}，{'，'.join(kind)})")
    except Exception:
        print("  读取音频设备失败：")
        traceback.print_exc(file=sys.stdout)


async def check_speech() -> bool:
    import httpx
    from qq_voice_call.settings import VoiceCallConfig
    from qq_voice_call.speech import LocalSpeech

    raw = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    config = VoiceCallConfig.model_validate(raw)
    speech_cfg = config.speech
    print("\n== 插件语音配置 ==")
    print(f"  识别：{speech_cfg.asr_url}（{speech_cfg.asr_model}）")
    print(f"  合成：{speech_cfg.tts_backend} @ {speech_cfg.tts_url}，采样率 {speech_cfg.tts_sample_rate}，"
          f"流式模式 {speech_cfg.gpt_sovits_streaming_mode}")
    for style in speech_cfg.voice_styles:
        exists = "存在" if Path(style.ref_audio_path).is_file() else "★文件不存在★"
        print(f"  情绪 {style.emotion}：{style.ref_audio_path}（{exists}）　原文：{style.prompt_text}")

    ok = True
    timeout = httpx.Timeout(speech_cfg.timeout_seconds, connect=5)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        speech = LocalSpeech(speech_cfg, client, config.chat.prompt_language)

        print("\n== 等待语音服务启动 ==")
        began = time.monotonic()
        while time.monotonic() - began < 180:
            ready = []
            for url, path in ((speech_cfg.asr_url, "/health"), (speech_cfg.tts_url, "/docs")):
                try:
                    ready.append((await client.get(url.rstrip("/") + path, timeout=3)).status_code == 200)
                except Exception:
                    ready.append(False)
            if all(ready):
                print(f"  两个服务都已就绪（等待 {time.monotonic() - began:.0f} 秒）")
                break
            await asyncio.sleep(3)
        else:
            print("  ★等待 3 分钟仍未全部就绪★，请查看 GPT-SoVITS / FunASR 服务窗口里的报错")

        print("\n== 语音识别服务 ==")
        try:
            response = await client.get(speech_cfg.asr_url.rstrip("/") + "/health")
            print(f"  /health：HTTP {response.status_code} {response.text[:200]}")
        except Exception as exc:
            ok = False
            print(f"  ★连接失败★：{type(exc).__name__}: {exc}（是否已运行 start-voice-services.bat？）")

        print("\n== GPT-SoVITS 试合成 ==")
        emotions = [style.emotion for style in speech_cfg.voice_styles] or ["neutral"]
        neutral_pcm = None
        for emotion in emotions:
            text = SAMPLES[emotion]
            began = time.monotonic()
            first = None
            chunks = []
            try:
                async for chunk in speech.synthesize(text, emotion):
                    if first is None:
                        first = time.monotonic() - began
                    chunks.append(chunk)
                pcm = b"".join(chunks)
                total = time.monotonic() - began
                seconds = len(pcm) / 2 / speech_cfg.tts_sample_rate
                target = LOG_DIR / f"tts_{emotion}.wav"
                save_wav(target, pcm, speech_cfg.tts_sample_rate)
                print(f"  {emotion}：首包 {first:.2f} 秒，合成完 {total:.2f} 秒，音频 {seconds:.1f} 秒 → {target.name}")
                if emotion == "neutral" or neutral_pcm is None:
                    neutral_pcm = (text, pcm)
            except Exception as exc:
                ok = False
                print(f"  {emotion}：★失败★ {type(exc).__name__}: {exc}")

        if neutral_pcm is not None:
            print("\n== 识别回环（把合成的语音送去识别） ==")
            text, pcm = neutral_pcm
            try:
                began = time.monotonic()
                heard = await speech.transcribe(resample_to_16k(pcm, speech_cfg.tts_sample_rate))
                print(f"  原文：{text}")
                print(f"  识别：{heard}（{time.monotonic() - began:.2f} 秒）")
            except Exception as exc:
                ok = False
                print(f"  ★识别失败★ {type(exc).__name__}: {exc}")
    return ok


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Python {sys.version.split()[0]}，插件目录 {PLUGIN_DIR}")
    list_audio_devices()
    import audio_loopback
    audio_loopback.report()
    try:
        ok = asyncio.run(check_speech())
    except Exception:
        ok = False
        print("\n★自检中断★")
        traceback.print_exc(file=sys.stdout)
    print("\n结论：" + ("语音识别与合成都正常。" if ok else "有项目失败，请看上面带 ★ 的行。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
