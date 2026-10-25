"""查询 NapCat 通话桥（6110）和 AV Host（6111）的状态，写入 scripts/logs/bridge-status.log。"""

from pathlib import Path

import json
import sys

import httpx

PLUGIN_DIR = Path(__file__).resolve().parents[1]
TOKEN_FILE = PLUGIN_DIR / "bridge" / "runtime" / "control.token"


def show(client: httpx.Client, name: str, url: str) -> None:
    print(f"\n== {name} ==")
    try:
        response = client.get(url)
        print(f"  HTTP {response.status_code}")
        data = response.json().get("data", response.json())
        if isinstance(data, dict):
            data.pop("recentEvents", None)
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
    except Exception as exc:
        print(f"  ★连接失败★ {type(exc).__name__}: {exc}")


def main() -> int:
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=3, trust_env=False, headers=headers) as client:
        show(client, "NapCat 通话桥（6110，需重启 NapCat 后才会出现）", "http://127.0.0.1:6110/v1/status")
        show(client, "AV Host（6111，start-call-bridge.bat 启动）", "http://127.0.0.1:6111/v1/status")
    log = PLUGIN_DIR / "bridge" / "runtime"
    latest = sorted(log.glob("av-host*.stdout.log"), key=lambda item: item.stat().st_mtime)[-1:]
    stderr = [item.with_name(item.name.replace(".stdout.", ".stderr.")) for item in latest]
    for path in [log / "av-host.log", *latest, *stderr]:
        name = path.name
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
            print(f"\n== {name}（最后 25 行） ==")
            print("\n".join("  " + line for line in lines))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
