"""Supervisor 层级编排测试。"""

from __future__ import annotations

import pytest

from deadman.orchestration.supervisor import (
    DEFAULT_AGENT,
    SubTask,
    _aggregate,
    plan_tasks,
    supervise,
)


@pytest.mark.asyncio
async def test_plan_tasks_degrades_without_llm():
    tasks = await plan_tasks("问题")
    assert len(tasks) == 1
    assert tasks[0].agent == DEFAULT_AGENT


def test_subtask_to_dict():
    s = SubTask(agent="legal_advisor", question="遗产继承")
    d = s.to_dict()
    assert d["agent"] == "legal_advisor" and d["question"] == "遗产继承"


@pytest.mark.asyncio
async def test_supervise_empty_question():
    result = await supervise("   ")
    assert result.answer == ""


def test_aggregate_no_results():
    out = _aggregate("q", [], [])
    assert out == "未能完成子任务编排。"


@pytest.mark.asyncio
async def test_supervise_without_llm_returns_default_task():
    # 无 LLM → 单任务默认智能体；graph 会跑（无 key 降级），不抛异常
    result = await supervise("测试问题")
    assert len(result.tasks) == 1
    assert result.tasks[0].agent == DEFAULT_AGENT
    assert result.degraded is True
