"""MultimodalPipeline - 多模态服务编排层。

职责:
    - 统一注册 / 路由各能力(OCR / ASR / TTS / Vision / ImageGen)
    - 每次 call 写 audit log(谁 / 何时 / 用了哪个 provider / 耗时 / 成本)
    - 集成 PIIRedactor:OCR 提取的文本默认走 PII 脱敏
    - 集成 BudgetCoordinator:每次多模态调用按预估成本扣 budget
    - feature flag 关闭时抛 MultimodalDisabledError

设计:
    - MultimodalConfig: pipeline 配置(开关 / 默认 provider / budget)
    - MultimodalPipeline: 编排 + audit + 集成 PII / budget
    - MultimodalDisabledError: feature flag 关闭时的统一异常

audit log 格式:
    {
        "ts": 1690000000.0,
        "capability": "ocr",        # ocr / asr / tts / vision / image_gen
        "provider": "cloud",
        "user_id": "u123",
        "success": true,
        "duration_ms": 250,
        "tokens_used": 0,
        "bytes_in": 102400,
        "bytes_out": 256,
        "pii_detected": true,
        "pii_redacted": true,
        "error": ""
    }

feature flag:`DEADMAN_MULTIMODAL_ENABLED=0`(默认 OFF)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import resolve_data_path
from .asr import ASRResult, ASRService
from .image_gen import ImageGenerator, ImageSize, ImageStyle
from .ocr import DocType, OCRResult, OCRService
from .tts import TTSResult, TTSService, VoiceProfile
from .vision import VisionService

logger = logging.getLogger(__name__)


class MultimodalDisabledError(Exception):
    """多模态功能被禁用(feature flag DEADMAN_MULTIMODAL_ENABLED=0)。"""

    def __init__(self, message: str = "Multimodal disabled (DEADMAN_MULTIMODAL_ENABLED=0)") -> None:
        super().__init__(message)


@dataclass
class MultimodalConfig:
    """MultimodalPipeline 配置。

    Attributes:
        enable_ocr / enable_asr / enable_tts / enable_vision / enable_image_gen:
            各能力开关(独立于 feature flag,feature flag 是总闸)
        default_provider: 默认 provider 名(如 "cloud" / "mock")
            若为 None 则走 fallback 链
        budget_token_per_session: 每会话多模态 token budget 上限
            (由 BudgetCoordinator 强制,超限降级)
        audit_log_enabled: 是否写 audit log(默认 True)
        pii_redact_ocr: 是否对 OCR 输出做 PII 脱敏(默认 True)
    """

    enable_ocr: bool = True
    enable_asr: bool = True
    enable_tts: bool = True
    enable_vision: bool = True
    enable_image_gen: bool = True
    default_provider: str | None = None
    budget_token_per_session: int = 50_000
    audit_log_enabled: bool = True
    pii_redact_ocr: bool = True


@dataclass
class AuditEntry:
    """单次多模态调用的审计记录。"""

    ts: float
    capability: str           # ocr / asr / tts / vision / image_gen
    provider: str
    user_id: str
    success: bool
    duration_ms: float
    tokens_used: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    pii_detected: bool = False
    pii_redacted: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =====================================================================
# MultimodalPipeline
# =====================================================================


class MultimodalPipeline:
    """多模态服务编排层。

    用法:
        pipe = MultimodalPipeline()
        if pipe.is_enabled():
            ocr_result = pipe.ocr_extract(Path("id.png"), DocType.ID_CARD, user_id="u1")
            asr_result = pipe.asr_transcribe(Path("a.mp3"), language="zh", user_id="u1")
    """

    def __init__(
        self,
        config: MultimodalConfig | None = None,
        ocr_service: OCRService | None = None,
        asr_service: ASRService | None = None,
        tts_service: TTSService | None = None,
        vision_service: VisionService | None = None,
        image_generator: ImageGenerator | None = None,
        audit_log_path: Path | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.config = config or MultimodalConfig()
        self._ocr = ocr_service or OCRService()
        self._asr = asr_service or ASRService()
        self._tts = tts_service or TTSService()
        self._vision = vision_service or VisionService()
        self._imgen = image_generator or ImageGenerator()
        # audit log 路径(走多租户路由)
        self._audit_log_path = audit_log_path or resolve_data_path("multimodal/_audit.jsonl")
        self._audit_buffer: list[AuditEntry] = []

    # ==================================================================
    # 总闸
    # ==================================================================

    def is_enabled(self) -> bool:
        """多模态总开关。"""
        return is_enabled("multimodal")

    def _check_enabled(self) -> None:
        if not self.is_enabled():
            raise MultimodalDisabledError(
                "Multimodal pipeline disabled (DEADMAN_MULTIMODAL_ENABLED=0)"
            )

    def _check_capability(self, capability: str) -> None:
        """检查单能力是否在 config 中启用。"""
        flag_map = {
            "ocr": self.config.enable_ocr,
            "asr": self.config.enable_asr,
            "tts": self.config.enable_tts,
            "vision": self.config.enable_vision,
            "image_gen": self.config.enable_image_gen,
        }
        if not flag_map.get(capability, False):
            raise MultimodalDisabledError(
                f"Multimodal capability '{capability}' disabled in pipeline config"
            )

    # ==================================================================
    # 路由 + audit + budget + pii
    # ==================================================================

    def ocr_extract(
        self,
        image_path: Path,
        doc_type: DocType = DocType.OTHER,
        user_id: str = "unknown",
    ) -> OCRResult:
        """OCR 提取(默认对输出做 PII 脱敏)。"""
        self._check_enabled()
        self._check_capability("ocr")

        start = time.time()
        error_msg = ""
        pii_detected = False
        pii_redacted = False
        try:
            result = self._ocr.extract(image_path, doc_type)
            # PII 脱敏(关键集成点:OCR 提取的文本必须过 PII 检测)
            if self.config.pii_redact_ocr and result.text:
                try:
                    from ..infrastructure.defense.pii_guard import PIIRedactor

                    redactor = PIIRedactor()
                    pii_result = redactor.detect(result.text)
                    if pii_result.has_pii:
                        pii_detected = True
                        redacted_result = redactor.redact(result.text)
                        result.text = redacted_result.redacted_text
                        result.redacted = True
                        pii_redacted = True
                        logger.info(
                            "OCR output PII redacted (file_id-type=%s, matches=%d)",
                            doc_type.value,
                            len(pii_result.matches),
                        )
                except Exception as e:
                    logger.warning("PII redaction failed (continuing): %s", e)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="ocr",
                    provider=result.provider,
                    user_id=user_id,
                    success=True,
                    duration_ms=duration_ms,
                    bytes_in=Path(image_path).stat().st_size if Path(image_path).exists() else 0,
                    bytes_out=len(result.text.encode("utf-8")),
                    pii_detected=pii_detected,
                    pii_redacted=pii_redacted,
                )
            )
            self._consume_budget(user_id, tokens=100, consumer="multimodal_ocr")
            return result
        except Exception as e:
            if isinstance(e, MultimodalDisabledError):
                raise
            error_msg = str(e)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="ocr",
                    provider="error",
                    user_id=user_id,
                    success=False,
                    duration_ms=duration_ms,
                    error=error_msg,
                )
            )
            raise

    def asr_transcribe(
        self,
        audio_path: Path,
        language: str = "auto",
        user_id: str = "unknown",
    ) -> ASRResult:
        """ASR 转写。"""
        self._check_enabled()
        self._check_capability("asr")

        start = time.time()
        error_msg = ""
        try:
            result = self._asr.transcribe(audio_path, language)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="asr",
                    provider=result.provider,
                    user_id=user_id,
                    success=True,
                    duration_ms=duration_ms,
                    bytes_in=Path(audio_path).stat().st_size if Path(audio_path).exists() else 0,
                    bytes_out=len(result.text.encode("utf-8")),
                    tokens_used=max(0, len(result.text) // 4),
                )
            )
            self._consume_budget(user_id, tokens=200, consumer="multimodal_asr")
            return result
        except Exception as e:
            if isinstance(e, MultimodalDisabledError):
                raise
            error_msg = str(e)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="asr",
                    provider="error",
                    user_id=user_id,
                    success=False,
                    duration_ms=duration_ms,
                    error=error_msg,
                )
            )
            raise

    def tts_synthesize(
        self,
        text: str,
        voice: VoiceProfile = VoiceProfile.GENTLE_FEMALE,
        speed: float = 1.0,
        user_id: str = "unknown",
    ) -> TTSResult:
        """TTS 合成语音。"""
        self._check_enabled()
        self._check_capability("tts")

        start = time.time()
        error_msg = ""
        try:
            result = self._tts.synthesize(text, voice, speed)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="tts",
                    provider=result.provider,
                    user_id=user_id,
                    success=True,
                    duration_ms=duration_ms,
                    bytes_in=len(text.encode("utf-8")),
                    bytes_out=len(result.audio_bytes),
                    tokens_used=max(0, len(text) // 4),
                )
            )
            self._consume_budget(user_id, tokens=150, consumer="multimodal_tts")
            return result
        except Exception as e:
            if isinstance(e, MultimodalDisabledError):
                raise
            error_msg = str(e)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="tts",
                    provider="error",
                    user_id=user_id,
                    success=False,
                    duration_ms=duration_ms,
                    error=error_msg,
                )
            )
            raise

    def vision_describe(
        self,
        image_path: Path,
        prompt: str = "描述这张图片",
        user_id: str = "unknown",
    ) -> str:
        """Vision 描述图片。"""
        self._check_enabled()
        self._check_capability("vision")

        start = time.time()
        error_msg = ""
        try:
            text = self._vision.describe(image_path, prompt)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="vision",
                    provider="unknown",
                    user_id=user_id,
                    success=True,
                    duration_ms=duration_ms,
                    bytes_in=Path(image_path).stat().st_size if Path(image_path).exists() else 0,
                    bytes_out=len(text.encode("utf-8")),
                    tokens_used=max(0, len(text) // 4),
                )
            )
            self._consume_budget(user_id, tokens=300, consumer="multimodal_vision")
            return text
        except Exception as e:
            if isinstance(e, MultimodalDisabledError):
                raise
            error_msg = str(e)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="vision",
                    provider="error",
                    user_id=user_id,
                    success=False,
                    duration_ms=duration_ms,
                    error=error_msg,
                )
            )
            raise

    def image_gen_generate(
        self,
        prompt: str,
        style: ImageStyle = ImageStyle.MEMORIAL_CARD,
        size: ImageSize = ImageSize.SQUARE_1024,
        user_id: str = "unknown",
    ) -> bytes:
        """生成图片。"""
        self._check_enabled()
        self._check_capability("image_gen")

        start = time.time()
        error_msg = ""
        try:
            img_bytes = self._imgen.generate(prompt, style, size)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="image_gen",
                    provider="unknown",
                    user_id=user_id,
                    success=True,
                    duration_ms=duration_ms,
                    bytes_in=len(prompt.encode("utf-8")),
                    bytes_out=len(img_bytes),
                    tokens_used=500,  # 图片生成按固定 500 token 计
                )
            )
            self._consume_budget(user_id, tokens=500, consumer="multimodal_image_gen")
            return img_bytes
        except Exception as e:
            if isinstance(e, MultimodalDisabledError):
                raise
            error_msg = str(e)
            duration_ms = (time.time() - start) * 1000
            self._record_audit(
                AuditEntry(
                    ts=start,
                    capability="image_gen",
                    provider="error",
                    user_id=user_id,
                    success=False,
                    duration_ms=duration_ms,
                    error=error_msg,
                )
            )
            raise

    # ==================================================================
    # 路由(by capability string)
    # ==================================================================

    def route(
        self,
        capability: str,
        user_id: str = "unknown",
        **kwargs: Any,
    ) -> Any:
        """按 capability 名路由到对应服务。

        capability ∈ {"ocr", "asr", "tts", "vision", "image_gen"}
        kwargs 透传给对应方法。
        """
        self._check_enabled()
        if capability == "ocr":
            return self.ocr_extract(
                kwargs["image_path"],
                kwargs.get("doc_type", DocType.OTHER),
                user_id=user_id,
            )
        if capability == "asr":
            return self.asr_transcribe(
                kwargs["audio_path"],
                kwargs.get("language", "auto"),
                user_id=user_id,
            )
        if capability == "tts":
            return self.tts_synthesize(
                kwargs["text"],
                kwargs.get("voice", VoiceProfile.GENTLE_FEMALE),
                kwargs.get("speed", 1.0),
                user_id=user_id,
            )
        if capability == "vision":
            return self.vision_describe(
                kwargs["image_path"],
                kwargs.get("prompt", "描述这张图片"),
                user_id=user_id,
            )
        if capability == "image_gen":
            return self.image_gen_generate(
                kwargs["prompt"],
                kwargs.get("style", ImageStyle.MEMORIAL_CARD),
                kwargs.get("size", ImageSize.SQUARE_1024),
                user_id=user_id,
            )
        raise ValueError(f"Unknown capability: {capability}")

    def list_capabilities(self) -> list[str]:
        """列出当前可用的能力(config 中启用的)。"""
        flags = {
            "ocr": self.config.enable_ocr,
            "asr": self.config.enable_asr,
            "tts": self.config.enable_tts,
            "vision": self.config.enable_vision,
            "image_gen": self.config.enable_image_gen,
        }
        return [cap for cap, on in flags.items() if on]

    # ==================================================================
    # 内部:audit / budget
    # ==================================================================

    def _record_audit(self, entry: AuditEntry) -> None:
        if not self.config.audit_log_enabled:
            return
        with self._lock:
            self._audit_buffer.append(entry)
        # 写到 JSONL 文件(append,失败不抛)
        try:
            self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            logger.warning("Audit log write failed: %s", e)

    def get_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """读取 audit log(最近 limit 条)。"""
        with self._lock:
            buffer = list(self._audit_buffer[-limit:])
        return [e.to_dict() for e in buffer]

    def _consume_budget(self, user_id: str, tokens: int, consumer: str) -> None:
        """通过 BudgetCoordinator 扣减预算(失败不阻塞)。"""
        try:
            from ..infrastructure.defense.budget_coordinator import (
                BudgetCoordinator,
                BudgetDimension,
                BudgetScope,
                get_budget_coordinator,
            )

            bc: BudgetCoordinator = get_budget_coordinator()
            alloc = bc.allocate(
                scope=BudgetScope.USER,
                scope_id=user_id,
                dimension=BudgetDimension.LLM_TOKENS,
                amount=tokens,
                consumer=consumer,
            )
            if alloc is None:
                logger.warning(
                    "Multimodal budget rejected for user=%s consumer=%s tokens=%d (degrading)",
                    user_id, consumer, tokens,
                )
            else:
                # 立即释放(实际已用 = amount)
                bc.release(alloc.allocation_id, actual_used=tokens)
        except Exception as e:
            logger.debug("Budget consume failed (non-fatal): %s", e)

    # ==================================================================
    # 服务注册(支持注入 / 替换)
    # ==================================================================

    def register_ocr(self, service: OCRService) -> None:
        with self._lock:
            self._ocr = service

    def register_asr(self, service: ASRService) -> None:
        with self._lock:
            self._asr = service

    def register_tts(self, service: TTSService) -> None:
        with self._lock:
            self._tts = service

    def register_vision(self, service: VisionService) -> None:
        with self._lock:
            self._vision = service

    def register_image_gen(self, gen: ImageGenerator) -> None:
        with self._lock:
            self._imgen = gen


# =====================================================================
# 全局单例
# =====================================================================

_pipeline_instance: MultimodalPipeline | None = None
_pipeline_lock = threading.Lock()


def get_multimodal_pipeline() -> MultimodalPipeline:
    """获取全局 MultimodalPipeline 单例。"""
    global _pipeline_instance
    if _pipeline_instance is None:
        with _pipeline_lock:
            if _pipeline_instance is None:
                _pipeline_instance = MultimodalPipeline()
    return _pipeline_instance


def reset_multimodal_pipeline() -> None:
    """重置全局 pipeline 单例(测试用)。"""
    global _pipeline_instance
    with _pipeline_lock:
        _pipeline_instance = None
