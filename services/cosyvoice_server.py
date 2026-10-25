"""在 CosyVoice 自己的 Python 环境运行；不修改它的代码或依赖。"""

from pathlib import Path
from threading import Event, Lock
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import argparse
import asyncio
import logging
import sys
import uvicorn


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    emotion: Literal["neutral", "happy", "sad", "angry", "gentle", "surprised"] = "neutral"


INSTRUCTIONS = {
    "neutral": "自然平静地说话", "happy": "开心地说话", "sad": "悲伤地说话",
    "angry": "生气地说话", "gentle": "温柔地说话", "surprised": "惊讶地说话",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 CosyVoice 3 本地流式情绪语音服务")
    parser.add_argument("--repo", required=True, help="CosyVoice 项目绝对路径")
    parser.add_argument("--model-dir", required=True, help="已下载模型的本地目录")
    parser.add_argument("--reference", required=True, help="角色参考音频绝对路径")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()
    repo = Path(args.repo).resolve(strict=True)
    model_path = Path(args.model_dir).resolve(strict=True)
    reference = Path(args.reference).resolve(strict=True)
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "third_party" / "Matcha-TTS"))
    from cosyvoice.cli.cosyvoice import AutoModel

    model = AutoModel(model_dir=str(model_path))
    lock = Lock()
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ready": True, "sample_rate": model.sample_rate}

    @app.post("/tts")
    async def synthesize(payload: SynthesisRequest, request: Request):
        # 插话后的新回复等待旧推理停止；不并发调用同一个模型实例。
        async with asyncio.timeout(40):
            while not lock.acquire(blocking=False):
                if await request.is_disconnected():
                    raise HTTPException(499, "客户端已取消合成")
                await asyncio.sleep(0.05)
        # 用独立 worker 保证迭代器的 next/close 在同一线程执行。断连后不再送出旧音频。
        cancelled = Event()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)

        def put(item: object) -> bool:
            future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            while not cancelled.is_set():
                try:
                    future.result(timeout=0.1)
                    return True
                except TimeoutError:
                    continue
            future.cancel()
            return False

        def worker() -> None:
            generator = None
            try:
                instruction = f"You are a helpful assistant. {INSTRUCTIONS[payload.emotion]}。<|endofprompt|>"
                generator = model.inference_instruct2(payload.text, instruction, str(reference), stream=True)
                for result in generator:
                    if cancelled.is_set():
                        break
                    pcm = (result["tts_speech"].detach().cpu().clamp(-1, 1).numpy() * 32767).astype("<i2").tobytes()
                    if not put(pcm):
                        break
                if not cancelled.is_set():
                    put(None)
            except Exception as exc:
                logging.exception("CosyVoice 合成失败")
                if not cancelled.is_set():
                    put(exc)
            finally:
                try:
                    if generator is not None:
                        generator.close()
                finally:
                    lock.release()

        worker_task = asyncio.create_task(asyncio.to_thread(worker))

        async def stream():
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    yield item
            finally:
                cancelled.set()
                # 推理内核不可强制中断；worker 在下一块返回后退出，锁保持到真正结束。
                await asyncio.shield(worker_task)

        return StreamingResponse(stream(), media_type="application/octet-stream", headers={
            "X-Audio-Format": "pcm_s16le_mono", "X-Audio-Sample-Rate": str(model.sample_rate),
        })

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
