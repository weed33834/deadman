"""LLM 智能办案助手测试。"""

from __future__ import annotations

import pytest

from deadman.org.smart_case import _rule_based, generate_case_brief, suggest_next_action


@pytest.mark.asyncio
async def test_suggest_rule_fallback_without_llm():
    # 无 LLM key → 规则化建议，next_status 合法
    r = await suggest_next_action({"status": "in_progress", "case_type": "funeral"})
    assert r["degraded"] is True
    assert r["next_status"] in ("pending_input", "closed")  # in_progress 的合法去向


@pytest.mark.asyncio
async def test_suggest_unknown_status_uses_created_fallback():
    r = await suggest_next_action({"status": "weird", "case_type": "funeral"})
    assert r["next_status"] in ("assigned", "in_progress", "cancelled")


def test_rule_based_has_required_keys():
    r = _rule_based("funeral", "created")
    assert {"next_status", "required_materials", "actions", "note", "degraded"} <= set(r)


@pytest.mark.asyncio
async def test_generate_case_brief_fallback():
    b = await generate_case_brief({"case_type": "funeral", "status": "in_progress"}, {"name": "王建国"})
    assert "王建国" in b
    assert "funeral" in b
