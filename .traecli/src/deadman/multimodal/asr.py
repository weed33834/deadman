"""ASR 服务 - 语音转文字。

设计:
    - ASRResult: 转写结果(text + language + confidence + segments)
    - ASRProvider: provider 抽象基类
    - 各 provider(whisper.cpp / openai whisper / cloud)可选,懒加载
    - ASRService: provider fallback 链 cloud → openai → local → empty

降级策略:
    - cloud 不可用 → 切 openai whisper(API)
    - openai 不可用 → 切 whisper.cpp(本地)
    - 全部失败 → 返回空 text,confidence=0

业务场景(deadman):
    - 用户上传口述遗嘱音频 → 转文字
    - 葬礼现场录音 → 转文字(用于悼文素材)
    - 多语言支持(中文 / 英文 / 日语)

feature flag:`DEADMAN_MULTIMODAL_ENABLED=0`(默认 OFF)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..infrastructure.feature_flags import is_enabled

logger = logging.getLogger(__name__)


# 默认语言代码(BCP-47)
SUPPORTED_LANGUAGES: tuple[str, ...] = ("zh", "en", "ja", "ko", "auto")


@dataclass
class ASRSegment:
    """单段时间轴片段。"""

    text: str
    start: float  # 秒
    end: float    # 秒
    confidence: float = 1.0


@dataclass
class ASRResult:
    """ASR 转写结果。

    Attributes:
        text: 完整转写文本
        language: 检测到的语言(BCP-47,如 zh / en)
        confidence: 整体置信度 0.0-1.0
        segments: 分段时间轴片段
        provider: 实际使用的 provider 名
    """

    text: str
    language: str
    confidence: float
    segments: list[ASRSegment] = field(default_factory=list)
    provider: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "confidence": self.confidence,
            "segments": [
                {"text": s.text, "start": s.start, "end": s.end, "confidence": s.confidence}
                for s in self.segments
            ],
            "provider": self.provider,
        }


# =====================================================================
# Provider 抽象 + 实现
# =====================================================================


class ASRProvider:
    """ASR provider 基类。"""

    name: str = "base"

    def is_available(self) -> bool:
        return False

    def transcribe(self, audio_path: Path, language: str) -> ASRResult:
        raise NotImplementedError


class CloudASRProvider(ASRProvider):
    """云 ASR provider(mock 模式)。

    实际生产应接阿里云 NLS / 腾讯云 ASR / Google Speech-to-Text。
    """

    name = "cloud"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    def is_available(self) -> bool:
        return True  # mock 总是可用

    def transcribe(self, audio_path: Path, language: str) -> ASRResult:
        detected = language if language != "auto" else "zh"
        mock_text = "这是一段录音转写文本。" if detected == "zh" else "This is a transcribed audio."
        seg = ASRSegment(text=mock_text, start=0.0, end=2.5, confidence=0.95)
        return ASRResult(
            text=mock_text,
            language=detected,
            confidence=0.95,
            segments=[seg],
            provider=self.name,
        )


class OpenAIWhisperProvider(ASRProvider):
    """OpenAI Whisper API provider。

    通过 openai 库调用 Whisper API。openai 未安装 / 无 API key 时不可用。
    """

    name = "openai_whisper"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import openai  # type: ignore
            self._available = bool(self.api_key)
        except Exception as e:
            logger.debug("openai package not available: %s", e)
            self._available = False
        return self._available

    def transcribe(self, audio_path: Path, language: str) -> ASRResult:
        import openai  # type: ignore

        client = openai.OpenAI(api_key=self.api_key)
        with open(audio_path, "rb") as f:
            resp = client.audio.transcriptions.create(  # type: ignore[call-overload]
                model="whisper-1",
                file=f,
                language=None if language == "auto" else language,
                response_format="verbose_json",
            )
        segments = [
            ASRSegment(
                text=s.get("text", ""),
                start=float(s.get("start", 0.0)),
                end=float(s.get("end", 0.0)),
                confidence=float(s.get("avg_logprob", 0.0)),
            )
            for s in getattr(resp, "segments", []) or []
        ]
        return ASRResult(
            text=getattr(resp, "text", ""),
            language=getattr(resp, "language", language),
            confidence=0.9,
            segments=segments,
            provider=self.name,
        )


class WhisperCppProvider(ASRProvider):
    """whisper.cpp 本地 provider。

    通过 whispercpp / pywhispercpp 调用本地模型。
    依赖未安装时 is_available 返回 False。
    """

    name = "whisper_cpp"

    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import pywhispercpp  # type: ignore
            self._available = True
        except Exception as e:
            logger.debug("pywhispercpp not available: %s", e)
            self._available = False
        return self._available

    def transcribe(self, audio_path: Path, language: str) -> ASRResult:
        import pywhispercpp  # type: ignore

        model = pywhispercpp.Whisper(model_path=self.model_path) if self.model_path else pywhispercpp.Whisper()
        segments = model.transcribe(str(audio_path))
        text_parts = [s.text for s in segments]
        seg_list = [
            ASRSegment(
                text=s.text,
                start=float(getattr(s, "t0", 0.0)),
                end=float(getattr(s, "t1", 0.0)),
                confidence=float(getattr(s, "p", 0.8)),
            )
            for s in segments
        ]
        return ASRResult(
            text="".join(text_parts),
            language=language if language != "auto" else "zh",
            confidence=0.8,
            segments=seg_list,
            provider=self.name,
        )


# =====================================================================
# ASRService - provider fallback 链
# =====================================================================


class ASRService:
    """ASR 服务 - provider fallback 链 cloud → openai → whisper.cpp。

    用法:
        svc = ASRService()
        if svc.is_enabled():
            result = svc.transcribe(Path("audio.mp3"), language="auto")
            print(result.text)
    """

    def __init__(
        self,
        cloud_api_key: str | None = None,
        openai_api_key: str | None = None,
        custom_providers: list[ASRProvider] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.cloud_api_key = cloud_api_key
        self.openai_api_key = openai_api_key
        if custom_providers is not None:
            self._providers: list[ASRProvider] = list(custom_providers)
        else:
            self._providers = [
                CloudASRProvider(api_key=cloud_api_key),
                OpenAIWhisperProvider(api_key=openai_api_key),
                WhisperCppProvider(),
            ]

    def is_enabled(self) -> bool:
        return is_enabled("multimodal")

    def register_provider(self, provider: ASRProvider, position: int | None = None) -> None:
        with self._lock:
            if position is None:
                self._providers.append(provider)
            else:
                self._providers.insert(position, provider)

    def list_providers(self) -> list[str]:
        with self._lock:
            return [p.name for p in self._providers]

    def transcribe(
        self,
        audio_path: Path,
        language: str = "auto",
    ) -> ASRResult:
        """转写音频为文字。

        Args:
            audio_path: 音频文件路径(mp3/wav/m4a 等)
            language: 语言代码(zh/en/ja/ko/auto),auto 表示自动检测

        Returns:
            ASRResult
        """
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError("ASR service disabled (DEADMAN_MULTIMODAL_ENABLED=0)")

        if language not in SUPPORTED_LANGUAGES:
            logger.warning("Unsupported language %s, fallback to auto", language)
            language = "auto"

        if not Path(audio_path).exists():
            return ASRResult(
                text="",
                language=language,
                confidence=0.0,
                provider="none",
            )

        with self._lock:
            providers = list(self._providers)

        last_error: Exception | None = None
        for provider in providers:
            try:
                if not provider.is_available():
                    logger.debug("ASR provider %s not available, skip", provider.name)
                    continue
                result = provider.transcribe(audio_path, language)
                logger.info(
                    "ASR transcribed via %s (lang=%s, confidence=%.2f)",
                    provider.name, result.language, result.confidence,
                )
                return result
            except Exception as e:
                last_error = e
                logger.warning("ASR provider %s failed: %s", provider.name, e)
                continue

        logger.error("All ASR providers failed, last_error=%s", last_error)
        return ASRResult(
            text="",
            language=language,
            confidence=0.0,
            provider="failed",
        )


# 全局单例
_asr_instance: ASRService | None = None
_asr_lock = threading.Lock()


def get_asr_service() -> ASRService:
    """获取全局 ASRService 单例。"""
    global _asr_instance
    if _asr_instance is None:
        with _asr_lock:
            if _asr_instance is None:
                _asr_instance = ASRService()
    return _asr_instance
