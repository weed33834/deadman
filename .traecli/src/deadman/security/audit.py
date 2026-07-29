"""P5.1 审计链（append-only）- 平台所有关键操作的不可篡改审计日志

借鉴 append-only audit log / blockchain 的链式 hash 设计，把每次关键事件
（工具调用 / 规则触发 / 转介 / Handoff / PII 脱敏 / 安全告警）记录为一条
审计事件，持久化到 `data/audit.jsonl`（append-only），让平台能：
- 追溯任何关键操作的执行链（谁/何时/对什么/做了什么）
- 通过链式 hash 校验审计链完整性（任何中间篡改可被检测）
- 按事件类型 / actor / target / 时间范围查询历史审计

核心组件：
- AuditEvent: 单条审计事件（event_id/event_type/actor/action/target/timestamp/
  metadata/prev_hash/curr_hash）
- AuditChain: append-only 写入器 + 链式 hash 校验 + 多维查询

Feature flag: DEADMAN_AUDIT_CHAIN_ENABLED=0 默认关闭
- 关闭时所有写操作（append）静默 no-op（返回 None），
  读操作（query/verify_chain）返回空，调用方走旧路径，行为完全不变
- 开启时所有操作生效；持久化用 append-only（open(..., "a") + 行级 JSON）

降级路径全覆盖：
1. feature flag 关闭 → 写 no-op / 读返回空
2. 持久化目录不可写 → 仅内存操作，记 warning 不抛异常
3. JSON 解析失败 → 跳过该行（append-only 容错）
4. metadata 不可序列化 → 用 default=str 兜底，不阻塞审计写入
5. 链式校验遇到断链 → 返回 False，但提供详细 mismatch 信息便于诊断

设计要点：
- append-only：永不覆写已有行，仅追加新行（防篡改基础）
- 链式 hash：每条记录的 prev_hash 指向前一条的 curr_hash，
  任何中间修改都会让后续所有 hash 校验失败
- 仅用 hashlib（标准库），不引入 cryptography 等重依赖
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
AUDIT_CHAIN_ENABLED: bool = os.environ.get(
    "DEADMAN_AUDIT_CHAIN_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 持久化文件路径：
# settings.project_root 是 .traecli/，其 parent 是 /workspace/deadman/，
# 因此 audit.jsonl 落在 /workspace/deadman/data/audit.jsonl
DEFAULT_AUDIT_PATH = Path("data") / "audit.jsonl"

# 链式 hash 用的固定字段顺序（保证 hash 跨版本稳定）
_HASH_FIELD_ORDER: tuple[str, ...] = (
    "event_id",
    "event_type",
    "actor",
    "action",
    "target",
    "timestamp",
    "prev_hash",
    "metadata",
)

# 支持的事件类型（开放枚举，未列出的事件类型仍可写入，仅用于文档/校验提示）
AUDIT_EVENT_TYPES: frozenset[str] = frozenset({
    "tool_call",
    "rule_triggered",
    "transfer",
    "handoff",
    "pii_sanitized",
    "security_alert",
})

# 链起始 prev_hash（64 个 0，对齐 SHA-256 hex 长度）
GENESIS_HASH = "0" * 64


# =====================================================================
# 数据模型
# =====================================================================


@dataclass
class AuditEvent:
    """单条审计事件

    Attributes:
        event_id: 事件唯一 ID（默认自动生成 uuid4 hex）
        event_type: 事件类型（tool_call/rule_triggered/transfer/handoff/
                    pii_sanitized/security_alert，或其他自定义类型）
        actor: 触发事件的主体（user/agent_name/system/tool_name 等）
        action: 具体动作描述（如 "call_tool" / "trigger_rule" / "sanitize_pii"）
        target: 动作对象（如工具名/规则名/字段名，可为空）
        timestamp: ISO8601 时间戳
        metadata: 附加元数据（任意 dict，会参与 hash 计算）
        prev_hash: 前一条审计记录的 curr_hash（链式 hash）；
                   首条记录为 "0" * 64（genesis prev_hash）
        curr_hash: 本条记录的 SHA-256（基于上述字段计算，链式校验用）
    """

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_type: str = ""
    actor: str = ""
    action: str = ""
    target: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    curr_hash: str = ""  # 由 compute_hash 填充

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的 dict"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "prev_hash": self.prev_hash,
            "curr_hash": self.curr_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditEvent:
        """从 dict 反序列化（容错：缺失字段填默认）"""
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {"_raw": str(metadata)}
        return cls(
            event_id=str(data.get("event_id", "")),
            event_type=str(data.get("event_type", "")),
            actor=str(data.get("actor", "")),
            action=str(data.get("action", "")),
            target=str(data.get("target", "")),
            timestamp=str(data.get("timestamp", "")),
            metadata=metadata,
            prev_hash=str(data.get("prev_hash", GENESIS_HASH)),
            curr_hash=str(data.get("curr_hash", "")),
        )


# =====================================================================
# Hash 计算
# =====================================================================


def compute_hash(event: AuditEvent) -> str:
    """计算本条事件的 curr_hash（链式 hash）

    hash 输入 = event_id + event_type + actor + action + target + timestamp +
                prev_hash + json(metadata)
    保证同一组字段值产生相同 hash，便于链式校验。

    Args:
        event: 已填好 prev_hash 的审计事件

    Returns:
        64 字符 hex 字符串
    """
    try:
        metadata_json = json.dumps(
            event.metadata, sort_keys=True, ensure_ascii=False, default=str
        )
    except (TypeError, ValueError) as e:
        logger.debug("metadata 序列化失败，退化为空: %s", e)
        metadata_json = ""
    parts = [
        str(event.event_id),
        str(event.event_type),
        str(event.actor),
        str(event.action),
        str(event.target),
        str(event.timestamp),
        str(event.prev_hash),
        metadata_json,
    ]
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# =====================================================================
# AuditChain
# =====================================================================


class AuditChain:
    """审计链写入器 - append-only + 链式 hash + 多维查询

    所有写操作在 AUDIT_CHAIN_ENABLED=False 时静默 no-op（返回 None）。
    所有读操作在 AUDIT_CHAIN_ENABLED=False 时返回空（[]/False）。
    """

    def __init__(self, persist_path: str | Path | None = None):
        """Args:
            persist_path: 持久化文件路径；None 用默认 data/audit.jsonl
                          （相对 settings.project_root.parent，即 /workspace/deadman/）
        """
        if persist_path is None:
            # settings.project_root 是 .traecli/，parent 是 /workspace/deadman/
            self._path = settings.project_root.parent / DEFAULT_AUDIT_PATH
        else:
            self._path = Path(persist_path)
        # 内存缓存：最近一条记录的 curr_hash（链式 hash 用）
        # 启动时尝试从磁盘加载最后一条记录，恢复链状态
        self._last_hash: str = GENESIS_HASH
        self._load_last_hash()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_last_hash(self) -> None:
        """从磁盘加载最后一条记录的 curr_hash（恢复链状态）

        失败时保持 _last_hash = GENESIS_HASH（视为空链）
        """
        if not self._path.exists():
            return
        try:
            last_hash = GENESIS_HASH
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict) and data.get("curr_hash"):
                            last_hash = str(data["curr_hash"])
                    except json.JSONDecodeError:
                        # 跳过损坏行（append-only 容错）
                        continue
            self._last_hash = last_hash
        except OSError as e:
            logger.warning("加载 audit.jsonl 失败: %s", e)

    def _append_to_disk(self, event: AuditEvent) -> bool:
        """原子追加一条记录到 jsonl 文件

        使用 open(..., "a") 模式追加（append-only，永不覆写）。
        失败时仅 warning，不抛异常。

        Returns:
            True 表示成功写入；False 表示写入失败
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError as e:
            logger.warning("audit chain 追加失败（仅内存）: %s", e)
            return False

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def append(
        self,
        event_type: str,
        actor: str = "",
        action: str = "",
        target: str = "",
        metadata: dict[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: str | None = None,
    ) -> AuditEvent | None:
        """追加一条审计事件到链

        Args:
            event_type: 事件类型（tool_call/rule_triggered/transfer/handoff/
                        pii_sanitized/security_alert 等）
            actor: 触发主体
            action: 动作描述
            target: 动作对象
            metadata: 附加元数据
            event_id: 事件 ID（None 自动生成 uuid4 hex）
            timestamp: 时间戳（None 自动用 datetime.now().isoformat()）

        Returns:
            AuditEvent 实例（已写入磁盘或仅内存）；feature flag 关闭时返回 None

        降级路径：
        1. AUDIT_CHAIN_ENABLED=False → 返回 None
        2. 文件追加失败 → 仅内存更新 _last_hash，不抛异常
        3. metadata 不可序列化 → compute_hash 内部用 default=str 兜底
        """
        if not AUDIT_CHAIN_ENABLED:
            logger.debug(
                "audit chain disabled (DEADMAN_AUDIT_CHAIN_ENABLED=0), skip"
            )
            return None

        event = AuditEvent(
            event_id=event_id or uuid.uuid4().hex,
            event_type=event_type,
            actor=actor,
            action=action,
            target=target,
            timestamp=timestamp or datetime.now().isoformat(),
            metadata=metadata or {},
            prev_hash=self._last_hash,
        )
        event.curr_hash = compute_hash(event)

        # 追加到磁盘（失败仅 warning，不影响内存链状态推进）
        self._append_to_disk(event)

        # 更新内存中的 last_hash（无论磁盘是否成功，内存链向前推进）
        self._last_hash = event.curr_hash

        logger.info(
            "audit event appended: type=%s actor=%s action=%s (event_id=%s, curr_hash=%s...)",
            event.event_type, event.actor, event.action,
            event.event_id, event.curr_hash[:8],
        )
        return event

    def append_event(self, event: AuditEvent) -> str | None:
        """追加一个已构造的 AuditEvent 到链（高级用法）

        会重算 prev_hash（用链当前 last_hash）和 curr_hash，保证链式衔接。

        Args:
            event: 预构造的事件（prev_hash/curr_hash 会被覆盖）

        Returns:
            event.event_id；feature flag 关闭时返回 None
        """
        if not AUDIT_CHAIN_ENABLED:
            return None
        event.prev_hash = self._last_hash
        event.curr_hash = compute_hash(event)
        self._append_to_disk(event)
        self._last_hash = event.curr_hash
        return event.event_id

    # ------------------------------------------------------------------
    # 链式校验
    # ------------------------------------------------------------------

    def verify_chain(self) -> bool:
        """校验审计链完整性

        规则：
        - 每条记录的 prev_hash 必须等于前一条的 curr_hash
        - 每条记录的 curr_hash 必须等于按字段重算的 hash
        - 首条记录的 prev_hash 必须为 GENESIS_HASH

        Returns:
            True 表示链完整；False 表示有篡改或断链；
            feature flag 关闭返回 False（空链视为未启用）
        """
        if not AUDIT_CHAIN_ENABLED:
            return False
        entries = self._load_all_events()
        if not entries:
            return False  # 空链
        prev_hash = GENESIS_HASH
        for i, event in enumerate(entries):
            # 1. prev_hash 链接校验
            if event.prev_hash != prev_hash:
                logger.warning(
                    "audit chain broken at index %d: prev_hash mismatch "
                    "(expected %s..., got %s...)",
                    i, prev_hash[:8], event.prev_hash[:8],
                )
                return False
            # 2. curr_hash 重算校验
            recomputed = compute_hash(event)
            if event.curr_hash != recomputed:
                logger.warning(
                    "audit chain broken at index %d: curr_hash mismatch "
                    "(expected %s..., got %s...)",
                    i, recomputed[:8], event.curr_hash[:8],
                )
                return False
            prev_hash = event.curr_hash
        return True

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query(
        self,
        event_type: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        since: str | None = None,
    ) -> list[AuditEvent]:
        """多维查询审计事件

        - event_type 给定 → 按事件类型过滤
        - actor 给定 → 按 actor 过滤
        - target 给定 → 按 target 过滤
        - since 给定 → 返回 timestamp >= since 的事件（ISO8601 字符串字典序比较）
        - 多条件同时给定 → 取交集
        - 都不给定 → 返回完整链

        Args:
            event_type: 事件类型
            actor: 触发主体
            target: 动作对象
            since: ISO8601 时间戳下界（含）

        Returns:
            匹配的审计事件列表（按文件顺序）；feature flag 关闭返回 []
        """
        if not AUDIT_CHAIN_ENABLED:
            return []
        entries = self._load_all_events()
        results: list[AuditEvent] = []
        for e in entries:
            if event_type is not None and e.event_type != event_type:
                continue
            if actor is not None and e.actor != actor:
                continue
            if target is not None and e.target != target:
                continue
            if since is not None and e.timestamp < since:
                continue
            results.append(e)
        return results

    def get_chain(self) -> list[AuditEvent]:
        """返回完整审计链（按时间顺序）

        feature flag 关闭返回 []。
        """
        if not AUDIT_CHAIN_ENABLED:
            return []
        return self._load_all_events()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _load_all_events(self) -> list[AuditEvent]:
        """从磁盘加载全部审计事件（按文件顺序）

        容错：跳过损坏的行（append-only 容错）
        """
        if not self._path.exists():
            return []
        events: list[AuditEvent] = []
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict):
                            events.append(AuditEvent.from_dict(data))
                    except json.JSONDecodeError:
                        # 跳过损坏行
                        continue
        except OSError as e:
            logger.warning("读取 audit.jsonl 失败: %s", e)
            return []
        return events

    def count(self) -> int:
        """返回审计事件总数（feature flag 关闭返回 0）"""
        if not AUDIT_CHAIN_ENABLED:
            return 0
        return len(self._load_all_events())

    def clear(self) -> None:
        """清空审计日志（主要用于测试）

        注意：清空会破坏链式 hash，仅用于测试场景重置。
        """
        self._last_hash = GENESIS_HASH
        if not AUDIT_CHAIN_ENABLED:
            return
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError as e:
            logger.warning("清空 audit.jsonl 失败: %s", e)


# =====================================================================
# 全局单例（延迟初始化，避免 import 时读盘）
# =====================================================================

_chain_instance: AuditChain | None = None


def get_audit_chain() -> AuditChain:
    """获取全局 AuditChain 单例"""
    global _chain_instance
    if _chain_instance is None:
        _chain_instance = AuditChain()
    return _chain_instance


def reset_audit_chain() -> None:
    """重置全局单例（主要用于测试）

    下次 get_audit_chain() 会重新构造实例，从磁盘重新加载 last_hash。
    """
    global _chain_instance
    _chain_instance = None
