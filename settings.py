from typing import List, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import model_validator


class PluginSettings(PluginConfigBase):
    __ui_label__ = "插件"
    enabled: bool = Field(default=False, description="配置好本地服务和音频路由后启用；不会自动录音")
    config_version: str = Field(default="0.2.0", description="配置版本")
    allowed_qq_ids: List[str] = Field(default_factory=list, description="允许绑定的私聊 QQ 号，留空禁止启动")


class AudioSettings(PluginConfigBase):
    __ui_label__ = "通话音频"
    input_device: int = Field(default=-1, ge=-1, description="对方声音的录音设备编号；从插件音频设备页查询")
    output_device: int = Field(default=-1, ge=-1, description="送给 QQ 麦克风的播放设备编号")
    input_device_name: str = Field(default="", description="按名称选择录音设备（优先于编号，编号会随插拔变化），例如 VIRTUAL REC 3/4 (ROUTIST R2)")
    output_device_name: str = Field(default="", description="按名称选择播放设备（优先于编号），例如 PLAYBACK 5/6 (ROUTIST R2)")
    vad_mode: int = Field(default=2, ge=0, le=3, description="语音检测强度，越高越严格")
    start_ms: int = Field(default=100, ge=40, le=300, multiple_of=20, description="连续说话多久触发插话，毫秒")
    barge_in_ms: int = Field(default=600, ge=100, le=3000, multiple_of=20,
                             description="小睦说话时，对方连续说话多久才打断她；更短的声音（嗯、咳嗽、回声）会被忽略")
    end_ms: int = Field(default=600, ge=200, le=2000, multiple_of=20, description="停顿多久提交识别，毫秒")
    early_asr_ms: int = Field(default=300, ge=0, le=1980, multiple_of=20,
                              description="停顿多久先在后台提前识别，毫秒；对方没再出声就直接用这次结果，0 为关闭。必须小于 end_ms")
    max_utterance_seconds: int = Field(default=20, ge=2, le=30, description="每段语音最长秒数")
    idle_seconds: int = Field(default=120, ge=30, le=600, description="没有检测到用户讲话时自动停止桥接的秒数")
    max_call_minutes: int = Field(default=30, ge=1, le=120, description="单次桥接最长分钟数")
    route_call_audio: bool = Field(default=True, description="自动把 QQ 通话组件的扬声器/麦克风切到下面两个虚拟设备（需要 svcl.exe）")
    svcl_path: str = Field(default="", description="svcl.exe 路径；留空则使用插件目录 tools\\svcl\\svcl.exe")
    call_render_device: str = Field(default=r"ROUTIST*\Device\PLAYBACK 3/4\Render",
                                    description="QQ 通话组件的输出设备（svcl 的 Command-Line Friendly ID）")
    call_capture_device: str = Field(default=r"ROUTIST*\Device\VIRTUAL REC 5/6\Capture",
                                     description="QQ 通话组件的麦克风（svcl 的 Command-Line Friendly ID）")

    @model_validator(mode="after")
    def early_before_end(self) -> "AudioSettings":
        # 提前识别的切片保留 100ms 尾部静音，至少要停顿 120ms 才有意义。
        if self.early_asr_ms and not 120 <= self.early_asr_ms < self.end_ms:
            raise ValueError("audio.early_asr_ms 必须为 0，或在 120ms 与 end_ms 之间")
        return self

    def has_devices(self) -> bool:
        has_input = bool(self.input_device_name.strip()) or self.input_device >= 0
        has_output = bool(self.output_device_name.strip()) or self.output_device >= 0
        return has_input and has_output


EMOTIONS = ("neutral", "happy", "sad", "angry", "gentle", "surprised")
Emotion = Literal["neutral", "happy", "sad", "angry", "gentle", "surprised"]


class VoiceStyle(PluginConfigBase):
    """GPT-SoVITS 用一段参考音频决定语气；每种情绪配一段对应语气的参考音频。"""

    emotion: Emotion = Field(default="neutral", description="对应的回复情绪：neutral/happy/sad/angry/gentle/surprised")
    ref_audio_path: str = Field(default="", description="参考音频的绝对路径，建议 3～10 秒、单人、无背景音")
    prompt_text: str = Field(default="", description="参考音频里说的原文，必须与音频一致")
    prompt_lang: str = Field(default="zh", description="参考音频语言：zh/en/ja/ko/yue")


