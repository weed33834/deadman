"""P4.3 Agent 注册中心 - 测试矩阵

覆盖点：
1. test_register_discover: 注册+按能力发现
2. test_unregister: 注销
3. test_heartbeat_updates_status: 心跳更新
4. test_health_check: 健康检查
5. test_persist_to_json: 持久化
6. test_disabled_no_change: feature flag 关闭
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import deadman.orchestration.agent_registry as registry_module
from deadman.a2a.models import AgentCard, AgentCardSkill
from deadman.orchestration.agent_registry import (
    HEARTBEAT_TIMEOUT_SECONDS,
    AgentRegistry,
)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _enable_registry(monkeypatch):
    """每个测试默认开启 agent registry feature flag"""
    monkeypatch.setattr(registry_module, "AGENT_REGISTRY_ENABLED", True)
    yield


@pytest.fixture
def tmp_registry_path(tmp_path) -> Path:
    """临时持久化文件路径（每个测试独立）"""
    return tmp_path / "agent_registry.json"


@pytest.fixture
def legal_card() -> AgentCard:
    return AgentCard(
        name="legal-advisor",
        description="法律顾问",
        version="1.0",
        url="http://localhost:8001/a2a",
        skills=[
            AgentCardSkill(
                id="inheritance",
                name="遗产继承",
                description="继承法咨询",
                tags=["legal", "inheritance"],
            ),
        ],
    )


@pytest.fixture
def financial_card() -> AgentCard:
    return AgentCard(
        name="financial-analyst",
        description="财务分析师",
        version="1.0",
        url="http://localhost:8002/a2a",
        skills=[
            AgentCardSkill(
                id="tax",
                name="税务规划",
                description="税务咨询",
                tags=["financial", "tax"],
            ),
        ],
    )


# =====================================================================
# 1. 注册+发现
# =====================================================================


class TestRegisterDiscover:
    def test_register_discover(self, tmp_registry_path, legal_card, financial_card):
        """register 返回 True，discover 按能力标签返回匹配的 AgentCard"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        assert reg.register(legal_card, capabilities=["legal", "inheritance"]) is True
        assert reg.register(financial_card, capabilities=["financial"]) is True

        # 按能力标签发现
        legal_agents = reg.discover("legal")
        assert len(legal_agents) == 1
        assert legal_agents[0].name == "legal-advisor"

        financial_agents = reg.discover("financial")
        assert len(financial_agents) == 1
        assert financial_agents[0].name == "financial-analyst"

        # inheritance 能力只属于 legal-advisor
        inh_agents = reg.discover("inheritance")
        assert len(inh_agents) == 1
        assert inh_agents[0].name == "legal-advisor"

    def test_discover_by_skill_tags(self, tmp_registry_path, legal_card):
        """discover 也能匹配 card.skills[].tags（兼容 A2A skill.tags 模型）"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        # 不传 capabilities，仅靠 card.skills.tags 匹配
        reg.register(legal_card)
        # "legal" 在 skill.tags 中
        agents = reg.discover("legal")
        assert len(agents) == 1
        # "inheritance" 也在 skill.tags 中
        agents = reg.discover("inheritance")
        assert len(agents) == 1

    def test_register_returns_false_for_empty_name(self, tmp_registry_path):
        """card.name 为空时返回 False"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        empty_card = AgentCard(name="", description="", version="", url="")
        assert reg.register(empty_card) is False

    def test_get_returns_entry(self, tmp_registry_path, legal_card):
        """get 按名返回 AgentRegistryEntry"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        reg.register(legal_card, capabilities=["legal"])
        entry = reg.get("legal-advisor")
        assert entry is not None
        assert entry.name == "legal-advisor"
        assert entry.capabilities == ["legal"]
        assert entry.status == "healthy"

    def test_get_returns_none_for_unknown(self, tmp_registry_path):
        """get 未知 agent 返回 None"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        assert reg.get("nonexistent") is None


# =====================================================================
# 2. 注销
# =====================================================================


