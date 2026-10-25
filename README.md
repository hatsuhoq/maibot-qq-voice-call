# QQ 实时语音插件（Windows 手动接听版）

独立的 MaiBot 第三方插件，插件 ID 为 `hatsuhoq.maibot-qq-voice-call`。
所有代码位于此目录；不修改 MaiBot、NapCat 或 QQ，不导入 `src.*`。
入口是 `plugin.py`，清单是 `_manifest.json`；实现全部三个生命周期和 `create_plugin()`。
使用 `@API` 和 `@Command`，不使用 `@Action`。

## 能做什么

- 在 Windows QQ 已手动接通的单人电话中，通过两条指定的音频通道听取、回复。
- WebRTC VAD 检测说话和停顿；默认用 GPT-SoVITS 整合包自带的 FunASR Paraformer 识别中文，用 GPT-SoVITS api_v2 逐句流式合成（也可换回 faster-whisper / CosyVoice 3）。
- 通过 SDK 读取真实 QQ 私聊、人设、近期消息，并使用 MaiBot 已配置的模型生成短回复和情绪。
- 用户插话时取消当前识别/回复/合成请求并清空播放队列；只有完整播放的助手回复进入电话历史。
- 中文控制页显示状态和 ASR、LLM、TTS 首段耗时。
- 配置热更新、卸载、异常、空闲超时、通话时长上限均停止音频桥接。

## 当前边界

这是手动接听、音频设备桥接的第一版。**不会自动接听、拨号、挂断或识别实际来电者**，也不订阅 QQ 通话状态；电话挂断后应立即停止桥接。空闲停止不能替代手动停止。
必须由本机操作人确认实际来电者与绑定 QQ 号一致。启动仅开放给宿主控制页，其他插件不能调用该 API。
一台机器同时仅支持一通电话。不支持群通话、说话人分离。

语音识别在停顿后执行；SDK 的 `llm.generate` 等待完整文本，然后才开始 TTS 音频流。
因此是支持插话的轮次式语音对话，不是语音到语音模型的全双工生成；不承诺固定毫秒延迟。
取消 HTTP/RPC 请求不保证已经运行的远端模型内核立即停止，但旧结果不会重新播放。
CosyVoice 服务会等待被取消的上一段推理结束后再处理新请求。

ASR/TTS 只允许本机地址。回复 LLM 沿用 MaiBot 的任务配置：若该任务调用云端模型，转写文本和人设会发送给该模型。希望完全离线时需在 MaiBot 配置本地 LLM 任务。
插件不保存原始音频，不写回宿主记忆，不向聊天发送转写；电话历史只保存在内存，停止后清空。读取的是基础人设及近期聊天，不等于运行完整 Planner 或其他插件的人设钩子。

## 1. 插件依赖

要求 MaiBot 1.3.6、SDK 2.12.0 及以上的 2.x、Python 3.12+。
保持目录为 `plugins/qq_voice_call/`。宿主按清单安装运行依赖。
本目录的独立开发环境：

```powershell
cd <MaiBot 目录>\plugins\qq_voice_call
uv sync --group dev
```

插件默认关闭，加载不会打开麦克风。实际 `config.toml` 由 Runner 生成；模板见 `examples/config.example.toml`。

## 2. 准备两条独立的音频通道

可以使用两条虚拟音频线，也可以使用声卡的独立 Loopback 通道。必须先在设备控制面板确认路由。

```text
QQ 的扬声器 → 通道 A 的播放端 → 通道 A 的录音端 → 插件 input_device
插件 output_device → 通道 B 的播放端 → 通道 B 的录音端 → QQ 的麦克风
```

不要把 A 和 B 混在一起，不要把系统全局声音或自己的扬声器回送到 A。插件没有声学回声消除；错误回送会让麦麦听到自己的回复并不断打断。
本机检测到 ROUTIST R2 的 VIRTUAL REC / PLAYBACK 设备，但未验证它们的内部路由，不能只凭名称认定两端配对。

打开 MaiBot「插件扩展 → QQ 语音通话」，点击「查询设备」。选择正确设备编号填入配置：

- 推荐用 `audio.input_device_name` / `audio.output_device_name` 按名称选择（例如 `VIRTUAL REC 3/4 (ROUTIST R2)`、`PLAYBACK 5/6 (ROUTIST R2)`），Windows 的设备编号会随启用、插拔而变化；填了名称时忽略编号，优先匹配 MME 接口。
- `audio.input_device`：A 的录音端，要求单声道 16kHz PCM16。
- `audio.output_device`：B 的播放端，要求单声道、与 TTS 相同采样率的 PCM16。
- 编号随设备、驱动和启动环境变化可能变化；插拔设备后重新查询。Windows MME 共享模式通常比独占设备更容易协商采样率；具体以启动检查为准。

## 3. 本地识别与合成（默认：复用 GPT-SoVITS 整合包，无需另外下载）

默认方案全部使用 GPT-SoVITS 整合包（如 GPT-SoVITS-v2pro-20250604）里已有的环境和模型，不往 MaiBot 主环境装任何依赖：

- **识别**：`services/funasr_server.py` 用整合包 `runtime\python.exe` 加载 `tools\asr\models` 下的 Paraformer-large、FSMN-VAD 和标点模型，监听 `127.0.0.1:8091`。
- **合成**：插件直接调用 GPT-SoVITS `api_v2.py`（`127.0.0.1:9880`）的 `/tts`，`streaming_mode=1` 逐句返回，插件解析流式 WAV 头后播放。与现有的 GPT-SoVITS 语音消息插件共用同一个服务，插件不会切换模型权重。

