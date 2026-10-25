from datetime import datetime
from types import SimpleNamespace

import logging
import pytest

from qq_voice_call.invite import Inviter, InviteState, blocked_reason, in_quiet_hours, parse_decision
from qq_voice_call.settings import InviteSettings, VoiceCallConfig


def test_parse_decision_variants():
    d = parse_decision('好的```json\n{"invite": true, "reason": "想她了", "message": "有空打给我吗？"}\n```')
    assert d.invite and d.message == "有空打给我吗？"
    assert not parse_decision('{"invite": false, "reason": "对方在上课", "message": ""}').invite
    with pytest.raises(ValueError):
        parse_decision('{"invite": true, "reason": "x", "message": ""}')
    with pytest.raises(ValueError):
        parse_decision("我觉得现在不适合")


def test_quiet_hours_across_midnight():
    assert in_quiet_hours(datetime(2026, 1, 1, 1, 0), "00:30", "09:00")
    assert not in_quiet_hours(datetime(2026, 1, 1, 9, 0), "00:30", "09:00")
    assert in_quiet_hours(datetime(2026, 1, 1, 23, 30), "23:00", "08:00")
    assert in_quiet_hours(datetime(2026, 1, 1, 7, 59), "23:00", "08:00")
    assert not in_quiet_hours(datetime(2026, 1, 1, 12, 0), "23:00", "08:00")


def test_blocked_reasons(tmp_path):
    s = InviteSettings(enabled=True)
    now = datetime(2026, 1, 1, 20, 0)
    t = now.timestamp()
    state = InviteState(tmp_path / "s.json")
    assert blocked_reason(now, s, state, False, t - 60) is None
    assert blocked_reason(now, s, state, True, None) == "正在通话"
    assert "安静" in blocked_reason(datetime(2026, 1, 1, 3, 0), s, state, False, None)
    state.last_call_at = t - 3600
    assert "通话" in blocked_reason(now, s, state, False, None)
    state.last_call_at = None
    state.invites = [t - 7 * 3600]
    assert "没回复" in blocked_reason(now, s, state, False, t - 8 * 3600)
    assert blocked_reason(now, s, state, False, t - 3600) is None
    state.invites = [t - 2 * 3600]
    assert "6" in blocked_reason(now, s, state, False, t)
    state.invites = [datetime(2026, 1, 1, 9, 30).timestamp(), datetime(2026, 1, 1, 13, 0).timestamp()]
    s.min_hours_between = 1
    assert "今天已经约了 2 次" == blocked_reason(now, s, state, False, t)


class FakeCtx:
    def __init__(self, response):
        self.sent = []
        self.response = response
        self.chat = SimpleNamespace(get_stream_by_user_id=self._stream)
        self.message = SimpleNamespace(build_readable=self._readable)
        self.config = SimpleNamespace(get=self._config)
        self.llm = SimpleNamespace(generate=self._generate)
        self.send = SimpleNamespace(text=self._send)
        self.prompts = []

    async def _stream(self, qq, platform="qq"):
        return {"session_id": "s1", "is_group_session": False, "user_nickname": "穗"}

    async def call_capability(self, name, **kw):
        return [{"timestamp": str(datetime.now().timestamp() - 600),
                 "message_info": {"user_info": {"user_id": "123"}}}]

    async def _readable(self, messages, **kw):
        return "穗：今天考试没考好"

    async def _config(self, key, default=""):
        return "小睦" if key == "bot.nickname" else ""

    async def _generate(self, prompt, model="", max_tokens=None):
        self.prompts.append(prompt[0]["content"])
        return {"success": True, "response": self.response}

    async def _send(self, text, stream_id):
        self.sent.append((text, stream_id))


def make_inviter(tmp_path, response, in_call=False):
    config = VoiceCallConfig()
    config.invite.enabled = True
    config.invite.quiet_start = config.invite.quiet_end = "00:00"  # 测试时不受时间影响
    ctx = FakeCtx(response)
    inviter = Inviter(ctx, lambda: config.invite, lambda: ["123"], lambda: in_call,
                      logging.getLogger("t"), tmp_path / "state.json")
    return inviter, ctx


async def test_check_sends_invitation_and_persists(tmp_path):
    inviter, ctx = make_inviter(tmp_path, '{"invite": true, "reason": "她心情不好", "message": "要不要打给我聊聊？"}')
    decision = await inviter.check()
    assert decision.invite and ctx.sent == [("要不要打给我聊聊？", "s1")]
    assert "穗：今天考试没考好" in ctx.prompts[0] and "小睦" in ctx.prompts[0]
    assert len(InviteState(tmp_path / "state.json").invites) == 1
    # 刚约过：硬规则拦住，不再调用模型
    again = await inviter.check()
    assert not again.invite and len(ctx.prompts) == 1 and len(ctx.sent) == 1


async def test_check_respects_model_no_and_in_call(tmp_path):
    inviter, ctx = make_inviter(tmp_path, '{"invite": false, "reason": "她在上课", "message": ""}')
    assert not (await inviter.check()).invite and ctx.sent == []
    busy, ctx2 = make_inviter(tmp_path / "b", '{"invite": true, "reason": "x", "message": "打给我"}', in_call=True)
    assert (await busy.check()).reason == "正在通话" and ctx2.prompts == []
