from pathlib import Path
from string import Template
from typing import Dict, List, Literal

from maibot_sdk.context import PluginContext
from pydantic import BaseModel, ConfigDict, Field

import asyncio
import json
import re

from .settings import ChatSettings


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    emotion: Literal["neutral", "happy", "sad", "angry", "gentle", "surprised"]


def parse_reply(raw: str, max_chars: int) -> Reply:
    """模型偶尔会在 JSON 前后多输出内容或加代码围栏；取第一个 JSON 对象。

    完全没有 JSON 时，把模型自己的话当作 neutral 情绪的回复（这是模型的原话，不是插件编造的兜底）。
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    start = text.find("{")
    if start >= 0:
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            reply = Reply.model_validate({"text": str(data.get("text", "")).strip(),
                                          "emotion": data.get("emotion", "neutral")})
            return _clip(reply, max_chars)
    if not text or "{" in text or '"emotion"' in text:
        raise ValueError(f"电话回复格式无法解析：{text[:120]}")
    return _clip(Reply(text=text[:500], emotion="neutral"), max_chars)


def _clip(reply: Reply, max_chars: int) -> Reply:
    if not reply.text.strip():
        raise ValueError("电话回复为空")
    if len(reply.text) <= max_chars:
        return reply
    # 超长时在句末标点处截断，而不是整句丢弃。
    cut = reply.text[:max_chars]
    # 优先在句号类标点处截断，其次逗号；截断点不早于一半长度。
    for marks in ("。！？!?；;…~", "，,"):
        index = max(cut.rfind(mark) for mark in marks)
        if index >= max_chars // 2:
            return Reply(text=cut[:index + 1], emotion=reply.emotion)
    return Reply(text=cut, emotion=reply.emotion)


class Dialogue:
    def __init__(self, ctx: PluginContext, config: ChatSettings, system: str) -> None:
        self.ctx = ctx
        self.config = config
        self.system = system
        self.history: List[Dict[str, str]] = []

    @classmethod
    async def create(cls, ctx: PluginContext, config: ChatSettings, stream_id: str, name: str) -> "Dialogue":
        nickname, personality, style = await asyncio.gather(
            ctx.config.get("bot.nickname", ""), ctx.config.get("personality.personality", ""),
            ctx.config.get("personality.behavior_style", ""),
        )
        if not all(isinstance(value, str) for value in (nickname, personality, style)):
            raise ValueError("MaiBot 昵称、人设和行为风格必须为字符串")
        context = ""
        if config.context_messages:
            messages = await ctx.message.get_recent(stream_id, limit=config.context_messages)
            if not isinstance(messages, list):
                raise ValueError("读取真实私聊上下文失败")
            context = await ctx.message.build_readable(messages, replace_bot_name=False, timestamp_mode="relative")
            if not isinstance(context, str):
                raise ValueError("私聊上下文格式错误")
        path = Path(__file__).parent / "prompts" / f"{config.prompt_language}.txt"
        template = await asyncio.to_thread(path.read_text, encoding="utf-8")
        system = Template(template).substitute(nickname=nickname, personality=personality, style=style,
                                               name=name, context=context[-8000:], max_chars=config.max_reply_chars)
        return cls(ctx, config, system)

    async def reply(self, text: str) -> Reply:
        # 用户已经说出的内容保留；只有完整播放完的助手回复才由 played() 记入历史。
        self.history.append({"role": "user", "content": text})
        self.history = self.history[-self.config.history_turns * 2:]
        result = await self.ctx.llm.generate(
            prompt=[{"role": "system", "content": self.system}] + self.history,
            # 中文约 1～2 token 一个字，再加 JSON 外壳；固定 256 会在 max_reply_chars 调大时截断 JSON。
            model=self.config.task_name, max_tokens=self.config.max_reply_chars * 2 + 64,
        )
        if not result["success"]:
            raise RuntimeError("电话回复模型调用失败，请查看 MaiBot 模型日志")
        return parse_reply(result["response"], self.config.max_reply_chars)

    def played(self, reply: Reply) -> None:
        self.history.append({"role": "assistant", "content": reply.model_dump_json()})
        self.history = self.history[-self.config.history_turns * 2:]

    def interrupted(self, reply: Reply, fraction: float) -> None:
        """说到一半被打断：只记入大约已经播出的部分，让模型知道自己说到哪了、对方没听完后面的话。"""
        spoken = reply.text[:max(1, round(len(reply.text) * fraction))]
        self.played(Reply(text=f"{spoken}……（被对方打断）", emotion=reply.emotion))
