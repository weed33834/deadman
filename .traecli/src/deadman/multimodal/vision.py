"""Vision 服务 - 图片理解(GPT-4V / Claude Vision / Qwen-VL)。

设计:
    - VisionProvider: provider 抽象基类
    - 各 provider(gpt-4o / claude-3.5-sonnet / qwen-vl)可选,懒加载
    - VisionService:
        - describe(image, prompt): 自然语言描述
        - extract_objects(image): 抽取物体清单

业务场景(deadman):
    - 用户上传逝者照片 → memorial description(温馨怀念的描述,不评判外貌)
    - 上传文档图片 → 自动分类(身份证 / 死亡证明 / 遗嘱 / 其他)
    - 上传葬礼现场照片 → 提取场景元素(花圈 / 遗像 / 致辞台)

降级策略:
    - 所有 cloud provider 不可用 → 走 mock(返回占位描述)
    - 业务层可基于 mock 文本继续流程,不阻塞

feature flag:`DEADMAN_MULTIMODAL_ENABLED=0`(默认 OFF)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from .ocr import DocType

logger = logging.getLogger(__name__)


@dataclass
class VisionDescription:
    """图片描述结果。"""

    text: str
    doc_type: DocType | None = None  # 自动分类的文档类型
    confidence: float = 0.0
    provider: str = "unknown"


@dataclass
class DetectedObject:
    """检测到的单个物体。"""

    label: str
    confidence: float
    bbox: tuple[float, float, float, float] | None = None  # (x1, y1, x2, y2) 归一化坐标

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "confidence": self.confidence,
            "bbox": list(self.bbox) if self.bbox else None,
        }


# =====================================================================
# Provider 抽象 + 实现
# =====================================================================


class VisionProvider:
    """Vision provider 基类。"""

    name: str = "base"

    def is_available(self) -> bool:
        return False

    def describe(self, image_path: Path, prompt: str) -> VisionDescription:
        raise NotImplementedError

    def extract_objects(self, image_path: Path) -> list[DetectedObject]:
        raise NotImplementedError


class GPT4VisionProvider(VisionProvider):
    """OpenAI GPT-4o Vision provider。

    通过 openai 库调用。库未安装 / 无 key 时不可用。
    """

    name = "gpt-4o"

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
            logger.debug("openai not available: %s", e)
            self._available = False
        return self._available

    def describe(self, image_path: Path, prompt: str) -> VisionDescription:
        import base64

        import openai  # type: ignore

        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        client = openai.OpenAI(api_key=self.api_key)
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        )
        text = resp.choices[0].message.content or ""
        return VisionDescription(text=text, confidence=0.9, provider=self.name)

    def extract_objects(self, image_path: Path) -> list[DetectedObject]:
        desc = self.describe(image_path, "List all visible objects as a JSON list.")
        # 简化:不解析 JSON,返回单条
        return [DetectedObject(label=desc.text, confidence=desc.confidence)]


class ClaudeVisionProvider(VisionProvider):
    """Anthropic Claude 3.5 Sonnet Vision provider。

    通过 anthropic 库调用。库未安装 / 无 key 时不可用。
    """

    name = "claude-3.5-sonnet"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import anthropic  # type: ignore
            self._available = bool(self.api_key)
        except Exception as e:
            logger.debug("anthropic not available: %s", e)
            self._available = False
        return self._available

    def describe(self, image_path: Path, prompt: str) -> VisionDescription:
        import base64

        import anthropic  # type: ignore

        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        client = anthropic.Anthropic(api_key=self.api_key)
        resp = client.messages.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": b64,
                    }},
                ],
            }],
        )
        text = resp.content[0].text if resp.content else ""
        return VisionDescription(text=text, confidence=0.9, provider=self.name)

    def extract_objects(self, image_path: Path) -> list[DetectedObject]:
        desc = self.describe(image_path, "List visible objects as JSON list.")
        return [DetectedObject(label=desc.text, confidence=desc.confidence)]


class QwenVLProvider(VisionProvider):
    """阿里 Qwen-VL provider。

    通过 dashscope 库调用。库未安装 / 无 key 时不可用。
    """

    name = "qwen-vl"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import dashscope  # type: ignore
            self._available = bool(self.api_key)
        except Exception as e:
            logger.debug("dashscope not available: %s", e)
            self._available = False
        return self._available

    def describe(self, image_path: Path, prompt: str) -> VisionDescription:
        import dashscope  # type: ignore

        resp = dashscope.MultiModalConversation.call(
            api_key=self.api_key,
            model="qwen-vl-max",
            messages=[{
                "role": "user",
                "content": [
                    {"image": str(image_path)},
                    {"text": prompt},
                ],
            }],
        )
        text = ""
        if resp.status_code == 200 and resp.output.choices:
            text = resp.output.choices[0].message.content[0].get("text", "")
        return VisionDescription(text=text, confidence=0.85, provider=self.name)

    def extract_objects(self, image_path: Path) -> list[DetectedObject]:
        desc = self.describe(image_path, "List visible objects as JSON.")
        return [DetectedObject(label=desc.text, confidence=desc.confidence)]


class MockVisionProvider(VisionProvider):
    """Mock Vision provider - 总是可用,返回占位描述。

    deadman 场景:对照片返回温和的纪念描述模板,对文档返回类型分类。
    """

    name = "mock"

    def is_available(self) -> bool:
        return True

    def describe(self, image_path: Path, prompt: str) -> VisionDescription:
        # 简单启发式:基于 prompt 关键词判断意图
        prompt_lower = prompt.lower()
        if any(kw in prompt_lower for kw in ("memorial", "纪念", "悼", "逝者")):
            text = "这是一张珍贵的纪念照片,记录了逝者生前的温暖时刻,神情安详,值得珍藏。"
            return VisionDescription(text=text, confidence=0.7, provider=self.name)
        if any(kw in prompt_lower for kw in ("classify", "分类", "type")):
            return VisionDescription(
                text="document",
                doc_type=DocType.OTHER,
                confidence=0.7,
                provider=self.name,
            )
        text = "图片描述(占位):画面包含若干物体,色调平和。"
        return VisionDescription(text=text, confidence=0.6, provider=self.name)

    def extract_objects(self, image_path: Path) -> list[DetectedObject]:
        return [
            DetectedObject(label="person", confidence=0.85),
            DetectedObject(label="background", confidence=0.7),
        ]


# =====================================================================
# VisionService
# =====================================================================


class VisionService:
    """Vision 服务 - 图片理解(provider fallback 链)。

    用法:
        svc = VisionService()
        if svc.is_enabled():
            desc = svc.describe(Path("photo.jpg"), "用温暖语言描述逝者")
            objs = svc.extract_objects(Path("photo.jpg"))
    """

    def __init__(
        self,
        openai_api_key: str | None = None,
        anthropic_api_key: str | None = None,
        dashscope_api_key: str | None = None,
        custom_providers: list[VisionProvider] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        if custom_providers is not None:
            self._providers: list[VisionProvider] = list(custom_providers)
        else:
            self._providers = [
                GPT4VisionProvider(api_key=openai_api_key),
                ClaudeVisionProvider(api_key=anthropic_api_key),
                QwenVLProvider(api_key=dashscope_api_key),
                MockVisionProvider(),
            ]

    def is_enabled(self) -> bool:
        return is_enabled("multimodal")

    def register_provider(self, provider: VisionProvider, position: int | None = None) -> None:
        with self._lock:
            if position is None:
                self._providers.append(provider)
            else:
                self._providers.insert(position, provider)

    def list_providers(self) -> list[str]:
        with self._lock:
            return [p.name for p in self._providers]

    def describe(self, image_path: Path, prompt: str = "描述这张图片") -> str:
        """自然语言描述图片。

        Args:
            image_path: 图片路径
            prompt: 描述指令(如"用温暖语言描述逝者")

        Returns:
            描述文本
        """
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError("Vision service disabled (DEADMAN_MULTIMODAL_ENABLED=0)")

        if not Path(image_path).exists():
            return ""

        with self._lock:
            providers = list(self._providers)

        last_error: Exception | None = None
        for provider in providers:
            try:
                if not provider.is_available():
                    continue
                desc = provider.describe(image_path, prompt)
                logger.info(
                    "Vision describe via %s (confidence=%.2f)",
                    provider.name, desc.confidence,
                )
                return desc.text
            except Exception as e:
                last_error = e
                logger.warning("Vision provider %s failed: %s", provider.name, e)
                continue

        logger.error("All vision providers failed, last_error=%s", last_error)
        return ""

    def extract_objects(self, image_path: Path) -> list[dict[str, Any]]:
        """抽取图片中的物体清单。

        Args:
            image_path: 图片路径

        Returns:
            list of dict: [{label, confidence, bbox}, ...]
        """
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError("Vision service disabled (DEADMAN_MULTIMODAL_ENABLED=0)")

        if not Path(image_path).exists():
            return []

        with self._lock:
            providers = list(self._providers)

        last_error: Exception | None = None
        for provider in providers:
            try:
                if not provider.is_available():
                    continue
                objs = provider.extract_objects(image_path)
                logger.info(
                    "Vision extract_objects via %s (count=%d)",
                    provider.name, len(objs),
                )
                return [o.to_dict() for o in objs]
            except Exception as e:
                last_error = e
                logger.warning("Vision provider %s failed: %s", provider.name, e)
                continue

        logger.error("All vision providers failed, last_error=%s", last_error)
        return []


# 全局单例
_vision_instance: VisionService | None = None
_vision_lock = threading.Lock()


def get_vision_service() -> VisionService:
    """获取全局 VisionService 单例。"""
    global _vision_instance
    if _vision_instance is None:
        with _vision_lock:
            if _vision_instance is None:
                _vision_instance = VisionService()
    return _vision_instance
