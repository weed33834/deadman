"""Plan-and-Execute 编排模式 - PlannerAgent 产出 DAG + Executor 逐节点执行

P1.1 实现（v1.2 计划文档 P1.1）：复杂问题先由 Planner 用强模型产出 DAG
（步骤 + 依赖关系），Executor 按 DAG 拓扑序逐节点执行（可调 ReActLoop 或
便宜模型）。

核心数据结构：
- PlanStep: DAG 节点 {step_id, action, tool_hint, depends_on, expected_output, status}
- Plan: 完整 DAG {plan_id, steps, created_at, source_query, cache_hit, degraded}

韧性 / 安全特性（三大铁律）：
- feature flag: DEADMAN_PLAN_EXECUTE_ENABLED=0（默认关闭，保留 ReAct 旧行为）
- PlanCache: 相似问题（关键词 Jaccard > 0.85）复用历史 Plan（内存 LRU 100 条）
- 失败回退：Planner 失败 → 返回 Plan(degraded=True)，调用方降级到 ReAct
- LLM 不可用 → 返回降级 Plan，调用方自行降级
- chat_json 解析失败 / 无 steps → 回退到降级 Plan
- 单步执行失败 → 该步标记 failed，下游步骤标记 skipped（不阻断整体）

降级路径：
- LLM 不可用 → Plan(degraded=True)
- chat_json 抛异常 → Plan(degraded=True)
- steps 字段缺失或空 → Plan(degraded=True)
- 单步执行抛异常 → step 标记 failed，继续后续无依赖步骤
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..llm import LLMClient
from ..utils.text_similarity import jaccard_similarity as _jaccard_sim
from ..utils.text_similarity import tokenize as _tokenize

logger = logging.getLogger(__name__)

# =====================================================================
# 配置（全部 feature flag，默认安全）
# =====================================================================

# Plan-and-Execute 总开关：默认关闭，保留 ReAct 旧行为
PLAN_EXECUTE_ENABLED: bool = os.environ.get("DEADMAN_PLAN_EXECUTE_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Plan 缓存大小（LRU）
PLAN_CACHE_MAX_SIZE: int = int(os.environ.get("DEADMAN_PLAN_CACHE_SIZE", "100"))

# 相似度阈值（Jaccard > 此值则复用历史 Plan）
PLAN_CACHE_JACCARD_THRESHOLD: float = float(os.environ.get("DEADMAN_PLAN_CACHE_JACCARD", "0.85"))

# 复杂度判断阈值
COMPLEX_QUERY_MIN_KEYWORDS: int = int(os.environ.get("DEADMAN_COMPLEX_QUERY_MIN_KEYWORDS", "3"))
COMPLEX_QUERY_MIN_LENGTH: int = int(os.environ.get("DEADMAN_COMPLEX_QUERY_MIN_LENGTH", "100"))


# =====================================================================
# 辅助函数 - 使用共享 text_similarity 模块
# =====================================================================


def _jaccard_similarity(a: str, b: str) -> float:
    """Jaccard 相似度（代理到共享模块）。"""
    return _jaccard_sim(_tokenize(a), _tokenize(b))


# =====================================================================
# 数据结构
# =====================================================================


@dataclass
class PlanStep:
    """DAG 节点 - 单步执行计划"""

    step_id: str
    action: str
    tool_hint: str = ""
    depends_on: list[str] = field(default_factory=list)
    expected_output: str = ""
    status: str = "pending"  # pending / running / done / failed / skipped

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.action,
            "tool_hint": self.tool_hint,
            "depends_on": list(self.depends_on),
            "expected_output": self.expected_output,
            "status": self.status,
        }


@dataclass
class Plan:
    """完整执行计划 - DAG

    degraded=True 表示 Planner 失败，调用方应降级到 ReAct。
    """

    plan_id: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    created_at: str = ""
    source_query: str = ""
    cache_hit: bool = False
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "source_query": self.source_query,
            "cache_hit": self.cache_hit,
            "degraded": self.degraded,
        }


# =====================================================================
# 复杂度判断
# =====================================================================


def is_complex_query(query: str) -> bool:
    """判断 query 是否需要 Plan-Execute 模式

    规则：关键词数 >= 3 或 长度 > 100

    Args:
        query: 用户问题

    Returns:
        True 表示复杂问题，建议走 Plan-Execute
    """
    if not query:
        return False
    if len(query) > COMPLEX_QUERY_MIN_LENGTH:
        return True
    keywords = _tokenize(query)
    return len(keywords) >= COMPLEX_QUERY_MIN_KEYWORDS


# =====================================================================
# PlanCache - LRU + Jaccard 相似度
# =====================================================================


class PlanCache:
    """相似问题复用历史 Plan（内存 LRU）

    - get: 精确命中或 Jaccard > 阈值的相似 query 命中
    - put: 写入；降级 Plan 不缓存
    - LRU: 超过 max_size 淘汰最久未访问
    """

    def __init__(self, max_size: int = PLAN_CACHE_MAX_SIZE) -> None:
        self._store: OrderedDict[str, Plan] = OrderedDict()
        self.max_size = max(1, max_size)

    def get(self, query: str) -> Plan | None:
        """精确命中或 Jaccard > 阈值的相似 query 命中"""
        if not query:
            return None
        # 1. 精确命中
        if query in self._store:
            self._store.move_to_end(query)
            plan = self._store[query]
            plan.cache_hit = True
            return plan
        # 2. 相似命中
        best_query: str | None = None
        best_score: float = 0.0
        for cached_query in self._store:
            score = _jaccard_similarity(query, cached_query)
            if score > PLAN_CACHE_JACCARD_THRESHOLD and score > best_score:
                best_score = score
                best_query = cached_query
        if best_query is not None:
            self._store.move_to_end(best_query)
            plan = self._store[best_query]
            plan.cache_hit = True
            return plan
        return None

    def put(self, query: str, plan: Plan) -> None:
        """写入缓存；降级 Plan 不缓存（避免污染）"""
        if not query or plan.degraded:
            return
        # 复制一份避免外部修改影响缓存
        cached = Plan(
            plan_id=plan.plan_id,
            steps=[
                PlanStep(
                    step_id=s.step_id,
                    action=s.action,
                    tool_hint=s.tool_hint,
                    depends_on=list(s.depends_on),
                    expected_output=s.expected_output,
                    status="pending",  # 缓存中状态重置
                )
                for s in plan.steps
            ],
            created_at=plan.created_at,
            source_query=plan.source_query,
            cache_hit=False,
            degraded=False,
        )
        self._store[query] = cached
        self._store.move_to_end(query)
        # LRU 淘汰
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# =====================================================================
# PlannerAgent
# =====================================================================


_PLANNER_PROMPT = """你是身后事平台 PlannerAgent。请将用户问题分解为可执行的 DAG 步骤。

