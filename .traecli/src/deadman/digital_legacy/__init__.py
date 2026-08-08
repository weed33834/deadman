"""数字遗产清单模块

提供结构化的数字资产登记、继承人指派与移交 / 注销方案生成，
对标 Cipherwill / BeyondLife / GoodTrust 等竞品的核心能力。
"""

from .generator import (
    build_checklist,
    generate_plan_llm,
    render_plan_markdown,
)
from .models import (
    CATEGORY_GUIDANCE,
    AssetAction,
    AssetCategory,
    AssetRegister,
    DigitalAsset,
    Heir,
    Sensitivity,
)
from .store import DigitalLegacyStore

__all__ = [
    "AssetAction",
    "AssetCategory",
    "AssetRegister",
    "CATEGORY_GUIDANCE",
    "DigitalAsset",
    "Heir",
    "Sensitivity",
    "DigitalLegacyStore",
    "build_checklist",
    "generate_plan_llm",
    "render_plan_markdown",
]
