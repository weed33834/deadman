"""P8.3.1 Graphiti-style 时序知识图谱运行时。

设计目标:
    - 提供 Graphiti 风格的时序知识图(temporal knowledge graph)抽象
    - 支持按时间点 "穿越" 查询(get_temporal),还原历史节点状态
    - BFS 邻接搜索(max_depth 控制搜索深度)
    - 重依赖 graphiti / networkx 必须 OPTIONAL:不可用时降级到纯内存 dict-of-dicts
    - feature flag DEADMAN_KNOWLEDGE_GRAPH_ENABLED 默认关闭

设计原则:
    - 三大铁律:flag 关闭走降级、重依赖 lazy import、不破坏现有测试
    - 原子写:持久化用 .tmp + os.replace
    - 线程安全:threading.RLock 守护内存图
    - 时序模型:节点带 valid_from / valid_to,query at_time 时仅返回当时存活的节点

模块结构:
    - Episode: 一次知识更新事件(text/json)
    - KGNode: 知识图节点(带时序)
    - KGEdge: 知识图边(关系)
    - GraphitiRuntime: 运行时(add_episode / search / get_temporal)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from ..infrastructure.feature_flags import is_enabled

logger = logging.getLogger(__name__)


# =====================================================================
# 可选依赖:graphiti / networkx,缺失时降级到内存图
# =====================================================================
try:  # pragma: no cover - 可选依赖
    import networkx  # type: ignore
    _HAS_NETWORKX = True
except ImportError:  # pragma: no cover
    networkx = None  # type: ignore
    _HAS_NETWORKX = False

try:  # pragma: no cover - 可选依赖
    import graphiti  # type: ignore
    _HAS_GRAPHITI = True
except ImportError:  # pragma: no cover
    graphiti = None  # type: ignore
    _HAS_GRAPHITI = False


# =====================================================================
# 数据模型
# =====================================================================

class EpisodeType(str, Enum):
    """事件类型(Graphiti 风格)。"""

    TEXT = "text"  # 自然语言文本
    JSON = "json"  # 结构化 JSON
    MESSAGE = "message"  # 对话消息
    DOCUMENT = "document"  # 文档片段


@dataclass
class Episode:
    """一次知识更新事件。

    Graphiti 风格的 episode:外部信息流入知识图的单元事件,
    后台 LLM/规则抽取节点与关系后写入 KG。

    Attributes:
        content: 事件内容(text 或 JSON 字符串)
        source: 来源标识(如 "official_law:cn" / "court_case:bj-2024-001")
        timestamp: 事件发生时间(epoch 秒);默认当前时间
        type: 事件类型(text/json/message/document)
        metadata: 附加元信息(trust_level / tags / ref_url 等)
    """

    content: str
    source: str
    timestamp: float = field(default_factory=time.time)
    type: EpisodeType = EpisodeType.TEXT
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Episode":
        return cls(
            content=data["content"],
            source=data["source"],
            timestamp=data.get("timestamp", time.time()),
            type=EpisodeType(data.get("type", "text")),
            metadata=data.get("metadata", {}) or {},
        )


@dataclass
class KGNode:
    """知识图节点(带时序)。

    时序模型:
        - valid_from: 节点开始生效时间(epoch 秒)
        - valid_to: 节点失效时间(None 表示仍有效,epoch 秒表示已失效)

    get_temporal(node_id, at_time) 查询时:
        - valid_from <= at_time <= valid_to(若非 None) → 返回该节点
        - 否则视为当时不存在

    Attributes:
        id: 节点 ID(稳定唯一)
        type: 节点类型(entity / fact / event / document / law)
        content: 节点内容(自然语言描述或 JSON 字符串)
        properties: 附加属性(trust_level / source / tags)
        valid_from: 生效起始时间
        valid_to: 失效时间(None 表示仍有效)
    """

    id: str
    type: str = "entity"
    content: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    valid_from: float = field(default_factory=time.time)
    valid_to: Optional[float] = None

    def is_valid_at(self, at_time: float) -> bool:
        """该节点在 at_time 时是否有效。"""
        if at_time < self.valid_from:
            return False
        if self.valid_to is not None and at_time > self.valid_to:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KGNode":
        return cls(
            id=data["id"],
            type=data.get("type", "entity"),
            content=data.get("content", ""),
            properties=data.get("properties", {}) or {},
            valid_from=data.get("valid_from", time.time()),
            valid_to=data.get("valid_to"),
        )


@dataclass
class KGEdge:
    """知识图边(关系)。

    Attributes:
        from_id: 起点节点 ID
        to_id: 终点节点 ID
        type: 关系类型(same_as / cites / amends / abrogates / derives_from)
        properties: 附加属性(weight / source / timestamp)
    """

    from_id: str
    to_id: str
    type: str = "related_to"
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KGEdge":
        return cls(
            from_id=data["from_id"],
            to_id=data["to_id"],
            type=data.get("type", "related_to"),
            properties=data.get("properties", {}) or {},
        )


# =====================================================================
# GraphitiRuntime - 时序知识图运行时
# =====================================================================

class _InMemoryGraph:
    """纯 Python dict-of-dicts 内存图(networkx 降级后端)。

    数据结构:
        - _nodes: {node_id: KGNode}
        - _adj: {from_id: {to_id: {edge_type: KGEdge}}}

    线程安全:外层 GraphitiRuntime 持有 RLock,本类不重复加锁。
    """

    def __init__(self) -> None:
        self._nodes: dict[str, KGNode] = {}
        # _adj[from_id][to_id] = {edge_type: KGEdge}
        self._adj: dict[str, dict[str, dict[str, KGEdge]]] = {}
        self._reverse_adj: dict[str, list[str]] = {}  # to_id -> [from_id]

    def add_node(self, node: KGNode) -> None:
        self._nodes[node.id] = node
        self._adj.setdefault(node.id, {})
        self._reverse_adj.setdefault(node.id, [])

    def get_node(self, node_id: str) -> Optional[KGNode]:
        return self._nodes.get(node_id)

    def add_edge(self, edge: KGEdge) -> None:
        self._adj.setdefault(edge.from_id, {}).setdefault(edge.to_id, {})[edge.type] = edge
        self._reverse_adj.setdefault(edge.to_id, []).append(edge.from_id)

    def neighbors(self, node_id: str) -> list[str]:
        """正向邻居(BFS 用)。"""
        return list(self._adj.get(node_id, {}).keys())

    def all_nodes(self) -> list[KGNode]:
        return list(self._nodes.values())

    def all_edges(self) -> list[KGEdge]:
        out: list[KGEdge] = []
        for _from_id, targets in self._adj.items():
            for _to_id, edges in targets.items():
                out.extend(edges.values())
        return out

    def remove_node(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)
        self._adj.pop(node_id, None)
        # 清理反向引用
        for _to_id, srcs in list(self._reverse_adj.items()):
            if node_id in srcs:
                srcs.remove(node_id)
        # 清理正向边中含 node_id 的
        for _from_id, targets in list(self._adj.items()):
            if node_id in targets:
                del targets[node_id]

    def count_nodes(self) -> int:
        return len(self._nodes)

    def count_edges(self) -> int:
        return sum(
            len(edges)
            for targets in self._adj.values()
            for edges in targets.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self.all_edges()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_InMemoryGraph":
        g = cls()
        for nd in data.get("nodes", []):
            g.add_node(KGNode.from_dict(nd))
        for ed in data.get("edges", []):
            g.add_edge(KGEdge.from_dict(ed))
        return g


class GraphitiRuntime:
    """Graphiti 风格时序知识图运行时。

    用法:
        rt = GraphitiRuntime()
        ep_id = rt.add_episode(Episode(content="...", source="..."))
        nodes = rt.search("社保", max_depth=2)
        old = rt.get_temporal(node_id, at_time=past_ts)

    设计:
        - 重依赖 graphiti/networkx 必须 lazy import,不可用降级到 _InMemoryGraph
        - 持久化可选:不传 persist_path 则纯内存,传了则每次写后原子落盘
        - 线程安全:RLock 守护 add/search/get_temporal
        - 时序:节点带 valid_from/valid_to,update_node 时旧节点 valid_to=now,
          新节点 valid_from=now,实现 "事实变更" 留痕
    """

    def __init__(
        self,
        persist_path: Optional[Path] = None,
        use_real_graphiti: bool = False,
    ) -> None:
        """构造运行时。

        Args:
            persist_path: 持久化路径(传了则每次写后落盘;None 纯内存)
            use_real_graphiti: True 时尝试用 graphiti 包;False 默认走内存
                              (graphiti 包含 LLM 调用,CI 默认走内存)
        """
        self.persist_path = persist_path
        self._lock = threading.RLock()
        self._episodes: dict[str, Episode] = {}
        self._graph = _InMemoryGraph()
        self._use_real_graphiti = False

        if use_real_graphiti and _HAS_GRAPHITI:
            try:
                # graphiti 的真实 Graphiti 客户端初始化(需 LLM/Neo4j 配置)
                # 此处仅占位,真实使用时由调用方注入完整客户端
                self._use_real_graphiti = True
                logger.info("GraphitiRuntime: 真实 graphiti 已启用")
            except Exception as e:  # pragma: no cover - 极端情况
                logger.warning("Graphiti init failed, fallback to in-memory: %s", e)
                self._use_real_graphiti = False

        if persist_path is not None:
            self._load()

    # ==================================================================
    # Episode API
    # ==================================================================

    def add_episode(self, episode: Episode) -> str:
        """添加一次知识事件。

        事件本身记录到 _episodes;同时从 episode.content 抽取出节点与边
        (本期简化:每条 episode 至少产出 1 个 KGNode,content 作为节点内容)。

        Returns:
            episode_id(UUID)
        """
        if not is_enabled("knowledge_graph"):
            # flag 关闭时静默返回 ID(不实际入库),保持调用方 API 兼容
            logger.debug("knowledge_graph disabled, episode %s not stored", episode.source)
            return str(uuid4())

        with self._lock:
            ep_id = str(uuid4())
            episode.metadata["episode_id"] = ep_id
            self._episodes[ep_id] = episode

            # 抽取节点:每条 episode → 1 个 KGNode(content=episode.content)
            # 真实 graphiti 会用 LLM 抽取实体/关系,此处简化为单节点
            node_id = f"node-{ep_id}"
            node = KGNode(
                id=node_id,
                type="entity",
                content=episode.content,
                properties={
                    "source": episode.source,
                    "episode_id": ep_id,
                    "episode_type": episode.type.value,
                    **episode.metadata,
                },
                valid_from=episode.timestamp,
            )
            self._graph.add_node(node)
            self._persist()
            return ep_id

    def add_node(self, node: KGNode) -> None:
        """直接添加一个节点(供 KnowledgeManager / 测试用)。"""
        with self._lock:
            self._graph.add_node(node)
            self._persist()

    def add_edge(self, edge: KGEdge) -> None:
        """直接添加一条边。"""
        with self._lock:
            self._graph.add_edge(edge)
            self._persist()

    def get_node(self, node_id: str) -> Optional[KGNode]:
        """按 ID 取节点。"""
        with self._lock:
            return self._graph.get_node(node_id)

    # ==================================================================
    # Search API
    # ==================================================================

    def search(
        self,
        query: str,
        max_depth: int = 2,
        top_k: int = 10,
    ) -> list[KGNode]:
        """BFS 搜索:从 query 命中的种子节点出发,沿边扩展 max_depth 跳。

        种子匹配规则(简化):
            - 节点 content / properties.source 含 query 子串(大小写不敏感)

        Args:
            query: 查询字符串(关键词)
            max_depth: BFS 最大深度(默认 2)
            top_k: 最多返回的节点数

        Returns:
            匹配及邻居节点列表(去重,按 BFS 访问顺序)
        """
        if not is_enabled("knowledge_graph"):
            return []

        with self._lock:
            q_lower = (query or "").lower()
            # 1. 找种子节点
            seeds: list[str] = []
            for node in self._graph.all_nodes():
                if self._node_matches(node, q_lower):
                    seeds.append(node.id)
            if not seeds:
                return []

            # 2. BFS 扩展
            visited: set[str] = set()
            ordered: list[KGNode] = []
            queue: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
            while queue and len(ordered) < top_k:
                nid, depth = queue.popleft()
                if nid in visited:
                    continue
                visited.add(nid)
                node = self._graph.get_node(nid)
                if node is not None:
                    ordered.append(node)
                if depth >= max_depth:
                    continue
                for nb in self._graph.neighbors(nid):
                    if nb not in visited:
                        queue.append((nb, depth + 1))
            return ordered

    @staticmethod
    def _node_matches(node: KGNode, q_lower: str) -> bool:
        """种子匹配:content / source 含 query 子串。"""
        if not q_lower:
            return False
        if q_lower in (node.content or "").lower():
            return True
        src = node.properties.get("source", "")
        if q_lower in src.lower():
            return True
        return False

    # ==================================================================
    # Temporal API
    # ==================================================================

    def get_temporal(
        self,
        node_id: str,
        at_time: Optional[float] = None,
    ) -> Optional[KGNode]:
        """时序穿越查询:返回 node_id 在 at_time 时刻的版本。

        时序模型:
            - 节点带 valid_from / valid_to
            - 同一逻辑实体多次更新时,我们用 "node_id-{version}" 串联不同版本
              (本期简化:每个 node_id 单版本,直接判断 is_valid_at)
            - 若节点已失效(valid_to < at_time) → 返回 None
            - 若节点尚未生效(valid_from > at_time) → 返回 None
            - at_time=None → 用当前时间

        Args:
            node_id: 节点 ID
            at_time: 查询时间点(epoch 秒);None 表示当前

        Returns:
            节点(若当时有效),否则 None
        """
        if not is_enabled("knowledge_graph"):
            return None

        with self._lock:
            node = self._graph.get_node(node_id)
            if node is None:
                return None
            ts = at_time if at_time is not None else time.time()
            if node.is_valid_at(ts):
                return node
            return None

    def invalidate_node(self, node_id: str, at_time: Optional[float] = None) -> bool:
        """将节点置为失效(valid_to=now),用于法规变更等场景留痕。

        Returns:
            True 表示成功置失效
        """
        with self._lock:
            node = self._graph.get_node(node_id)
            if node is None:
                return False
            node.valid_to = at_time if at_time is not None else time.time()
            self._persist()
            return True

    # ==================================================================
    # 工具方法
    # ==================================================================

    def count_nodes(self) -> int:
        with self._lock:
            return self._graph.count_nodes()

    def count_edges(self) -> int:
        with self._lock:
            return self._graph.count_edges()

    def all_nodes(self) -> list[KGNode]:
        with self._lock:
            return self._graph.all_nodes()

    def all_episodes(self) -> list[Episode]:
        with self._lock:
            return list(self._episodes.values())

    # ==================================================================
    # 持久化
    # ==================================================================

    def _persist(self) -> None:
        """原子落盘(.tmp + os.replace)。"""
        if self.persist_path is None:
            return
        try:
            data = {
                "version": 1,
                "updated_at": time.time(),
                "episodes": [e.to_dict() for e in self._episodes.values()],
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
            logger.error("GraphitiRuntime persist failed: %s", e)

    def _load(self) -> None:
        """从磁盘加载(失败时空数据,不抛异常)。"""
        if self.persist_path is None or not self.persist_path.exists():
            return
        try:
            text = self.persist_path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
            for ed in data.get("episodes", []):
                ep = Episode.from_dict(ed)
                self._episodes[ep.metadata.get("episode_id", str(uuid4()))] = ep
            self._graph = _InMemoryGraph.from_dict(data.get("graph", {}))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("GraphitiRuntime load failed, using empty: %s", e)


__all__ = [
    "Episode",
    "EpisodeType",
    "KGNode",
    "KGEdge",
    "GraphitiRuntime",
    "_InMemoryGraph",
]
