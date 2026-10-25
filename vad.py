from collections import deque
from typing import Deque, List, Optional, Tuple

from .settings import AudioSettings


class TurnDetector:
    """接收 16kHz 单声道 PCM16 的 20ms 帧，保留起音并限制单段长度。"""

    def __init__(self, config: AudioSettings) -> None:
        self.config = config
        self.pre_roll: Deque[bytes] = deque(maxlen=15)
        self.frames: List[bytes] = []
        self.active = False
        self.voiced = 0
        self.silent = 0
        self.early: Optional[bytes] = None

    def _cut(self) -> bytes:
        # 尾部静音保留 100ms，避免切掉句尾。提前识别和最终提交用同一规则，停顿后没再出声时两者字节完全相同。
        return b"".join(self.frames[:len(self.frames) - max(0, self.silent - 5)])

    def take_early(self) -> Optional[bytes]:
        """取出停顿达到 early_asr_ms 时的语音切片（每段话最多一次）。"""
        early, self.early = self.early, None
        return early

    def feed(self, frame: bytes, is_speech: bool) -> Tuple[bool, Optional[bytes]]:
        if len(frame) != 640:
            raise ValueError("输入音频必须为 16kHz 单声道 PCM16，每帧 20ms")
        started = False
        if not self.active:
            self.pre_roll.append(frame)
            self.voiced = self.voiced + 1 if is_speech else 0
            if self.voiced >= self.config.start_ms // 20:
                self.active = True
                started = True
                self.frames = list(self.pre_roll)
                self.pre_roll.clear()
        else:
            self.frames.append(frame)
            self.voiced += int(is_speech)
            self.silent = 0 if is_speech else self.silent + 1
            if (self.silent >= self.config.end_ms // 20
                    or len(self.frames) >= self.config.max_utterance_seconds * 50):
                # 短噪声不交给 ASR。
                result = self._cut()
                valid = self.voiced >= 10
                self.active = False
                self.frames = []
                self.voiced = self.silent = 0
                self.early = None
                return started, result if valid else None
            if self.config.early_asr_ms and self.silent == self.config.early_asr_ms // 20 and self.voiced >= 10:
                self.early = self._cut()
        return started, None
