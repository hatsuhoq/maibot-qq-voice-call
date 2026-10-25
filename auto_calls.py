from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import asyncio
import httpx
import logging
import time

from .settings import BridgeSettings

LONG_POLL_MS = 2000


class AutoCalls:
    """只信任带鉴权桥接报告的 connected 状态；后台轮询与模型启动互不阻塞。"""

    def __init__(self, config: BridgeSettings, allowed: List[str], on_start: Callable[[str, bool], Awaitable[Any]],
                 on_end: Callable[[], Awaitable[None]], logger: logging.Logger,
                 on_phase: Optional[Callable[[str, Optional[str]], None]] = None) -> None:
        self.config = config
        self.on_phase = on_phase
        self.allowed = allowed
        self.on_start = on_start
        self.on_end = on_end
        self.logger = logger
        self.state = "正在连接通话桥"
        self.error = ""
        self.task: Optional[asyncio.Task[None]] = None
        self.pending: Optional[asyncio.Task[Any]] = None
        self.key: Optional[str] = None
        self.legacy_warned = False
        self.last_phase: Optional[str] = None

    async def _end(self) -> None:
        if self.pending is not None:
            if not self.pending.done():
                self.pending.cancel()
            # 取出后台异常，避免丢失启动失败的原因。
            result = await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None
            if isinstance(result[0], Exception):
                self.error = f"{type(result[0]).__name__}: {result[0]}"
                self.logger.warning(f"电话已接通，但开始对话失败：{self.error}")
        await self.on_end()

    async def apply(self, data: Dict[str, Any]) -> None:
        phase = data["phase"]
        if phase not in {"idle", "ringing", "accepting", "accepted", "connected", "ended", "error"}:
            raise ValueError("QQ 桥返回未知通话状态")
        if self.pending is not None and self.pending.done():
            self.pending.result()
        peer = data.get("callerUin")
        invite = data.get("inviteAt")
        next_key = None
        if phase == "connected" and peer in self.allowed:
            if not isinstance(invite, str) or not invite:
                raise ValueError("通话桥未提供来电标识")
            next_key = f"{invite}:{peer}"
        if next_key != self.key:
            await self._end()
            self.key = next_key
            if next_key is not None:
                self.pending = asyncio.create_task(self.on_start(peer, True), name="qq_auto_call_start")
        if phase != self.last_phase:
            self.last_phase = phase
            if self.on_phase is not None:
                self.on_phase(phase, peer)
            if phase != "idle":
                allowed = "在允许名单内" if peer in self.allowed else ("尚未识别来电者" if not peer else "不在允许名单，不会接听")
                self.logger.info(f"通话桥状态：{phase}，来电 QQ {peer or '未知'}（{allowed}）")
        labels = {"idle": "等待来电", "ringing": "来电响铃", "accepting": "正在接听", "accepted": "已接受来电",
                  "connected": "通话已接通", "ended": "电话已挂断", "error": "通话桥报告错误"}
        self.state = labels[phase]

    async def run(self) -> None:
        try:
            path = Path(self.config.token_file)
            if not path.is_absolute():
                raise ValueError("bridge.token_file 必须为绝对路径")
            token = (await asyncio.to_thread(path.read_text, encoding="utf-8")).strip()
            if len(token.encode()) < 32:
                raise ValueError("通话桥 Token 长度不足")
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{self.config.port}", headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(3 + LONG_POLL_MS / 1000, connect=3), trust_env=False, follow_redirects=False,
            ) as client:
                renewed = 0.0
                version: Optional[str] = None
                try:
                    while True:
                        if time.monotonic() - renewed >= 5:
                            response = await client.put("/v1/policy", json={"enabled": True, "allowed": self.allowed})
                            response.raise_for_status()
                            if response.json()["code"] != 0:
                                raise ValueError("通话桥拒绝启用自动接听")
                            renewed = time.monotonic()
                        # 长轮询：状态一变通话桥立即返回，最多挂起 LONG_POLL_MS（短于策略续约间隔）。
                        params = {"since": version, "waitMs": LONG_POLL_MS} if version else None
                        response = await client.get("/v1/calls/current", params=params)
                        response.raise_for_status()
                        body = response.json()
                        if body["code"] != 0:
                            raise ValueError("读取通话状态失败")
                        await self.apply(body["data"])
                        version = body.get("version")
                        if version is None:
                            if not self.legacy_warned:
                                self.legacy_warned = True
                                self.logger.warning("NapCat 通话桥版本较旧，不支持长轮询，改为每 0.5 秒查询一次；"
                                                    "把 bridge/napcat-plugin/index.mjs 复制到 NapCat 插件目录并重载即可更新")
                            await asyncio.sleep(0.5)
                finally:
                    # 即使桥已离线，也先释放本地录音；失联的自动接听策略最多 15 秒失效。
                    await self._end()
                    try:
                        response = await client.put("/v1/policy", json={"enabled": False, "allowed": []})
                        response.raise_for_status()
                    except Exception:
                        self.logger.exception("未能通知通话桥关闭自动接听，策略将在最后续约后 15 秒内失效")
        except asyncio.CancelledError:
            self.state = "自动接听已停止"
            raise
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "自动接听发生错误"
            self.logger.exception("自动通话桥连接失败")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def snapshot(self) -> Dict[str, Any]:
        return {"state": self.state, "error": self.error, "active": self.task is not None and not self.task.done()}
