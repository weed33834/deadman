"""PII 文本/字段脱敏统一实现。

原先 PII 掩码正则与替换逻辑在 doc_extract / notification_letters /
security.content_sandbox / memory 等多处重复实现，且规则略有漂移。
这里收敛为单一事实源：规则变更只改本文件，全项目生效。

- ``mask_text_pii``：文本级脱敏（身份证/手机号/银行卡/邮箱）。
- ``mask_text_pii_detected``：同脱敏，另返回是否检出 PII（沙箱/护栏用）。
- ``mask_value``：字段级脱敏（保留首尾 2 字符，中间 ``***``；过短全掩码）。

原则：脱敏后保留长度/首尾信息以辅助校对，但不还原完整号码。
"""

from __future__ import annotations

import re

__all__ = ["mask_text_pii", "mask_text_pii_detected", "mask_value"]

# 注意：Python3 中 \w 含 CJK，PII 紧贴中文时 \b 不生效（如「手机13800138000」）。
# 因此用「非数字」前后断言 (?<!\d)/(?!\d) 表示「独立号码段」，保证中文语境也可脱敏。
# 身份证 18 位：前 6 地区码 + 8 生日 + 3 序号 + 1 校验（末位放宽避免漏）
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
# 手机号 11 位（1 开头）
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
# 银行卡 16-19 位连续数字（独立段）
_BANK_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
# 邮箱：前 1 字符 + *** + @域名（用非邮箱字符前断言，兼容紧贴中文）
_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?!\S)")


def _mask_bank(m: re.Match) -> str:
    digits = m.group(0)
    return f"{digits[:4]}{'*' * (len(digits) - 8)}{digits[-4:]}"


def mask_text_pii(text: str) -> str:
    """文本级 PII 脱敏：身份证/手机号/银行卡/邮箱。"""
    if not text:
        return text
    text = _ID_CARD_RE.sub(lambda m: f"{m.group(1)}********{m.group(2)}", text)
    text = _PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", text)
    text = _BANK_RE.sub(_mask_bank, text)
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    return text


def mask_text_pii_detected(text: str) -> tuple[str, bool]:
    """文本级 PII 脱敏；返回 ``(脱敏后文本, 是否检出 PII)``（护栏用）。"""
    if not text:
        return text, False
    if not (_ID_CARD_RE.search(text) or _PHONE_RE.search(text)
            or _BANK_RE.search(text) or _EMAIL_RE.search(text)):
        return text, False
    return mask_text_pii(text), True


def mask_value(value: str) -> str:
    """字段级掩码：保留首尾 2 字符，中间 ``***``；过短则全部 ``***``。"""
    if value is None:
        return ""
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:2]}***{s[-2:]}"
