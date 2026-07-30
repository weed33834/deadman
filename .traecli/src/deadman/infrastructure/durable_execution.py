"""P7.6 Durable Execution - 幂等键 + 崩溃恢复 + 重放。

借鉴 Temporal/Cadence 的 Durable Execution 模式:

    1. 幂等键(idempotency_key):
        - 每个副作用节点(write_file/init_transfer/execute_code)启动前生成 idempotency_key
        - 副作用执行前先查 idempotency_log,已执行则直接返回缓存结果
        - 防止崩溃重放或重试时重复执行(如重复转账)

    2. 检查点(checkpoint):
        - LangGraph 已有 SqliteSaver,但需扩展记录副作用节点的执行状态
        - 每个副作用节点完成后写 durable_log(append-only,JSONL)
        - 崩溃恢复时:从 checkpoint 加载 state → 跳过 durable_log 中已执行的副作用

    3. 重放(replay):
        - 给定 trace_id,可重放整个执行图
        - 副作用节点不实际执行,直接从 durable_log 取结果(读模式)
        - 用于调试 + 测试 prompt 变更影响

    4. 超时补偿(saga):
        - 长时间副作用(如等待银行回调)配 saga_pattern
        - 失败时执行补偿动作(回滚)

feature flag:`DEADMAN_DURABLE_EXECUTION_ENABLED=0` 默认关闭。
关闭时副作用节点直接执行(原有行为),不记录 durable_log。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Self

from .feature_flags import is_enabled

logger = logging.getLogger(__name__)


# durable log 文件位置(append-only JSONL)
DEFAULT_DURABLE_LOG = Path(os.environ.get("DEADMAN_DURABLE_LOG", "data/durable_log.jsonl"))


class ExecutionStatus(str, Enum):
    """副作用节点执行状态。"""

    STARTED = "started"  # 已开始,未完成
    COMPLETED = "completed"  # 成功完成
    FAILED = "failed"  # 执行失败
    COMPENSATED = "compensated"  # 已执行补偿动作


@dataclass
class DurableRecord:
    """单条 durable log 记录(append-only)。"""

    record_id: str  # 自增 ID(便于追溯)
    idempotency_key: str  # 业务幂等键(防止重复执行)
    trace_id: str  # 关联 trace
    node_name: str  # 节点名(如 "write_file" / "init_transfer")
    status: ExecutionStatus
    args_hash: str  # 参数哈希(检测参数变化)
    result: Any = None  # 执行结果(完成时填充)
    error: str | None = None  # 失败原因
    started_at: float = 0.0
    completed_at: float = 0.0
    duration_ms: float = 0.0
    saga_compensation: str | None = None  # 补偿动作描述


class DurableExecutionError(Exception):
    """Durable Execution 异常(如幂等键冲突)。"""


class DurableExecutionManager:
    """Durable Execution 管理器。

    用法:
        dm = DurableExecutionManager()
        idem_key = dm.generate_idempotency_key("write_file", {"path": "...", "content": "..."})

        # 检查是否已执行
        existing = dm.lookup(idem_key)
        if existing and existing.status == ExecutionStatus.COMPLETED:
            return existing.result  # 直接返回缓存结果

        # 否则执行(并自动记录)
        with dm.execution_scope(idem_key, trace_id="t1", node_name="write_file"):
            result = do_real_write(...)
            return result
    """

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path or DEFAULT_DURABLE_LOG
        self._lock = threading.RLock()
        # 内存索引:{idempotency_key: DurableRecord} 启动时懒加载
        self._index: dict[str, DurableRecord] = {}
        self._loaded = False

    # ==================================================================
    # 幂等键生成
    # ==================================================================

    @staticmethod
    def generate_idempotency_key(
        node_name: str,
        args: dict,
        salt: str | None = None,
    ) -> str:
        """生成幂等键 - 基于 node_name + args 哈希。

        同 node_name + 同 args → 同 key(幂等)
        同 node_name + 不同 args → 不同 key(允许重新执行)
        """
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        raw = f"{node_name}:{args_str}"
        if salt:
            raw = f"{raw}:{salt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def generate_trace_id() -> str:
        """生成 trace_id(UUID v4)。"""
        return str(uuid.uuid4())

    # ==================================================================
    # 查询
    # ==================================================================

    def lookup(self, idempotency_key: str) -> DurableRecord | None:
        """查询幂等键的执行记录。"""
        if not is_enabled("durable_execution"):
            return None
        with self._lock:
            self._load()
            return self._index.get(idempotency_key)

    def lookup_by_trace(self, trace_id: str) -> list[DurableRecord]:
        """查询某 trace 的所有执行记录(用于重放)。"""
        if not is_enabled("durable_execution"):
            return []
        with self._lock:
            self._load()
            return [r for r in self._index.values() if r.trace_id == trace_id]

    # ==================================================================
    # 记录
    # ==================================================================

    def record_start(
        self,
        idempotency_key: str,
        trace_id: str,
        node_name: str,
        args: dict,
    ) -> DurableRecord:
        """记录副作用开始(写 durable_log)。

        若已存在 COMPLETED 记录,直接返回(幂等)。
        若已存在 STARTED 记录(未完成),抛异常(可能崩溃了)。
        """
        with self._lock:
            self._load()
            existing = self._index.get(idempotency_key)
            if existing:
                if existing.status == ExecutionStatus.COMPLETED:
                    # 幂等:已执行,返回缓存
                    return existing
                if existing.status == ExecutionStatus.STARTED:
                    # 可能崩溃了,标记 FAILED(由调用方决定是否重试)
                    logger.warning(
                        "Durable record %s in STARTED state, possible crash. Marking FAILED.",
                        idempotency_key,
                    )
                    existing.status = ExecutionStatus.FAILED
                    existing.error = "previous_start_did_not_complete"
                    self._update_record(existing)

            # 写新 STARTED 记录
            record = DurableRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                trace_id=trace_id,
                node_name=node_name,
                status=ExecutionStatus.STARTED,
                args_hash=self._hash_args(args),
                started_at=time.time(),
            )
            self._append_record(record)
            self._index[idempotency_key] = record
            return record

    def record_complete(
        self,
        idempotency_key: str,
        result: Any = None,
        error: str | None = None,
    ) -> DurableRecord:
        """记录副作用完成(成功/失败)。"""
        with self._lock:
            self._load()
            record = self._index.get(idempotency_key)
            if record is None:
                # 没有 STARTED 记录(可能是 feature flag 切换中)
                logger.warning("record_complete: no STARTED record for %s", idempotency_key)
                record = DurableRecord(
                    record_id=str(uuid.uuid4()),
                    idempotency_key=idempotency_key,
                    trace_id="unknown",
                    node_name="unknown",
                    status=ExecutionStatus.STARTED,
                    args_hash="",
                    started_at=time.time(),
                )
                self._index[idempotency_key] = record

            record.status = ExecutionStatus.FAILED if error else ExecutionStatus.COMPLETED
            record.result = result
            record.error = error
            record.completed_at = time.time()
            record.duration_ms = (record.completed_at - record.started_at) * 1000
            self._update_record(record)
            return record

    def record_compensation(
        self,
        idempotency_key: str,
        compensation_action: str,
    ) -> DurableRecord | None:
        """记录补偿动作(用于 saga 模式)。"""
        with self._lock:
            self._load()
            record = self._index.get(idempotency_key)
            if record is None:
                return None
            record.status = ExecutionStatus.COMPENSATED
            record.saga_compensation = compensation_action
            self._update_record(record)
            return record

    # ==================================================================
    # 上下文管理器 - 简化用法
    # ==================================================================

    class _ExecutionScope:
        """execution_scope 上下文管理器。"""

        def __init__(
            self,
            manager: DurableExecutionManager,
            idempotency_key: str,
            trace_id: str,
            node_name: str,
            args: dict,
        ) -> None:
            self.manager = manager
            self.idempotency_key = idempotency_key
            self.trace_id = trace_id
            self.node_name = node_name
            self.args = args
            self.record: DurableRecord | None = None
            self.cached_result: Any = None
            self.is_cached: bool = False

        def __enter__(self) -> Self:
            if not is_enabled("durable_execution"):
                # feature flag 关闭:不做任何事,直接执行
                return self

            self.record = self.manager.record_start(
                self.idempotency_key,
                self.trace_id,
                self.node_name,
                self.args,
            )
            # 如果是已 COMPLETED 的记录(幂等命中),标记 cached
            if self.record.status == ExecutionStatus.COMPLETED:
                self.is_cached = True
                self.cached_result = self.record.result
            return self

        def __exit__(self, exc_type, exc_val, exc_tb) -> None:
            if not is_enabled("durable_execution"):
                return
            if self.is_cached:
                # 幂等命中,不重复记录
                return

            if exc_type is None:
                # 成功完成(result 由 caller 通过 set_result 设置)
                self.manager.record_complete(self.idempotency_key, result=self.cached_result)
            else:
                # 失败
                self.manager.record_complete(
                    self.idempotency_key,
                    error=f"{exc_type.__name__}: {exc_val}",
                )
            return  # 不吞异常,继续抛出

        def set_result(self, result: Any) -> None:
            """设置执行结果(在 with 块内调用)。"""
            self.cached_result = result

    def execution_scope(
        self,
        idempotency_key: str,
        trace_id: str,
        node_name: str,
        args: dict,
    ) -> DurableExecutionManager._ExecutionScope:
        """创建执行作用域(上下文管理器)。

        用法:
            with dm.execution_scope(key, trace_id, "write_file", args) as scope:
                if scope.is_cached:
                    return scope.cached_result  # 幂等命中
                result = do_write(...)
                scope.set_result(result)
                return result
        """
        return self._ExecutionScope(self, idempotency_key, trace_id, node_name, args)

    # ==================================================================
    # 重放
    # ==================================================================

    def replay(
        self,
        trace_id: str,
        on_node: Callable[[DurableRecord], None] | None = None,
    ) -> list[DurableRecord]:
        """重放某 trace 的所有副作用节点(只读模式)。

        Args:
            trace_id: 要重放的 trace ID
            on_node: 每个节点的回调(用于自定义处理)
        """
        records = self.lookup_by_trace(trace_id)
        if on_node:
            for record in records:
                on_node(record)
        return records

    # ==================================================================
    # 内部:文件 IO
    # ==================================================================

    @staticmethod
    def _hash_args(args: dict) -> str:
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(args_str.encode("utf-8")).hexdigest()[:16]

    def _load(self) -> None:
        """加载 durable_log 构建内存索引(惰性,只加载一次)。"""
        if self._loaded:
            return
        try:
            if self.log_path.exists():
                with open(self.log_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            record = DurableRecord(
                                record_id=data["record_id"],
                                idempotency_key=data["idempotency_key"],
                                trace_id=data.get("trace_id", ""),
                                node_name=data.get("node_name", ""),
                                status=ExecutionStatus(data.get("status", "started")),
                                args_hash=data.get("args_hash", ""),
                                result=data.get("result"),
                                error=data.get("error"),
                                started_at=data.get("started_at", 0.0),
                                completed_at=data.get("completed_at", 0.0),
                                duration_ms=data.get("duration_ms", 0.0),
                                saga_compensation=data.get("saga_compensation"),
                            )
                            self._index[record.idempotency_key] = record
                        except (json.JSONDecodeError, KeyError, ValueError) as e:
                            logger.warning("Failed to parse durable log line: %s", e)
        except OSError as e:
            logger.warning("Failed to load durable log: %s", e)
        self._loaded = True

    def _append_record(self, record: DurableRecord) -> None:
        """追加新记录到 durable_log。"""
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._record_to_dict(record), ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error("Failed to append durable record: %s", e)
            raise

    def _update_record(self, record: DurableRecord) -> None:
        """更新记录(append-only 模式:写一条新记录覆盖状态)。

        durable_log 是 append-only 的,update 通过追加新行实现(同一 idempotency_key 多行,以最后一行为准)。
        """
        self._append_record(record)

    @staticmethod
    def _record_to_dict(record: DurableRecord) -> dict:
        return {
            "record_id": record.record_id,
            "idempotency_key": record.idempotency_key,
            "trace_id": record.trace_id,
            "node_name": record.node_name,
            "status": record.status.value,
            "args_hash": record.args_hash,
            "result": record.result,
            "error": record.error,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "duration_ms": record.duration_ms,
            "saga_compensation": record.saga_compensation,
            "updated_at": time.time(),
        }


# 全局单例
_dm_instance: DurableExecutionManager | None = None
_dm_lock = threading.Lock()


def get_durable_manager() -> DurableExecutionManager:
    global _dm_instance
    if _dm_instance is None:
        with _dm_lock:
            if _dm_instance is None:
                _dm_instance = DurableExecutionManager()
    return _dm_instance
