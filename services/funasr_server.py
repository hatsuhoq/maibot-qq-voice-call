"""用 GPT-SoVITS 整合包自带的 FunASR Paraformer 中文模型提供本地语音识别。

在 GPT-SoVITS 的 runtime\\python.exe 中运行（Python 3.9），不向 MaiBot 主环境安装任何依赖。
模型直接读取整合包 tools/asr/models 下已下载的文件，不联网下载。
协议与插件的 asr_server.py 相同：GET /health，POST /v1/audio/transcriptions（OpenAI 格式）。
"""

import argparse
import io
import os
import threading
import wave
from typing import Optional

MODEL_DIRS = {
    "asr": "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "vad": "speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "punc": "punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
}
MAX_UPLOAD = 1024 * 1024


def read_pcm16(raw: bytes):
    """只接受插件发送的 16kHz 单声道 PCM16 WAV，返回 float32 波形。"""
    import numpy as np

    with wave.open(io.BytesIO(raw), "rb") as reader:
        if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1, 2, 16000):
            raise ValueError("只接受 16kHz 单声道 PCM16 WAV")
        frames = reader.readframes(reader.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本地 FunASR 中文语音识别服务")
    parser.add_argument("--gsv-root", required=True, help="GPT-SoVITS 整合包根目录（包含 tools 和 runtime）")
    parser.add_argument("--device", default="cuda:0", help="cuda:0 或 cpu")
    parser.add_argument("--no-punc", action="store_true", help="不加标点，可略微降低延迟")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()

    models_root = os.path.join(os.path.abspath(args.gsv_root), "tools", "asr", "models")
    paths = {key: os.path.join(models_root, name) for key, name in MODEL_DIRS.items()}
    for key, path in paths.items():
        if not os.path.isfile(os.path.join(path, "model.pt")):
            raise SystemExit("找不到 FunASR 模型（%s）：%s" % (key, path))

    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from funasr import AutoModel
    import uvicorn

    options = dict(model=paths["asr"], vad_model=paths["vad"], device=args.device, disable_update=True,
                   disable_pbar=True, log_level="ERROR")
    if not args.no_punc:
        options["punc_model"] = paths["punc"]
    model = AutoModel(**options)
    # 预热一次，避免第一通电话的第一句话额外等待模型初始化。
    import numpy as np
    model.generate(input=np.zeros(16000, dtype="float32"))

    lock = threading.Lock()
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ready": True, "model": "paraformer-zh", "device": args.device}

    @app.post("/v1/audio/transcriptions")
    def transcribe(file: UploadFile = File(...), model_name: Optional[str] = Form(None, alias="model"),
                   language: str = Form("zh")):
        if language != "zh":
            raise HTTPException(400, "该服务只识别中文")
        raw = file.file.read(MAX_UPLOAD + 1)
        if not raw or len(raw) > MAX_UPLOAD:
            raise HTTPException(413, "音频为空或超过 1MiB")
        try:
            audio = read_pcm16(raw)
        except (ValueError, wave.Error) as exc:
            raise HTTPException(400, str(exc))
        # 同一模型实例不并发推理；插话取消后等上一段结束再处理新的一段。
        if not lock.acquire(timeout=40):
            raise HTTPException(503, "上一次识别未能及时结束")
        try:
            result = model.generate(input=audio)
        finally:
            lock.release()
        text = "".join(item.get("text", "") for item in result).strip()
        return {"text": text}

    print("FunASR 语音识别已就绪：http://127.0.0.1:%d" % args.port, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
