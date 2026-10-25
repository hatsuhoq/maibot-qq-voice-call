"""小睦主动约电话：定期让模型读最近的私聊记录，自己判断现在适不适合约对方打电话。

模型只决定“约不约、怎么说”；频率上限、安静时段、通话中不打扰等硬规则由插件把关，
避免模型判断失误变成刷屏。约的方式是发一条 QQ 私聊消息，对方打过来后由自动接听处理。
"""

from datetime import datetime, time as dtime
from pathlib import Path
from string import Template
from typing import Any, Callable, List, Optional

from pydantic import BaseModel, Field

import asyncio
import json
import logging
import re

from .settings import InviteSettings

WEEKDAYS = "一二三四五六日"


class Decision(BaseModel):
    invite: bool
    reason: str = ""
    message: str = Field(default="", max_length=200)


def parse_decision(raw: str) -> Decision:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip()).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError(f"约电话判断格式无法解析：{text[:120]}")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(data, dict):
        raise ValueError("约电话判断不是 JSON 对象")
    invite = data.get("invite")
    decision = Decision(invite=invite is True or str(invite).lower() == "true",
                        reason=str(data.get("reason", "")).strip()[:200],
                        message=str(data.get("message", "")).strip()[:200])
    if decision.invite and not decision.message:
        raise ValueError("模型决定约电话，但没有给出要发送的话")
    return decision


def _clock(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    begin, finish = _clock(start), _clock(end)
    current = now.time()
    if begin == finish:
        return False
    if begin < finish:
        return begin <= current < finish
    return current >= begin or current < finish  # 跨午夜，例如 00:30-09:00 或 23:00-08:00


def describe_gap(seconds: Optional[float]) -> str:
    if seconds is None:
        return "很久（最近三天没有记录）"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} 小时前"
    return f"{hours / 24:.1f} 天前"


