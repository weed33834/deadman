"""数字遗产清单 - 数据模型

参考竞品：
    - Cipherwill：加密死人开关 + 数字资产转移
    - BeyondLife：连接 Google Drive / Twitter 等，管理数字遗产
    - GoodTrust / My-Legacy.ai：数字资产清点与继承人指派

设计要点：
    - 与 vault（密钥保险库）互补：vault 存「秘密本身」，本模块存「资产清单与移交指引」。
    - access_hint（访问/恢复方式）属敏感字段，落盘时由 store 加密，不存明文。
    - 严守数据纪律：绝不编造具体金额、电话、深链 URL；
      金额/估值仅由用户自填，指引用通用步骤 + 引导查阅官方帮助中心。
    - 遵守 rules/legal-compliance-framework.md（PIPL：去标识化、最小必要）
      与 rules/safety-protocol.md（涉及自杀/非正常死亡触发时延）。

类别与处置动作常量供 generator / tools 共用。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# =====================================================================
# 枚举常量
# =====================================================================
class AssetCategory(str, Enum):
    SOCIAL = "social"  # 社交 / 内容账号
    FINANCIAL = "financial"  # 银行 / 支付 / 证券
    CRYPTO = "crypto"  # 加密资产 / 钱包
    ACCOUNT = "account"  # 通用账号 / 云盘 / 会员
    DOCUMENT = "document"  # 证件 / 合同扫描件
    DEVICE = "device"  # 手机 / 电脑 / 硬件密钥
    SUBSCRIPTION = "subscription"  # 订阅服务
    OTHER = "other"


class AssetAction(str, Enum):
    TRANSFER = "transfer"  # 转移给继承人
    CLOSE = "close"  # 注销 / 关闭
    MEMORIALIZE = "memorialize"  # 纪念化（转为纪念账号）
    KEEP = "keep"  # 保留（如加密资产继续持有）
    DECIDE = "decide"  # 待定


class Sensitivity(str, Enum):
    PUBLIC = "public"  # 可公开（如「我有一个微博账号」）
    INTERNAL = "internal"  # 仅继承人可见
    CONFIDENTIAL = "confidential"  # 敏感（账号、估值）
    SECRET = "secret"  # 高度敏感（访问/恢复方式）


# 各类别通用处置指引（规则驱动，无 LLM 依赖；不编造深链）
CATEGORY_GUIDANCE: dict[str, str] = {
    AssetCategory.SOCIAL.value: (
        "社交 / 内容账号：可指定「纪念化」转为纪念账号，或注销。转移通常需身份验证与死亡证明，"
        "建议提前在平台设置遗产联系人并查阅其帮助中心继承流程。"
    ),
    AssetCategory.FINANCIAL.value: (
        "金融账户（银行 / 支付 / 证券）：继承需遗嘱 + 死亡证明 + 继承权公文书。"
        "余额由法定继承人依法继承；切勿共享登录密码，一律走官方继承流程。"
    ),
    AssetCategory.CRYPTO.value: (
        "加密资产 / 钱包：去中心化资产由私钥 / 助记词控制，平台无法重置。"
        "最稳妥做法是将助记词离线保管并提前告知指定继承人（或存入保险库）。"
    ),
    AssetCategory.ACCOUNT.value: (
        "通用账号 / 云盘 / 会员：可注销或转移；订阅类记得取消自动续费，避免持续扣费。"
    ),
    AssetCategory.DOCUMENT.value: (
        "证件 / 合同扫描件：妥善加密保管；身故后由继承人凭法律关系与证明依法调取。"
    ),
    AssetCategory.DEVICE.value: (
        "手机 / 电脑 / 硬件密钥：提前设置遗产联系人（如 Apple Legacy Contact、Google 遗嘱联系人），"
        "或离线记录解锁 / 恢复方式。"
    ),
    AssetCategory.SUBSCRIPTION.value: ("订阅服务：取消或转移，避免逝者账户持续扣费。"),
    AssetCategory.OTHER.value: ("其他数字资产：按性质决定移交或注销，必要时咨询专业人士。"),
}

_VALID_CATEGORIES = {c.value for c in AssetCategory}
_VALID_ACTIONS = {a.value for a in AssetAction}
_VALID_SENSITIVITY = {s.value for s in Sensitivity}


# =====================================================================
# 数据结构
# =====================================================================
@dataclass
class Heir:
    id: str
    name: str  # 化名 / 关系，如「长子」「配偶」
    relationship: str = ""  # 法律关系，如「子女」「配偶」
    contact_hint: str = ""  # 联系方式提示（不存明文电话，按数据纪律）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Heir:
        return cls(
            id=d["id"],
            name=d["name"],
            relationship=d.get("relationship", ""),
            contact_hint=d.get("contact_hint", ""),
        )


@dataclass
class DigitalAsset:
    id: str
    category: str
    name: str
    owner_hint: str = ""  # 登录账号 / 标识提示（不存明文密码）
    location: str = ""  # URL 或说明，如 https://weixin.qq.com
    access_hint: str = ""  # 访问 / 恢复方式（敏感，落盘加密）
    estimated_value: str | None = None  # 不编造金额；由用户自填或留空
    action_on_death: str = AssetAction.DECIDE.value
    assigned_heir_id: str | None = None
    sensitivity: str = Sensitivity.INTERNAL.value
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.category not in _VALID_CATEGORIES:
            self.category = AssetCategory.OTHER.value
        if self.action_on_death not in _VALID_ACTIONS:
            self.action_on_death = AssetAction.DECIDE.value
        if self.sensitivity not in _VALID_SENSITIVITY:
            self.sensitivity = Sensitivity.INTERNAL.value
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now

    @property
    def guidance(self) -> str:
        return CATEGORY_GUIDANCE.get(self.category, CATEGORY_GUIDANCE[AssetCategory.OTHER.value])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DigitalAsset:
        return cls(
            id=d["id"],
            category=d.get("category", AssetCategory.OTHER.value),
            name=d["name"],
            owner_hint=d.get("owner_hint", ""),
            location=d.get("location", ""),
            access_hint=d.get("access_hint", ""),
            estimated_value=d.get("estimated_value"),
            action_on_death=d.get("action_on_death", AssetAction.DECIDE.value),
            assigned_heir_id=d.get("assigned_heir_id"),
            sensitivity=d.get("sensitivity", Sensitivity.INTERNAL.value),
            notes=d.get("notes", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


@dataclass
class AssetRegister:
    """某用户的完整数字遗产清单（含继承人与资产）。"""

    user_id: str
    heirs: list[Heir] = field(default_factory=list)
    assets: list[DigitalAsset] = field(default_factory=list)
    updated_at: str = ""

    def heir_by_id(self, heir_id: str | None) -> Heir | None:
        if not heir_id:
            return None
        return next((h for h in self.heirs if h.id == heir_id), None)

    def summary(self) -> dict[str, int]:
        by_cat: dict[str, int] = {}
        for a in self.assets:
            by_cat[a.category] = by_cat.get(a.category, 0) + 1
        return {
            "total_assets": len(self.assets),
            "total_heirs": len(self.heirs),
            "by_category": by_cat,
            "unassigned": sum(1 for a in self.assets if not a.assigned_heir_id),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "heirs": [h.to_dict() for h in self.heirs],
            "assets": [a.to_dict() for a in self.assets],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AssetRegister:
        return cls(
            user_id=d["user_id"],
            heirs=[Heir.from_dict(h) for h in d.get("heirs", [])],
            assets=[DigitalAsset.from_dict(a) for a in d.get("assets", [])],
            updated_at=d.get("updated_at", ""),
        )
