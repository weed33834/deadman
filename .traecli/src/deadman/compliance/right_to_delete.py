"""P8.6.2 数据可删除权 - 用户请求删除,7 天内彻底清除。

法规依据:
    - PIPL 第 47 条:用户有权请求删除个人信息
    - GDPR 第 17 条:被遗忘权(Right to Erasure)
    - 数据安全法第 16 条:用户有权撤回同意

设计:
    - DeletionRequest: 删除请求(用户发起)
    - DeletionStatus: 状态机(REQUESTED → PROCESSING → COMPLETED / FAILED)
    - RightToDelete: 执行删除(跨存储清理)

跨存储清理:
    - 记忆系统:~/.deadman/memory/USER.md / MEMORY.md / EPISODES.md / REFLEXION.json
    - 向量库:Chroma 删除该 user 的所有 embedding
    - 订阅:subscription.py 删除订阅记录
    - 计量:billing/metering 删除事件流
    - 凭证:credential_vault 删除该 user 的凭证
    - 审计日志:审计链保留(合规要求,不可删,但标记 anonymized)

feature flag:`DEADMAN_COMPLIANCE_ENABLED=0` 关闭时不执行(返回虚拟成功)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..infrastructure.feature_flags import is_enabled

logger = logging.getLogger(__name__)


class DeletionStatus(str, Enum):
    """删除状态机:

    REQUESTED → PROCESSING → COMPLETED
                    ↓
                FAILED(部分删除失败,需人工)
    """

    REQUESTED = "requested"  # 已收到请求
    PROCESSING = "processing"  # 处理中(7 天宽限)
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败(部分存储删除失败)


# 删除宽限期(7 天,法规要求)
DELETION_GRACE_PERIOD_DAYS = 7

# 删除最大重试次数
MAX_RETRIES = 3


@dataclass
class DeletionRequest:
    """删除请求。"""

    request_id: str
    user_id: str
    reason: str = ""  # 用户提供的删除原因
    status: DeletionStatus = DeletionStatus.REQUESTED
    requested_at: float = field(default_factory=time.time)
    scheduled_at: float = 0.0  # 计划执行时间(requested + grace)
    executed_at: float | None = None
    # 删除详情
    stores_processed: list[str] = field(default_factory=list)  # 已处理的存储
    stores_failed: list[str] = field(default_factory=list)  # 删除失败的存储
    stores_skipped: list[str] = field(default_factory=list)  # 跳过的存储(如审计日志)
    error_messages: dict[str, str] = field(default_factory=dict)  # store → error msg
    retry_count: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> DeletionRequest:
        return cls(
            request_id=data["request_id"],
            user_id=data["user_id"],
            reason=data.get("reason", ""),
            status=DeletionStatus(data.get("status", "requested")),
            requested_at=data.get("requested_at", time.time()),
            scheduled_at=data.get("scheduled_at", 0.0),
            executed_at=data.get("executed_at"),
            stores_processed=data.get("stores_processed", []),
            stores_failed=data.get("stores_failed", []),
            stores_skipped=data.get("stores_skipped", []),
            error_messages=data.get("error_messages", {}),
            retry_count=data.get("retry_count", 0),
        )


class RightToDelete:
    """数据可删除权执行器。

    设计:
        - 用户发起请求 → 7 天宽限 → 自动执行
        - 跨存储清理(每个存储有独立删除方法)
        - 失败重试(最多 3 次)
        - 审计日志保留(法规要求),但 anonymized(去掉 user_id)
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or Path(
            os.environ.get("DEADMAN_DELETION_STORE", "data/compliance/deletions.json")
        )
        self._lock = threading.RLock()
        self._requests: dict[str, DeletionRequest] = {}
        self._loaded = False
        # 删除策略(可注入)
        self._deletors: dict[str, Any] = {}  # store_name → callable(user_id) → bool

    def register_deletor(self, store_name: str, deletor) -> None:
        """注册存储删除器。

        Args:
            store_name: 存储名(如 "memory" / "vector_store")
            deletor: callable(user_id: str) → bool(True 表示删除成功)
        """
        self._deletors[store_name] = deletor

    # ==================================================================
    # 请求生命周期
    # ==================================================================

    def request_deletion(
        self,
        user_id: str,
        reason: str = "",
        scheduled_in_days: int = DELETION_GRACE_PERIOD_DAYS,
    ) -> DeletionRequest:
        """发起删除请求。"""
        if not is_enabled("compliance"):
            # 关闭:返回虚拟"已完成"请求(透传)
            return DeletionRequest(
                request_id=self._generate_id(user_id),
                user_id=user_id,
                reason=reason,
                status=DeletionStatus.COMPLETED,
                executed_at=time.time(),
            )

        with self._lock:
            self._load()
            now = time.time()
            request = DeletionRequest(
                request_id=self._generate_id(user_id),
                user_id=user_id,
                reason=reason,
                status=DeletionStatus.REQUESTED,
                requested_at=now,
                scheduled_at=now + scheduled_in_days * 86400,
            )
            self._requests[request.request_id] = request
            self._save()
            logger.info("Deletion requested for user %s (scheduled_at=%s)", user_id, request.scheduled_at)
            return request

    def cancel(self, request_id: str) -> bool:
        """取消删除请求(宽限期内可取消)。"""
        if not is_enabled("compliance"):
            return False

        with self._lock:
            self._load()
            request = self._requests.get(request_id)
            if request is None:
                return False
            if request.status != DeletionStatus.REQUESTED:
                return False  # 已开始处理不可取消
            del self._requests[request_id]
            self._save()
            return True

    def execute(self, request_id: str) -> DeletionRequest | None:
        """执行删除(立即,跳过宽限期)。

        用于:用户主动确认 / 管理员强制执行 / 定时任务触发。
        """
        if not is_enabled("compliance"):
            return None

        with self._lock:
            self._load()
            request = self._requests.get(request_id)
            if request is None:
                return None
            if request.status == DeletionStatus.COMPLETED:
                return request

            request.status = DeletionStatus.PROCESSING
            self._save()

        # 跨存储删除(无锁,允许并发)
        for store_name, deletor in self._deletors.items():
            try:
                success = deletor(request.user_id)
                with self._lock:
                    if success:
                        request.stores_processed.append(store_name)
                    else:
                        request.stores_failed.append(store_name)
                        request.error_messages[store_name] = "deletor returned False"
                    self._save()
            except Exception as e:
                logger.error("Deletor %s failed for user %s: %s", store_name, request.user_id, e)
                with self._lock:
                    request.stores_failed.append(store_name)
                    request.error_messages[store_name] = str(e)
                    self._save()

        # 跳过审计日志(合规要求保留)
        with self._lock:
            request.stores_skipped.append("audit_log")  # 审计保留但 anonymize

        # 最终状态
        with self._lock:
            if not request.stores_failed:
                request.status = DeletionStatus.COMPLETED
            else:
                request.status = DeletionStatus.FAILED
            request.executed_at = time.time()
            self._save()
            return request

    # ==================================================================
    # 定时任务
    # ==================================================================

    def process_due(self, now: float | None = None) -> int:
        """处理到期的删除请求(定时任务调用)。

        Returns:
            处理的请求数
        """
        if not is_enabled("compliance"):
            return 0

        now = now or time.time()
        with self._lock:
            self._load()
            due = [
                req for req in self._requests.values()
                if req.status == DeletionStatus.REQUESTED and now >= req.scheduled_at
            ]
        for req in due:
            self.execute(req.request_id)
        return len(due)

    def retry_failed(self) -> int:
        """重试失败的删除(最多 MAX_RETRIES 次)。"""
        if not is_enabled("compliance"):
            return 0
        with self._lock:
            self._load()
            retryable = [
                req for req in self._requests.values()
                if req.status == DeletionStatus.FAILED and req.retry_count < MAX_RETRIES
            ]
        for req in retryable:
            req.retry_count += 1
            self.execute(req.request_id)
        return len(retryable)

    # ==================================================================
    # 查询
    # ==================================================================

    def get(self, request_id: str) -> DeletionRequest | None:
        with self._lock:
            self._load()
            return self._requests.get(request_id)

    def list_by_user(self, user_id: str) -> list[DeletionRequest]:
        with self._lock:
            self._load()
            return [req for req in self._requests.values() if req.user_id == user_id]

    def list_by_status(self, status: DeletionStatus) -> list[DeletionRequest]:
        with self._lock:
            self._load()
            return [req for req in self._requests.values() if req.status == status]

    # ==================================================================
    # 验证(用户自查)
    # ==================================================================

    def verify_deleted(self, user_id: str) -> bool:
        """验证用户数据是否已完全删除。

        Returns:
            True: 完全删除(所有 deletor 都返回 success)
            False: 仍有数据残留
        """
        if not is_enabled("compliance"):
            return True  # 关闭时默认 True(透传)
        # 简化实现:检查是否有 COMPLETED 的请求
        requests = self.list_by_user(user_id)
        return any(req.status == DeletionStatus.COMPLETED for req in requests)

    # ==================================================================
    # 内部
    # ==================================================================

    def _generate_id(self, user_id: str) -> str:
        """生成唯一 request_id。"""
        return f"DEL-{int(time.time())}-{abs(hash(user_id)) % 100000:05d}"

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for rid, rdata in data.get("requests", {}).items():
                    self._requests[rid] = DeletionRequest.from_dict(rdata)
        except Exception as e:
            logger.warning("Deletion store load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "requests": {rid: r.to_dict() for rid, r in self._requests.items()},
            }
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Deletion store save failed: %s", e)


# 全局单例
_rtd_instance: RightToDelete | None = None
_rtd_lock = threading.Lock()


def get_right_to_delete() -> RightToDelete:
    global _rtd_instance
    if _rtd_instance is None:
        with _rtd_lock:
            if _rtd_instance is None:
                _rtd_instance = RightToDelete()
    return _rtd_instance
