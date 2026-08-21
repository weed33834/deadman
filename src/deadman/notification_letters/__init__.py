"""Phase 15: 通知信函生成器（notification_letters）

中国本土化版 8 类通知信函草稿生成器。

参考 Lantern（美国市场）的 8 类通知信函模板化生成器，本土化为：
    1. household_cancellation        户口注销通知
    2. social_security_benefit       社保丧葬费申领
    3. provident_fund_withdrawal     公积金提取申请
    4. medical_insurance_cancellation 医保账户注销
    5. bank_account_inheritance      银行账户解冻/继承
    6. property_inheritance_notarization 房产继承公证申请
    7. credit_card_cancellation      信用卡销户
    8. internet_account_cancellation 互联网账号注销

合规约束：
    - rules/legal-compliance-framework.md 第五章 PIPL：
        LetterRequest.decedent_id_masked 必须为脱敏后的身份证号
        （如 110101********1234），生成器不再额外处理身份证号明文
    - rules/service-boundary-framework.md：
        仅生成草稿，不代办；附 disclaimer
    - rules/integrity-framework.md：
        不编造官方电话/地址；占位符统一格式 [xxx] 留给用户填写
    - LLM 不可用时降级为纯模板填充，confidence=0.3
"""

from __future__ import annotations

from .generator import LetterGenerator
from .models import LetterRequest, LetterResult
from .templates import LETTER_TEMPLATES, LETTER_TYPES

__all__ = [
    "LETTER_TEMPLATES",
    "LETTER_TYPES",
    "LetterGenerator",
    "LetterRequest",
    "LetterResult",
]
