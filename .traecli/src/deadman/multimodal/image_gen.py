"""Image Generator 服务 - 图片生成(讣告 / 纪念卡片)。

设计:
    - ImageStyle: 风格枚举(memorial_card / obituary / portrait / condolence_card)
    - StylePreset: 风格预设(色调 / 构图 / 提示词模板)
    - ImageGenProvider: provider 抽象基类
    - 各 provider(dall-e / stable-diffusion / midjourney)可选,懒加载
    - ImageGenerator: provider fallback 链 cloud → sd → mock

风格预设(deadman 场景关键):
    - memorial_card: 柔和色调(米白 / 淡灰 / 浅金),庄重克制,避免鲜艳色彩
    - obituary: 正式讣告风格(白底黑字 + 简洁边框)
    - portrait: 人物肖像(暖色调,写实风格,体现逝者生前样貌)
    - condolence_card: 吊唁卡(深色调 + 莲花 / 十字 / 烛光等宗教元素)

合规关联:
    - 不得生成含真人肖像(逝者肖像由用户上传,本服务仅生成场景化图片)
    - 不得包含特定宗教符号(除非用户明确指定信仰)
    - 默认风格走"世俗庄重",不预设宗教色彩

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


class ImageStyle(str, Enum):
    """图片风格枚举。"""

    MEMORIAL_CARD = "memorial_card"     # 纪念卡片
    OBITUARY = "obituary"               # 讣告
    PORTRAIT = "portrait"               # 人物肖像
    CONDOLENCE_CARD = "condolence_card"  # 吊唁卡


class ImageSize(str, Enum):
    """图片尺寸(主流 provider 通用)。"""

    SQUARE_512 = "512x512"
    SQUARE_1024 = "1024x1024"
    LANDSCAPE_1792 = "1792x1024"
    PORTRAIT_1024 = "1024x1792"


# 风格预设 - 死亡场景敏感,色彩 / 构图必须庄重
STYLE_PRESETS: dict[ImageStyle, dict[str, Any]] = {
    ImageStyle.MEMORIAL_CARD: {
        "color_palette": ("cream", "light_gray", "pale_gold"),
        "tone": "muted, respectful, restrained",
        "prompt_template": (
            "A memorial card in muted cream and pale gold tones, "
            "minimalist composition, soft lighting, respectful and "
            "restrained aesthetic, no text"
        ),
        "negative_prompt": "bright colors, festive, cartoon, text",
    },
    ImageStyle.OBITUARY: {
        "color_palette": ("white", "black", "gray"),
        "tone": "formal, somber, dignified",
        "prompt_template": (
            "An obituary notice in formal black and white, "
            "elegant serif border, dignified composition, no text"
        ),
        "negative_prompt": "colorful, casual, decorative flourishes",
    },
    ImageStyle.PORTRAIT: {
        "color_palette": ("warm_beige", "soft_brown"),
        "tone": "warm, realistic, dignified",
        "prompt_template": (
            "A dignified portrait in warm beige tones, "
            "realistic style, soft focus background, "
            "respectful and serene mood"
        ),
        "negative_prompt": "cartoon, exaggerated features, festive colors",
    },
    ImageStyle.CONDOLENCE_CARD: {
        "color_palette": ("deep_navy", "soft_white", "candle_glow"),
        "tone": "somber, comforting, candle-lit",
        "prompt_template": (
            "A condolence card in deep navy with a soft candle glow, "
            "minimalist, comforting and serene, no religious symbols "
            "unless specified, no text"
        ),
        "negative_prompt": "bright colors, festive, specific religious symbols",
    },
}


# =====================================================================
# Provider 抽象 + 实现
# =====================================================================


class ImageGenProvider:
    """Image generation provider 基类。"""

    name: str = "base"

    def is_available(self) -> bool:
        return False

    def generate(self, prompt: str, style: ImageStyle, size: ImageSize) -> bytes:
        raise NotImplementedError


class DallEProvider(ImageGenProvider):
    """OpenAI DALL-E 3 provider。

    通过 openai 库调用。库未安装 / 无 key 时不可用。
    """

    name = "dall-e"

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

    def generate(self, prompt: str, style: ImageStyle, size: ImageSize) -> bytes:
        import openai  # type: ignore

        preset = STYLE_PRESETS[style]
        full_prompt = f"{preset['prompt_template']}. User intent: {prompt}"
        client = openai.OpenAI(api_key=self.api_key)
        resp = client.images.generate(
            model="dall-e-3",
            prompt=full_prompt,
            size=size.value,
            n=1,
        )
        # 实际应通过 url 下载,这里简化直接返回 b64
        import base64
        b64 = resp.data[0].b64_json
        return base64.b64decode(b64) if b64 else b""


class StableDiffusionProvider(ImageGenProvider):
    """Stable Diffusion provider(本地 / 自托管)。

    通过 diffusers / stability-sdk 调用。库未安装时不可用。
    """

    name = "stable-diffusion"

    def __init__(self, model_path: Optional[str] = None, api_key: Optional[str] = None) -> None:
        self.model_path = model_path
        self.api_key = api_key
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import diffusers  # type: ignore  # noqa: F401
            self._available = True
        except Exception as e:
            logger.debug("diffusers not available: %s", e)
            self._available = False
        return self._available

    def generate(self, prompt: str, style: ImageStyle, size: ImageSize) -> bytes:
        import io

        import torch  # type: ignore
        from diffusers import StableDiffusionPipeline  # type: ignore

        preset = STYLE_PRESETS[style]
        pipe = StableDiffusionPipeline.from_pretrained(
            self.model_path or "stabilityai/stable-diffusion-2-1",
            torch_dtype=torch.float16,
        )
        full_prompt = f"{preset['prompt_template']}. {prompt}. Negative: {preset['negative_prompt']}"
        image = pipe(full_prompt, num_inference_steps=20).images[0]
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()


class MidjourneyProvider(ImageGenProvider):
    """Midjourney provider(通过第三方 API)。

    通过 midjourney-python 等第三方库调用。库未安装时不可用。
    """

    name = "midjourney"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import midjourney  # type: ignore  # noqa: F401
            self._available = bool(self.api_key)
        except Exception as e:
            logger.debug("midjourney not available: %s", e)
            self._available = False
        return self._available

    def generate(self, prompt: str, style: ImageStyle, size: ImageSize) -> bytes:
        import midjourney  # type: ignore

        preset = STYLE_PRESETS[style]
        client = midjourney.Client(api_key=self.api_key)
        full_prompt = f"{preset['prompt_template']} --ar {size.value}"
        result = client.imagine(full_prompt)
        return result.image_bytes


class MockImageGenProvider(ImageGenProvider):
    """Mock Image Gen provider - 总是可用,返回占位 PNG。"""

    name = "mock"

    # 8x8 PNG(透明)的最小字节序列
    _PLACEHOLDER_PNG: bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08"
        b"\x00\x00\x00\x08\x08\x06\x00\x00\x00\xc4\x0f\xbe\x8b"
        b"\x00\x00\x00\x1aIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03"
        b"\x00\x01\x5d\xcc\xdb\xd2\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def is_available(self) -> bool:
        return True

    def generate(self, prompt: str, style: ImageStyle, size: ImageSize) -> bytes:
        preset = STYLE_PRESETS[style]
        # 用 prompt + style 元信息生成一段可识别的占位字节
        meta = f"MOCK-IMG|style={style.value}|size={size.value}|palette={preset['color_palette']}".encode("utf-8")
        return self._PLACEHOLDER_PNG + b"\n--META--\n" + meta


# =====================================================================
# ImageGenerator
# =====================================================================


class ImageGenerator:
    """图片生成器(provider fallback 链)。

    用法:
        gen = ImageGenerator()
        if gen.is_enabled():
            img_bytes = gen.generate("怀念父亲", ImageStyle.MEMORIAL_CARD, ImageSize.SQUARE_1024)
            with open("card.png", "wb") as f:
                f.write(img_bytes)
    """

    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        sd_model_path: Optional[str] = None,
        midjourney_api_key: Optional[str] = None,
        custom_providers: Optional[list[ImageGenProvider]] = None,
    ) -> None:
        self._lock = threading.RLock()
        if custom_providers is not None:
            self._providers: list[ImageGenProvider] = list(custom_providers)
        else:
            self._providers = [
                DallEProvider(api_key=openai_api_key),
                StableDiffusionProvider(model_path=sd_model_path),
                MidjourneyProvider(api_key=midjourney_api_key),
                MockImageGenProvider(),
            ]

    def is_enabled(self) -> bool:
        return is_enabled("multimodal")

    def register_provider(self, provider: ImageGenProvider, position: Optional[int] = None) -> None:
        with self._lock:
            if position is None:
                self._providers.append(provider)
            else:
                self._providers.insert(position, provider)

    def list_providers(self) -> list[str]:
        with self._lock:
            return [p.name for p in self._providers]

    def get_style_preset(self, style: ImageStyle) -> dict[str, Any]:
        """获取风格预设(供 UI 展示 / 调试)。"""
        return STYLE_PRESETS[style]

    def generate(
        self,
        prompt: str,
        style: ImageStyle = ImageStyle.MEMORIAL_CARD,
        size: ImageSize = ImageSize.SQUARE_1024,
    ) -> bytes:
        """生成图片。

        Args:
            prompt: 用户描述意图(如"怀念父亲,他喜欢读书")
            style: 图片风格(决定色调 / 构图)
            size: 输出尺寸

        Returns:
            图片二进制(PNG/JPG)
        """
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError(
                "ImageGen service disabled (DEADMAN_MULTIMODAL_ENABLED=0)"
            )

        if not prompt:
            prompt = "memorial"

        with self._lock:
            providers = list(self._providers)

        last_error: Optional[Exception] = None
        for provider in providers:
            try:
                if not provider.is_available():
                    continue
                img_bytes = provider.generate(prompt, style, size)
                logger.info(
                    "ImageGen generated via %s (style=%s, size=%s, bytes=%d)",
                    provider.name, style.value, size.value, len(img_bytes),
                )
                return img_bytes
            except Exception as e:
                last_error = e
                logger.warning("ImageGen provider %s failed: %s", provider.name, e)
                continue

        logger.error("All ImageGen providers failed, last_error=%s", last_error)
        return b""


# 全局单例
_imgen_instance: Optional[ImageGenerator] = None
_imgen_lock = threading.Lock()


def get_image_generator() -> ImageGenerator:
    """获取全局 ImageGenerator 单例。"""
    global _imgen_instance
    if _imgen_instance is None:
        with _imgen_lock:
            if _imgen_instance is None:
                _imgen_instance = ImageGenerator()
    return _imgen_instance
