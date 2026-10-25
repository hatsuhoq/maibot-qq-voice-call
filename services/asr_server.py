"""独立进程运行 faster-whisper；模型必须预先下载到本地。"""

from pathlib import Path
from threading import Lock
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel

import argparse
import io
import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本地中文语音识别服务")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    path = Path(args.model_dir).resolve(strict=True)
    model = WhisperModel(str(path), device=args.device, compute_type=args.compute_type, local_files_only=True)
    lock = Lock()
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ready": True, "model": "local-whisper"}

    @app.post("/v1/audio/transcriptions")
    def transcribe(file: Annotated[UploadFile, File()], model: str = Form("local-whisper"), language: str = Form("zh")):
        if model != "local-whisper" or language != "zh":
            raise HTTPException(400, "该服务只接受 local-whisper 模型与 zh 语言")
        raw = file.file.read(1024 * 1024 + 1)
        if not raw or len(raw) > 1024 * 1024:
            raise HTTPException(413, "音频为空或超过 1MiB")
        # 客户端取消请求不会停止 CTranslate2 内核；等待上一段真正结束后再推理。
        if not lock.acquire(timeout=40):
            raise HTTPException(503, "上一次识别未能及时结束")
        try:
            segments, _ = recognizer.transcribe(io.BytesIO(raw), language="zh", beam_size=1,
                                                condition_on_previous_text=False, vad_filter=True)
            return {"text": "".join(s.text for s in segments).strip()}
        finally:
            lock.release()

    recognizer = model
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
