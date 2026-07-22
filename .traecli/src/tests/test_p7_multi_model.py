"""P7: 多模型分工测试（借鉴 OpenDeepResearch configuration.py）

覆盖：
  - get_llm_for_use_case 工厂函数行为
  - 未配置专用模型时回退主 LLM
  - 配置 "provider:model" 后构造专用 client
  - 仅 model 名（无 provider）时沿用主 provider
  - API key 缺失时回退主 LLM
  - use_case 缓存复用
  - nodes.py / episodic.py 实际接入 get_llm_for_use_case
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest


# =====================================================================
# get_llm_for_use_case 工厂函数
# =====================================================================


class TestGetLLMForUseCase:
    """P7: get_llm_for_use_case 工厂函数核心行为"""

    def setup_method(self):
        """每个测试前清空 use_case 缓存，避免相互污染"""
        from deadman.llm import _llm_client_cache
        _llm_client_cache.clear()

    def test_unconfigured_falls_back_to_main_llm(self, monkeypatch):
        """未配置专用模型时，所有 use_case 都回退到主 llm_client"""
        from deadman.llm import get_llm_for_use_case, llm_client, _llm_client_cache
        from deadman.config import settings

        # 确保专用模型配置为空
        monkeypatch.setattr(settings, "llm_model_router", "")
        monkeypatch.setattr(settings, "llm_model_summarizer", "")
        monkeypatch.setattr(settings, "llm_model_respond", "")
        _llm_client_cache.clear()

        assert get_llm_for_use_case("router") is llm_client
        assert get_llm_for_use_case("summarizer") is llm_client
        assert get_llm_for_use_case("respond") is llm_client
        # 未知 use_case 也回退主 LLM
        assert get_llm_for_use_case("unknown_use_case") is llm_client

    def test_router_uses_dedicated_model_when_configured(self, monkeypatch):
        """配置 LLM_MODEL_ROUTER 后 router 用例返回专用 client"""
        from deadman.llm import get_llm_for_use_case, _llm_client_cache
        from deadman.config import settings

        monkeypatch.setattr(settings, "llm_provider", "openai")
        monkeypatch.setattr(settings, "llm_api_key", "sk-main")
        monkeypatch.setattr(settings, "llm_base_url", "")
        monkeypatch.setattr(settings, "llm_model_router", "openai:gpt-4o-mini")
        monkeypatch.setattr(settings, "llm_model_summarizer", "")
        monkeypatch.setattr(settings, "llm_model_respond", "")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-router")
        _llm_client_cache.clear()

        client = get_llm_for_use_case("router")
        assert client.provider == "openai"
        assert client.model == "gpt-4o-mini"
        assert client.api_key == "sk-test-router"

    def test_provider_model_format_parsed(self, monkeypatch):
        """provider:model 格式被正确解析，按 provider 取默认 base_url/env_key"""
        from deadman.llm import get_llm_for_use_case, _llm_client_cache
        from deadman.config import settings

        monkeypatch.setattr(settings, "llm_provider", "openai")
        monkeypatch.setattr(settings, "llm_model_respond", "anthropic:claude-3-5-sonnet")
        monkeypatch.setattr(settings, "llm_model_router", "")
        monkeypatch.setattr(settings, "llm_model_summarizer", "")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        _llm_client_cache.clear()

        client = get_llm_for_use_case("respond")
        assert client.provider == "anthropic"
        assert client.model == "claude-3-5-sonnet"
        assert client.api_key == "sk-ant-test"
        # Anthropic 默认 base_url
        assert "anthropic.com" in client.base_url

    def test_model_only_without_provider_uses_main_provider(self, monkeypatch):
        """仅 model 名（无 provider:前缀）时沿用主 provider 配置"""
        from deadman.llm import get_llm_for_use_case, _llm_client_cache
        from deadman.config import settings

        monkeypatch.setattr(settings, "llm_provider", "openai")
        monkeypatch.setattr(settings, "llm_api_key", "sk-main")
        monkeypatch.setattr(settings, "llm_base_url", "https://custom.api/v1")
        monkeypatch.setattr(settings, "llm_model_router", "gpt-4o-mini")
        monkeypatch.setattr(settings, "llm_model_summarizer", "")
        monkeypatch.setattr(settings, "llm_model_respond", "")
        _llm_client_cache.clear()

        client = get_llm_for_use_case("router")
        assert client.provider == "openai"
        assert client.model == "gpt-4o-mini"
        assert client.api_key == "sk-main"
        assert client.base_url == "https://custom.api/v1"

    def test_missing_api_key_falls_back_to_main(self, monkeypatch):
        """配置的 provider 未设置 API key 时回退主 LLM"""
        from deadman.llm import get_llm_for_use_case, llm_client, _llm_client_cache
        from deadman.config import settings

        monkeypatch.setattr(settings, "llm_model_router", "anthropic:claude-haiku-4-5")
        monkeypatch.setattr(settings, "llm_model_summarizer", "")
        monkeypatch.setattr(settings, "llm_model_respond", "")
        # 不设置 ANTHROPIC_API_KEY
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _llm_client_cache.clear()

        client = get_llm_for_use_case("router")
        # 回退到主 LLM
        assert client is llm_client

    def test_cache_reuses_same_client_instance(self, monkeypatch):
        """同一 use_case 多次调用返回同一 client 实例（缓存复用）"""
        from deadman.llm import get_llm_for_use_case, _llm_client_cache
        from deadman.config import settings

        monkeypatch.setattr(settings, "llm_model_router", "openai:gpt-4o-mini")
        monkeypatch.setattr(settings, "llm_model_summarizer", "")
        monkeypatch.setattr(settings, "llm_model_respond", "")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-cache-test")
        _llm_client_cache.clear()

        client1 = get_llm_for_use_case("router")
        client2 = get_llm_for_use_case("router")
        assert client1 is client2
        # 缓存命中
        assert "router" in _llm_client_cache

    def test_different_use_cases_get_different_clients(self, monkeypatch):
        """不同 use_case 配置不同模型时返回不同 client"""
        from deadman.llm import get_llm_for_use_case, _llm_client_cache
        from deadman.config import settings

        monkeypatch.setattr(settings, "llm_model_router", "openai:gpt-4o-mini")
        monkeypatch.setattr(settings, "llm_model_respond", "openai:gpt-4o")
        monkeypatch.setattr(settings, "llm_model_summarizer", "")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-multi-test")
        _llm_client_cache.clear()

        router_client = get_llm_for_use_case("router")
        respond_client = get_llm_for_use_case("respond")
        assert router_client is not respond_client
        assert router_client.model == "gpt-4o-mini"
        assert respond_client.model == "gpt-4o"


# =====================================================================
# nodes.py 实际接入验证
# =====================================================================


class TestNodesIntegration:
    """P7: 验证 nodes.py 实际调用 get_llm_for_use_case 而非直接 llm_client"""

    def test_router_node_calls_get_llm_for_use_case_router(self, monkeypatch):
        """router_node 应调用 get_llm_for_use_case('router')"""
        from deadman.orchestration import nodes
        from deadman.orchestration.state import create_initial_state

        mock_client = MagicMock()
        mock_client.api_key = "sk-test"
        mock_client.chat_json = AsyncMock(return_value={
            "agent": "death_aftercare",
            "reason": "test",
            "confidence": 0.9,
        })
        captured_use_cases: list[str] = []

        def fake_get_llm(use_case: str):
            captured_use_cases.append(use_case)
            return mock_client

        monkeypatch.setattr(nodes, "get_llm_for_use_case", fake_get_llm)

        state = create_initial_state("我想咨询身后事")
        import asyncio
        asyncio.run(nodes.router_node(state))

        assert "router" in captured_use_cases

    def test_agent_node_calls_get_llm_for_use_case_respond(self, monkeypatch):
        """agent_node 应调用 get_llm_for_use_case('respond')"""
        from deadman.orchestration import nodes
        from deadman.orchestration.state import create_initial_state

        mock_client = MagicMock()
        mock_client.api_key = "sk-test"
        mock_client.chat = AsyncMock(return_value="智能体响应内容")
        captured_use_cases: list[str] = []

        def fake_get_llm(use_case: str):
            captured_use_cases.append(use_case)
            return mock_client

        monkeypatch.setattr(nodes, "get_llm_for_use_case", fake_get_llm)

        state = create_initial_state("咨询身后事")
        state["current_agent"] = "death_aftercare"
        import asyncio
        asyncio.run(nodes.agent_node(state))

        assert "respond" in captured_use_cases

    def test_user_confirm_node_calls_get_llm_for_use_case_respond(self, monkeypatch):
        """user_confirm_node 生成转介话术应调用 get_llm_for_use_case('respond')"""
        from deadman.orchestration import nodes
        from deadman.orchestration.state import create_initial_state
        from deadman.types import TransferSummary

        mock_client = MagicMock()
        mock_client.api_key = "sk-test"
        mock_client.chat = AsyncMock(return_value="转介话术")
        captured_use_cases: list[str] = []

        def fake_get_llm(use_case: str):
            captured_use_cases.append(use_case)
            return mock_client

        monkeypatch.setattr(nodes, "get_llm_for_use_case", fake_get_llm)

        state = create_initial_state("x")
        state["pending_transfer"] = TransferSummary(
            from_agent="death_aftercare",
            to_agent="legal_advisor",
            reason="测试转介",
            user_situation="测试",
            current_question="测试",
            completed_items=[],
            pending_items=[],
        )
        # transfer_confirmed 为 None 触发话术生成
        state["transfer_confirmed"] = None

        import asyncio
        asyncio.run(nodes.user_confirm_node(state))

        assert "respond" in captured_use_cases


# =====================================================================
# episodic.py 实际接入验证
# =====================================================================


class TestEpisodicIntegration:
    """P7: 验证 episodic._summarize_turn 调用 get_llm_for_use_case('summarizer')"""

    def test_summarize_turn_calls_get_llm_for_use_case_summarizer(self, monkeypatch):
        from deadman.memory.episodic import EpisodicMemory
        from deadman.llm import _llm_client_cache

        mock_client = MagicMock()
        mock_client.api_key = "sk-test"
        mock_client.chat = AsyncMock(return_value="摘要内容")
        captured_use_cases: list[str] = []

        def fake_get_llm(use_case: str):
            captured_use_cases.append(use_case)
            return mock_client

        # episodic 通过 from ..llm import get_llm_for_use_case 导入
        import deadman.llm as llm_module
        monkeypatch.setattr(llm_module, "get_llm_for_use_case", fake_get_llm)
        # episodic 模块内的 get_llm_for_use_case 引用也要替换
        import deadman.memory.episodic as ep
        monkeypatch.setattr(ep, "get_llm_for_use_case", fake_get_llm)
        _llm_client_cache.clear()

        memory = EpisodicMemory()
        turn = {"role": "user", "content": "测试内容", "agent": "death_aftercare"}

        import asyncio
        result = asyncio.run(memory._summarize_turn(turn))

        assert "summarizer" in captured_use_cases
        assert result == "摘要内容"