class TestUnregister:
    def test_unregister(self, tmp_registry_path, legal_card):
        """unregister 已注册 agent 返回 True，未知 agent 返回 False"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        reg.register(legal_card)
        assert reg.unregister("legal-advisor") is True
        # 再次注销返回 False
        assert reg.unregister("legal-advisor") is False
        # 注销后 get 返回 None
        assert reg.get("legal-advisor") is None
        # 注销后 discover 找不到
        assert reg.discover("legal") == []


# =====================================================================
# 3. 心跳更新
# =====================================================================


class TestHeartbeatUpdatesStatus:
    def test_heartbeat_updates_status(self, tmp_registry_path, legal_card):
        """heartbeat 更新已有 agent 的 status 和 last_heartbeat"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        reg.register(legal_card)
        original_hb = reg.get("legal-advisor").last_heartbeat
        # 心跳更新状态为 "degraded"
        reg.heartbeat("legal-advisor", status="degraded")
        entry = reg.get("legal-advisor")
        assert entry.status == "degraded"
        assert entry.last_heartbeat >= original_hb

    def test_heartbeat_lazy_registers_unknown(self, tmp_registry_path):
        """heartbeat 未知 agent 时自动 lazy 注册"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        reg.heartbeat("new-agent", status="healthy")
        entry = reg.get("new-agent")
        assert entry is not None
        assert entry.status == "healthy"
        assert entry.name == "new-agent"


# =====================================================================
# 4. 健康检查
# =====================================================================


class TestHealthCheck:
    def test_health_check(self, tmp_registry_path, legal_card, monkeypatch):
        """health_check 标记心跳超时的 agent 为 unhealthy"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        reg.register(legal_card)
        # 手动把 last_heartbeat 调到很久以前
        reg._entries["legal-advisor"].last_heartbeat = datetime.now() - timedelta(
            seconds=HEARTBEAT_TIMEOUT_SECONDS + 100
        )
        health = reg.health_check()
        assert health["legal-advisor"] == "unhealthy"

    def test_health_check_healthy_agent(self, tmp_registry_path, legal_card):
        """心跳及时的 agent 保持 healthy"""
        reg = AgentRegistry(persist_path=tmp_registry_path)
        reg.register(legal_card)
        # 刚注册，心跳肯定及时
        health = reg.health_check()
        assert health["legal-advisor"] == "healthy"


# =====================================================================
# 5. 持久化
# =====================================================================


class TestPersistToJson:
    def test_persist_to_json(self, tmp_registry_path, legal_card):
        """register 后写入 JSON 文件，重启后能加载回来"""
        reg1 = AgentRegistry(persist_path=tmp_registry_path)
        reg1.register(legal_card, capabilities=["legal"])
        # 文件存在
        assert tmp_registry_path.exists()
        # 文件内容是合法 JSON
        data = json.loads(tmp_registry_path.read_text(encoding="utf-8"))
        assert "entries" in data
        assert len(data["entries"]) == 1
        assert data["entries"][0]["name"] == "legal-advisor"
        # 重新构造 AgentRegistry（模拟重启）能加载回来
        reg2 = AgentRegistry(persist_path=tmp_registry_path)
        entry = reg2.get("legal-advisor")
        assert entry is not None
        assert entry.name == "legal-advisor"
        assert entry.capabilities == ["legal"]

    def test_persist_survives_unregister(self, tmp_registry_path, legal_card, financial_card):
        """unregister 也持久化"""
        reg1 = AgentRegistry(persist_path=tmp_registry_path)
        reg1.register(legal_card)
        reg1.register(financial_card)
        reg1.unregister("legal-advisor")
        # 重启
        reg2 = AgentRegistry(persist_path=tmp_registry_path)
        assert reg2.get("legal-advisor") is None
        assert reg2.get("financial-analyst") is not None

    def test_load_corrupt_json_is_empty(self, tmp_registry_path):
        """加载损坏的 JSON 不抛异常，从空开始"""
        tmp_registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_registry_path.write_text("not a valid json {", encoding="utf-8")
        # 不抛异常
        reg = AgentRegistry(persist_path=tmp_registry_path)
        assert reg.list_all() == []


# =====================================================================
# 6. feature flag 关闭
# =====================================================================


class TestDisabledNoChange:
    def test_disabled_no_change(self, monkeypatch, tmp_registry_path, legal_card):
        """feature flag 关闭：写操作 no-op，读操作返回空"""
        monkeypatch.setattr(registry_module, "AGENT_REGISTRY_ENABLED", False)
        reg = AgentRegistry(persist_path=tmp_registry_path)
        # 写操作返回 False / no-op
        assert reg.register(legal_card) is False
        assert reg.unregister("any") is False
        reg.heartbeat("any")  # no-op
        # 读操作返回空
        assert reg.discover("legal") == []
        assert reg.get("any") is None
        assert reg.health_check() == {}
        assert reg.list_all() == []
        # 不持久化（文件不创建）
        assert not tmp_registry_path.exists()

    def test_disabled_does_not_load_existing_file(
        self, monkeypatch, tmp_registry_path, legal_card
    ):
        """feature flag 关闭时不加载已有文件"""
        # 先开启 flag 写入数据
        monkeypatch.setattr(registry_module, "AGENT_REGISTRY_ENABLED", True)
        reg1 = AgentRegistry(persist_path=tmp_registry_path)
        reg1.register(legal_card, capabilities=["legal"])
        assert tmp_registry_path.exists()
        # 关闭 flag 重新构造
        monkeypatch.setattr(registry_module, "AGENT_REGISTRY_ENABLED", False)
        reg2 = AgentRegistry(persist_path=tmp_registry_path)
        # 即使文件存在，get 仍返回 None（flag 关闭）
        assert reg2.get("legal-advisor") is None
