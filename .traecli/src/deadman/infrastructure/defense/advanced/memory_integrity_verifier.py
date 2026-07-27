"""D31:记忆完整性验证器(Memory Integrity Verifier)。

问题:
    deadman 4 层记忆系统(working/episodic/semantic/procedural)在长期运行中,
    可能遭遇以下攻击:

    1. **Memory Poisoning**:恶意用户通过对话注入虚假记忆(2025 论文
       "Memory Poisoning in LLM Agents"),诱导 agent 把虚假事实写入 episodic,
       后续会话基于错误记忆回答。
    2. **Memory Tampering**:攻击者(或带外操作)修改已写入的记忆文件 / DB,
       导致 hash / 时间戳不一致。
    3. **Memory Replay**:攻击者重放旧记忆(从备份 / 历史日志)绕过当前护栏。
    4. **Cross-User Leakage**:相似度异常(跨用户记忆 embedding 距离过近),
       反推用户身份(违反 k-匿名)。
    5. **Memory "Revival"**:删除某记忆后,新对话 LLM 又推断出类似记忆
       (v1.5 已识别,需要 negation memory + tombstone)。

    生产风险:
    - 用户得到错误答案(基于被投毒的记忆)
    - 跨用户隐私泄漏(GDPR / 个保法违规)
    - 平台质量长期下滑(错误记忆累积)
    - 删除请求失效(GDPR 删除后记忆仍残留)

缓解:
    1. **Hash Chain**:每条记忆带 `prev_hash + own_hash + content_hash`,
       形成链式结构,任何篡改都会破坏链。
    2. **Provenance Tracking**:记录记忆来源(user / agent / external / system)
       + 信任级别,低信任来源触发更严格校验。
    3. **Poisoning Detection**:异常模式检测
       - 来源不可信(external + 低 trust)
       - 内容矛盾(与已有记忆冲突)
       - 频率异常(单 user / session 短时间写入过多)
    4. **Replay Detection**:相同 content_hash 在不同 session 出现 → 告警
    5. **Cross-User Leakage Detection**:跨用户记忆相似度检测
    6. **Chain Verification**:`verify_chain()` 校验 hash 链完整性,防篡改
    7. **Tombstone**:删除记忆时记录 tombstone,防止"复活"

设计:
    - MemoryRecord:带 provenance + hash chain 的记忆记录
    - IntegrityViolation:违规告警
    - MemoryIntegrityVerifier:主验证器(线程安全 + 可持久化)

集成:
    memory/episodic_store.py 写入前:
        verifier = get_memory_integrity_verifier()
        record = verifier.create_record(
            user_id="u1",
            session_id="s1",
            content="用户希望按民法典继承编处理",
            source=MemorySource.USER,
            trust_level=TrustLevel.HIGH,
        )
        violations = verifier.check_record(record)
        if any(v.severity == AlertSeverity.CRITICAL for v in violations):
            # 拒绝写入 + 告警
            ...
        else:
            verifier.append_record(record)

    定期(每日 / 每周):
        result = verifier.verify_chain(user_id="u1")
        if not result.is_valid:
            # 触发审计 + 告警
            ...

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用,关闭后透传)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from ...feature_flags import is_enabled
from ....utils.text_similarity import tokenize, jaccard_similarity

logger = logging.getLogger(__name__)


# =====================================================================
# 枚举
# =====================================================================

class MemorySource(str, Enum):
    """记忆来源(决定基线信任度)。"""

    USER = "user"  # 用户直接输入
    AGENT = "agent"  # agent 推断 / 生成
    SYSTEM = "system"  # 系统配置 / 默认值
    EXTERNAL = "external"  # 外部数据源(文档 / API / RAG)
    INFERRED = "inferred"  # LLM 从已有记忆推断
    TOOL = "tool"  # 工具调用结果


class TrustLevel(str, Enum):
    """来源信任级别。"""

    HIGH = "high"  # 用户直接确认 / 系统配置
    MEDIUM = "medium"  # agent 推断 / 工具结果
    LOW = "low"  # 外部数据源 / 未确认
    UNTRUSTED = "untrusted"  # 攻击者可能控制(应拒绝)


class ViolationType(str, Enum):
    """完整性违规类型。"""

    NONE = "none"
    POISONING = "poisoning"  # 投毒:来源不可信 + 内容异常
    TAMPERING = "tampering"  # 篡改:hash 链断裂
    REPLAY = "replay"  # 重放:相同 hash 跨 session 出现
    CROSS_USER_LEAK = "cross_user_leak"  # 跨用户泄漏:相似度异常
    FREQUENCY_ANOMALY = "frequency_anomaly"  # 频率异常:短时间大量写入
    CONTENT_CONFLICT = "content_conflict"  # 内容矛盾:与已有记忆冲突
    REVIVAL_DETECTED = "revival_detected"  # 复活:已删除记忆再次出现


class AlertSeverity(str, Enum):
    """告警严重度。"""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# =====================================================================
# 数据类
# =====================================================================

@dataclass
class MemoryRecord:
    """带 provenance + hash chain 的记忆记录。

    每条记录包含:
    - record_id:唯一 ID
    - user_id / session_id:归属
    - content:实际内容
    - content_hash:content 的 sha256(用于去重 / replay 检测)
    - prev_hash:链上上一条的 own_hash(首条为 "0" * 16)
    - own_hash:本条的链 hash(prev_hash + record_id + content_hash 的 sha256)
    - source / trust_level:provenance
    - timestamp:写入时间
    - metadata:额外信息(如 rule_version)
    """

    record_id: str
    user_id: str
    session_id: str
    content: str
    source: MemorySource = MemorySource.USER
    trust_level: TrustLevel = TrustLevel.MEDIUM
    timestamp: float = field(default_factory=time.time)
    prev_hash: str = "0" * 16
    own_hash: str = ""
    content_hash: str = ""
    # 该记录是否已被删除(tombstone)
    deleted: bool = False
    deleted_at: float = 0.0
    # 额外元数据(如 rule_version, agent_name)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = self._compute_content_hash()
        if not self.own_hash:
            self.own_hash = self._compute_own_hash()

    def _compute_content_hash(self) -> str:
        """content 的 sha256 前 16 字符。"""
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:16]

    def _compute_own_hash(self) -> str:
        """链 hash:prev_hash + record_id + content_hash。"""
        material = f"{self.prev_hash}:{self.record_id}:{self.content_hash}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.value
        d["trust_level"] = self.trust_level.value
        return d

    def verify_own_hash(self) -> bool:
        """校验 own_hash 是否正确。"""
        return self._compute_own_hash() == self.own_hash

    def verify_content_hash(self) -> bool:
        """校验 content_hash 是否正确。"""
        return self._compute_content_hash() == self.content_hash


@dataclass
class IntegrityViolation:
    """完整性违规告警。"""

    timestamp: float = field(default_factory=time.time)
    user_id: str = ""
    session_id: str = ""
    record_id: str = ""
    severity: AlertSeverity = AlertSeverity.WARNING
    violation_type: ViolationType = ViolationType.NONE
    message: str = ""
    # 关联的证据(如冲突的已有记忆 ID / 跨用户相似度)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["violation_type"] = self.violation_type.value
        return d


@dataclass
class ChainVerificationResult:
    """链验证结果。"""

    user_id: str
    is_valid: bool = True
    # 断裂点(第一个不一致的 record_id)
    broken_at: str = ""
    # 总记录数
    total_records: int = 0
    # 已删除记录数(tombstone)
    deleted_records: int = 0
    # 违规列表
    violations: list[IntegrityViolation] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["violations"] = [v.to_dict() for v in self.violations]
        return d


# =====================================================================
# 默认配置
# =====================================================================

VERIFIER_DEFAULTS = {
    # 单 user / session 短时间(秒)内写入超过此数 → 频率异常
    "frequency_anomaly_window_seconds": 60,
    "frequency_anomaly_max_records": 20,
    # 跨用户相似度阈值(超过 → 跨用户泄漏告警)
    "cross_user_similarity_threshold": 0.85,
    # 相同 content_hash 在多少天内重复出现 → 重放告警
    "replay_window_seconds": 86400,  # 1 天
    "replay_min_different_sessions": 2,
    # 历史保留(用于趋势分析)
    "history_retention": 10000,
    # 跨用户相似度比对:最近 N 条(避免全量比对性能问题)
    "cross_user_compare_recent": 500,
}


# =====================================================================
# 文本相似度计算 - 使用共享 text_similarity 模块
# =====================================================================


def _text_similarity(text_a: str, text_b: str) -> float:
    """文本相似度(Jaccard on tokens, 0-1)。"""
    return jaccard_similarity(tokenize(text_a), tokenize(text_b))


# =====================================================================
# Memory Integrity Verifier
# =====================================================================

class MemoryIntegrityVerifier:
    """记忆完整性验证器。

    用法:
        verifier = get_memory_integrity_verifier()

        # 1. 写入前检查 + 创建记录(自动 hash chain)
        record = verifier.create_record(
            user_id="u1",
            session_id="s1",
            content="用户希望按民法典继承编处理",
            source=MemorySource.USER,
            trust_level=TrustLevel.HIGH,
        )

        # 2. 写入前检测(可选择性拒绝)
        violations = verifier.check_record(record)
        if any(v.severity == AlertSeverity.CRITICAL for v in violations):
            # 拒绝写入 + 告警
            return

        # 3. 写入(append 到链)
        verifier.append_record(record)

        # 4. 定期校验链完整性
        result = verifier.verify_chain(user_id="u1")
        if not result.is_valid:
            # 触发审计 + 告警
            ...

        # 5. 删除时记录 tombstone(防止"复活")
        verifier.delete_record(record_id="r-1", user_id="u1")
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        store_path: Optional[str] = None,
    ) -> None:
        self.config = {**VERIFIER_DEFAULTS, **(config or {})}
        self.store_path = store_path
        self._lock = threading.RLock()
        # user_id -> list[MemoryRecord](链)
        self._chains: dict[str, list[MemoryRecord]] = defaultdict(list)
        # user_id -> set[content_hash](已删除,用于复活检测)
        self._tombstones: dict[str, set[str]] = defaultdict(set)
        # user_id -> deque[(timestamp, session_id)](频率统计)
        self._write_history: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
        # 全局 content_hash -> list[(user_id, session_id, timestamp)](跨用户 / 重放检测)
        self._content_index: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        # 违规历史
        self._violations: deque[IntegrityViolation] = deque(maxlen=self.config["history_retention"])
        # 统计
        self._stats: dict[str, int] = defaultdict(int)
        if store_path and os.path.exists(store_path):
            self._load()

    # ==================================================================
    # 记录创建 / 追加 / 删除
    # ==================================================================

    def create_record(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        source: MemorySource = MemorySource.USER,
        trust_level: Optional[TrustLevel] = None,
        metadata: Optional[dict] = None,
        record_id: Optional[str] = None,
    ) -> MemoryRecord:
        """创建一条记忆记录(自动计算 hash + 接到链尾)。

        注意:此方法仅创建,不写入。要写入需调用 append_record()。
        """
        # 自动推断 trust_level(若未指定)
        if trust_level is None:
            trust_level = self._default_trust_for_source(source)

        # 取链尾 hash 作为 prev_hash
        with self._lock:
            chain = self._chains.get(user_id, [])
            prev_hash = chain[-1].own_hash if chain else "0" * 16

        # record_id:用户指定 or 自动生成
        rid = record_id or f"r-{user_id}-{int(time.time() * 1000)}-{len(chain)}"

        record = MemoryRecord(
            record_id=rid,
            user_id=user_id,
            session_id=session_id,
            content=content,
            source=source,
            trust_level=trust_level,
            prev_hash=prev_hash,
            metadata=metadata or {},
        )
        return record

    def append_record(self, record: MemoryRecord) -> None:
        """追加记录到链尾(不重新计算 hash,信任 create_record 已算好)。"""
        with self._lock:
            chain = self._chains[record.user_id]
            # 校验 prev_hash 是否匹配链尾
            expected_prev = chain[-1].own_hash if chain else "0" * 16
            if record.prev_hash != expected_prev:
                logger.warning(
                    "Memory record prev_hash mismatch: expected %s, got %s (user=%s, record=%s)",
                    expected_prev, record.prev_hash, record.user_id, record.record_id,
                )
                # 强制重算 prev_hash + own_hash(保持链完整性)
                record.prev_hash = expected_prev
                record.own_hash = record._compute_own_hash()

            chain.append(record)
            # 更新索引
            self._content_index[record.content_hash].append(
                (record.user_id, record.session_id, record.timestamp)
            )
            self._write_history[record.user_id].append(
                (record.timestamp, record.session_id)
            )
            self._stats["appended"] += 1
            self._save()

    def delete_record(self, *, record_id: str, user_id: str) -> bool:
        """删除记录(软删除 + tombstone,防"复活")。"""
        with self._lock:
            chain = self._chains.get(user_id, [])
            for rec in chain:
                if rec.record_id == record_id and not rec.deleted:
                    rec.deleted = True
                    rec.deleted_at = time.time()
                    # 加入 tombstone
                    self._tombstones[user_id].add(rec.content_hash)
                    self._stats["deleted"] += 1
                    self._save()
                    logger.info(
                        "Deleted memory record %s (user=%s), tombstone added",
                        record_id, user_id,
                    )
                    return True
            return False

    # ==================================================================
    # 投毒 / 异常检测
    # ==================================================================

    def check_record(self, record: MemoryRecord) -> list[IntegrityViolation]:
        """检测单条记录的异常(写入前调用,可拒绝)。

        检测项:
            1. 来源信任度(UNTRUSTED → CRITICAL)
            2. 频率异常(短时间大量写入 → WARNING)
            3. 重放检测(相同 hash 跨 session 出现 → WARNING)
            4. 跨用户泄漏(与最近其他用户记忆相似度高 → CRITICAL)
            5. 复活检测(content_hash 在 tombstone 中 → CRITICAL)
            6. 内容冲突(与同用户已有记忆矛盾 → WARNING)
        """
        violations: list[IntegrityViolation] = []

        if not is_enabled("defense"):
            return violations

        # 1. 来源信任度
        if record.trust_level == TrustLevel.UNTRUSTED:
            violations.append(IntegrityViolation(
                user_id=record.user_id,
                session_id=record.session_id,
                record_id=record.record_id,
                severity=AlertSeverity.CRITICAL,
                violation_type=ViolationType.POISONING,
                message=f"Untrusted source: {record.source.value}",
                evidence={"source": record.source.value, "trust": record.trust_level.value},
            ))
            self._stats["poisoning_blocked"] += 1

        # 2. 频率异常
        freq_violation = self._check_frequency(record)
        if freq_violation:
            violations.append(freq_violation)

        # 3. 重放检测
        replay_violation = self._check_replay(record)
        if replay_violation:
            violations.append(replay_violation)

        # 4. 跨用户泄漏
        leak_violation = self._check_cross_user_leak(record)
        if leak_violation:
            violations.append(leak_violation)

        # 5. 复活检测
        revival_violation = self._check_revival(record)
        if revival_violation:
            violations.append(revival_violation)

        # 6. 内容冲突
        conflict_violation = self._check_content_conflict(record)
        if conflict_violation:
            violations.append(conflict_violation)

        # 累计违规
        if violations:
            with self._lock:
                self._stats["violations_detected"] += len(violations)
                self._violations.extend(violations)

        return violations

    def _check_frequency(self, record: MemoryRecord) -> Optional[IntegrityViolation]:
        """频率异常:短时间写入过多。"""
        window = self.config["frequency_anomaly_window_seconds"]
        threshold = self.config["frequency_anomaly_max_records"]
        cutoff = record.timestamp - window

        with self._lock:
            history = self._write_history.get(record.user_id, deque())
            # 计算窗口内写入数
            recent = [(ts, sid) for ts, sid in history if ts >= cutoff]
            # 模拟写入后(尚未实际 append)
            count_in_window = len(recent) + 1  # +1 for current

        if count_in_window > threshold:
            return IntegrityViolation(
                user_id=record.user_id,
                session_id=record.session_id,
                record_id=record.record_id,
                severity=AlertSeverity.WARNING,
                violation_type=ViolationType.FREQUENCY_ANOMALY,
                message=(
                    f"Frequency anomaly: {count_in_window} records in {window}s "
                    f"(threshold={threshold})"
                ),
                evidence={"count": count_in_window, "window_seconds": window},
            )
        return None

    def _check_replay(self, record: MemoryRecord) -> Optional[IntegrityViolation]:
        """重放检测:相同 content_hash 在不同 session 出现。"""
        window = self.config["replay_window_seconds"]
        min_sessions = self.config["replay_min_different_sessions"]
        cutoff = record.timestamp - window

        with self._lock:
            occurrences = self._content_index.get(record.content_hash, [])
            recent_sessions = {
                sid for uid, sid, ts in occurrences
                if ts >= cutoff and uid == record.user_id
            }

        # 当前 session 也算一个
        recent_sessions.add(record.session_id)
        if len(recent_sessions) >= min_sessions:
            return IntegrityViolation(
                user_id=record.user_id,
                session_id=record.session_id,
                record_id=record.record_id,
                severity=AlertSeverity.WARNING,
                violation_type=ViolationType.REPLAY,
                message=(
                    f"Replay detected: content_hash {record.content_hash} "
                    f"appeared in {len(recent_sessions)} sessions within {window}s"
                ),
                evidence={
                    "content_hash": record.content_hash,
                    "distinct_sessions": len(recent_sessions),
                },
            )
        return None

    def _check_cross_user_leak(self, record: MemoryRecord) -> Optional[IntegrityViolation]:
        """跨用户泄漏:与最近其他用户记忆相似度过高。"""
        threshold = self.config["cross_user_similarity_threshold"]
        recent_n = self.config["cross_user_compare_recent"]

        with self._lock:
            # 收集其他用户最近 N 条记忆的 content
            other_contents: list[tuple[str, str]] = []  # (user_id, content)
            for uid, chain in self._chains.items():
                if uid == record.user_id:
                    continue
                recent_records = [r for r in chain[-recent_n:] if not r.deleted]
                for r in recent_records:
                    other_contents.append((uid, r.content))

        if not other_contents:
            return None

        max_sim = 0.0
        max_sim_user = ""
        for other_uid, other_content in other_contents:
            sim = _text_similarity(record.content, other_content)
            if sim > max_sim:
                max_sim = sim
                max_sim_user = other_uid

        if max_sim >= threshold:
            return IntegrityViolation(
                user_id=record.user_id,
                session_id=record.session_id,
                record_id=record.record_id,
                severity=AlertSeverity.CRITICAL,
                violation_type=ViolationType.CROSS_USER_LEAK,
                message=(
                    f"Cross-user leakage: similarity={max_sim:.3f} "
                    f"with user {max_sim_user} (threshold={threshold})"
                ),
                evidence={
                    "max_similarity": max_sim,
                    "other_user": max_sim_user,
                    "threshold": threshold,
                },
            )
        return None

    def _check_revival(self, record: MemoryRecord) -> Optional[IntegrityViolation]:
        """复活检测:已删除的 content_hash 再次出现。"""
        with self._lock:
            tombstones = self._tombstones.get(record.user_id, set())

        if record.content_hash in tombstones:
            return IntegrityViolation(
                user_id=record.user_id,
                session_id=record.session_id,
                record_id=record.record_id,
                severity=AlertSeverity.CRITICAL,
                violation_type=ViolationType.REVIVAL_DETECTED,
                message=(
                    f"Revival detected: content_hash {record.content_hash} "
                    f"was previously deleted (in tombstone)"
                ),
                evidence={"content_hash": record.content_hash},
            )
        return None

    def _check_content_conflict(self, record: MemoryRecord) -> Optional[IntegrityViolation]:
        """内容冲突:与同用户已有记忆矛盾(简单实现:相同关键词但不同结论)。

        启发式:若新记忆包含 "不是" / "取消" / "撤销" / "false" 等否定词,
        且与已有记忆相似度 > 0.4,则视为冲突(同主题但结论相反)。
        """
        # 否定词(中英文)
        negation_markers = ["不是", "取消", "撤销", "false", "wrong", "不正确", "已变更", "已废止"]
        if not any(m in record.content.lower() for m in negation_markers):
            return None

        with self._lock:
            chain = self._chains.get(record.user_id, [])
            # 找最相似的未删除记忆
            max_sim = 0.0
            conflict_record_id = ""
            for r in chain:
                if r.deleted or r.record_id == record.record_id:
                    continue
                sim = _text_similarity(record.content, r.content)
                if sim > max_sim:
                    max_sim = sim
                    conflict_record_id = r.record_id

        if max_sim > 0.4:
            return IntegrityViolation(
                user_id=record.user_id,
                session_id=record.session_id,
                record_id=record.record_id,
                severity=AlertSeverity.WARNING,
                violation_type=ViolationType.CONTENT_CONFLICT,
                message=(
                    f"Content conflict: new record contradicts existing "
                    f"record {conflict_record_id} (similarity={max_sim:.3f})"
                ),
                evidence={
                    "conflict_record_id": conflict_record_id,
                    "similarity": max_sim,
                },
            )
        return None

    # ==================================================================
    # 链完整性验证
    # ==================================================================

    def verify_chain(self, *, user_id: str) -> ChainVerificationResult:
        """校验 hash 链完整性(防篡改)。

        检查项:
            1. 每条 own_hash 是否正确
            2. 每条 content_hash 是否正确
            3. prev_hash 是否接续(链未断裂)
        """
        result = ChainVerificationResult(user_id=user_id)

        if not is_enabled("defense"):
            result.is_valid = True
            return result

        with self._lock:
            chain = list(self._chains.get(user_id, []))

        result.total_records = len(chain)
        result.deleted_records = sum(1 for r in chain if r.deleted)

        prev_hash = "0" * 16
        for r in chain:
            # 1. own_hash 校验
            if not r.verify_own_hash():
                result.is_valid = False
                result.broken_at = r.record_id
                result.violations.append(IntegrityViolation(
                    user_id=user_id,
                    record_id=r.record_id,
                    severity=AlertSeverity.CRITICAL,
                    violation_type=ViolationType.TAMPERING,
                    message=f"own_hash mismatch at record {r.record_id}",
                    evidence={"expected": r._compute_own_hash(), "actual": r.own_hash},
                ))
                break

            # 2. content_hash 校验
            if not r.verify_content_hash():
                result.is_valid = False
                result.broken_at = r.record_id
                result.violations.append(IntegrityViolation(
                    user_id=user_id,
                    record_id=r.record_id,
                    severity=AlertSeverity.CRITICAL,
                    violation_type=ViolationType.TAMPERING,
                    message=f"content_hash mismatch at record {r.record_id}",
                    evidence={"expected": r._compute_content_hash(), "actual": r.content_hash},
                ))
                break

            # 3. prev_hash 接续校验
            if r.prev_hash != prev_hash:
                result.is_valid = False
                result.broken_at = r.record_id
                result.violations.append(IntegrityViolation(
                    user_id=user_id,
                    record_id=r.record_id,
                    severity=AlertSeverity.CRITICAL,
                    violation_type=ViolationType.TAMPERING,
                    message=f"prev_hash broken at record {r.record_id}: expected {prev_hash}, got {r.prev_hash}",
                    evidence={"expected_prev": prev_hash, "actual_prev": r.prev_hash},
                ))
                break

            prev_hash = r.own_hash

        with self._lock:
            if result.is_valid:
                self._stats["chain_verifications_ok"] += 1
            else:
                self._stats["chain_verifications_broken"] += 1
                self._violations.extend(result.violations)

        return result

    # ==================================================================
    # 查询 / 审计接口
    # ==================================================================

    def get_chain(self, *, user_id: str, include_deleted: bool = True) -> list[MemoryRecord]:
        """获取用户记忆链(只读副本)。"""
        with self._lock:
            chain = list(self._chains.get(user_id, []))
        if not include_deleted:
            chain = [r for r in chain if not r.deleted]
        return chain

    def get_record(self, *, record_id: str, user_id: str) -> Optional[MemoryRecord]:
        """按 ID 查找记录。"""
        with self._lock:
            for r in self._chains.get(user_id, []):
                if r.record_id == record_id:
                    return r
        return None

    def list_violations(
        self,
        *,
        user_id: Optional[str] = None,
        severity: Optional[AlertSeverity] = None,
        limit: int = 100,
    ) -> list[IntegrityViolation]:
        """列出违规记录(可过滤)。"""
        with self._lock:
            results = list(self._violations)
        if user_id:
            results = [v for v in results if v.user_id == user_id]
        if severity:
            results = [v for v in results if v.severity == severity]
        return results[-limit:]

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        with self._lock:
            stats = dict(self._stats)
            stats["total_users"] = len(self._chains)
            stats["total_records"] = sum(len(c) for c in self._chains.values())
            stats["total_tombstones"] = sum(len(t) for t in self._tombstones.values())
            stats["total_violations"] = len(self._violations)
            return stats

    def list_users_over_threshold(
        self,
        *,
        min_violations: int = 5,
    ) -> list[dict[str, Any]]:
        """列出违规超过阈值的用户(用于看板)。"""
        with self._lock:
            per_user_count: dict[str, int] = defaultdict(int)
            for v in self._violations:
                per_user_count[v.user_id] += 1
        return [
            {"user_id": uid, "violation_count": cnt}
            for uid, cnt in sorted(per_user_count.items(), key=lambda x: -x[1])
            if cnt >= min_violations
        ]

    # ==================================================================
    # 内部
    # ==================================================================

    @staticmethod
    def _default_trust_for_source(source: MemorySource) -> TrustLevel:
        """根据来源推断默认 trust_level。"""
        if source in (MemorySource.USER, MemorySource.SYSTEM):
            return TrustLevel.HIGH
        if source in (MemorySource.AGENT, MemorySource.TOOL):
            return TrustLevel.MEDIUM
        if source == MemorySource.INFERRED:
            return TrustLevel.MEDIUM
        # EXTERNAL
        return TrustLevel.LOW

    def _save(self) -> None:
        """持久化到磁盘(若配置了 store_path)。"""
        if not self.store_path:
            return
        try:
            os.makedirs(os.path.dirname(self.store_path) or ".", exist_ok=True)
            with self._lock:
                data = {
                    "chains": {
                        uid: [r.to_dict() for r in chain]
                        for uid, chain in self._chains.items()
                    },
                    "tombstones": {
                        uid: list(ts) for uid, ts in self._tombstones.items()
                    },
                }
            with open(self.store_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save memory integrity store: %s", e)

    def _load(self) -> None:
        """从磁盘加载。"""
        if not self.store_path or not os.path.exists(self.store_path):
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                for uid, records_data in data.get("chains", {}).items():
                    chain: list[MemoryRecord] = []
                    for r_data in records_data:
                        # 还原枚举
                        r_data["source"] = MemorySource(r_data["source"])
                        r_data["trust_level"] = TrustLevel(r_data["trust_level"])
                        rec = MemoryRecord(**r_data)
                        chain.append(rec)
                    self._chains[uid] = chain
                for uid, ts_list in data.get("tombstones", {}).items():
                    self._tombstones[uid] = set(ts_list)
                # 重建 content_index
                for uid, chain in self._chains.items():
                    for r in chain:
                        self._content_index[r.content_hash].append(
                            (r.user_id, r.session_id, r.timestamp)
                        )
                # 重建 write_history
                for uid, chain in self._chains.items():
                    for r in chain:
                        self._write_history[uid].append((r.timestamp, r.session_id))
            logger.info("Loaded memory integrity store from %s", self.store_path)
        except Exception as e:
            logger.error("Failed to load memory integrity store: %s", e)


# =====================================================================
# 全局单例
# =====================================================================

_verifier_instance: Optional[MemoryIntegrityVerifier] = None
_verifier_lock = threading.RLock()


def get_memory_integrity_verifier() -> MemoryIntegrityVerifier:
    """获取全局 MemoryIntegrityVerifier 单例。"""
    global _verifier_instance
    with _verifier_lock:
        if _verifier_instance is None:
            _verifier_instance = MemoryIntegrityVerifier()
        return _verifier_instance


def reset_memory_integrity_verifier() -> None:
    """重置全局单例(测试用)。"""
    global _verifier_instance
    with _verifier_lock:
        _verifier_instance = None


__all__ = [
    "AlertSeverity",
    "ChainVerificationResult",
    "IntegrityViolation",
    "MemoryIntegrityVerifier",
    "MemoryRecord",
    "MemorySource",
    "TrustLevel",
    "ViolationType",
    "get_memory_integrity_verifier",
    "reset_memory_integrity_verifier",
]
