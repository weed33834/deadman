"""P4.5 Handoff 状态血缘 - 每次转交的不可篡改审计链

借鉴 append-only audit log / blockchain 的链式 hash 设计，把每次 Handoff
记录为一条审计事件，持久化到 `data/handoff_audit.jsonl`（append-only），
让平台能：
- 追溯一次会话内/跨会话的 agent 转交链（"血缘"）
- 通过上下文 hash 校验转交链完整性（任何中间篡改可被检测）
- 跨会话复现 handoff 决策路径（debug / 评测）

核心组件：
- HandoffAuditEntry: 单次 handoff 的审计记录（from/to/reason/时间戳/上下文 hash/前一条 hash）
- HandoffAuditLogger: append-only 写入器 + 链式 hash 校验 + 血缘查询

Feature flag: DEADMAN_HANDOFF_AUDIT_ENABLED=0 默认关闭
- 关闭时所有写操作（log_handoff）静默 no-op，读操作（verify_chain / get_chain /
  get_lineage）返回空，调用方走旧路径，行为完全不变
- 开启时所有操作生效；持久化用 append-only（open(..., "a") + 行级 JSON）

降级路径全覆盖：
1. feature flag 关闭 → 写 no-op / 读返回空
2. 持久化目录不可写 → 仅内存操作，记 warning 不抛异常
3. JSON 解析失败 → 跳过该行（append-only 容错）
4. 上下文 hash 计算失败 → 用空字符串占位，不阻塞审计写入
5. 链式校验遇到断链 → 返回 False，但提供详细 mismatch 信息便于诊断

设计要点：
- append-only：永不覆写已有行，仅追加新行（防篡改基础）
- 链式 hash：每条记录的 prev_hash 指向前一条的 curr_hash，
  任何中间修改都会让后续所有 hash 校验失败
- 上下文 hash：把 from/to/reason/context_variables/compressed_message
  打包做 SHA-256，便于检测转交内容是否被中途篡改
- 轻量级：不引入 cryptography 等重依赖，仅用 hashlib（标准库）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
HANDOFF_AUDIT_ENABLED: bool = os.environ.get(
    "DEADMAN_HANDOFF_AUDIT_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 持久化文件路径（相对 project_root）
DEFAULT_AUDIT_PATH = "data/handoff_audit.jsonl"

# 链式 hash 用的固定字段顺序（保证 hash 跨版本稳定）
_HASH_FIELD_ORDER: tuple[str, ...] = (
    "transfer_id",
    "from_agent",
    "to_agent",
    "reason",
    "compressed_message",
    "context_variables_hash",
    "created_at",
    "prev_hash",
)


# =====================================================================
# 数据模型
# =====================================================================


@dataclass
class HandoffAuditEntry:
    """单次 handoff 的审计记录

    Attributes:
        transfer_id: 与 HandoffContext.transfer_id 一致（关联 handoff 主体）
        from_agent: 来源智能体名
        to_agent: 目标智能体名
        reason: 转交原因
        compressed_message: LLM 压缩后的消息摘要（用于复现/调试）
        context_variables_hash: context_variables 的 SHA-256（不存原始值，避免 PII 泄露）
        created_at: ISO8601 时间戳
        prev_hash: 前一条审计记录的 curr_hash（链式 hash）；
                   首条记录为 "0" * 64（genesis prev_hash）
        curr_hash: 本条记录的 SHA-256（基于上述字段计算，链式校验用）

    Note:
        context_variables 仅存 hash 不存原始值，避免 PII 落盘到审计日志
        （审计日志通常保留期更长，PII 风险更高）
    """

    transfer_id: str
    from_agent: str
    to_agent: str
    reason: str
    compressed_message: str
    context_variables_hash: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    prev_hash: str = "0" * 64  # genesis prev_hash（64 个 0，对齐 SHA-256 hex 长度）
    curr_hash: str = ""  # 由 _compute_curr_hash 填充

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的 dict"""
        return {
            "transfer_id": self.transfer_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "reason": self.reason,
            "compressed_message": self.compressed_message,
            "context_variables_hash": self.context_variables_hash,
            "created_at": self.created_at,
            "prev_hash": self.prev_hash,
            "curr_hash": self.curr_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HandoffAuditEntry":
        """从 dict 反序列化（容错：缺失字段填默认）"""
        return cls(
            transfer_id=str(data.get("transfer_id", "")),
            from_agent=str(data.get("from_agent", "")),
            to_agent=str(data.get("to_agent", "")),
            reason=str(data.get("reason", "")),
            compressed_message=str(data.get("compressed_message", "")),
            context_variables_hash=str(data.get("context_variables_hash", "")),
            created_at=str(data.get("created_at", "")),
            prev_hash=str(data.get("prev_hash", "0" * 64)),
            curr_hash=str(data.get("curr_hash", "")),
        )


# =====================================================================
# Hash 计算
# =====================================================================


def _compute_context_hash(context_variables: dict[str, Any] | None) -> str:
    """计算 context_variables 的 SHA-256 hex

    - 把 dict 序列化为 JSON（sort_keys=True 保证稳定）再 hash
    - 失败时返回空字符串（不阻塞审计写入）

    Args:
        context_variables: 跨 agent 传递的上下文变量

    Returns:
        64 字符 hex 字符串；输入为空返回空字符串
    """
    if not context_variables:
        return ""
    try:
        # sort_keys + ensure_ascii 让 hash 跨平台稳定
        payload = json.dumps(
            context_variables, sort_keys=True, ensure_ascii=False, default=str
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
    except (TypeError, ValueError) as e:
        logger.debug("计算 context_variables hash 失败: %s", e)
        return ""


def _compute_curr_hash(entry: HandoffAuditEntry) -> str:
    """计算本条记录的 curr_hash（链式 hash）

    hash 输入 = 按固定字段顺序拼接的字段值（不含 curr_hash 本身）
    保证同一组字段值产生相同 hash，便于链式校验。

    Args:
        entry: 已填好 prev_hash 的审计记录

    Returns:
        64 字符 hex 字符串
    """
    data = entry.to_dict()
    # 按 _HASH_FIELD_ORDER 顺序拼接字段值
    parts: list[str] = []
    for field_name in _HASH_FIELD_ORDER:
        parts.append(str(data.get(field_name, "")))
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# =====================================================================
# HandoffAuditLogger
# =====================================================================


class HandoffAuditLogger:
    """Handoff 审计日志写入器 - append-only + 链式 hash + 血缘查询

    所有写操作在 HANDOFF_AUDIT_ENABLED=False 时静默 no-op。
    所有读操作在 HANDOFF_AUDIT_ENABLED=False 时返回空（[]/False/None）。
    """

    def __init__(self, persist_path: str | Path | None = None):
        """Args:
            persist_path: 持久化文件路径；None 用默认 data/handoff_audit.jsonl
        """
        if persist_path is None:
            self._path = settings.project_root / DEFAULT_AUDIT_PATH
        else:
            self._path = Path(persist_path)
        # 内存缓存：最近一条记录的 curr_hash（链式 hash 用）
        # 启动时尝试从磁盘加载最后一条记录，恢复链状态
        self._last_hash: str = "0" * 64
        self._load_last_hash()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_last_hash(self) -> None:
        """从磁盘加载最后一条记录的 curr_hash（恢复链状态）

        失败时保持 _last_hash = "0" * 64（视为空链）
        """
        if not self._path.exists():
            return
        try:
            # 逐行读，取最后一条合法 JSON 行的 curr_hash
            last_hash = "0" * 64
            with open(self._path, "r", encoding="utf-8") as f:
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
            logger.warning("加载 handoff_audit.jsonl 失败: %s", e)

    def _append(self, entry: HandoffAuditEntry) -> bool:
        """原子追加一条记录到 jsonl 文件

        使用 open(..., "a") 模式追加（append-only，永不覆写）。
        失败时仅 warning，不抛异常。

        Returns:
            True 表示成功写入；False 表示写入失败
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry.to_dict(), ensure_ascii=False)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError as e:
            logger.warning("handoff_audit 追加失败（仅内存）: %s", e)
            return False

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def log_handoff(
        self,
        transfer_id: str,
        from_agent: str,
        to_agent: str,
        reason: str,
        compressed_message: str = "",
        context_variables: dict[str, Any] | None = None,
    ) -> HandoffAuditEntry | None:
        """记录一次 handoff 到审计链

        Args:
            transfer_id: 与 HandoffContext.transfer_id 一致
            from_agent: 来源智能体名
            to_agent: 目标智能体名
            reason: 转交原因
            compressed_message: LLM 压缩后的消息摘要
            context_variables: 跨 agent 传递的上下文（仅 hash 落盘，原始值不存）

        Returns:
            HandoffAuditEntry 实例；feature flag 关闭时返回 None（调用方走旧路径）

        降级路径：
        1. HANDOFF_AUDIT_ENABLED=False → 返回 None
        2. context_variables 不可序列化 → context_variables_hash 退化为 ""
        3. 文件追加失败 → 仅内存更新 _last_hash，不抛异常
        """
        if not HANDOFF_AUDIT_ENABLED:
            logger.debug(
                "handoff audit disabled (DEADMAN_HANDOFF_AUDIT_ENABLED=0), skip"
            )
            return None

        # 计算上下文 hash（不存原始 context_variables，避免 PII 落盘）
        ctx_hash = _compute_context_hash(context_variables)

        # 构造审计条目（prev_hash 链接到上一条）
        entry = HandoffAuditEntry(
            transfer_id=transfer_id or str(uuid.uuid4()),
            from_agent=from_agent,
            to_agent=to_agent,
            reason=reason,
            compressed_message=compressed_message,
            context_variables_hash=ctx_hash,
            prev_hash=self._last_hash,
        )
        # 计算 curr_hash（基于 transfer_id/from/to/reason/ctx_hash/created_at/prev_hash）
        entry.curr_hash = _compute_curr_hash(entry)

        # 追加到磁盘（失败仅 warning，不影响内存链状态推进）
        self._append(entry)

        # 更新内存中的 last_hash（无论磁盘是否成功，内存链向前推进）
        self._last_hash = entry.curr_hash

        logger.info(
            "handoff audit logged: %s -> %s (transfer_id=%s, curr_hash=%s...)",
            from_agent, to_agent, entry.transfer_id, entry.curr_hash[:8],
        )
        return entry

    # ------------------------------------------------------------------
    # 链式校验
    # ------------------------------------------------------------------

    def verify_chain(self) -> bool:
        """校验审计链完整性

        规则：
        - 每条记录的 prev_hash 必须等于前一条的 curr_hash
        - 每条记录的 curr_hash 必须等于按字段重算的 hash
        - 首条记录的 prev_hash 必须为 "0" * 64

        Returns:
            True 表示链完整；False 表示有篡改或断链；
            feature flag 关闭返回 False（空链视为未启用）
        """
        if not HANDOFF_AUDIT_ENABLED:
            return False
        entries = self._load_all_entries()
        if not entries:
            return False  # 空链
        prev_hash = "0" * 64
        for i, entry in enumerate(entries):
            # 1. prev_hash 链接校验
            if entry.prev_hash != prev_hash:
                logger.warning(
                    "handoff audit chain broken at index %d: prev_hash mismatch "
                    "(expected %s..., got %s...)",
                    i, prev_hash[:8], entry.prev_hash[:8],
                )
                return False
            # 2. curr_hash 重算校验
            recomputed = _compute_curr_hash(entry)
            if entry.curr_hash != recomputed:
                logger.warning(
                    "handoff audit chain broken at index %d: curr_hash mismatch "
                    "(expected %s..., got %s...)",
                    i, recomputed[:8], entry.curr_hash[:8],
                )
                return False
            prev_hash = entry.curr_hash
        return True

    # ------------------------------------------------------------------
    # 血缘查询
    # ------------------------------------------------------------------

    def get_chain(self) -> list[HandoffAuditEntry]:
        """返回完整审计链（按时间顺序）

        feature flag 关闭返回 []。
        """
        if not HANDOFF_AUDIT_ENABLED:
            return []
        return self._load_all_entries()

    def get_lineage(
        self, transfer_id: str | None = None, agent_name: str | None = None
    ) -> list[HandoffAuditEntry]:
        """查询血缘：按 transfer_id 或 agent_name 过滤

        - transfer_id 给定 → 返回该次 handoff 的单条记录
        - agent_name 给定 → 返回该 agent 参与的所有 handoff（from 或 to）
        - 同时给定 → 取交集
        - 都不给定 → 返回完整链

        Args:
            transfer_id: handoff 转交 ID
            agent_name: agent 名（匹配 from_agent 或 to_agent）

        Returns:
            匹配的审计记录列表；feature flag 关闭返回 []
        """
        if not HANDOFF_AUDIT_ENABLED:
            return []
        entries = self._load_all_entries()
        results: list[HandoffAuditEntry] = []
        for e in entries:
            if transfer_id and e.transfer_id != transfer_id:
                continue
            if agent_name and e.from_agent != agent_name and e.to_agent != agent_name:
                continue
            results.append(e)
        return results

    def get_lineage_chain_for_agent(self, agent_name: str) -> list[HandoffAuditEntry]:
        """查询某 agent 的转交链（按时间顺序，包含入向和出向）

        用于回答"agent X 是怎么被卷入这次会话的"。

        Args:
            agent_name: 目标 agent 名

        Returns:
            按时间顺序的审计记录列表（from 或 to 等于 agent_name）；
            feature flag 关闭返回 []
        """
        if not HANDOFF_AUDIT_ENABLED:
            return []
        return [
            e for e in self._load_all_entries()
            if e.from_agent == agent_name or e.to_agent == agent_name
        ]

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _load_all_entries(self) -> list[HandoffAuditEntry]:
        """从磁盘加载全部审计记录（按文件顺序）

        容错：跳过损坏的行（append-only 容错）
        """
        if not self._path.exists():
            return []
        entries: list[HandoffAuditEntry] = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict):
                            entries.append(HandoffAuditEntry.from_dict(data))
                    except json.JSONDecodeError:
                        # 跳过损坏行
                        continue
        except OSError as e:
            logger.warning("读取 handoff_audit.jsonl 失败: %s", e)
            return []
        return entries

    def count(self) -> int:
        """返回审计记录总数（feature flag 关闭返回 0）"""
        if not HANDOFF_AUDIT_ENABLED:
            return 0
        return len(self._load_all_entries())

    def clear(self) -> None:
        """清空审计日志（主要用于测试）

        注意：清空会破坏链式 hash，仅用于测试场景重置。
        """
        self._last_hash = "0" * 64
        if not HANDOFF_AUDIT_ENABLED:
            return
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError as e:
            logger.warning("清空 handoff_audit.jsonl 失败: %s", e)


# =====================================================================
# 全局单例（延迟初始化，避免 import 时读盘）
# =====================================================================

_logger_instance: HandoffAuditLogger | None = None


def get_handoff_audit_logger() -> HandoffAuditLogger:
    """获取全局 HandoffAuditLogger 单例"""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = HandoffAuditLogger()
    return _logger_instance


def reset_handoff_audit_logger() -> None:
    """重置全局单例（主要用于测试）

    下次 get_handoff_audit_logger() 会重新构造实例，从磁盘重新加载 last_hash。
    """
    global _logger_instance
    _logger_instance = None
