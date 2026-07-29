"""OCR 服务 - 图片识别(证件 / 文档)。

设计:
    - OCRResult: 单次识别结果(text + confidence + doc_type + fields)
    - DocType: 文档类型枚举(id_card / passport / death_certificate / will / other)
    - OCRProvider: provider 抽象基类
    - 各 provider 实现可选,通过 try/except 懒加载,缺失时降级
    - OCRService: provider fallback 链 cloud → local → manual

降级策略:
    - cloud 不可用 → 切 local(Tesseract)
    - local 不可用 → 切 manual(返回提示用户手动输入的占位结果)
    - 全部失败 → 返回 confidence=0.0 的空结果

业务场景(deadman):
    - id_card: 身份证识别(用于逝者身份核验)
    - passport: 护照识别(跨境死亡证明)
    - death_certificate: 死亡证明字段提取(姓名 / 死因 / 日期 / 编号)
    - will: 遗嘱文本提取
    - other: 通用图片文字提取

feature flag:`DEADMAN_MULTIMODAL_ENABLED=0`(默认 OFF)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..infrastructure.feature_flags import is_enabled

logger = logging.getLogger(__name__)


class DocType(str, Enum):
    """文档类型(决定字段提取策略)。"""

    ID_CARD = "id_card"
    PASSPORT = "passport"
    DEATH_CERTIFICATE = "death_certificate"
    WILL = "will"
    OTHER = "other"


@dataclass
class OCRResult:
    """OCR 识别结果。

    Attributes:
        text: 提取到的全文
        confidence: 置信度 0.0-1.0
        doc_type: 文档类型
        fields: 结构化字段(id_card → {name, id_number, ...};
                  death_certificate → {decedent_name, death_date, ...})
        provider: 实际使用的 provider 名
        redacted: 是否经过 PII 脱敏(由 pipeline 触发)
    """

    text: str
    confidence: float
    doc_type: DocType
    fields: dict[str, Any] = field(default_factory=dict)
    provider: str = "unknown"
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "doc_type": self.doc_type.value,
            "fields": self.fields,
            "provider": self.provider,
            "redacted": self.redacted,
        }


# =====================================================================
# Provider 抽象 + 实现
# =====================================================================


class OCRProvider:
    """OCR provider 基类。"""

    name: str = "base"

    def is_available(self) -> bool:
        """provider 是否可用(API key 配置 / 依赖安装)。"""
        return False

    def extract(self, image_path: Path, doc_type: DocType) -> OCRResult:
        raise NotImplementedError


class CloudOCRProvider(OCRProvider):
    """云 OCR provider(mock 模式)。

    实际生产应接阿里云 OCR / 腾讯云 OCR / Google Vision。
    本实现:无 API key 时走 mock,返回 canned 数据。
    """

    name = "cloud"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    def is_available(self) -> bool:
        # mock 模式总是可用(无 key 时返回 canned)
        return True

    def extract(self, image_path: Path, doc_type: DocType) -> OCRResult:
        # mock 数据(真实场景:调云 API)
        mock_text = self._mock_text(doc_type)
        mock_fields = self._mock_fields(doc_type)
        return OCRResult(
            text=mock_text,
            confidence=0.92,
            doc_type=doc_type,
            fields=mock_fields,
            provider=self.name,
        )

    def _mock_text(self, doc_type: DocType) -> str:
        if doc_type == DocType.ID_CARD:
            return "姓名 张三 身份证号 110101199001011234"
        if doc_type == DocType.PASSPORT:
            return "Passport E12345678 Name ZHANG SAN"
        if doc_type == DocType.DEATH_CERTIFICATE:
            return "死亡证明 张三 2025年1月1日死亡 死因 自然死亡 编号 DC20250101001"
        if doc_type == DocType.WILL:
            return "遗嘱 立遗嘱人 张三 财产由儿子继承"
        return "图片文字提取结果"

    def _mock_fields(self, doc_type: DocType) -> dict[str, Any]:
        if doc_type == DocType.ID_CARD:
            return {"name": "张三", "id_number": "110101199001011234"}
        if doc_type == DocType.PASSPORT:
            return {"name": "ZHANG SAN", "passport_number": "E12345678"}
        if doc_type == DocType.DEATH_CERTIFICATE:
            return {
                "decedent_name": "张三",
                "death_date": "2025-01-01",
                "cause": "自然死亡",
                "certificate_number": "DC20250101001",
            }
        if doc_type == DocType.WILL:
            return {"testator": "张三", "beneficiary": "儿子"}
        return {}


class TesseractOCRProvider(OCRProvider):
    """本地 Tesseract OCR provider。

    通过 pytesseract 调用本地 tesseract 可执行文件。
    pytesseract 未安装 / tesseract 二进制缺失时 is_available 返回 False。
    """

    name = "tesseract"

    def __init__(self) -> None:
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore

            # 探测 tesseract 二进制
            pytesseract.get_tesseract_version()
            self._available = True
        except Exception as e:  # ImportError / EnvironmentError
            logger.debug("Tesseract not available: %s", e)
            self._available = False
        return self._available

    def extract(self, image_path: Path, doc_type: DocType) -> OCRResult:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        text = pytesseract.image_to_string(Image.open(image_path))
        return OCRResult(
            text=text.strip(),
            confidence=0.7,
            doc_type=doc_type,
            fields={},
            provider=self.name,
        )


class ManualInputProvider(OCRProvider):
    """手动输入兜底 provider。

    所有自动 provider 不可用时返回提示,业务层应转人工录入。
    """

    name = "manual"

    def is_available(self) -> bool:
        return True

    def extract(self, image_path: Path, doc_type: DocType) -> OCRResult:
        return OCRResult(
            text=f"[需要人工录入] 自动 OCR 不可用,请手动输入 {doc_type.value} 内容",
            confidence=0.0,
            doc_type=doc_type,
            fields={},
            provider=self.name,
        )


# =====================================================================
# OCRService - provider fallback 链
# =====================================================================


class OCRService:
    """OCR 服务 - provider fallback 链 cloud → local → manual。

    用法:
        svc = OCRService()
        if svc.is_enabled():
            result = svc.extract(Path("id.png"), DocType.ID_CARD)
            print(result.text, result.confidence)
    """

    def __init__(
        self,
        cloud_api_key: str | None = None,
        custom_providers: list[OCRProvider] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.cloud_api_key = cloud_api_key
        if custom_providers is not None:
            self._providers: list[OCRProvider] = list(custom_providers)
        else:
            # 默认 fallback 链:cloud → tesseract → manual
            self._providers = [
                CloudOCRProvider(api_key=cloud_api_key),
                TesseractOCRProvider(),
                ManualInputProvider(),
            ]

    def is_enabled(self) -> bool:
        """feature flag 是否开启。"""
        return is_enabled("multimodal")

    def register_provider(self, provider: OCRProvider, position: int | None = None) -> None:
        """注册 / 插入 provider。"""
        with self._lock:
            if position is None:
                self._providers.append(provider)
            else:
                self._providers.insert(position, provider)

    def list_providers(self) -> list[str]:
        """列出所有已注册 provider 名。"""
        with self._lock:
            return [p.name for p in self._providers]

    def extract(
        self,
        image_path: Path,
        doc_type: DocType = DocType.OTHER,
    ) -> OCRResult:
        """提取图片文字。

        fallback 链:依次尝试 provider,首个可用的执行。
        全部失败 → 返回 confidence=0 的兜底结果。

        Args:
            image_path: 图片路径
            doc_type: 文档类型(决定字段提取策略)

        Returns:
            OCRResult
        """
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError("OCR service disabled (DEADMAN_MULTIMODAL_ENABLED=0)")

        if not Path(image_path).exists():
            return OCRResult(
                text="",
                confidence=0.0,
                doc_type=doc_type,
                fields={},
                provider="none",
            )

        with self._lock:
            providers = list(self._providers)

        last_error: Exception | None = None
        for provider in providers:
            try:
                if not provider.is_available():
                    logger.debug("OCR provider %s not available, skip", provider.name)
                    continue
                result = provider.extract(image_path, doc_type)
                logger.info(
                    "OCR extracted via %s (confidence=%.2f, doc_type=%s)",
                    provider.name, result.confidence, doc_type.value,
                )
                return result
            except Exception as e:
                last_error = e
                logger.warning("OCR provider %s failed: %s", provider.name, e)
                continue

        # 全部失败 → 兜底
        logger.error("All OCR providers failed, last_error=%s", last_error)
        return OCRResult(
            text="",
            confidence=0.0,
            doc_type=doc_type,
            fields={},
            provider="failed",
        )


# 全局单例
_ocr_instance: OCRService | None = None
_ocr_lock = threading.Lock()


def get_ocr_service() -> OCRService:
    """获取全局 OCRService 单例。"""
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_lock:
            if _ocr_instance is None:
                _ocr_instance = OCRService()
    return _ocr_instance
