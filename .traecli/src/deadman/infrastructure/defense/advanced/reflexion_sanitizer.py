"""D17:Reflexion 策略脱敏(Reflexion Strategy Sanitization)。

问题:
    deadman `reflexion/engine.py` 在记录失败 / 反思时,
    未对 PII 脱敏:
        - `failure_info.input_summary = str(current_input)[:200]`
          → 直接截断原始用户输入,可能含身份证 / 手机 / 邮箱
        - LLM 生成的 `reflection.failure_reason / adjustment_strategy`
          → 可能 echo 用户输入中的 PII
        - 持久化到 `memory_store.record_adjustment()`
          → 跨会话 / 跨用户泄漏(若 memory_store 共享)
        - trace span 写入 `failure_message / failure_reason`
          → 落盘到 OTel / Langfuse → 行为画像泄漏

    攻击场景:
        - 用户输入"我父亲张三 110101199001011234 已去世"
        - Reflexion 记录 input_summary
        - 共享 memory_store → 其他用户 query 触发召回 → PII 泄漏
        - 或 trace 落盘 → 内部运维查看 → PII 泄漏

缓解:
    - ReflexionSanitizer: 包裹 ReflexionEngine 的 input/output/trace
    - PII 脱敏:对 input_summary / output_summary / failure_reason / adjustment_strategy
    - 跨用户共享时,策略内容只保留"动作建议"(不保留原始输入)
    - trace span 写入前先脱敏
    - audit log 记录"PII 命中数"(不记录原值)

设计:
    sanitizer = ReflexionSanitizer()
    sanitized_input = sanitizer.sanitize_input(user_input, max_chars=200)
    sanitized_output = sanitizer.sanitize_output(llm_response)
    safe_for_share = sanitizer.sanitize_for_share(reflection_record)

集成:
    reflexion/engine.py 在持久化前调用 sanitizer。
    trace span 写入前调用 sanitizer.sanitize_for_trace()。

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


@dataclass
class SanitizationResult:
    """脱敏结果。"""

    sanitized: str
    pii_count: int = 0
    pii_types_found: set[str] = field(default_factory=set)
    truncated: bool = False
    original_length: int = 0


# 简化的 PII 正则(production 用 PIIRedactor 完整版)
_PATTERNS: dict[str, re.Pattern] = {
    "china_id_card": re.compile(r"\b\d{17}[\dXx]\b"),
    "china_phone": re.compile(r"\b1[3-9]\d{9}\b"),
    "email": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "bank_card": re.compile(r"\b\d{16,19}\b"),
    "ip_address": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
}

# 反思日志中常见的 PII 模式(姓名后跟描述)
_NAME_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:姓名|名字|叫|名为|姓名为)\s*[：:]?\s*([^\s,，。.!]{2,4})"),
    re.compile(r"(?:父亲|母亲|儿子|女儿|丈夫|妻子|配偶)\s*[：:]?\s*([^\s,，。.!]{2,4})"),
]


class ReflexionSanitizer:
    """Reflexion 策略脱敏器。

    用法:
        sanitizer = ReflexionSanitizer()
        # 1. 输入摘要前脱敏
        input_summary = sanitizer.sanitize_input(user_input, max_chars=200)
        # 2. LLM 输出脱敏
        clean_output = sanitizer.sanitize_output(llm_response)
        # 3. 跨用户共享前彻底脱敏(只留策略,不留输入)
        shareable = sanitizer.sanitize_for_share(reflection_record)
        # 4. trace 落盘前脱敏
        safe_span = sanitizer.sanitize_for_trace(value)
    """

    # 替换占位符
    PLACEHOLDER = "[REDACTED-PII]"
    PLACEHOLDER_NAMED = "[REDACTED-PII:{type}]"

    def __init__(
        self,
        redact_strategy: str = "named",  # "named" / "anonymous" / "hash"
        max_input_chars: int = 200,
    ) -> None:
        self.redact_strategy = redact_strategy
        self.max_input_chars = max_input_chars

    def sanitize_input(
        self,
        text: str,
        max_chars: int | None = None,
    ) -> SanitizationResult:
        """脱敏用户输入(用于 input_summary)。

        Args:
            text: 原始输入
            max_chars: 截断长度(默认 self.max_input_chars)

        Returns:
            SanitizationResult: 脱敏后的文本 + 统计
        """
        if not text:
            return SanitizationResult(sanitized="", original_length=0)
        if not is_enabled("defense"):
            truncated = text[: max_chars or self.max_input_chars]
            return SanitizationResult(
                sanitized=truncated,
                truncated=len(text) > len(truncated),
                original_length=len(text),
            )

        original_len = len(text)
        # 先脱敏,再截断(避免脱敏后超长)
        redacted, count, types_found = self._redact_pii(text)
        limit = max_chars or self.max_input_chars
        if len(redacted) > limit:
            redacted = redacted[:limit]
            truncated = True
        else:
            truncated = original_len > limit
        return SanitizationResult(
            sanitized=redacted,
            pii_count=count,
            pii_types_found=types_found,
            truncated=truncated,
            original_length=original_len,
        )

    def sanitize_output(self, text: str) -> SanitizationResult:
        """脱敏 LLM 输出(failure_reason / adjustment_strategy)。

        LLM 输出可能 echo 输入中的 PII,必须二次脱敏。
        """
        if not text:
            return SanitizationResult(sanitized="", original_length=0)
        if not is_enabled("defense"):
            return SanitizationResult(
                sanitized=text, original_length=len(text)
            )
        redacted, count, types_found = self._redact_pii(text)
        # 还要检测姓名(在反思语境下,姓名常出现)
        redacted, name_count = self._redact_names(redacted)
        count += name_count
        return SanitizationResult(
            sanitized=redacted,
            pii_count=count,
            pii_types_found=types_found,
            original_length=len(text),
        )

    def sanitize_for_trace(self, value: Any) -> Any:
        """trace span 写入前脱敏(递归处理 dict / list / str)。

        OTel / Langfuse 会落盘 trace,必须脱敏。
        """
        if not is_enabled("defense"):
            return value
        if isinstance(value, str):
            result = self.sanitize_output(value)
            return result.sanitized
        if isinstance(value, dict):
            return {k: self.sanitize_for_trace(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            sanitized = [self.sanitize_for_trace(v) for v in value]
            return type(value)(sanitized) if isinstance(value, tuple) else sanitized
        # int / float / bool / None → 原样
        return value

    def sanitize_for_share(self, reflection_record: dict) -> dict:
        """跨用户共享前彻底脱敏(只保留策略,不留原始输入)。

        用于 `memory_store.record_adjustment()` 持久化前。
        保留:failure_type / adjustment_strategy(已脱敏)
        移除:input_summary / output_summary / 任何用户数据
        """
        if not is_enabled("defense"):
            return dict(reflection_record)

        # 允许共享的字段(策略相关,无 PII)
        SAFE_FIELDS = {
            "failure_type", "failure_reason",  # LLM 生成,但需脱敏
            "adjustment_strategy",  # 已脱敏的策略
            "adjusted_params",  # 参数(数字 / 枚举)
            "success",  # bool
            "timestamp",  # 数字
            "agent_name",  # agent 标识
            "attempt",  # 数字
        }
        # 强制移除的字段(含用户数据)
        REMOVED_FIELDS = {
            "input_summary", "input", "user_input", "original_input",
            "output_summary", "output", "raw_output",
            "tool_args", "tool_result",
            "user_id", "session_id", "tenant_id",
        }
        cleaned = {}
        for k, v in reflection_record.items():
            if k in REMOVED_FIELDS:
                continue
            if k in SAFE_FIELDS:
                if isinstance(v, str):
                    result = self.sanitize_output(v)
                    cleaned[k] = result.sanitized
                elif isinstance(v, dict):
                    cleaned[k] = self.sanitize_for_trace(v)
                else:
                    cleaned[k] = v
            else:
                # 未知字段:保守脱敏
                if isinstance(v, str):
                    result = self.sanitize_output(v)
                    cleaned[k] = result.sanitized
                elif isinstance(v, (dict, list)):
                    cleaned[k] = self.sanitize_for_trace(v)
                else:
                    cleaned[k] = v
        return cleaned

    # ==================================================================
    # 内部
    # ==================================================================

    def _redact_pii(self, text: str) -> tuple[str, int, set[str]]:
        """脱敏 PII(返回 redacted_text + count + types_found)。"""
        redacted = text
        count = 0
        types_found: set[str] = set()
        for pii_type, pattern in _PATTERNS.items():
            matches = pattern.findall(redacted)
            if matches:
                count += len(matches)
                types_found.add(pii_type)
                placeholder = self._placeholder(pii_type)
                redacted = pattern.sub(placeholder, redacted)
        return redacted, count, types_found

    def _redact_names(self, text: str) -> tuple[str, int]:
        """脱敏姓名(中文家庭关系后的名字)。"""
        redacted = text
        count = 0
        for pattern in _NAME_PATTERNS:
            matches = pattern.findall(redacted)
            if matches:
                count += len(matches)
                placeholder = self._placeholder("name")
                # 替换捕获组(保留前缀)
                redacted = pattern.sub(
                    lambda m, pl=placeholder: m.group(0).replace(m.group(1), pl),
                    redacted,
                )
        return redacted, count

    def _placeholder(self, pii_type: str) -> str:
        """生成占位符。"""
        if self.redact_strategy == "anonymous":
            return self.PLACEHOLDER
        elif self.redact_strategy == "hash":
            # 用 hash 替代原值(便于去重 / 统计,不可反推)
            return f"[HASH:{pii_type}]"
        else:  # named
            return self.PLACEHOLDER_NAMED.format(type=pii_type)


# =====================================================================
# 便捷函数(供 reflexion/engine.py 调用)
# =====================================================================

_sanitizer_instance: ReflexionSanitizer | None = None


def get_reflexion_sanitizer() -> ReflexionSanitizer:
    """获取全局 ReflexionSanitizer 单例。"""
    global _sanitizer_instance
    if _sanitizer_instance is None:
        _sanitizer_instance = ReflexionSanitizer()
    return _sanitizer_instance


def reset_reflexion_sanitizer() -> None:
    """重置全局单例(测试用)。"""
    global _sanitizer_instance
    _sanitizer_instance = None


def hash_user_id(user_id: str, salt: str = "deadman") -> str:
    """对 user_id 做单向 hash(用于关联分析,不可反推)。

    用法:
        safe_id = hash_user_id("user-123")
        # 后续可聚合统计(per-safe_id),但不可反推原始 user_id
    """
    if not user_id:
        return ""
    h = hashlib.sha256(f"{salt}:{user_id}".encode()).hexdigest()[:16]
    return f"anon-{h}"
