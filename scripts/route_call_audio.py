"""手动把 QQ 通话组件的声音切到虚拟声卡，并列出 svcl 看到的虚拟设备名称，便于核对配置。"""

from pathlib import Path

import csv
import io
import subprocess
import sys
import tomllib

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

from qq_voice_call.call_routing import NO_WINDOW, find_av_host_pids, route, svcl_path  # noqa: E402
from qq_voice_call.settings import VoiceCallConfig  # noqa: E402


def main() -> int:
    raw = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    audio = VoiceCallConfig.model_validate(raw).audio
    svcl = svcl_path(audio.svcl_path)
    print(f"svcl：{svcl}（{'存在' if svcl.is_file() else '★不存在★'}）")
    if not svcl.is_file():
        print("请下载 https://www.nirsoft.net/utils/svcl-x64.zip ，解压后把 svcl.exe 放到上面的位置。")
        return 1
    listing = subprocess.run([str(svcl), "/scomma", "", "/Columns", "Name,Type,Direction,Command-Line Friendly ID"],
                             capture_output=True, timeout=20, creationflags=NO_WINDOW)
    text = listing.stdout.decode("utf-8", "replace") if listing.stdout[:3] == b"\xef\xbb\xbf" or not listing.stdout \
        else listing.stdout.decode(sys.getfilesystemencoding(), "replace")
    print("\n== svcl 看到的虚拟声卡设备 ==")
    for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(row) >= 4 and row[1] == "Device" and ("PLAYBACK" in row[0] or "VIRTUAL REC" in row[0]):
            print(f"  {row[3]}")
    print(f"\n配置里的通话设备：\n  输出 {audio.call_render_device}\n  麦克风 {audio.call_capture_device}")
    pids = find_av_host_pids()
    print(f"\nQQ 通话组件进程：{pids or '没找到（请先运行 start-call-bridge.bat）'}")
    if not pids:
        return 1
    try:
        print(route(svcl, audio.call_render_device, audio.call_capture_device, pids))
    except Exception as exc:
        print(f"★切换失败：{exc}")
        return 1
    print("完成。通话时打开音量合成器，通话中的 QQ 应显示 PLAYBACK 3/4 / VIRTUAL REC 5/6。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
