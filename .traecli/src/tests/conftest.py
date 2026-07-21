"""pytest 全局配置 - sys.path 设置 + mock LLM fixtures

把 /workspace/.traecli/src 加到 sys.path，让 `import deadman` 能工作。
LLM 调用全部走 mock，不真正调外部 API。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# === 把 src 目录加到 sys.path，让 import deadman 能工作 ===
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


@pytest.fixture
def mock_llm_client():
    """构造一个 mock LLMClient，所有 async 方法返回可定制的结果。

    用法：
        def test_xxx(mock_llm_client):
            mock_llm_client.chat.return_value = "你好"
            # 或对于 AsyncMock：mock_llm_client.chat = AsyncMock(return_value="...")
    """
    client = MagicMock()
    client.api_key = "test-key-not-real"  # 标记为可用，避免触发降级分支
    client.chat = AsyncMock(return_value="mock-response")
    client.chat_json = AsyncMock(return_value={"status": "ok"})
    client.sample_multiple = AsyncMock(
        return_value=["sample-1", "sample-2", "sample-3"]
    )
    client.provider = "openai"
    client.model = "gpt-4o-mock"
    return client


@pytest.fixture
def patch_llm(monkeypatch, mock_llm_client):
    """把 deadman.llm.llm_client 全局单例替换为 mock。

    测试中需要全局 llm_client 时用这个 fixture。
    """
    import deadman.llm as llm_module

    monkeypatch.setattr(llm_module, "llm_client", mock_llm_client)
    # 同步替换已经导入到各模块的引用
    import deadman.memory.manager as mm
    import deadman.memory.episodic as ep
    import deadman.reflexion.engine as rfe
    import deadman.orchestration.nodes as nodes

    monkeypatch.setattr(mm, "llm_client", mock_llm_client)
    monkeypatch.setattr(ep, "llm_client", mock_llm_client)
    monkeypatch.setattr(rfe, "llm_client", mock_llm_client)
    monkeypatch.setattr(nodes, "llm_client", mock_llm_client)
    return mock_llm_client


@pytest.fixture(autouse=True)
def _reset_global_singletons():
    """每个测试前后清空全局单例，避免相互污染。

    覆盖：tracer / metrics_collector / rule_loader 缓存。
    """
    # 测试前清理
    try:
        from deadman.observability.tracer import tracer
        from deadman.observability.metrics import metrics_collector

        tracer.clear()
        metrics_collector.clear()
    except Exception:
        pass
    yield
    # 测试后清理
    try:
        from deadman.observability.tracer import tracer
        from deadman.observability.metrics import metrics_collector

        tracer.clear()
        metrics_collector.clear()
    except Exception:
        pass