一键启动与自检（在资源管理器里双击即可）：

1. `scripts\start-voice-services.bat`：分别在两个窗口启动 9880 和 8091 服务，已在运行的会跳过。本机路径（整合包、NapCat、MaiBot 等）写在 `scripts\paths.local.bat`：首次使用复制 `scripts\paths.example.bat` 为 `paths.local.bat` 并按实际位置填写，该文件不会被提交。
2. `scripts\selfcheck.bat`：列出音频设备编号，用每种情绪的参考音频试合成一句，再把合成结果送去识别。结果在 `scripts\logs\selfcheck.log`，试听文件在 `scripts\logs\tts_*.wav`。

情绪靠参考音频控制：在 `speech.voice_styles` 中为 neutral/happy/sad/angry/gentle/surprised 各配一段对应语气的 3～10 秒参考音频和原文。只配 neutral 时所有情绪共用它。
`speech.tts_sample_rate` 必须等于 GPT-SoVITS 实际输出：v2/v2Pro 为 32000，v4 为 48000；不一致时插件会报错并提示正确数值。
GPT-SoVITS api_v2 的推理不加锁；插话取消后旧请求会在当前一句合成完时停止，期间新请求可能稍有排队。

### 可选：faster-whisper 识别

```powershell
uv sync --extra asr --group dev
uv run --extra asr python services/asr_server.py --model-dir <模型目录>\faster-whisper-small --device cpu --compute-type int8
```

模型需预先下载到本地；把 `speech.asr_model` 改为 `local-whisper`。

### 可选：CosyVoice 3 合成

在 CosyVoice 自己的环境里运行 `services/cosyvoice_server.py --repo ... --model-dir ... --reference ...`（监听 8092），然后把 `speech.tts_backend` 改为 `cosyvoice`、`tts_url` 改为 `http://127.0.0.1:8092`、`tts_sample_rate` 填 `/health` 返回的值。

## 5. 使用

1. 让目标 QQ 用户先向麦麦发一条私聊消息，确保真实聊天流已存在。
2. 在插件配置中填 `plugin.allowed_qq_ids`（字符串列表），音频设备编号、本地服务地址、`chat.task_name`，再启用插件。
3. 在 QQ 客户端手动接通单人电话，设置上面的扬声器/麦克风设备。
4. 在插件控制页填写实际对方 QQ，勾选已接通并核对来电者，点击开始。
5. 对方说话后停顿约 600ms，插件识别、生成回复并播放。连续说话约 100ms 会触发插话取消。
6. 结束时先点击停止，再在 QQ 挂断。当前绑定的私聊也可以发送 `/电话停止` 停止桥接。

控制页显示错误时，按错误修复服务、模型、音频设备或配置后重新开始；不会静默重试或自动重新录音。
`voice_stop` 仅关闭音频，不调用 QQ 挂断。配置更新会结束当前桥接，需要手动重新开始。

## 一键启动

双击 `scripts\start-all.bat`，按顺序检查并启动：Studio One 通话路由工程 → GPT-SoVITS（9880）与 FunASR（8091）→ NapCat 与通话桥（6110）→ 通话组件 AV Host（6111）→  麦麦（MaiBot）。已经在运行的会跳过，最后输出汇总，带 ★ 的是需要处理的项。
- Studio One 工程会在“文档\Studio One\Songs”里自动找名字含“通话 / 路由 / call / route”的 `.song`；找不到时在 `scripts\paths.local.bat` 里填写 `STUDIO_ONE_SONG`。
- 插件每 15 秒检查一次，语音服务或通话桥晚于 麦麦就绪、或中途重启时，会自动恢复等待来电；在控制页点“停止”后不会自动重开。
- 停止通话组件：`scripts\stop-call-bridge.bat`（不影响 NapCat 和 麦麦）。

## 测试

```powershell
cd <MaiBot 目录>\plugins\qq_voice_call
uv run --group dev pytest -q
uv run --group dev ruff check .
```

自动测试使用 SDK、本地内存音频和 HTTP 模拟，覆盖：VAD 起止、缓存背压、插话时取消旧回复、错误音频格式、真实聊天流缺失、跨私聊停止拒绝、重复启动、启动取消、配置热更新、卸载资源回收。测试不会打开真实录音设备或调用模型。

真实验收步骤：

1. 两个 `/health` 返回 ready；TTS 采样率一致。
2. 查询设备，验证 A 通道只有来电者、B 通道只有麦麦；先降低音量避免错误回送。
3. 连续聊五轮；检查实际内容、耗时、情绪表现和是否串入系统声音。
4. 在麦麦说话中插话，确认旧声音停止、随后回复新问题。
5. 在识别、回复、播放中分别停止；确认音频设备释放，不能在停止后播出迟到结果。
6. 通话中更新配置或卸载插件，确认立即停止收发；关闭模型服务验证错误显示。

当前代码验证不代表真实 QQ 通话验收完成，需本机模型与音频路由联调。

## 参考

- [MaiBot SDK](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md)
- [sounddevice 原始音频流](https://python-sounddevice.readthedocs.io/en/latest/api/raw-streams.html)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [CosyVoice 示例](https://github.com/QwenAudio/CosyVoice/blob/main/example.py)
