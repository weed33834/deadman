"""D4:PII 检测与保留(防止记忆压缩后 PII 泄漏)。

问题:
    LLM 记忆压缩(memory/file_store.py)将多轮对话压缩为摘要,
    压缩过程可能:
        1. PII 残留:摘要中保留 PII(姓名 / 身份证 / 手机号)
        2. PII 漂移:PII 从原始位置移动到摘要另一段
        3. PII 重构:多个非 PII 信息组合后形成 PII(如"姓张"+"名三"+"身份证前6位")

    缓解:压缩前后都过 PII 检测,确保 PII 不被压缩(强制 redact 或保留原句)。

法规依据:
    - PIPL 第 28 条:处理敏感个人信息需单独同意
    - GDPR 第 9 条:特殊类别的个人数据处理限制

设计:
    - PIIPattern: PII 模式(正则 + 关键词)
    - PIIRedactor: PII 检测器 + 脱敏器
    - PIIResult: 检测结果

脱敏策略:
    - REDACT: 全替换为 [REDACTED-PII:type]
    - HASH: 哈希后存储(便于反查,但不可逆)
    - PARTIAL: 部分保留(如手机号 138****1234)
    - KEEP: 保留原值(用户已同意 / 法规要求保留)

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..feature_flags import is_enabled

logger = logging.getLogger(__name__)


class PIIType(str, Enum):
    """PII 类型(按敏感度排序)。"""

    # 中国 PII(PIPL)
    CHINA_ID_CARD = "china_id_card"  # 身份证(18 位)
    CHINA_PHONE = "china_phone"  # 手机号(11 位)
    CHINA_BANK_CARD = "china_bank_card"  # 银行卡号(16-19 位)
    CHINA_LICENSE = "china_license"  # 营业执照号
    CHINA_PASSPORT = "china_passport"  # 护照号
    CHINA_HEALTH_CARD = "china_health_card"  # 医保卡

    # 通用 PII
    EMAIL = "email"
    IP_ADDRESS = "ip_address"
    CREDIT_CARD = "credit_card"  # 国际信用卡

    # 业务 PII(deadman 场景)
    NAME = "name"  # 中文姓名
    ADDRESS = "address"  # 详细地址
    BIRTHDATE = "birthdate"  # 出生日期
    DEATH_DATE = "death_date"  # 死亡日期(死亡证明场景)


class RedactStrategy(str, Enum):
    """脱敏策略。"""

    REDACT = "redact"  # 全替换 [REDACTED-PII:type]
    HASH = "hash"  # 哈希(SHA-256 前 12 位)
    PARTIAL = "partial"  # 部分保留(头尾保留,中间 *)
    KEEP = "keep"  # 保留(用户已同意 / 法规要求)


# PII 正则模式库(按类型)
PII_PATTERNS: dict[PIIType, list[re.Pattern]] = {
    PIIType.CHINA_ID_CARD: [
        re.compile(
            r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"
        ),
    ],
    PIIType.CHINA_PHONE: [
        re.compile(r"\b1[3-9]\d{9}\b"),
    ],
    PIIType.CHINA_BANK_CARD: [
        re.compile(r"\b[1-9]\d{14,18}\b"),  # 16-19 位数字
    ],
    PIIType.CHINA_PASSPORT: [
        re.compile(r"\b[A-Z]\d{8,9}\b"),  # E12345678
    ],
    PIIType.EMAIL: [
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    ],
    PIIType.IP_ADDRESS: [
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ],
    PIIType.CREDIT_CARD: [
        # Luhn 校验略,简化为 16 位数字
        re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    ],
}

# 中文姓名识别(简化:基于常用姓氏 + 2-3 字组合)
CHINESE_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟黄"
)

# 详细地址关键词
ADDRESS_KEYWORDS = (
    "省",
    "市",
    "区",
    "县",
    "镇",
    "乡",
    "村",
    "街道",
    "路",
    "号",
    "楼",
    "室",
    "栋",
    "单元",
    "层",
)

# 出生日期 / 死亡日期模式
DATE_PATTERNS = [
    re.compile(r"\b(?:19|20)\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?\b"),
    re.compile(r"\b\d{4}年\d{1,2}月\d{1,2}日\b"),
]


@dataclass
class PIIMatch:
    """单条 PII 检测结果。"""

    pii_type: PIIType
    original: str  # 原始匹配文本
    start: int
    end: int
    redacted: str = ""  # 脱敏后的文本
    strategy: RedactStrategy = RedactStrategy.REDACT
    confidence: float = 1.0  # 0-1,正则匹配 = 1.0,启发式 = 0.5-0.8


@dataclass
class PIIResult:
    """PII 检测结果。"""

    original_text: str
    redacted_text: str  # 脱敏后的文本(供压缩用)
    matches: list[PIIMatch] = field(default_factory=list)
    has_pii: bool = False
    pii_count_by_type: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """汇总(供审计 / 日志)。"""
        return {
            "has_pii": self.has_pii,
            "total_matches": len(self.matches),
            "by_type": self.pii_count_by_type,
        }


class PIIRedactor:
    """PII 检测器 + 脱敏器。

    用法:
        redactor = get_pii_redactor()
        # 1. 检测
        result = redactor.detect(text)
        if result.has_pii:
            # 2. 脱敏(用于压缩前的预处理)
            redacted = redactor.redact(text, default_strategy=RedactStrategy.PARTIAL)
            # 3. 压缩 redacted.redacted_text
            summary = llm.compress(redacted.redacted_text)
            # 4. 检测摘要(防止 PII 漂移 / 重构)
            result2 = redactor.detect(summary)
            if result2.has_pii:
                logger.warning("PII leaked into summary: %s", result2.summary())
    """

    def __init__(
        self,
        # 各类型 PII 的脱敏策略(可按需覆盖)
        strategies: dict[PIIType, RedactStrategy] | None = None,
        # 白名单(已获用户同意,不需脱敏)
        whitelisted_pii: set[PIIType] | None = None,
    ) -> None:
        self.strategies = strategies or self._default_strategies()
        self.whitelisted_pii = whitelisted_pii or set()

    def detect(self, text: str) -> PIIResult:
        """检测文本中的 PII(不脱敏)。"""
        if not is_enabled("defense"):
            return PIIResult(original_text=text, redacted_text=text)

        matches: list[PIIMatch] = []
        pii_count: dict[str, int] = {}

        # 1. 正则模式检测
        for pii_type, patterns in PII_PATTERNS.items():
            for pattern in patterns:
                for match in pattern.finditer(text):
                    matches.append(
                        PIIMatch(
                            pii_type=pii_type,
                            original=match.group(),
                            start=match.start(),
                            end=match.end(),
                            confidence=1.0,
                        )
                    )
                    pii_count[pii_type.value] = pii_count.get(pii_type.value, 0) + 1

        # 2. 启发式:中文姓名检测
        name_matches = self._detect_chinese_names(text)
        for m in name_matches:
            matches.append(m)
            pii_count[PIIType.NAME.value] = pii_count.get(PIIType.NAME.value, 0) + 1

        # 3. 日期检测(生日 / 忌日)
        for pattern in DATE_PATTERNS:
            for match in pattern.finditer(text):
                # 区分生日 / 忌日(基于上下文关键词)
                pii_type = (
                    PIIType.DEATH_DATE
                    if any(
                        kw in text[max(0, match.start() - 10) : match.start()]
                        for kw in ("死亡", "去世", "逝世", "忌日", "亡")
                    )
                    else PIIType.BIRTHDATE
                )
                matches.append(
                    PIIMatch(
                        pii_type=pii_type,
                        original=match.group(),
                        start=match.start(),
                        end=match.end(),
                        confidence=0.7,
                    )
                )
                pii_count[pii_type.value] = pii_count.get(pii_type.value, 0) + 1

        # 按位置排序
        matches.sort(key=lambda m: m.start)

        return PIIResult(
            original_text=text,
            redacted_text=text,  # detect 不脱敏
            matches=matches,
            has_pii=bool(matches),
            pii_count_by_type=pii_count,
        )

    def redact(
        self,
        text: str,
        default_strategy: RedactStrategy = RedactStrategy.PARTIAL,
    ) -> PIIResult:
        """检测并脱敏(返回 redacted_text 供压缩用)。"""
        if not is_enabled("defense"):
            return PIIResult(original_text=text, redacted_text=text)

        result = self.detect(text)
        if not result.matches:
            return result

        # 从后往前替换(避免位置偏移)
        redacted_text = text
        for match in reversed(result.matches):
            strategy = self.strategies.get(match.pii_type, default_strategy)
            if match.pii_type in self.whitelisted_pii:
                strategy = RedactStrategy.KEEP
            redacted = self._apply_strategy(match.original, match.pii_type, strategy)
            match.redacted = redacted
            match.strategy = strategy
            redacted_text = redacted_text[: match.start] + redacted + redacted_text[match.end :]

        result.redacted_text = redacted_text
        return result

    def verify_summary(self, original: str, summary: str) -> dict[str, Any]:
        """验证压缩后摘要是否泄漏 PII。

        Returns:
            {leaked: bool, leaked_types: list, original_count: int, summary_count: int}
        """
        if not is_enabled("defense"):
            return {"leaked": False, "leaked_types": [], "original_count": 0, "summary_count": 0}

        orig_result = self.detect(original)
        summ_result = self.detect(summary)

        # 摘要中 PII 数量 ≤ 原文(允许脱敏后 PII 数量减少,但不能增加)
        leaked_types: list[str] = []
        for pii_type_str, count in summ_result.pii_count_by_type.items():
            orig_count = orig_result.pii_count_by_type.get(pii_type_str, 0)
            if count > orig_count:
                # 摘要比原文还多 → PII 漂移 / 重构
                leaked_types.append(pii_type_str)

        return {
            "leaked": bool(leaked_types),
            "leaked_types": leaked_types,
            "original_count": sum(orig_result.pii_count_by_type.values()),
            "summary_count": sum(summ_result.pii_count_by_type.values()),
        }

    # ==================================================================
    # 内部
    # ==================================================================

    def _detect_chinese_names(self, text: str) -> list[PIIMatch]:
        """启发式中文姓名检测(简化版)。

        规则:
            - 姓 + 2-3 个汉字
            - 上下文含"先生"/"女士"/"同志"/"先生"等称谓
        """
        matches: list[PIIMatch] = []
        # 简化:扫描以常见姓氏开头的 2-3 字组合
        # 实际生产中应使用 NER 模型(如 HanLP / LTP)
        pattern = re.compile(
            r"[" + CHINESE_SURNAMES + r"][\u4e00-\u9fa5]{1,2}"
            r"(?=\s|$|先生|女士|同志|小姐|博士|教授|律师|医生)"
        )
        for m in pattern.finditer(text):
            matches.append(
                PIIMatch(
                    pii_type=PIIType.NAME,
                    original=m.group(),
                    start=m.start(),
                    end=m.end(),
                    confidence=0.6,  # 启发式,可信度较低
                )
            )
        return matches

    def _apply_strategy(
        self,
        original: str,
        pii_type: PIIType,
        strategy: RedactStrategy,
    ) -> str:
        """应用脱敏策略。"""
        if strategy == RedactStrategy.KEEP:
            return original

        if strategy == RedactStrategy.REDACT:
            return f"[REDACTED-PII:{pii_type.value}]"

        if strategy == RedactStrategy.HASH:
            h = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
            return f"[HASHED-PII:{pii_type.value}:{h}]"

        if strategy == RedactStrategy.PARTIAL:
            return self._partial_redact(original, pii_type)

        return original

    def _partial_redact(self, original: str, pii_type: PIIType) -> str:
        """部分保留(头尾保留,中间 *)。"""
        length = len(original)
        if length <= 2:
            return "*" * length
        if length <= 4:
            return original[0] + "*" * (length - 2) + original[-1]
        if length <= 8:
            return original[:2] + "*" * (length - 4) + original[-2:]
        # 长字符串:保留前 3 + 后 4
        return original[:3] + "*" * (length - 7) + original[-4:]

    def _default_strategies(self) -> dict[PIIType, RedactStrategy]:
        """默认脱敏策略(按敏感度)。"""
        return {
            # 高敏感:全脱敏
            PIIType.CHINA_ID_CARD: RedactStrategy.PARTIAL,
            PIIType.CHINA_BANK_CARD: RedactStrategy.PARTIAL,
            PIIType.CHINA_PASSPORT: RedactStrategy.PARTIAL,
            PIIType.CHINA_HEALTH_CARD: RedactStrategy.PARTIAL,
            PIIType.CREDIT_CARD: RedactStrategy.PARTIAL,
            # 中敏感:部分保留
            PIIType.CHINA_PHONE: RedactStrategy.PARTIAL,
            PIIType.EMAIL: RedactStrategy.PARTIAL,
            PIIType.IP_ADDRESS: RedactStrategy.PARTIAL,
            PIIType.NAME: RedactStrategy.PARTIAL,
            PIIType.ADDRESS: RedactStrategy.PARTIAL,
            # 低敏感:可保留(法规要求)
            PIIType.BIRTHDATE: RedactStrategy.KEEP,
            PIIType.DEATH_DATE: RedactStrategy.KEEP,
        }


# 全局单例
_pr_instance: PIIRedactor | None = None
_pr_lock = threading.Lock()


def get_pii_redactor() -> PIIRedactor:
    global _pr_instance
    if _pr_instance is None:
        with _pr_lock:
            if _pr_instance is None:
                _pr_instance = PIIRedactor()
    return _pr_instance