class InviteState:
    """记录约过的时间和上次通话时间，保存在插件目录，重启 MaiBot 后仍有效。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.invites: List[float] = []
        self.last_call_at: Optional[float] = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.invites = [float(item) for item in data.get("invites", [])][-50:]
            self.last_call_at = data.get("last_call_at")
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"invites": self.invites[-50:], "last_call_at": self.last_call_at}
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    @property
    def last_invite_at(self) -> Optional[float]:
        return self.invites[-1] if self.invites else None


def blocked_reason(now: datetime, settings: InviteSettings, state: InviteState, in_call: bool,
                   last_user_at: Optional[float]) -> Optional[str]:
    """硬规则：返回不能约的原因；可以交给模型判断时返回 None。"""
    stamp = now.timestamp()
    if in_call:
        return "正在通话"
    if in_quiet_hours(now, settings.quiet_start, settings.quiet_end):
        return f"安静时段 {settings.quiet_start}-{settings.quiet_end}"
    if state.last_call_at and stamp - state.last_call_at < settings.after_call_hours * 3600:
        return f"上次通话不到 {settings.after_call_hours} 小时"
    last = state.last_invite_at
    if last and stamp - last < settings.min_hours_between * 3600:
        return f"距上次约电话不到 {settings.min_hours_between} 小时"
    if last and (last_user_at is None or last_user_at < last):
        return "上次约了还没回复，不追着问"
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if sum(1 for item in state.invites if item >= midnight) >= settings.max_per_day:
        return f"今天已经约了 {settings.max_per_day} 次"
    return None


class Inviter:
    def __init__(self, ctx: Any, settings: Callable[[], InviteSettings], allowed: Callable[[], List[str]],
                 in_call: Callable[[], bool], logger: logging.Logger, state_path: Path) -> None:
        self.ctx = ctx
        self.settings = settings
        self.allowed = allowed
        self.in_call = in_call
        self.logger = logger
        self.state = InviteState(state_path)
        self.last_reason = ""

    def target(self) -> str:
        settings = self.settings()
        return settings.target_qq or (self.allowed()[0] if self.allowed() else "")

    def call_started(self) -> None:
        self.state.last_call_at = datetime.now().timestamp()
        self.state.save()

    async def run(self) -> None:
        await asyncio.sleep(120)  # 等 MaiBot 启动完成
        while True:
            settings = self.settings()
            if settings.enabled:
                try:
                    await self.check()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.warning(f"主动约电话检查失败：{type(exc).__name__}: {exc}")
            await asyncio.sleep(settings.check_minutes * 60)

    async def _chat(self, qq: str):
        stream = await self.ctx.chat.get_stream_by_user_id(qq, platform="qq")
        if not isinstance(stream, dict) or not stream.get("session_id") or stream.get("is_group_session"):
            raise ValueError(f"找不到与 {qq} 的私聊，请先让对方给小睦发一条消息")
        stream_id = stream["session_id"]
        messages = await self.ctx.call_capability("message.get_recent", chat_id=stream_id,
                                                  limit=self.settings().context_messages, hours=72)
        if not isinstance(messages, list):
            raise ValueError("读取私聊记录失败")
        last_user_at = None
        for message in messages:
            user = ((message.get("message_info") or {}).get("user_info") or {}).get("user_id")
            if str(user) == qq:
                last_user_at = max(last_user_at or 0, float(message.get("timestamp") or 0))
        readable = ""
        if messages:
            readable = await self.ctx.message.build_readable(messages, replace_bot_name=False,
                                                             timestamp_mode="relative")
        name = stream.get("user_nickname") or qq
        return stream_id, name, readable if isinstance(readable, str) else "", last_user_at

    async def check(self, force: bool = False) -> Decision:
        """force=True：测试用，跳过硬规则直接问模型；模型同意时照常发送。"""
        settings = self.settings()
        qq = self.target()
        if not qq:
            raise ValueError("没有可以约的 QQ 号（invite.target_qq 和 plugin.allowed_qq_ids 都为空）")
        now = datetime.now()
        stream_id, name, readable, last_user_at = await self._chat(qq)
        blocked = None if force else blocked_reason(now, settings, self.state, self.in_call(), last_user_at)
        if blocked:
            if blocked != self.last_reason:
                self.logger.info(f"主动约电话：暂不考虑（{blocked}）")
            self.last_reason = blocked
            return Decision(invite=False, reason=blocked)
        decision = await self._ask(settings, now, name, readable, last_user_at)
        self.last_reason = ""
        if not decision.invite:
            self.logger.info(f"主动约电话：小睦决定先不约（{decision.reason}）")
            return decision
        if self.in_call():
            return Decision(invite=False, reason="判断期间电话已接通")
        await self.ctx.send.text(decision.message, stream_id)
        self.state.invites.append(datetime.now().timestamp())
        self.state.save()
        self.logger.info(f"主动约电话：已发给 {name}「{decision.message}」（理由：{decision.reason}）")
        return decision

    async def _ask(self, settings: InviteSettings, now: datetime, name: str, readable: str,
                   last_user_at: Optional[float]) -> Decision:
        nickname, personality, style = await asyncio.gather(
            self.ctx.config.get("bot.nickname", ""), self.ctx.config.get("personality.personality", ""),
            self.ctx.config.get("personality.behavior_style", ""),
        )
        stamp = now.timestamp()
        last_call = self.state.last_call_at
        template = (Path(__file__).parent / "prompts" / "invite.zh-CN.txt").read_text(encoding="utf-8")
        prompt = Template(template).substitute(
            nickname=nickname, personality=personality, style=style, name=name,
            now=f"{now:%Y-%m-%d %H:%M} 星期{WEEKDAYS[now.weekday()]}",
            last_user=describe_gap(None if last_user_at is None else stamp - last_user_at),
            last_call=describe_gap(None if not last_call else stamp - last_call) if last_call else "还没打过",
            last_invite=describe_gap(stamp - self.state.last_invite_at) if self.state.last_invite_at else "还没约过",
            context=(readable or "（最近三天没有聊天记录）")[-6000:],
        )
        result = await self.ctx.llm.generate(prompt=[{"role": "user", "content": prompt}],
                                             model=settings.task_name, max_tokens=300)
        if not result.get("success"):
            raise RuntimeError("模型调用失败，请查看 MaiBot 模型日志")
        return parse_decision(result["response"])

