"""上下文/token 预算管理 + 查询改写测试。"""

from __future__ import annotations

import pytest

from deadman.orchestration.context import build_context, estimate_tokens, trim_history
from deadman.research.query_rewrite import rewrite_query


class TestContext:
    def test_estimate_tokens_cjk_and_ascii(self):
        # 中文按字、英文按 4 字符/token
        assert estimate_tokens("北京") >= 2
        assert estimate_tokens("hello world") >= 2

    def test_trim_history_keeps_recent(self):
        history = [
            {"role": "user", "content": "第一轮问题" + "字" * 200},
            {"role": "assistant", "content": "第一轮回答" + "字" * 200},
            {"role": "user", "content": "第二轮问题"},
        ]
        trimmed = trim_history(history, budget=50)
        assert trimmed  # 保留最近
        # 预算极小 → 至少保留最新一条
        tiny = trim_history(history, budget=2)
        assert tiny and tiny[-1] == history[-1]

    def test_build_context(self):
        msgs = build_context(
            [{"role": "user", "content": "旧"}],
            "新问题",
            budget=30,
            system="系统提示",
        )
        assert msgs[0]["role"] == "system"
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "新问题"


class TestQueryRewrite:
    @pytest.mark.asyncio
    async def test_rewrite_without_llm_returns_original(self):
        q, changed = await rewrite_query("北京身后事材料")
        assert q == "北京身后事材料" and changed is False

    @pytest.mark.asyncio
    async def test_rewrite_empty(self):
        q, changed = await rewrite_query("   ")
        assert changed is False
