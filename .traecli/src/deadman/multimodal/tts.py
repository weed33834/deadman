"""TTS 服务 - 文字转语音。

设计:
    - TTSResult: 合成结果(audio_bytes + format + duration + voice)
    - VoiceProfile: 嗓音 profile 枚举(gentle_male / gentle_female / professional_*)
    - TTSProvider: provider 抽象基类
    - 各 provider(azure / openai / edge-tts)可选,懒加载
    - TTSService: provider fallback 链 azure → openai → edge → empty

嗓音 profile 设计(deadman 场景关键):
    - gentle_male / gentle_female: 悼文 / 追思会致辞(温和抚慰)
    - professional_male / professional_female: 公告 / 通知 / 法律文书(正式庄重)
    - 选错嗓音(如悼文用活泼女声)会冒犯丧属,所以嗓音必须显式枚举。

feature flag:`DEADMAN_MULTIMODAL_ENABLED=0`(默认 OFF)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled

logger = logging.getLogger(__name__)


class VoiceProfile(str, Enum):
    """嗓音 profile - 适配悼文/讣告/法律文书的语气。"""

    GENTLE_MALE = "gentle_male"          # 温和男声(悼文 / 家书)
    GENTLE_FEMALE = "gentle_female"      # 温和女声(悼文 / 家书)
    PROFESSIONAL_MALE = "professional_male"      # 正式男声(公告 / 通知)
    PROFESSIONAL_FEMALE = "professional_female"  # 正式女声(公告 / 通知)


class AudioFormat(str, Enum):
    """音频格式。"""

    MP3 = "mp3"
    WAV = "wav"


# 各嗓音 profile 对各 provider 的 voice ID 映射(示意,真实应查 provider 文档)
_VOICE_MAP: dict[VoiceProfile, dict[str, str]] = {
    VoiceProfile.GENTLE_MALE: {
        "azure": "zh-CN-YunxiNeural",
        "openai": "onyx",
        "edge": "zh-CN-YunxiNeural",
    },
    VoiceProfile.GENTLE_FEMALE: {
        "azure": "zh-CN-XiaoyiNeural",
        "openai": "shimmer",
        "edge": "zh-CN-XiaoyiNeural",
    },
    VoiceProfile.PROFESSIONAL_MALE: {
        "azure": "zh-CN-YunjianNeural",
        "openai": "echo",
        "edge": "zh-CN-YunjianNeural",
    },
    VoiceProfile.PROFESSIONAL_FEMALE: {
        "azure": "zh-CN-XiaochenNeural",
        "openai": "nova",
        "edge": "zh-CN-XiaochenNeural",
    },
}


@dataclass
class TTSResult:
    """TTS 合成结果。

    Attributes:
        audio_bytes: 音频二进制
        format: 音频格式(mp3 / wav)
        duration_seconds: 时长(秒)
        voice: 实际使用的嗓音 profile
        provider: 实际使用的 provider 名
    """

    audio_bytes: bytes
    format: AudioFormat
    duration_seconds: float
    voice: VoiceProfile
    provider: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format.value,
            "duration_seconds": self.duration_seconds,
            "voice": self.voice.value,
            "provider": self.provider,
            "size_bytes": len(self.audio_bytes),
        }


# =====================================================================
# Provider 抽象 + 实现
# =====================================================================


class TTSProvider:
    """TTS provider 基类。"""

    name: str = "base"

    def is_available(self) -> bool:
        return False

    def synthesize(self, text: str, voice: VoiceProfile, speed: float) -> TTSResult:
        raise NotImplementedError


class AzureTTSProvider(TTSProvider):
    """Azure Speech TTS provider。

    通过 azure-cognitiveservices-speech 库调用。库未安装 / 无 key 时不可用。
    """

    name = "azure"

    def __init__(self, api_key: Optional[str] = None, region: Optional[str] = None) -> None:
        self.api_key = api_key
        self.region = region
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import azure.cognitiveservices.speech  # type: ignore  # noqa: F401
            self._available = bool(self.api_key and self.region)
        except Exception as e:
            logger.debug("azure-cognitiveservices-speech not available: %s", e)
            self._available = False
        return self._available

    def synthesize(self, text: str, voice: VoiceProfile, speed: float) -> TTSResult:
        import azure.cognitiveservices.speech as speechsdk  # type: ignore

        voice_id = _VOICE_MAP[voice]["azure"]
        speech_config = speechsdk.SpeechConfig(
            subscription=self.api_key, region=self.region,
        )
        speech_config.speech_synthesis_voice_name = voice_id
        synth = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=None)
        result = synth.speak_audio_async(text).get()
        audio_bytes = result.audio_data
        duration = len(audio_bytes) / (16000 * 2)  # 16kHz 16bit 估算
        return TTSResult(
            audio_bytes=audio_bytes,
            format=AudioFormat.WAV,
            duration_seconds=duration,
            voice=voice,
            provider=self.name,
        )


class OpenAITTSProvider(TTSProvider):
    """OpenAI TTS provider。

    通过 openai 库调用。库未安装 / 无 key 时不可用。
    """

    name = "openai"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import openai  # type: ignore  # noqa: F401
            self._available = bool(self.api_key)
        except Exception as e:
            logger.debug("openai not available: %s", e)
            self._available = False
        return self._available

    def synthesize(self, text: str, voice: VoiceProfile, speed: float) -> TTSResult:
        import openai  # type: ignore

        client = openai.OpenAI(api_key=self.api_key)
        voice_id = _VOICE_MAP[voice]["openai"]
        resp = client.audio.speech.create(
            model="tts-1",
            voice=voice_id,
            input=text,
            speed=speed,
        )
        audio_bytes = resp.content
        # mp3 ~ 1 sec/KB 估算(粗略)
        duration = len(audio_bytes) / 4000.0
        return TTSResult(
            audio_bytes=audio_bytes,
            format=AudioFormat.MP3,
            duration_seconds=duration,
            voice=voice,
            provider=self.name,
        )


class EdgeTTSProvider(TTSProvider):
    """edge-tts provider(免费,基于微软 Edge TTS 服务)。

    通过 edge-tts 库调用,无需 API key。库未安装时不可用。
    """

    name = "edge_tts"

    def __init__(self) -> None:
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import edge_tts  # type: ignore  # noqa: F401
            self._available = True
        except Exception as e:
            logger.debug("edge-tts not available: %s", e)
            self._available = False
        return self._available

    def synthesize(self, text: str, voice: VoiceProfile, speed: float) -> TTSResult:
        import asyncio

        import edge_tts  # type: ignore

        voice_id = _VOICE_MAP[voice]["edge"]

        async def _run() -> bytes:
            communicate = edge_tts.Communicate(text, voice_id)
            data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    data += chunk["data"]
            return data

        audio_bytes = asyncio.run(_run())
        duration = len(audio_bytes) / 4000.0
        return TTSResult(
            audio_bytes=audio_bytes,
            format=AudioFormat.MP3,
            duration_seconds=duration,
            voice=voice,
            provider=self.name,
        )


class MockTTSProvider(TTSProvider):
    """Mock TTS provider - 总是可用,返回占位音频。

    业务层降级时使用,确保调用方不因无 provider 抛错。
    """

    name = "mock"

    def is_available(self) -> bool:
        return True

    def synthesize(self, text: str, voice: VoiceProfile, speed: float) -> TTSResult:
        # 占位字节(实际不是有效音频,仅用于测试 / 降级)
        audio_bytes = b"MOCK-AUDIO\x00\x01\x02" + text.encode("utf-8")[:64]
        duration = max(0.5, len(text) / (200 * speed))  # 200 字/分钟
        return TTSResult(
            audio_bytes=audio_bytes,
            format=AudioFormat.MP3,
            duration_seconds=duration,
            voice=voice,
            provider=self.name,
        )


# =====================================================================
# TTSService - provider fallback 链
# =====================================================================


class TTSService:
    """TTS 服务 - provider fallback 链 azure → openai → edge → mock。

    用法:
        svc = TTSService()
        if svc.is_enabled():
            result = svc.synthesize("悼文文本", VoiceProfile.GENTLE_MALE, speed=1.0)
            with open("out.mp3", "wb") as f:
                f.write(result.audio_bytes)
    """

    def __init__(
        self,
        azure_api_key: Optional[str] = None,
        azure_region: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        custom_providers: Optional[list[TTSProvider]] = None,
    ) -> None:
        self._lock = threading.RLock()
        if custom_providers is not None:
            self._providers: list[TTSProvider] = list(custom_providers)
        else:
            self._providers = [
                AzureTTSProvider(api_key=azure_api_key, region=azure_region),
                OpenAITTSProvider(api_key=openai_api_key),
                EdgeTTSProvider(),
                MockTTSProvider(),
            ]

    def is_enabled(self) -> bool:
        return is_enabled("multimodal")

    def register_provider(self, provider: TTSProvider, position: Optional[int] = None) -> None:
        with self._lock:
            if position is None:
                self._providers.append(provider)
            else:
                self._providers.insert(position, provider)

    def list_providers(self) -> list[str]:
        with self._lock:
            return [p.name for p in self._providers]

    def synthesize(
        self,
        text: str,
        voice: VoiceProfile = VoiceProfile.GENTLE_FEMALE,
        speed: float = 1.0,
    ) -> TTSResult:
        """合成语音。

        Args:
            text: 待合成文本
            voice: 嗓音 profile(悼文场景务必选 GENTLE_*)
            speed: 语速 0.5-2.0,1.0 = 正常

        Returns:
            TTSResult
        """
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError("TTS service disabled (DEADMAN_MULTIMODAL_ENABLED=0)")

        if not text:
            return TTSResult(
                audio_bytes=b"",
                format=AudioFormat.MP3,
                duration_seconds=0.0,
                voice=voice,
                provider="empty",
            )

        speed = max(0.5, min(2.0, speed))

        with self._lock:
            providers = list(self._providers)

        last_error: Optional[Exception] = None
        for provider in providers:
            try:
                if not provider.is_available():
                    logger.debug("TTS provider %s not available, skip", provider.name)
                    continue
                result = provider.synthesize(text, voice, speed)
                logger.info(
                    "TTS synthesized via %s (voice=%s, size=%d)",
                    provider.name, voice.value, len(result.audio_bytes),
                )
                return result
            except Exception as e:
                last_error = e
                logger.warning("TTS provider %s failed: %s", provider.name, e)
                continue

        logger.error("All TTS providers failed, last_error=%s", last_error)
        return TTSResult(
            audio_bytes=b"",
            format=AudioFormat.MP3,
            duration_seconds=0.0,
            voice=voice,
            provider="failed",
        )


# 全局单例
_tts_instance: Optional[TTSService] = None
_tts_lock = threading.Lock()


def get_tts_service() -> TTSService:
    """获取全局 TTSService 单例。"""
    global _tts_instance
    if _tts_instance is None:
        with _tts_lock:
            if _tts_instance is None:
                _tts_instance = TTSService()
    return _tts_instance
