"""D9:跨 session trace 脱敏(防行为画像泄漏)。

问题(v1.4 联动风险 7):
    跨 session trace 关联可形成"用户旅程"视图,即便单条 trace 脱敏,
    聚合后仍可能暴露用户行为模式(如"用户 A 总是问房产继承"),
    通过模式识别可反推身份。

    类似问题:Web 分析中"匿名 cookie 追踪"也面临此风险。

缓解:
    1. 跨 session trace 关联需用户明示同意(ConsentManager)
    2. 同意后 trace_id 用 hash(原始关联 + salt + 周期轮换)替代
    3. 行为模式聚合前必须 LDP(本地差分隐私)加噪
    4. 跨 session 关联有时间窗(默认 30 天后强制断链)
    5. 用户可一键断链(删除跨 session 关联)

设计:
    - TraceLinkStrategy: 关联策略(none / hash / aggregated / explicit_consent)
    - CrossSessionLinker: 跨 session 关联管理
    - BehaviorAggregator: 行为模式聚合(带 LDP)
    - TraceAnonymizer: trace 落盘前脱敏(配置驱动)

集成:
    observability/tracer.py 落盘前调用:
        anonymizer = get_trace_anonymizer()
        record = anonymizer.sanitize(trace_record, user_id=...)
        if anonymizer.can_link_cross_session(user_id):
            linked_id = anonymizer.link_id(session_id, user_id)

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..feature_flags import is_enabled

logger = logging.getLogger(__name__)


# 跨 session 关联默认时间窗(30 天)
DEFAULT_LINK_WINDOW_SECONDS = 30 * 86400

# LDP 默认 epsilon(差分隐私预算,越小越隐私但越不准)
DEFAULT_LDP_EPSILON = 1.0

# salt 轮换周期(7 天)
SALT_ROTATION_SECONDS = 7 * 86400


class TraceLinkStrategy(str, Enum):
    """跨 session trace 关联策略(从弱到强)。"""

    NONE = "none"  # 不关联(最隐私,无法做用户旅程分析)
    HASH = "hash"  # hash 关联(单方向,无法反推 session)
    AGGREGATED = "aggregated"  # 聚合后关联(带 LDP 噪声)
    EXPLICIT_CONSENT = "explicit_consent"  # 用户明示同意后关联


@dataclass
class LinkConsent:
    """跨 session 关联同意记录。"""

    user_id: str
    strategy: TraceLinkStrategy = TraceLinkStrategy.HASH
    granted_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # 0 = 永不过期
    revoked_at: float | None = None  # 撤回时间

    def is_valid(self, now: float | None = None) -> bool:
        """同意是否有效。"""
        if self.revoked_at is not None:
            return False
        if self.expires_at == 0:
            return True
        return (now or time.time()) < self.expires_at


@dataclass
class BehaviorPattern:
    """用户行为模式(脱敏后)。"""

    pattern_hash: str  # 模式 hash(不含原始内容)
    occurrence_count: int  # 出现次数(LDP 加噪后)
    last_seen: float
    # 不存储:原始 query / 工具调用 / 具体字段


class CrossSessionLinker:
    """跨 session 关联管理器。

    职责:
        - 维护 user_id → consent 映射
        - 生成 link_id(hash + salt + 周期轮换)
        - 控制 link_id 时间窗(过期自动失效)
    """

    def __init__(self, store_path: str | None = None) -> None:
        self.store_path = store_path or os.environ.get(
            "DEADMAN_TRACE_LINK_STORE", "data/defense/trace_links.json"
        )
        self._lock = threading.RLock()
        # user_id → LinkConsent
        self._consents: dict[str, LinkConsent] = {}
        # salt 管理(周期轮换)
        self._salt: str = ""
        self._salt_generated_at: float = 0.0
        self._loaded = False

    # ==================================================================
    # 同意管理
    # ==================================================================

    def grant_consent(
        self,
        user_id: str,
        strategy: TraceLinkStrategy = TraceLinkStrategy.HASH,
        expires_in_days: int = 30,
    ) -> LinkConsent:
        """授予跨 session 关联同意。"""
        with self._lock:
            self._load()
            now = time.time()
            consent = LinkConsent(
                user_id=user_id,
                strategy=strategy,
                granted_at=now,
                expires_at=now + expires_in_days * 86400 if expires_in_days > 0 else 0,
            )
            self._consents[user_id] = consent
            self._save()
            logger.info(
                "Cross-session link consent granted for user %s (strategy=%s)",
                user_id,
                strategy.value,
            )
            return consent

    def revoke_consent(self, user_id: str) -> bool:
        """撤回同意(立即断链)。"""
        with self._lock:
            self._load()
            consent = self._consents.get(user_id)
            if consent is None:
                return False
            consent.revoked_at = time.time()
            self._save()
            logger.info("Cross-session link consent revoked for user %s", user_id)
            return True

    def get_consent(self, user_id: str) -> LinkConsent | None:
        with self._lock:
            self._load()
            return self._consents.get(user_id)

    def can_link(self, user_id: str, now: float | None = None) -> bool:
        """检查是否可关联。"""
        if not is_enabled("defense"):
            return False
        with self._lock:
            self._load()
            consent = self._consents.get(user_id)
            if consent is None:
                return False
            return consent.is_valid(now)

    # ==================================================================
    # link_id 生成
    # ==================================================================

    def link_id(
        self,
        session_id: str,
        user_id: str,
        now: float | None = None,
    ) -> str | None:
        """生成跨 session link_id。

        - 无同意 / 撤回 → 返回 None(不关联)
        - 策略 HASH → hash(session + user + salt),salt 周期轮换
        - 策略 AGGREGATED → 同上,但聚合时 LDP 加噪
        - 策略 EXPLICIT_CONSENT → 同上,但需用户明示

        Returns:
            link_id 或 None(不可关联)
        """
        if not self.can_link(user_id, now):
            return None

        salt = self._get_rotated_salt(now or time.time())
        # HMAC-SHA256:防止 length extension attack
        msg = f"{session_id}|{user_id}"
        return hmac.new(salt.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def _get_rotated_salt(self, now: float) -> str:
        """获取周期轮换的 salt。"""
        with self._lock:
            self._load()
            # 首次或过期 → 生成新 salt
            if not self._salt or now - self._salt_generated_at > SALT_ROTATION_SECONDS:
                self._salt = os.urandom(32).hex()
                self._salt_generated_at = now
                self._save()
            return self._salt

    # ==================================================================
    # 内部
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            import json
            from pathlib import Path

            path: Path | str = self.store_path
            if isinstance(path, str):
                path = Path(path)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                self._salt = data.get("salt", "")
                self._salt_generated_at = data.get("salt_generated_at", 0.0)
                for uid, c in data.get("consents", {}).items():
                    self._consents[uid] = LinkConsent(
                        user_id=uid,
                        strategy=TraceLinkStrategy(c.get("strategy", "hash")),
                        granted_at=c.get("granted_at", 0.0),
                        expires_at=c.get("expires_at", 0.0),
                        revoked_at=c.get("revoked_at"),
                    )
        except Exception as e:
            logger.warning("TraceLink load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            import json
            from pathlib import Path

            path = Path(self.store_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "salt": self._salt,
                "salt_generated_at": self._salt_generated_at,
                "consents": {
                    uid: {
                        "strategy": c.strategy.value,
                        "granted_at": c.granted_at,
                        "expires_at": c.expires_at,
                        "revoked_at": c.revoked_at,
                    }
                    for uid, c in self._consents.items()
                },
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            logger.error("TraceLink save failed: %s", e)


class BehaviorAggregator:
    """行为模式聚合(带 LDP 差分隐私)。

    问题:即便 trace 脱敏,聚合统计可能反推个体。
    缓解:Local Differential Privacy(LDP),客户端加噪后上报。
    """

    def __init__(self, epsilon: float = DEFAULT_LDP_EPSILON) -> None:
        self.epsilon = epsilon
        self._lock = threading.RLock()
        # pattern_hash → 聚合计数
        self._patterns: dict[str, BehaviorPattern] = {}

    def add_pattern(self, pattern_hash: str, now: float | None = None) -> int:
        """添加一次模式观察(LDP 加噪后返回的 count)。

        Args:
            pattern_hash: 已 hash 的模式(不含原始内容)

        Returns:
            加噪后的观察 count
        """
        import math
        import random

        now = now or time.time()
        with self._lock:
            p = self._patterns.get(pattern_hash)
            if p is None:
                p = BehaviorPattern(
                    pattern_hash=pattern_hash,
                    occurrence_count=0,
                    last_seen=now,
                )
                self._patterns[pattern_hash] = p

            # LDP:Randomized Response
            # 真实 count +1,但以概率 e^eps / (1 + e^eps) 上报 +1,
            # 否则上报 +0(噪声),分析端可去除偏差
            prob_true = math.exp(self.epsilon) / (1 + math.exp(self.epsilon))
            if random.random() < prob_true:
                p.occurrence_count += 1
            p.last_seen = now
            return p.occurrence_count

    def get_patterns(self, min_count: int = 5) -> list[BehaviorPattern]:
        """获取模式列表(过滤低 count,防止稀疏样本反推)。"""
        with self._lock:
            return [p for p in self._patterns.values() if p.occurrence_count >= min_count]

    def purge_expired(self, max_age_seconds: int = 90 * 86400) -> int:
        """清理过期模式。"""
        now = time.time()
        with self._lock:
            expired = [h for h, p in self._patterns.items() if now - p.last_seen > max_age_seconds]
            for h in expired:
                del self._patterns[h]
            return len(expired)


class TraceAnonymizer:
    """trace 落盘前脱敏 + 跨 session 关联控制。

    职责:
        1. 字段级脱敏(去 PII / 替换敏感字段)
        2. 跨 session 关联决策(基于 consent)
        3. 行为模式聚合(LDP)
    """

    # trace 中需脱敏的字段(常见 PII / 敏感数据)
    SENSITIVE_FIELDS = {
        "user_id",
        "user_email",
        "user_phone",
        "user_name",
        "session_id",
        "ip_address",
        "device_id",
        "location",
        "query",
        "input",
        "user_input",
        "message",
        "tool_args",
        "tool_result",
        "llm_response",
    }

    def __init__(
        self,
        linker: CrossSessionLinker | None = None,
        aggregator: BehaviorAggregator | None = None,
    ) -> None:
        self.linker = linker or CrossSessionLinker()
        self.aggregator = aggregator or BehaviorAggregator()

    def sanitize(
        self,
        trace_record: dict[str, Any],
        *,
        user_id: str = "",
        session_id: str = "",
        link_cross_session: bool = True,
    ) -> dict[str, Any]:
        """脱敏 trace 记录。

        - 敏感字段替换为 hash 或 [REDACTED]
        - 跨 session link_id 仅在用户同意时生成
        """
        if not is_enabled("defense"):
            return trace_record

        sanitized = dict(trace_record)
        for key in list(sanitized.keys()):
            if key.lower() in self.SENSITIVE_FIELDS:
                value = sanitized[key]
                if isinstance(value, str) and value:
                    # hash 替代原值(可后续统计,不可反推)
                    sanitized[key] = self._hash_value(value)
                elif isinstance(value, dict | list):
                    sanitized[key] = "[REDACTED_COMPLEX]"
                else:
                    sanitized[key] = "[REDACTED]"

        # 跨 session link_id
        if link_cross_session and user_id and session_id:
            link_id = self.linker.link_id(session_id, user_id)
            if link_id:
                sanitized["link_id"] = link_id
                # 行为模式聚合(LDP)
                pattern = self._extract_pattern(trace_record)
                if pattern:
                    self.aggregator.add_pattern(pattern)
            else:
                sanitized["link_id"] = None  # 显式标记未关联

        return sanitized

    def can_link_cross_session(self, user_id: str) -> bool:
        """是否可关联跨 session(需用户同意)。"""
        return self.linker.can_link(user_id)

    def _hash_value(self, value: str) -> str:
        """hash 敏感值(不可逆)。"""
        import hashlib

        return "h:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _extract_pattern(self, trace_record: dict[str, Any]) -> str:
        """提取行为模式 hash(不含具体内容)。

        模式 = (span 类型 + 工具名 + 状态)
        如 "react.action.tool=search.success"
        """
        span_type = trace_record.get("span_type", "")
        tool_name = trace_record.get("tool_name", "")
        status = trace_record.get("status", "")
        pattern = f"{span_type}.{tool_name}.{status}"
        import hashlib

        return hashlib.sha256(pattern.encode("utf-8")).hexdigest()[:16]


# =====================================================================
# 全局单例
# =====================================================================

_anonymizer: TraceAnonymizer | None = None
_anonymizer_lock = threading.Lock()


def get_trace_anonymizer() -> TraceAnonymizer:
    global _anonymizer
    if _anonymizer is None:
        with _anonymizer_lock:
            if _anonymizer is None:
                _anonymizer = TraceAnonymizer()
    return _anonymizer
