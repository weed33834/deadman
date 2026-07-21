"""Phase 15: 通知信函数据模型

LetterRequest：信函生成请求
LetterResult：信函生成结果

合规关联：
    - rules/legal-compliance-framework.md 第五章 PIPL：
        decedent_id_masked 字段必须是已脱敏的身份证号
        （调用方负责脱敏，生成器不再处理明文）
    - rules/integrity-framework.md：
        placeholders 列出所有未填的占位符，confidence 标注生成可信度
    - rules/service-boundary-framework.md：
        disclaimer 字段固定为"信函仅为草稿"边界告知
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ====================================================================
# 8 类信函类型常量
# ====================================================================
LETTER_TYPE_HOUSEHOLD_CANCELLATION = "household_cancellation"
LETTER_TYPE_SOCIAL_SECURITY_BENEFIT = "social_security_benefit"
LETTER_TYPE_PROVIDENT_FUND_WITHDRAWAL = "provident_fund_withdrawal"
LETTER_TYPE_MEDICAL_INSURANCE_CANCELLATION = "medical_insurance_cancellation"
LETTER_TYPE_BANK_ACCOUNT_INHERITANCE = "bank_account_inheritance"
LETTER_TYPE_PROPERTY_INHERITANCE_NOTARIZATION = (
    "property_inheritance_notarization"
)
LETTER_TYPE_CREDIT_CARD_CANCELLATION = "credit_card_cancellation"
LETTER_TYPE_INTERNET_ACCOUNT_CANCELLATION = "internet_account_cancellation"


# ====================================================================
# 默认 disclaimer（service-boundary-framework.md 第三章）
# ====================================================================
DEFAULT_DISCLAIMER = (
    "信函仅为草稿，具体格式请以办理机构要求为准；"
    "占位符 [xxx] 需手动填写。"
)


# ====================================================================
# LetterRequest
# ====================================================================
@dataclass
class LetterRequest:
    """信函生成请求

    通用字段（所有信函类型都需要）：
        letter_type: 8 类信函类型之一（见上常量）
        decedent_name: 逝者姓名（调用方负责脱敏，如"张**"或真实姓名由用户决定）
        decedent_id_masked: 已脱敏的身份证号（如 "110101********1234"）
                            由调用方脱敏，本字段不再额外处理
        death_date: 死亡日期（ISO 字符串 YYYY-MM-DD）
        applicant_name: 申请人姓名（脱敏后或真实，由用户决定）
        applicant_relationship: 申请人与逝者关系（配偶/子女/父母/兄弟姐妹/其他）
        recipient_org: 收件机构（如"户籍所在地派出所"/"参保地社保局"等）
        extra_fields: dict，类型特定字段（账号/地址/银行/平台名等）
        language: 语言代码（zh-CN/en-US），默认 zh-CN
    """

    letter_type: str
    decedent_name: str
    decedent_id_masked: str
    death_date: str
    applicant_name: str
    applicant_relationship: str
    recipient_org: str
    extra_fields: dict[str, Any] = field(default_factory=dict)
    language: str = "zh-CN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "letter_type": self.letter_type,
            "decedent_name": self.decedent_name,
            "decedent_id_masked": self.decedent_id_masked,
            "death_date": self.death_date,
            "applicant_name": self.applicant_name,
            "applicant_relationship": self.applicant_relationship,
            "recipient_org": self.recipient_org,
            "extra_fields": dict(self.extra_fields),
            "language": self.language,
        }


# ====================================================================
# LetterResult
# ====================================================================
@dataclass
class LetterResult:
    """信函生成结果

    text: 信函正文（含已填字段 + 占位符 [xxx]）
    letter_type: 信函类型（与 LetterRequest 一致）
    confidence: 生成可信度（0-1）
                - 0.3：纯模板填充 + LLM 不可用
                - 0.7：纯模板填充
                - 0.9：模板填充 + LLM 语气优化
    placeholders: 未填写的占位符列表（如 ["[户籍所在地派出所名称]"]）
                  用户需手动填写这些字段
    disclaimer: 边界告知（service-boundary-framework.md 第三章）
    """

    text: str
    letter_type: str
    confidence: float
    placeholders: list[str] = field(default_factory=list)
    disclaimer: str = DEFAULT_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "letter_type": self.letter_type,
            "confidence": self.confidence,
            "placeholders": list(self.placeholders),
            "disclaimer": self.disclaimer,
        }
