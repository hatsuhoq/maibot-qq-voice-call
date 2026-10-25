from types import SimpleNamespace

import pytest

from qq_voice_call import call_routing


def test_route_sets_both_devices_for_every_pid(monkeypatch, tmp_path):
    svcl = tmp_path / "svcl.exe"
    svcl.write_bytes(b"")
    calls = []
    monkeypatch.setattr(call_routing.sys, "platform", "win32")
    monkeypatch.setattr(call_routing.subprocess, "run",
                        lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0))
    message = call_routing.route(svcl, "R", "C", [10, 20])
    assert [c[1:] for c in calls] == [["/SetAppDefault", d, "all", p] for p in ("10", "20") for d in ("R", "C")]
    assert "10, 20" in message


def test_route_reports_missing_tool_and_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(call_routing.sys, "platform", "win32")
    with pytest.raises(FileNotFoundError, match="svcl-x64.zip"):
        call_routing.route(tmp_path / "svcl.exe", "R", "C", [1])
    svcl = tmp_path / "svcl.exe"
    svcl.write_bytes(b"")
    monkeypatch.setattr(call_routing.subprocess, "run", lambda args, **kw: SimpleNamespace(returncode=2))
    with pytest.raises(RuntimeError, match="退出码 2"):
        call_routing.route(svcl, "R", "C", [1])
    with pytest.raises(RuntimeError, match="没找到 QQ 通话组件"):
        call_routing.route(svcl, "R", "C", [])
