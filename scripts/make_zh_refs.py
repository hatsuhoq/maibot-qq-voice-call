"""用“艾丽妮”v4 模型生成中文情绪参考音频，生成后切回原模型。

1. 从 tts_infer.yaml 读出当前（通话用的）模型路径，作为结束后要恢复的目标；
2. 切换到艾丽妮 v4 的 GPT / SoVITS 权重；
3. 每种情绪用对应的日语原声片段做参考，合成两句中文（不同随机种子），保存到 clips_zh；
4. 无论成功与否都切回原模型，避免影响平时的语音消息和电话。
"""

from pathlib import Path

import io
import os
import re
import sys
import time
import wave

import httpx

# 路径来自 scripts\paths.local.bat（由 make-zh-refs.bat 读取）。
GSV_ROOT = Path(os.environ["GSV"])
GPT_WEIGHTS = "GPT_weights_v4/艾丽妮-e10.ckpt"
SOVITS_WEIGHTS = "SoVITS_weights_v4/艾丽妮_e10_s200_l32.pth"
JA_CLIPS = Path(os.environ["ZH_REF_JA_CLIPS"])
OUT_DIR = Path(os.environ["ZH_REF_OUT_DIR"])
API = "http://127.0.0.1:9880"

# 情绪 → (日语参考片段, 参考原文, 要合成的中文句子)
JA_TEXT = {}
for line in (JA_CLIPS / "清单.txt").read_text(encoding="utf-8-sig").splitlines()[1:]:
    parts = line.split("\t")
    if len(parts) == 4:
        JA_TEXT[parts[2]] = parts[3]

PLAN = [
    ("neutral", "neutral.wav", "嗯，我在听呢。你今天过得怎么样，有什么想跟我说的吗？"),
    ("happy", "happy.wav", "真的吗？太好了！我就知道你一定可以做到的，好开心呀。"),
    ("gentle", "gentle.wav", "没关系的，累了就先休息一下吧，我会一直在这里陪着你。"),
    ("sad", "neutral_alt.wav", "这样啊……听你这么说，我心里也有点难过，对不起。"),
    ("surprised", "surprised.wav", "诶？你说的是真的吗？这也太突然了吧，我完全没想到！"),
    ("angry", "angry.wav", "都几点了还不睡？说了多少次了，身体要紧，快去休息！"),
]
SEEDS = (11, 23)


def current_weights() -> tuple:
    text = (GSV_ROOT / "GPT_SoVITS" / "configs" / "tts_infer.yaml").read_text(encoding="utf-8")
    custom = text.split("\nv1:")[0]
    t2s = re.search(r"t2s_weights_path:\s*(.+)", custom).group(1).strip()
    vits = re.search(r"vits_weights_path:\s*(.+)", custom).group(1).strip()
    return t2s, vits


def switch(client: httpx.Client, gpt: str, sovits: str) -> None:
    for endpoint, path in (("/set_gpt_weights", gpt), ("/set_sovits_weights", sovits)):
        response = client.get(API + endpoint, params={"weights_path": path})
        if response.status_code != 200:
            raise RuntimeError(f"{endpoint} 失败：{response.text[:300]}")


def duration(data: bytes) -> tuple:
    with wave.open(io.BytesIO(data)) as reader:
        return reader.getnframes() / reader.getframerate(), reader.getframerate()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    original = current_weights()
    print(f"当前模型（结束后恢复）：\n  GPT    {original[0]}\n  SoVITS {original[1]}")
    rows = ["情绪\t版本\t时长\t文件\t原文（prompt_text）\t参考的日语片段"]
    failures = 0
    with httpx.Client(timeout=300, trust_env=False) as client:
        for _ in range(90):  # 最多等 3 分钟 GPT-SoVITS 启动
            try:
                if client.get(API + "/docs", timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            print("★GPT-SoVITS（9880）没有启动，请先运行 start-voice-services.bat")
            return 1
        try:
            print("\n切换到艾丽妮 v4 模型（首次加载可能需要几十秒）……")
            switch(client, GPT_WEIGHTS, SOVITS_WEIGHTS)
            for emotion, ref, text in PLAN:
                for index, seed in enumerate(SEEDS, 1):
                    payload = {
                        "text": text, "text_lang": "zh",
                        "ref_audio_path": str(JA_CLIPS / ref), "prompt_text": JA_TEXT[ref], "prompt_lang": "ja",
                        "text_split_method": "cut0", "batch_size": 1, "seed": seed,
                        "media_type": "wav", "streaming_mode": False,
                    }
                    began = time.monotonic()
                    try:
                        response = client.post(API + "/tts", json=payload)
                        if response.status_code != 200:
                            raise RuntimeError(response.text[:300])
                        seconds, rate = duration(response.content)
                        name = f"{emotion}_{index}.wav"
                        (OUT_DIR / name).write_bytes(response.content)
                        mark = "" if 3 < seconds < 10 else "  ★不在 3～10 秒内，不能当参考"
                        print(f"  {name}：{seconds:.1f} 秒，{rate} Hz，耗时 {time.monotonic() - began:.1f} 秒{mark}")
                        rows.append(f"{emotion}\t{index}\t{seconds:.1f}s\t{name}\t{text}\t{ref}")
                    except Exception as exc:
                        failures += 1
                        print(f"  {emotion}_{index}：★失败 {type(exc).__name__}: {exc}")
        finally:
            print("\n切回原模型……")
            try:
                switch(client, *original)
                print("  已恢复。")
            except Exception as exc:
                failures += 1
                print(f"  ★恢复失败：{exc}\n  请重启 GPT-SoVITS 服务窗口（scripts\\run-gpt-sovits.bat）。")
    (OUT_DIR / "清单.txt").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    print(f"\n完成，文件在 {OUT_DIR}。" + ("有失败项，见上面带 ★ 的行。" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
