"""Supervisor 层级编排（P2）：主管拆解复杂请求 → 委派子智能体 → 聚合。

价值：单个请求需要多个领域智能体协作时（如"遗产继承 + 税务 + 跨境"），
由 supervisor 拆成子任务，逐个走子智能体（复用现有 build_main_graph，
不重复实现智能体执行），再把结果聚合成统一回答。

降级：LLM 不可用时按规则拆分（单子任务=原问题，原样走默认智能体）。

仅做编排层，不改动 8 个并列智能体的既有定义与规则链。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .nodes import AGENT_NAMES, DEFAULT_AGENT

logger = logging.getLogger(__name__)


@dataclass
class SubTask:
    """一个委派给子智能体的子任务。"""

    agent: str
    question: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SupervisorResult:
    """主管编排结果。"""

    question: str
    tasks: list[SubTask] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "tasks": [t.to_dict() for t in self.tasks],
            "results": self.results,
            "answer": self.answer,
            "degraded": self.degraded,
        }


def _llm_available() -> bool:
    from ..llm import llm_client

    return bool(getattr(llm_client, "api_key", None))


async def plan_tasks(question: str) -> list[SubTask]:
    """LLM 拆解为子任务；不可用/失败时退化为单任务（原问题 → 默认智能体）。"""
    if not _llm_available():
        return [SubTask(agent=DEFAULT_AGENT, question=question)]
    from ..llm import llm_client

    agent_names = ", ".join(a.replace("_", "-") for a in AGENT_NAMES)
    prompt = (
        "你是编排主管。把用户请求拆解为 1-3 个子任务，每个子任务分配给一个智能体。"
        f"可用智能体：{agent_names}。"
        '输出 JSON 数组：[{"agent":"death-aftercare","question":"子问题"},...]'
        "agent 必须来自可用智能体；question 为给该智能体的子问题。"
    )
    try:
        out = await llm_client.chat(
            [
                {"role": "system", "content": "你是多智能体编排主管，输出合法 JSON 数组。"},
                {"role": "user", "content": prompt + f"\n用户请求：{question}"},
            ],
            temperature=0.2,
        )
        arr = json.loads(out)
        if isinstance(arr, list):
            tasks: list[SubTask] = []
            for item in arr[:3]:
                agent = str(item.get("agent", "")).replace("-", "_")
                if agent not in AGENT_NAMES:
                    agent = DEFAULT_AGENT
                q = str(item.get("question", "")).strip()
                if q:
                    tasks.append(SubTask(agent=agent, question=q))
            if tasks:
                return tasks
    except Exception as exc:
        logger.warning("supervisor 拆解失败，退化单任务: %s", exc)
    return [SubTask(agent=DEFAULT_AGENT, question=question)]


async def supervise(question: str) -> SupervisorResult:
    """主管编排：拆解 → 委派子智能体（复用 build_main_graph）→ 聚合。"""
    if not question or not question.strip():
        return SupervisorResult(question=question or "", degraded=_llm_available())

    tasks = await plan_tasks(question.strip())

    from ..orchestration.graph import build_main_graph
    from ..orchestration.state import ConversationState

    graph = build_main_graph()
    results: list[dict[str, Any]] = []
    for i, task in enumerate(tasks):
        state = ConversationState(
            user_input=task.question,
            current_agent=task.agent,
            session_id=f"super-{i}-{hash(question) % 10000}",
            agent_name=task.agent,  # type: ignore[typeddict-unknown-key]
            user_id="supervisor",  # type: ignore[typeddict-unknown-key]
            history=[],  # type: ignore[typeddict-unknown-key]
        )
        try:
            rs = await graph.ainvoke(
                state, config={"configurable": {"thread_id": f"super-{i}"}}
            )
            resp = rs.get("final_response") or rs.get("draft_response", "")
            results.append(
                {"agent": task.agent.replace("_", "-"), "question": task.question, "response": resp}
            )
        except Exception as exc:  # pragma: no cover - 单子任务失败不阻断
            logger.warning("supervisor 子任务失败（跳过）: %s", exc)
            results.append(
                {"agent": task.agent.replace("_", "-"), "question": task.question, "error": str(exc)}
            )

    answer = _aggregate(question, tasks, results)
    return SupervisorResult(
        question=question.strip(),
        tasks=tasks,
        results=results,
        answer=answer,
        degraded=not _llm_available(),
    )


def _aggregate(question: str, tasks: list[SubTask], results: list[dict[str, Any]]) -> str:
    """聚合子智能体结果为统一回答（LLM 不可用时直接拼接各结果）。"""
    if not results:
        return "未能完成子任务编排。"
    if not _llm_available():
        parts = [f"【{r.get('agent', '-')}】{r.get('response') or r.get('error', '无响应')}" for r in results]
        return "\n\n".join(parts)

    from ..llm import llm_client

    blocks = "\n".join(
        f"[{r.get('agent', '-')}] {r.get('response') or r.get('error', '')}" for r in results
    )
    try:
        return llm_client.chat(
            [
                {"role": "system", "content": "你是编排主管，把子智能体结果汇总为连贯回答。"},
                {"role": "user", "content": f"请求：{question}\n子结果：\n{blocks}"},
            ],
            temperature=0.3,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("supervisor 聚合失败，直接拼接: %s", exc)
        return "\n\n".join(
            f"【{r.get('agent', '-')}】{r.get('response') or r.get('error', '')}" for r in results
        )
