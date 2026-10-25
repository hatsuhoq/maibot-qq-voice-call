"""把 QQ 通话组件（AV Host）的声音单独切到虚拟声卡。

AV Host 跟随 Windows 默认设备出声；音量合成器里给它单独指定设备即可，但这个设置只对当前进程有效，
通话桥重启后就会丢失。这里用 NirSoft SoundVolumeCommandLine（svcl.exe）按进程号设置，
只影响命令行里带本插件专用配置目录 av-host-profile 的 QQ 进程，不碰小睦和你自己的 QQ。
"""

from pathlib import Path
from typing import List, Optional, Sequence

import subprocess
import sys

PROFILE_MARK = "av-host-profile"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_SVCL = PLUGIN_DIR / "tools" / "svcl" / "svcl.exe"
PID_FILE = PLUGIN_DIR / "bridge" / "runtime" / "av-host.pid"

_FIND_PIDS = (
    "Get-CimInstance Win32_Process -Filter \"Name = 'QQ.exe'\" | "
    f"Where-Object {{ $_.CommandLine -like '*{PROFILE_MARK}*' -and $_.CommandLine -notmatch '(^|\\s)-q(\\s|$)' }} | "
    "ForEach-Object { $_.ProcessId }"
)


def svcl_path(configured: str = "") -> Path:
    return Path(configured) if configured.strip() else DEFAULT_SVCL


def find_av_host_pids() -> List[int]:
    """AV Host 主进程号（启动脚本记录的）加上所有带 av-host-profile 的 QQ 子进程。"""
    pids = set()
    try:
        pids.add(int(PID_FILE.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        pass
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _FIND_PIDS],
        capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW,
    )
    for line in result.stdout.split():
        if line.strip().isdigit():
            pids.add(int(line))
    return sorted(pids)


def route(svcl: Path, render: str, capture: str, pids: Optional[Sequence[int]] = None) -> str:
    """返回一句给日志看的结果；失败时抛出带中文原因的异常。"""
    if sys.platform != "win32":
        raise RuntimeError("只支持 Windows")
    if not svcl.is_file():
        raise FileNotFoundError(
            f"没找到 {svcl}。请下载 https://www.nirsoft.net/utils/svcl-x64.zip ，把里面的 svcl.exe 放到这个位置")
    targets = list(pids) if pids is not None else find_av_host_pids()
    if not targets:
        raise RuntimeError("没找到 QQ 通话组件进程，通话桥可能还没启动")
    failed = []
    for pid in targets:
        for device in (render, capture):
            done = subprocess.run([str(svcl), "/SetAppDefault", device, "all", str(pid)],
                                  capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
            if done.returncode != 0:
                failed.append(f"{pid}:{device}（退出码 {done.returncode}）")
    if failed:
        raise RuntimeError("部分设置失败：" + "，".join(failed))
    return f"已把 QQ 通话组件（进程 {', '.join(map(str, targets))}）的声音切到 {render} / {capture}"
