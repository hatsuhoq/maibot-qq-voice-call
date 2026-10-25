"""通话中诊断：小睦听不到对方时，在通话进行中运行，确认声音卡在哪一步。

1. Studio One 是否在运行；
2. svcl 列出的 QQ 音频会话：每个 QQ 进程实际用的是哪个设备；
3. 录 4 秒 VIRTUAL REC 3/4（插件的输入）和 PLAYBACK 3/4 的回环，看有没有对方的声音。
"""

from array import array
from pathlib import Path

import csv
import io
import math
import subprocess
import sys

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

from qq_voice_call.call_routing import NO_WINDOW, find_av_host_pids, svcl_path  # noqa: E402


def level(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return -120.0
    peak = max(abs(v) for v in samples)
    return 20 * math.log10(max(peak / 32768, 1e-6))


def studio_one() -> None:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    running = [row[0] for row in csv.reader(io.StringIO(out)) if row and "studio" in row[0].lower()]
    print(f"Studio One 进程：{running or '★没有在运行★'}")


def sessions() -> None:
    svcl = svcl_path()
    if not svcl.is_file():
        print("★没有 svcl.exe，跳过音频会话检查")
        return
    raw = subprocess.run([str(svcl), "/scomma", ""], capture_output=True, timeout=20, creationflags=NO_WINDOW).stdout
    text = raw.decode("utf-8-sig", "replace") if raw[:3] == b"\xef\xbb\xbf" else raw.decode("mbcs", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        print("★svcl 没有输出")
        return
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    av = set(find_av_host_pids())
    print(f"\n通话组件进程：{sorted(av)}")
    print("QQ 的音频会话（进程号 / 方向 / 实际设备 / 是否通话组件）：")
    found = False
    for row in rows[1:]:
        get = lambda key: row[col[key]] if key in col and col[key] < len(row) else ""  # noqa: E731
        if get("Type") != "Application" or "qq" not in (get("Process Path") + get("Name")).lower():
            continue
        found = True
        pid = get("Process ID")
        mark = "通话组件" if pid.isdigit() and int(pid) in av else "其他 QQ"
        device = get("Command-Line Friendly ID") or get("Device Name")
        if "PLAYBACK" not in device and "VIRTUAL REC" not in device:
            device += "  | " + " | ".join(f"{k}={row[i]}" for k, i in col.items() if i < len(row) and row[i])
        print(f"  {pid:>6}  {get('Direction'):<8} [{mark}]  {device}")
    if found and "Command-Line Friendly ID" not in col:
        print("  （svcl 没有输出设备通道名）")
    if not found:
        print("  （没有 QQ 的音频会话：通话组件此刻没有在播放/录音）")


def record() -> None:
    import sounddevice as sd

    hosts = sd.query_hostapis()
    for name in ("VIRTUAL REC 3/4", "VIRTUAL REC 5/6"):
        device = next((i for i, d in enumerate(sd.query_devices())
                       if name in d["name"] and hosts[d["hostapi"]]["name"] == "MME" and d["max_input_channels"]), None)
        if device is None:
            print(f"★找不到 {name}")
            continue
        with sd.RawInputStream(samplerate=48000, channels=2, dtype="int16", device=device) as stream:
            data, _ = stream.read(48000 * 4)
        peak = level(bytes(data))
        print(f"  {name}：4 秒内峰值 {peak:6.1f} dBFS" + ("  ← 有声音" if peak > -45 else "  （几乎无声）"))


def main() -> int:
    print("请在通话进行中运行，并且运行时一直对着手机说话。\n")
    studio_one()
    sessions()
    print("\n录音 4 秒（请继续说话）……")
    print("  VIRTUAL REC 3/4 = 对方声音经 Studio One 进插件；VIRTUAL REC 5/6 = 小睦的声音进 QQ 麦克风")
    try:
        record()
    except Exception as exc:
        print(f"★录音失败：{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
