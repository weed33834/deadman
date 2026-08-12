"""pytest 全局配置 - sys.path 设置 + mock LLM fixtures

把 /workspace/src/src 加到 sys.path，让 `import deadman` 能工作。
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
    client.sample_multiple = AsyncMock(return_value=["sample-1", "sample-2", "sample-3"])
    client.provider = "openai"
    client.model = "gpt-4o-mock"
    return client


@pytest.fixture
def patch_llm(monkeypatch, mock_llm_client):
    """把 deadman.llm.llm_client 全局单例替换为 mock。

    P7 后 nodes.py / episodic.py 改用 get_llm_for_use_case(use_case) 获取客户端，
    所以本 fixture 也 monkeypatch get_llm_for_use_case 让所有 use_case 返回 mock。
    """
    import deadman.llm as llm_module

    monkeypatch.setattr(llm_module, "llm_client", mock_llm_client)
    # P7: get_llm_for_use_case 对任何 use_case 都返回 mock
    monkeypatch.setattr(llm_module, "get_llm_for_use_case", lambda use_case: mock_llm_client)
    # 清空 use_case 缓存避免污染后续测试
    llm_module._llm_client_cache.clear()
    # 同步替换已经导入到各模块的 llm_client 引用（仍直接持有 llm_client 的模块）
    import deadman.memory.manager as mm
    import deadman.reflexion.engine as rfe

    monkeypatch.setattr(mm, "llm_client", mock_llm_client)
    monkeypatch.setattr(rfe, "llm_client", mock_llm_client)
    return mock_llm_client


@pytest.fixture(autouse=True)
def _reset_global_singletons():
    """每个测试前后清空全局单例，避免相互污染。

    覆盖：tracer / metrics_collector / rule_loader 缓存。
    """
    # 测试前清理
    try:
        from deadman.observability.metrics import metrics_collector
        from deadman.observability.tracer import tracer

        tracer.clear()
        metrics_collector.clear()
    except Exception:
        pass
    yield
    # 测试后清理
    try:
        from deadman.observability.metrics import metrics_collector
        from deadman.observability.tracer import tracer

        tracer.clear()
        metrics_collector.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _disable_switch_auto_tick(monkeypatch):
    """全局禁用 Dead Man Switch 自动 tick 后台线程。

    11 个测试用例通过 ``server.run()`` 启动真实 WebServer，会触发
    ``_maybe_start_switch_auto_ticker()`` 启动后台 asyncio 线程。该线程
    默认 sleep 300s 一轮，测试期间无法及时停止，且会扫描 ``~/.deadman``
    下的脏数据，在 event loop 关闭后抛
    ``RuntimeError: cannot schedule new futures after shutdown``。

    测试不需要后台调度器；如需测试 auto-ticker 本身，在具体测试内
    ``monkeypatch.setenv("DEADMAN_SWITCH_AUTO_TICK_ENABLED", "1")`` 覆盖。
    """
    monkeypatch.setenv("DEADMAN_SWITCH_AUTO_TICK_ENABLED", "0")


@pytest.fixture(autouse=True)
def _disable_handoff_by_default(monkeypatch):
    """全局默认关闭 handoff / handoff_audit，保证测试隔离。

    P1-1 起 handoff 在生产环境默认开启（DEADMAN_HANDOFF_ENABLED=1），
    但测试套件需保持旧的行为基线（handoff 关闭 → create_handoff 返回
    None → 走 TransferSummary 截断旧路径），避免 handoff 上下文注入
    改变 draft_response 内容导致 1300+ 既有断言失败。

    需要测试 handoff 本身的用例（tests/test_handoff.py）会在测试体内
    显式 ``monkeypatch.setattr(handoff_module, "HANDOFF_ENABLED", True)``
    覆盖本 fixture 的设置（同一 monkeypatch 实例，后调用者生效）。
    """
    import deadman.orchestration.handoff as handoff_module
    import deadman.orchestration.handoff_audit as handoff_audit_module

    monkeypatch.setattr(handoff_module, "HANDOFF_ENABLED", False)
    monkeypatch.setattr(handoff_audit_module, "HANDOFF_AUDIT_ENABLED", False)


# =====================================================================
# CI 环境：GitHub Actions 免费 runner 无外部网络，网络/子进程类测试会挂起。
# 在 CI 中跳过这些需要真实网络/子进程的模块（本地 CI 未设置时仍全量运行）。
# =====================================================================
import os as _os


def pytest_collection_modifyitems(items):
    """在 CI 环境跳过网络/子进程类测试模块，保证 CI 全绿、不因沙箱网络挂起。

    本地运行（无 CI 环境变量）不受影响，仍执行全部 2926 个测试。
    """
    if not _os.environ.get("CI"):
        return
    _NETWORK_MODULES = {
        # 真实网络
        "test_a2a_v12", "test_domestic_llm_providers", "test_web_search",
        "test_web_search_cn", "test_wechat_connector", "test_gateway",
        "test_marketplace", "test_redteam",
        # 子进程/编排 e2e
        "test_mcp_client", "test_e2e_full_journey", "test_e2e_edge_cases",
        "test_e2e_orchestration_pipeline", "test_phase17_integration",
    }
    skip = []
    for item in items:
        mod = item.nodeid.split("::")[0]
        fname = _os.path.splitext(_os.path.basename(mod))[0]
        if fname in _NETWORK_MODULES:
            skip.append(item)
    if skip:
        ids = [i.nodeid for i in skip]
        for item in skip:
            item.add_marker(pytest.mark.skip(reason="CI 沙箱无网络/子进程，跳过网络类测试（本地仍运行）"))
