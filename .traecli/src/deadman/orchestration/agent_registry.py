"""P4.3 Agent 注册中心 - A2A agent 的服务发现与健康监控

借鉴 MCP server registry / A2A agent directory 的设计，让平台能：
- 注册本地 + 远端 A2A agent（带 AgentCard + 能力标签）
- 按能力标签发现可用 agent（discover）
- 心跳保活，定时 health_check 标记失活 agent
- 持久化到 data/agent_registry.json（atomic_write，进程重启不丢）

Feature flag: DEADMAN_AGENT_REGISTRY_ENABLED=0 默认关闭
- 关闭时所有写操作（register/unregister/heartbeat）静默 no-op，
  读操作（discover/get/health_check）返回空，调用方走旧路径（仅本地
  agents_store + 单点 fetch），行为完全不变
- 开启时所有操作生效；持久化用 atomic_write（先写 .tmp 再 os.replace）

降级路径全覆盖：
1. feature flag 关闭 → 写 no-op / 读返回空
2. 持久化目录不可写 → 仅内存操作，记 warning 不抛异常
3. JSON 解析失败 → 视为空注册中心重新开始
4. heartbeat 未知 agent → 自动注册（lazy）
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..a2a.models import AgentCard
from ..config import settings

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
AGENT_REGISTRY_ENABLED: bool = os.environ.get(
    "DEADMAN_AGENT_REGISTRY_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 心跳超时阈值（秒）- 超过此值未心跳的 agent 标记为 unhealthy
HEARTBEAT_TIMEOUT_SECONDS: int = int(
    os.environ.get("DEADMAN_AGENT_REGISTRY_HEARTBEAT_TIMEOUT", "300")
)

# 持久化文件路径（相对 project_root）
DEFAULT_REGISTRY_PATH = "data/agent_registry.json"


# =====================================================================
# 数据模型
# =====================================================================


@dataclass
class AgentRegistryEntry:
    """Agent 注册中心条目

    Attributes:
        name: agent 唯一名（AgentCard.name）
        card: 完整的 AgentCard（含 skills / url / capabilities）
        status: 健康状态 ("healthy" / "unhealthy" / "unknown")
        last_heartbeat: 最近一次心跳时间
        capabilities: 能力标签（用于 discover 搜索，独立于 card.skills.tags）
    """

    name: str
    card: AgentCard
    status: str = "unknown"
    last_heartbeat: datetime = field(default_factory=datetime.now)
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的 dict"""
        return {
            "name": self.name,
            "card": self.card.to_dict(),
            "status": self.status,
            "last_heartbeat": self.last_heartbeat.isoformat(),
            "capabilities": list(self.capabilities),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentRegistryEntry":
        """从 dict 反序列化（容错：card 字段缺失时构造空 AgentCard）"""
        card_data = data.get("card") or {}
        try:
            # 重建 AgentCard（仅恢复核心字段，skills 简化为 AgentCardSkill 列表）
            from ..a2a.models import AgentCardSkill

            skills_data = card_data.get("skills", []) or []
            skills = [
                AgentCardSkill(
                    id=s.get("id", ""),
                    name=s.get("name", ""),
                    description=s.get("description", ""),
                    tags=s.get("tags", []) or [],
                    input_schema=s.get("inputSchema", {}) or s.get("input_schema", {}) or {},
                    output_schema=s.get("outputSchema", {}) or s.get("output_schema", {}) or {},
                    jurisdictions=s.get("jurisdictions", []) or [],
                )
                for s in skills_data
                if isinstance(s, dict)
            ]
            card = AgentCard(
                name=card_data.get("name", data.get("name", "")),
                description=card_data.get("description", ""),
                version=card_data.get("version", ""),
                url=card_data.get("url", ""),
                skills=skills,
                provider=card_data.get("provider", {}) or {},
                authentication=card_data.get("authentication", {}) or {},
                capabilities=card_data.get("capabilities", {}) or {},
            )
        except Exception as e:
            logger.warning("AgentRegistryEntry.from_dict 重建 AgentCard 失败: %s", e)
            card = AgentCard(
                name=data.get("name", ""),
                description="",
                version="",
                url="",
            )
        # 解析 last_heartbeat（容错）
        ts_str = data.get("last_heartbeat", "")
        try:
            last_hb = datetime.fromisoformat(ts_str) if ts_str else datetime.now()
        except (ValueError, TypeError):
            last_hb = datetime.now()
        return cls(
            name=data.get("name", card.name),
            card=card,
            status=data.get("status", "unknown"),
            last_heartbeat=last_hb,
            capabilities=list(data.get("capabilities", []) or []),
        )


# =====================================================================
# AgentRegistry
# =====================================================================


class AgentRegistry:
    """Agent 注册中心 - 注册 / 发现 / 心跳 / 健康检查 / 持久化

    所有写操作在 AGENT_REGISTRY_ENABLED=False 时静默 no-op。
    所有读操作在 AGENT_REGISTRY_ENABLED=False 时返回空（list/dict/None）。
    """

    def __init__(self, persist_path: str | Path | None = None):
        """Args:
            persist_path: 持久化文件路径；None 用默认 data/agent_registry.json
        """
        if persist_path is None:
            self._path = settings.project_root / DEFAULT_REGISTRY_PATH
        else:
            self._path = Path(persist_path)
        # 内存索引：name -> AgentRegistryEntry
        self._entries: dict[str, AgentRegistryEntry] = {}
        # 启动时尝试加载已有数据
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """从磁盘加载注册中心（容错：文件不存在/解析失败都视为空）"""
        if not self._path.exists():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                return
            entries_data = data.get("entries", [])
            for item in entries_data:
                if not isinstance(item, dict):
                    continue
                try:
                    entry = AgentRegistryEntry.from_dict(item)
                    self._entries[entry.name] = entry
                except Exception as e:
                    logger.warning("加载 registry 条目失败，跳过: %s", e)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("加载 agent_registry.json 失败，从空开始: %s", e)

    def _persist(self) -> None:
        """原子写入持久化文件（先写 .tmp 再 os.replace）

        失败时仅 warning，不抛异常（保证 register 等主流程不因持久化失败而中断）
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "entries": [e.to_dict() for e in self._entries.values()],
                "updated_at": datetime.now().isoformat(),
            }
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except OSError as e:
            logger.warning("agent_registry 持久化失败（仅内存）: %s", e)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def register(
        self, card: AgentCard, capabilities: list[str] | None = None
    ) -> bool:
        """注册一个 agent

        Args:
            card: AgentCard（name 字段作为唯一 key）
            capabilities: 能力标签（独立于 card.skills.tags，用于 discover）

        Returns:
            True 表示新注册或更新成功；False 表示 feature flag 关闭或 card 无 name

        feature flag 关闭时返回 False（调用方走旧路径）
        """
        if not AGENT_REGISTRY_ENABLED:
            return False
        if not card or not card.name:
            return False
        entry = AgentRegistryEntry(
            name=card.name,
            card=card,
            status="healthy",
            last_heartbeat=datetime.now(),
            capabilities=list(capabilities) if capabilities else [],
        )
        self._entries[card.name] = entry
        self._persist()
        logger.info("agent registered: %s (capabilities=%d)", card.name, len(entry.capabilities))
        return True

    def unregister(self, name: str) -> bool:
        """注销一个 agent

        Returns:
            True 表示存在并已移除；False 表示不存在或 feature flag 关闭
        """
        if not AGENT_REGISTRY_ENABLED:
            return False
        if name not in self._entries:
            return False
        del self._entries[name]
        self._persist()
        logger.info("agent unregistered: %s", name)
        return True

    def discover(self, capability: str) -> list[AgentCard]:
        """按能力标签发现 agent

        匹配规则：agent.capabilities 包含 capability，或 card.skills 中
        任意 skill.tags 包含 capability（兼容 A2A skill.tags 模型）

        Returns:
            匹配的 AgentCard 列表；feature flag 关闭返回 []
        """
        if not AGENT_REGISTRY_ENABLED:
            return []
        if not capability:
            return []
        results: list[AgentCard] = []
        for entry in self._entries.values():
            # 能力标签直接匹配
            if capability in entry.capabilities:
                results.append(entry.card)
                continue
            # 或 card.skills[].tags 匹配
            for skill in entry.card.skills:
                if capability in (skill.tags or []):
                    results.append(entry.card)
                    break
        return results

    def heartbeat(self, name: str, status: str = "healthy") -> None:
        """更新 agent 心跳

        未知 agent 自动注册一个空 AgentCard（lazy 注册，便于动态接入）。

        feature flag 关闭时静默 no-op。
        """
        if not AGENT_REGISTRY_ENABLED:
            return
        if not name:
            return
        now = datetime.now()
        if name in self._entries:
            self._entries[name].last_heartbeat = now
            self._entries[name].status = status
        else:
            # lazy 注册（card 为空，待后续 register 补全）
            self._entries[name] = AgentRegistryEntry(
                name=name,
                card=AgentCard(name=name, description="", version="", url=""),
                status=status,
                last_heartbeat=now,
                capabilities=[],
            )
        self._persist()

    def health_check(self) -> dict[str, str]:
        """检查所有 agent 健康状态

        Returns:
            {agent_name: status} 字典；feature flag 关闭返回 {}

        规则：last_heartbeat 距今超过 HEARTBEAT_TIMEOUT_SECONDS 标记为 "unhealthy"
        """
        if not AGENT_REGISTRY_ENABLED:
            return {}
        now = datetime.now()
        result: dict[str, str] = {}
        for name, entry in self._entries.items():
            elapsed = (now - entry.last_heartbeat).total_seconds()
            if elapsed > HEARTBEAT_TIMEOUT_SECONDS:
                entry.status = "unhealthy"
                result[name] = "unhealthy"
            else:
                result[name] = entry.status
        # 健康状态变更后持久化（仅当有 unhealthy 标记时）
        if any(v == "unhealthy" for v in result.values()):
            self._persist()
        return result

    def get(self, name: str) -> AgentRegistryEntry | None:
        """按名查询注册条目

        Returns:
            AgentRegistryEntry 或 None；feature flag 关闭返回 None
        """
        if not AGENT_REGISTRY_ENABLED:
            return None
        return self._entries.get(name)

    # ------------------------------------------------------------------
    # 辅助方法（不受 feature flag 限制，便于诊断）
    # ------------------------------------------------------------------

    def list_all(self) -> list[AgentRegistryEntry]:
        """列出所有注册条目（feature flag 关闭返回 []）"""
        if not AGENT_REGISTRY_ENABLED:
            return []
        return list(self._entries.values())

    def clear(self) -> None:
        """清空注册中心（主要用于测试）"""
        self._entries.clear()
        if AGENT_REGISTRY_ENABLED:
            self._persist()


# =====================================================================
# 全局单例（延迟初始化，避免 import 时读盘）
# =====================================================================

_registry_instance: AgentRegistry | None = None


def get_agent_registry() -> AgentRegistry:
    """获取全局 AgentRegistry 单例"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = AgentRegistry()
    return _registry_instance