class SpeechSettings(PluginConfigBase):
    __ui_label__ = "本地语音服务"
    asr_url: str = Field(default="http://127.0.0.1:8091", description="本地语音识别服务（随插件的 FunASR 或 faster-whisper 脚本）")
    asr_model: str = Field(default="paraformer-zh", description="语音识别服务的模型名称")
    tts_backend: Literal["gpt_sovits", "cosyvoice"] = Field(
        default="gpt_sovits", description="gpt_sovits=直接调用 GPT-SoVITS api_v2；cosyvoice=随插件的 CosyVoice 服务")
    tts_url: str = Field(default="http://127.0.0.1:9880", description="GPT-SoVITS api_v2 或 CosyVoice 服务的地址")
    tts_sample_rate: int = Field(
        default=32000, ge=16000, le=48000,
        description="TTS 输出采样率：GPT-SoVITS v2/v2Pro 为 32000，v3/v4 为 24000/48000；必须与实际输出一致")
    gpt_sovits_streaming_mode: int = Field(
        default=1, ge=0, le=3,
        description="GPT-SoVITS 流式模式：1=逐句返回（音质最好），2/3=更快出声但音质较低，0=整段合成完再播")
    gpt_sovits_speed: float = Field(default=1.0, ge=0.6, le=1.6, description="GPT-SoVITS 语速倍率")
    voice_styles: List[VoiceStyle] = Field(
        default_factory=list,
        description="GPT-SoVITS 情绪参考音频列表；至少配置 neutral，其他情绪缺失时使用 neutral")
    timeout_seconds: int = Field(default=45, ge=5, le=180, description="单次识别、回复或合成超时秒数")

    @model_validator(mode="after")
    def local_only(self) -> "SpeechSettings":
        from urllib.parse import urlsplit

        for value in (self.asr_url, self.tts_url):
            url = urlsplit(value)
            if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
                raise ValueError("语音服务地址必须是本机 HTTP 根地址，例如 http://127.0.0.1:8091")
        seen = [style.emotion for style in self.voice_styles]
        if len(seen) != len(set(seen)):
            raise ValueError("每种情绪只能配置一段参考音频")
        return self

    def style_for(self, emotion: str) -> VoiceStyle:
        """返回情绪对应的参考音频；没有单独配置时退回 neutral，neutral 也没有则报错。"""
        styles = {style.emotion: style for style in self.voice_styles}
        style = styles.get(emotion) or styles.get("neutral")
        if style is None or not style.ref_audio_path.strip():
            raise ValueError("请在插件配置的 voice_styles 中至少填写 neutral 情绪的参考音频路径和原文")
        return style


class ChatSettings(PluginConfigBase):
    __ui_label__ = "电话回复"
    task_name: str = Field(default="replyer", description="沿用 MaiBot 中已配置的模型任务")
    context_messages: int = Field(default=12, ge=0, le=30, description="接通时读取的当前私聊消息数量")
    history_turns: int = Field(default=8, ge=1, le=20, description="内存中保留的电话轮数，停止后清空")
    max_reply_chars: int = Field(default=180, ge=20, le=500, description="每次电话回复最大字数")
    prompt_language: Literal["zh-CN", "en-US", "ja-JP"] = Field(default="zh-CN", description="提示词模板语言")
    fillers: List[str] = Field(
        default_factory=lambda: ["嗯……", "嗯嗯。", "唔，我想想。"],
        description="识别出对方的话后立即播放的短应答，掩盖模型思考时间；接通时预先合成，留空则关闭")
    filler_delay_seconds: float = Field(default=1.2, ge=0, le=5, description="回复迟迟没生成好多少秒后才考虑插入短应答")
    filler_probability: float = Field(default=0.4, ge=0, le=1, description="满足条件时插入短应答的概率；连续两句不会都插")
    filler_min_chars: int = Field(default=6, ge=0, le=50, description="对方这句话至少多少字才考虑插入短应答")


class InviteSettings(PluginConfigBase):
    __ui_label__ = "主动约电话"
    enabled: bool = Field(default=False, description="让小睦根据私聊记录自己决定什么时候发消息约对方打电话")
    target_qq: str = Field(default="", description="约谁；留空则为允许通话名单里的第一个 QQ 号")
    check_minutes: int = Field(default=45, ge=10, le=720, description="每隔多少分钟让小睦考虑一次（每次调用一次回复模型）")
    min_hours_between: float = Field(default=6, ge=1, le=168, description="两次约电话至少间隔多少小时")
    max_per_day: int = Field(default=2, ge=1, le=10, description="每天最多约几次")
    after_call_hours: float = Field(default=3, ge=0, le=72, description="通完电话后多少小时内不再约")
    quiet_start: str = Field(default="00:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="安静时段开始（不约）")
    quiet_end: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="安静时段结束")
    context_messages: int = Field(default=30, ge=5, le=60, description="判断时读取最近多少条私聊（最多三天内）")
    task_name: str = Field(default="replyer", description="判断和写邀请消息使用的模型任务")


class BridgeSettings(PluginConfigBase):
    __ui_label__ = "QQ 自动接听桥"
    enabled: bool = Field(default=False, description="接入 Windows 原生 AVSDK 桥，按真实来电自动开始和停止对话")
    token_file: str = Field(default="", description="安装脚本生成的 control.token 绝对路径")
    port: int = Field(default=6110, ge=1024, le=65535, description="本机 NapCat 通话桥端口")


class VoiceCallConfig(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    bridge: BridgeSettings = Field(default_factory=BridgeSettings)
    invite: InviteSettings = Field(default_factory=InviteSettings)