# 输出格式（必须严格 JSON）
{{
  "steps": [
    {{
      "step_id": "s1",
      "action": "具体动作描述",
      "tool_hint": "建议使用的工具名（可空）",
      "depends_on": [],
      "expected_output": "预期输出描述"
    }},
    {{
      "step_id": "s2",
      "action": "...",
      "tool_hint": "...",
      "depends_on": ["s1"],
      "expected_output": "..."
    }}
  ]
}}

# 约束
- 步骤数 1-5 个，过多反而难执行
- depends_on 只能引用前序 step_id
- 复杂问题才需要多步，简单问题单步即可

# 用户问题
{user_input}

# 输出
只输出 JSON，不要其他文本："""


class PlannerAgent:
    """Plan-and-Execute 模式的 Planner + Executor

    用法：
        agent = PlannerAgent(llm=strong_llm)
        plan = await agent.plan(query)
        if plan.degraded:
            # 降级到 ReAct
        else:
            result = await agent.execute(plan)
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        cache: PlanCache | None = None,
    ) -> None:
        self.llm = llm
        self.cache = cache if cache is not None else PlanCache()

    async def plan(self, query: str) -> Plan:
        """生成 Plan：先查缓存，未命中调 LLM 产出 DAG"""
        # 1. 查缓存
        cached = self.cache.get(query)
        if cached is not None:
            logger.info("PlannerAgent 缓存命中: %s", query[:80])
            return cached

        # 2. LLM 不可用 → 降级
        if not self.llm or not getattr(self.llm, "api_key", ""):
            logger.info("PlannerAgent LLM 不可用，返回降级 Plan")
            return Plan(
                plan_id="",
                steps=[],
                created_at=datetime.now().isoformat(),
                source_query=query,
                degraded=True,
            )

        # 3. 调 LLM 产出 DAG
        prompt = _PLANNER_PROMPT.format(user_input=query)
        try:
            data = await self.llm.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
            )
        except Exception as e:
            logger.warning("PlannerAgent LLM 调用失败: %s", e)
            return Plan(
                plan_id="",
                steps=[],
                created_at=datetime.now().isoformat(),
                source_query=query,
                degraded=True,
            )

        # 4. 解析 LLM 输出
        steps_data = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps_data, list) or not steps_data:
            logger.warning("PlannerAgent LLM 输出无 steps 字段，降级")
            return Plan(
                plan_id="",
                steps=[],
                created_at=datetime.now().isoformat(),
                source_query=query,
                degraded=True,
            )

        steps: list[PlanStep] = []
        for raw in steps_data:
            if not isinstance(raw, dict):
                continue
            steps.append(
                PlanStep(
                    step_id=str(raw.get("step_id", f"s{len(steps) + 1}")),
                    action=str(raw.get("action", "")),
                    tool_hint=str(raw.get("tool_hint", "")),
                    depends_on=[str(d) for d in (raw.get("depends_on") or []) if d],
                    expected_output=str(raw.get("expected_output", "")),
                )
            )
        if not steps:
            return Plan(
                plan_id="",
                steps=[],
                created_at=datetime.now().isoformat(),
                source_query=query,
                degraded=True,
            )

        plan = Plan(
            plan_id=f"plan-{uuid.uuid4().hex[:8]}",
            steps=steps,
            created_at=datetime.now().isoformat(),
            source_query=query,
        )
        # 5. 写缓存
        self.cache.put(query, plan)
        return plan

    async def execute(self, plan: Plan) -> dict[str, Any]:
        """逐节点执行 Plan（拓扑序），返回每步结果

        Returns:
            {
                "degraded": bool,
                "results": [{"step_id", "ok", "output", "error"}, ...],
                "plan": plan.to_dict(),
            }
        """
        if plan.degraded or not plan.steps:
            return {"degraded": True, "results": [], "plan": plan.to_dict()}

        results: list[dict[str, Any]] = []
        done_steps: set[str] = set()
        pending = list(plan.steps)
        # 多轮扫描直到所有步骤处理完或无法推进（依赖未满足）
        max_rounds = len(plan.steps) + 1
        for _ in range(max_rounds):
            progressed = False
            still_pending: list[PlanStep] = []
            for step in pending:
                # 检查依赖是否都完成（失败的依赖也算"处理过"，但下游会 skipped）
                if any(
                    dep not in done_steps
                    for dep in step.depends_on
                    if dep in {s.step_id for s in plan.steps}
                ):
                    still_pending.append(step)
                    continue
                # 执行该步骤
                step.status = "running"
                step_result = await self._execute_step(step, results)
                results.append(step_result)
                if step_result.get("ok"):
                    step.status = "done"
                    done_steps.add(step.step_id)
                else:
                    step.status = "failed"
                progressed = True
            pending = still_pending
            if not pending or not progressed:
                break

        # 剩余 pending 标记 skipped（依赖失败）
        for step in pending:
            step.status = "skipped"

        return {
            "degraded": False,
            "results": results,
            "plan": plan.to_dict(),
        }

    async def _execute_step(
        self, step: PlanStep, prior_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """执行单步：调 LLM 用 prior_results 作为上下文"""
        if not self.llm or not getattr(self.llm, "api_key", ""):
            return {
                "step_id": step.step_id,
                "ok": False,
                "output": "",
                "error": "llm_unavailable",
            }
        context = json.dumps(
            [{"step_id": r["step_id"], "output": r.get("output", "")} for r in prior_results],
            ensure_ascii=False,
            default=str,
        )
        prompt = (
            f"执行以下步骤并输出结果。\n"
            f"动作: {step.action}\n"
            f"预期输出: {step.expected_output}\n"
            f"前序步骤结果: {context}\n"
            f"输出（纯文本，不要解释）:"
        )
        try:
            output = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            return {
                "step_id": step.step_id,
                "ok": True,
                "output": output,
                "error": "",
            }
        except Exception as e:
            logger.warning("PlannerAgent 执行步骤 %s 失败: %s", step.step_id, e)
            return {
                "step_id": step.step_id,
                "ok": False,
                "output": "",
                "error": f"{type(e).__name__}: {e}",
            }
