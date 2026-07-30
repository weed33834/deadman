"""P8.3.7 用户 / 租户私有知识图。

设计目标:
    - 每个租户的每个用户有独立的知识图(隔离)
    - 存储:knowledge/private/{tenant_id}/{user_id}.json
    - 跨租户访问直接抛 TenantIsolationError(强隔离)
    - 与 GraphitiRuntime 共享数据结构(KGNode / KGEdge)

法规依据:
    - PIPL 第 9 条:个人信息处理应"目的明确、最小必要"
    - 多租户架构强制数据隔离(参考 GDPR 第 25 条 "数据保护设计")

设计原则:
    - feature flag DEADMAN_KNOWLEDGE_GRAPH_ENABLED 默认关闭
    - 原子写:持久化用 .tmp + os.replace
    - 线程安全:threading.RLock
    - 严格隔离:任何跨租户访问抛异常(不静默降级)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import (
    resolve_data_path,
)
from .graphiti_runtime import KGEdge, KGNode, _InMemoryGraph

logger = logging.getLogger(__name__)


class TenantIsolationError(Exception):
    """跨租户访问违规。

    任何试图读取 / 写入其他租户私有图的访问都会抛此异常,
    不静默降级(避免数据泄漏)。

    Attributes:
        current_tenant_id: 当前上下文中的租户 ID
        attempted_tenant_id: 试图访问的租户 ID
    """

    def __init__(
        self,
        msg: str,
        *,
        current_tenant_id: str = "",
        attempted_tenant_id: str = "",
    ) -> None:
        self.current_tenant_id = current_tenant_id
        self.attempted_tenant_id = attempted_tenant_id
        super().__init__(msg)


class PrivateGraph:
    """用户 / 租户私有知识图。

    用法:
        graph = PrivateGraph(tenant_id="t-1", user_id="u-1")
        graph.add_node(KGNode(id="n1", content="..."))
        graph.add_edge(KGEdge(from_id="n1", to_id="n2"))
        nodes = graph.query("关键词")

    设计:
        - 每个实例绑定 (tenant_id, user_id) 对,跨租户访问抛异常
        - 持久化:knowledge/private/{tenant_id}/{user_id}.json
        - 通过 multi_tenant.resolve_data_path 路由(支持多租户 / 单租户)
        - flag 关闭时所有方法返回空 / 静默(no-op),不抛异常
    """

    def __init__(
        self,
        tenant_id: str,
        user_id: str,
        persist_root: Path | None = None,
    ) -> None:
        """构造私有图。

        Args:
            tenant_id: 租户 ID(强隔离边界)
            user_id: 用户 ID(同租户内不同用户仍隔离)
            persist_root: 持久化根目录;None 时用 resolve_data_path 计算
        """
        if not tenant_id:
            raise ValueError("tenant_id 不能为空")
        if not user_id:
            raise ValueError("user_id 不能为空")

        self.tenant_id = tenant_id
        self.user_id = user_id
        self._lock = threading.RLock()
        self._graph = _InMemoryGraph()

        if persist_root is not None:
            self.persist_path = persist_root / tenant_id / f"{user_id}.json"
        else:
            # 通过 multi_tenant 路由(支持租户隔离的目录结构)
            # 路径模式: knowledge/private/{tenant_id}/{user_id}.json
            self.persist_path = resolve_data_path(
                f"knowledge/private/{tenant_id}/{user_id}.json",
                tenant_id=tenant_id,
            )
        self._load()

    def _check_isolation(self, attempted_tenant_id: str) -> None:
        """跨租户访问检查。"""
        if attempted_tenant_id and attempted_tenant_id != self.tenant_id:
            raise TenantIsolationError(
                f"跨租户访问被拒: 当前 tenant={self.tenant_id}, "
                f"试图访问 tenant={attempted_tenant_id}",
                current_tenant_id=self.tenant_id,
                attempted_tenant_id=attempted_tenant_id,
            )

    def _check_isolation_user(self, attempted_user_id: str) -> None:
        """跨用户访问检查(同租户内不同用户也隔离)。"""
        if attempted_user_id and attempted_user_id != self.user_id:
            raise TenantIsolationError(
                f"跨用户访问被拒: 当前 user={self.user_id}, 试图访问 user={attempted_user_id}",
                current_tenant_id=self.tenant_id,
                attempted_tenant_id=self.tenant_id,
            )

    # ==================================================================
    # 节点 / 边 API
    # ==================================================================

    def add_node(self, node: KGNode) -> str:
        """添加节点(自动注入 tenant_id / user_id 隔离属性)。

        Returns:
            node.id
        """
        if not is_enabled("knowledge_graph"):
            return node.id
        with self._lock:
            # 注入隔离属性(便于序列化时识别归属)
            node.properties["_tenant_id"] = self.tenant_id
            node.properties["_user_id"] = self.user_id
            self._graph.add_node(node)
            self._persist()
            return node.id

    def add_edge(self, edge: KGEdge) -> None:
        """添加边。"""
        if not is_enabled("knowledge_graph"):
            return
        with self._lock:
            self._graph.add_edge(edge)
            self._persist()

    def get_node(self, node_id: str) -> KGNode | None:
        """按 ID 取节点(仅返回当前用户的)。"""
        with self._lock:
            node = self._graph.get_node(node_id)
            if node is None:
                return None
            # 二次校验:确保节点属于当前用户
            if (
                node.properties.get("_tenant_id", self.tenant_id) != self.tenant_id
                or node.properties.get("_user_id", self.user_id) != self.user_id
            ):
                return None
            return node

    def query(self, q: str, top_k: int = 10) -> list[KGNode]:
        """关键词查询当前用户图内的节点。

        匹配规则(简化):节点 content / source 含 q 子串。

        Args:
            q: 查询关键词
            top_k: 最多返回数

        Returns:
            匹配节点列表
        """
        if not is_enabled("knowledge_graph"):
            return []
        with self._lock:
            q_lower = (q or "").lower()
            out: list[KGNode] = []
            for node in self._graph.all_nodes():
                # 强制校验归属(防御性)
                if node.properties.get("_tenant_id") != self.tenant_id:
                    continue
                if node.properties.get("_user_id") != self.user_id:
                    continue
                if (
                    not q_lower
                    or q_lower in (node.content or "").lower()
                    or q_lower in str(node.properties.get("source", "")).lower()
                ):
                    out.append(node)
                if len(out) >= top_k:
                    break
            return out

    def list_nodes(self) -> list[KGNode]:
        """列出所有节点(仅供当前用户)。"""
        with self._lock:
            return [
                n
                for n in self._graph.all_nodes()
                if n.properties.get("_tenant_id") == self.tenant_id
                and n.properties.get("_user_id") == self.user_id
            ]

    def count(self) -> int:
        """当前用户图节点数。"""
        return len(self.list_nodes())

    # ==================================================================
    # 跨租户 / 跨用户访问 API(强制隔离)
    # ==================================================================

    def cross_tenant_query(
        self,
        other_tenant_id: str,
        q: str,
    ) -> list[KGNode]:
        """跨租户查询 → 强制抛 TenantIsolationError。

        本方法存在的目的是显式校验"跨租户访问被拒",所有调用
        都会抛异常(不存在合法的跨租户场景)。
        """
        self._check_isolation(other_tenant_id)
        # 若通过了 isolation check(理论上不会,因为 attempted != current)
        return []

    def cross_user_query(
        self,
        other_user_id: str,
        q: str,
    ) -> list[KGNode]:
        """跨用户查询 → 强制抛 TenantIsolationError。"""
        self._check_isolation_user(other_user_id)
        return []

    # ==================================================================
    # 持久化
    # ==================================================================

    def _persist(self) -> None:
        try:
            data = {
                "version": 1,
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
                "updated_at": time.time(),
                "graph": self._graph.to_dict(),
            }
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.persist_path)
        except OSError as e:
            logger.error("PrivateGraph persist failed: %s", e)

    def _load(self) -> None:
        try:
            if not self.persist_path.exists():
                return
            text = self.persist_path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
            # 二次校验:加载时确认文件归属
            file_tenant = data.get("tenant_id")
            file_user = data.get("user_id")
            if file_tenant and file_tenant != self.tenant_id:
                raise TenantIsolationError(
                    f"私有图文件 tenant 不匹配: file={file_tenant} expected={self.tenant_id}",
                    current_tenant_id=self.tenant_id,
                    attempted_tenant_id=file_tenant,
                )
            if file_user and file_user != self.user_id:
                raise TenantIsolationError(
                    f"私有图文件 user 不匹配: file={file_user} expected={self.user_id}",
                    current_tenant_id=self.tenant_id,
                    attempted_tenant_id=self.tenant_id,
                )
            self._graph = _InMemoryGraph.from_dict(data.get("graph", {}))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("PrivateGraph load failed, using empty: %s", e)


__all__ = [
    "PrivateGraph",
    "TenantIsolationError",
]
