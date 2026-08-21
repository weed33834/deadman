# Reflexion 反思重试机制

> 本文件定义智能体调用失败时的反思-调整-重试机制。借鉴 Reflexion（Shinn 等, 2023）、Reflexion Agent、Self-Refine、CRITIC（Self-Correcting with Tool-Integrated Critiquing）。

## 为什么需要 Reflexion

### 当前痛点

TEAM.md 已定义子智能体调用失败的处理：
- execution_mode: success / fallback / failed
- fallback 时由父智能体降级处理

但 fallback 是"放弃调用，父智能体自己上"，没有"分析为什么失败，调整后再试一次"。

```
当前流程：
1. death-aftercare 调用 death-aftercare-emotional
2. 失败（如平台不支持 subagent）
3. fallback：death-aftercare 自己评估情绪
4. 完成

问题：
- 失败原因未记录
- 下次同样失败
- 父智能体降级质量可能不如子智能体
```

### Reflexion 改进

```
改进流程：
1. death-aftercare 调用 death-aftercare-emotional
2. 失败
3. Reflexion：分析失败原因（如"子智能体调用格式错误"）
4. 调整：修正调用参数（如改为更简单的 prompt）
5. 重试：再调一次
6. 若仍失败 → fallback
7. 反思记忆：记录失败模式，下次避免

优势：
- 部分失败可恢复（如参数错误、超时）
- 失败模式可学习（跨会话）
- 减少不必要的 fallback
```

## 适用场景

### 场景 1：子智能体调用失败

```python
# 失败类型
FAILURE_TYPES = {
    "platform_not_supported": "平台不支持 subagent",
    "timeout": "子智能体响应超时",
    "format_error": "子智能体返回格式错误",
    "schema_validation_failed": "返回内容不符合预期 schema",
    "empty_response": "子智能体返回空",
    "rule_violation": "子智能体输出违反规则",
    "context_too_long": "上下文超长",
}
```

### 场景 2：工具调用失败

```python
TOOL_FAILURE_TYPES = {
    "tool_not_found": "工具不存在",
    "invalid_args": "参数错误",
    "tool_execution_error": "工具执行出错",
    "tool_timeout": "工具超时",
    "rate_limit": "频率限制",
    "permission_denied": "权限不足",
}
```

### 场景 3：转介失败

```python
TRANSFER_FAILURE_TYPES = {
    "user_declined": "用户拒绝转介",
    "target_agent_not_found": "目标智能体不存在",
    "transfer_summary_incomplete": "转介摘要不完整",
    "target_agent_busy": "目标智能体忙",
}
```

### 不适用 Reflexion 的场景

以下场景**不重试**，直接 fallback：
- 涉及安全的失败（如检测到 R3 危机）→ 立即 fallback 到 safety-protocol
- 规则违反（如子智能体输出违规内容）→ 不重试，直接降级
- 用户主动取消 → 不重试

## Reflexion 流程

```
┌──────────────────────────────────────────┐
│ 1. 智能体调用子智能体/工具/转介          │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 2. 调用结果评估                          │
│    - 成功 → 返回结果                     │
│    - 失败 → 进入 Reflexion               │
└────────────────┬─────────────────────────┘
                 ▼ (失败时)
┌──────────────────────────────────────────┐
│ 3. 失败原因分析（Reflexion）             │
│    - LLM 分析失败原因                    │
│    - 提取失败模式                        │
│    - 生成调整策略                        │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 4. 重试次数检查                          │
│    - 已重试 < 3 次 → 重试                │
│    - 已重试 ≥ 3 次 → fallback            │
└────────────────┬─────────────────────────┘
                 ▼ (重试)
┌──────────────────────────────────────────┐
│ 5. 调整调用参数                          │
│    - 加入反思到 prompt                   │
│    - 调整参数（如更简单）                │
│    - 切换策略（如换子智能体）            │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 6. 重试调用                              │
└────────────────┬─────────────────────────┘
                 ▼
       （回到步骤 2 评估）
```

## 实现

### Reflexion Engine

```python
# agents/reflexion.py（伪代码）

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

MAX_RETRIES = 3


@dataclass
class FailureRecord:
    """单次失败记录"""

    attempt: int  # 第几次尝试
    failure_type: str  # 失败类型
    failure_message: str  # 失败详情
    input_summary: str  # 输入摘要
    output_summary: Optional[str]  # 输出摘要（若有）
    timestamp: datetime


@dataclass
class Reflection:
    """反思结果"""

    failure_pattern: str  # 失败模式分类
    root_cause: str  # 根本原因
    adjustment_strategy: str  # 调整策略
    adjusted_prompt: Optional[str]  # 调整后的 prompt
    adjusted_args: Optional[dict]  # 调整后的参数
    alternative_approach: Optional[str]  # 替代方案


@dataclass
class ReflexionMemory:
    """跨会话反思记忆（与 Graphiti 集成）"""

    agent_name: str  # 哪个智能体的记忆
    failure_patterns: dict  # 失败模式 → 出现次数
    successful_adjustments: dict  # 失败模式 → 成功的调整策略
    last_updated: datetime


class ReflexionEngine:
    def __init__(self, agent_name: str, memory_store=None):
        self.agent_name = agent_name
        self.memory_store = memory_store  # Graphiti 客户端
        self.failures: List[FailureRecord] = []
        self.reflections: List[Reflection] = []

    async def execute_with_reflexion(
        self,
        operation: callable,
        initial_input: dict,
        operation_type: str,  # "subagent_call" | "tool_call" | "transfer"
    ) -> dict:
        """
        带反思重试的操作执行器。
        """
        current_input = initial_input

        for attempt in range(1, MAX_RETRIES + 1):
            # 1. 执行操作
            try:
                result = await operation(**current_input)

                # 2. 评估结果
                evaluation = self._evaluate_result(result, operation_type)

                if evaluation["success"]:
                    # 成功
                    if attempt > 1:
                        # 重试成功 → 记录成功的调整策略
                        await self._record_successful_adjustment(
                            self.failures[-1].failure_type, self.reflections[-1].adjustment_strategy
                        )
                    return {"success": True, "result": result, "attempts": attempt}

                else:
                    # 失败
                    failure = FailureRecord(
                        attempt=attempt,
                        failure_type=evaluation["failure_type"],
                        failure_message=evaluation["failure_message"],
                        input_summary=str(current_input)[:200],
                        output_summary=str(result)[:200] if result else None,
                        timestamp=datetime.now(),
                    )
                    self.failures.append(failure)

                    # 3. Reflexion：分析失败原因
                    reflection = await self._reflect(failure, operation_type)
                    self.reflections.append(reflection)

                    # 4. 调整输入
                    current_input = self._adjust_input(current_input, reflection)

                    # 记录 trace
                    self._log_reflexion_span(failure, reflection, attempt)

                    continue

            except Exception as e:
                # 异常失败
                failure = FailureRecord(
                    attempt=attempt,
                    failure_type="exception",
                    failure_message=str(e),
                    input_summary=str(current_input)[:200],
                    output_summary=None,
                    timestamp=datetime.now(),
                )
                self.failures.append(failure)

                reflection = await self._reflect(failure, operation_type)
                self.reflections.append(reflection)

                current_input = self._adjust_input(current_input, reflection)
                continue

        # 重试耗尽 → fallback
        return {
            "success": False,
            "fallback": True,
            "attempts": MAX_RETRIES,
            "failures": self.failures,
            "reflections": self.reflections,
            "fallback_reason": self._determine_fallback_reason(),
        }

    async def _reflect(self, failure: FailureRecord, operation_type: str) -> Reflection:
        """用 LLM 分析失败原因，生成调整策略"""

        # 加载历史反思记忆（如果有）
        memory = await self._load_memory()
        historical_pattern = memory.failure_patterns.get(failure.failure_type, 0) if memory else 0
        historical_adjustment = (
            memory.successful_adjustments.get(failure.failure_type) if memory else None
        )

        prompt = f"""
你是 Reflexion 反思引擎。分析以下失败，生成调整策略。

## 操作类型
{operation_type}

## 失败记录
- 第 {failure.attempt} 次尝试
- 失败类型：{failure.failure_type}
- 失败信息：{failure.failure_message}
- 输入摘要：{failure.input_summary}
- 输出摘要：{failure.output_summary or "无"}

## 历史经验
- 此类失败历史出现次数：{historical_pattern}
- 历史成功调整策略：{historical_adjustment or "无"}

## 反思任务
1. 分析失败的根本原因（不要只看表面）
2. 分类失败模式（如"参数格式错误"、"上下文过长"）
3. 生成调整策略：
   - 调整 prompt（更简洁/更明确）
   - 调整参数（如改用更小的查询范围）
   - 切换策略（如换子智能体/换工具）
4. 给出具体可执行的调整

## 输出 JSON
{{
  "failure_pattern": "...",
  "root_cause": "...",
  "adjustment_strategy": "...",
  "adjusted_prompt": "...",
  "adjusted_args": {{}},
  "alternative_approach": "..."
}}
"""
        result = await call_llm(prompt)
        return parse_reflection(result)

    def _adjust_input(self, original_input: dict, reflection: Reflection) -> dict:
        """根据反思调整输入"""
        adjusted = original_input.copy()

        # 应用 prompt 调整
        if reflection.adjusted_prompt:
            adjusted["prompt"] = reflection.adjusted_prompt

        # 应用参数调整
        if reflection.adjusted_args:
            adjusted.update(reflection.adjusted_args)

        # 加入反思历史到 prompt
        if "prompt" in adjusted:
            adjusted["prompt"] = self._inject_reflection_context(
                adjusted["prompt"],
                self.failures[-3:],  # 最近 3 次失败
            )

        return adjusted

    def _inject_reflection_context(self, prompt: str, recent_failures: list) -> str:
        """把最近的失败反思加入 prompt，避免重复错误"""
        if not recent_failures:
            return prompt

        reflection_context = "\n\n## 历史失败与反思（避免重复）\n"
        for f, r in zip(recent_failures, self.reflections[-len(recent_failures) :]):
            reflection_context += f"""
- 第 {f.attempt} 次失败：{f.failure_type}
  反思：{r.root_cause}
  本次调整：{r.adjustment_strategy}
"""

        return prompt + reflection_context

    def _evaluate_result(self, result: dict, operation_type: str) -> dict:
        """评估操作结果是否成功"""
        if operation_type == "subagent_call":
            if result.get("execution_mode") == "success":
                return {"success": True}
            elif result.get("execution_mode") == "fallback":
                return {
                    "success": False,
                    "failure_type": result.get("fallback_reason", "unknown"),
                    "failure_message": "子智能体进入 fallback 模式",
                }
            else:
                return {
                    "success": False,
                    "failure_type": "execution_failed",
                    "failure_message": str(result),
                }

        elif operation_type == "tool_call":
            if result.get("success", True) and not result.get("error"):
                return {"success": True}
            else:
                return {
                    "success": False,
                    "failure_type": result.get("error_type", "tool_error"),
                    "failure_message": result.get("error", "未知错误"),
                }

        elif operation_type == "transfer":
            if result.get("accepted"):
                return {"success": True}
            else:
                return {
                    "success": False,
                    "failure_type": "transfer_failed",
                    "failure_message": result.get("reason", "转介失败"),
                }

        return {"success": True}

    def _determine_fallback_reason(self) -> str:
        """重试耗尽后，决定 fallback 原因"""
        if not self.failures:
            return "unknown"

        last_failure = self.failures[-1]
        last_reflection = self.reflections[-1] if self.reflections else None

        reason = f"重试 {MAX_RETRIES} 次后仍失败。"
        reason += f"最后一次失败：{last_failure.failure_type} - {last_failure.failure_message}"
        if last_reflection:
            reason += f"反思：{last_reflection.root_cause}"
        return reason

    async def _load_memory(self) -> Optional[ReflexionMemory]:
        """从 Graphiti 加载反思记忆"""
        if not self.memory_store:
            return None
        return await self.memory_store.get_reflexion_memory(self.agent_name)

    async def _record_successful_adjustment(self, failure_type: str, adjustment_strategy: str):
        """记录成功的调整策略到记忆"""
        if not self.memory_store:
            return
        await self.memory_store.record_successful_adjustment(
            agent_name=self.agent_name,
            failure_type=failure_type,
            adjustment_strategy=adjustment_strategy,
        )

    def _log_reflexion_span(self, failure: FailureRecord, reflection: Reflection, attempt: int):
        """记录 Reflexion trace span"""
        log_trace(
            span_type="tool",
            span_name=f"tool.reflexion.attempt_{attempt}",
            attributes={
                "tool_name": "reflexion",
                "attempt": attempt,
                "failure_type": failure.failure_type,
                "failure_message": failure.failure_message,
                "failure_pattern": reflection.failure_pattern,
                "root_cause": reflection.root_cause,
                "adjustment_strategy": reflection.adjustment_strategy,
                "alternative_approach": reflection.alternative_approach,
            },
        )
```

### 在 TEAM.md 中集成

扩展 TEAM.md 的"子智能体调用失败处理"：

```python
# 父智能体调用子智能体的扩展
async def invoke_subagent_with_reflexion(
    parent_agent: str, subagent_name: str, task: str, context: dict
) -> dict:
    """带 Reflexion 的子智能体调用"""

    reflexion_engine = ReflexionEngine(agent_name=parent_agent, memory_store=graphiti_client)

    async def call_subagent(**kwargs):
        # 实际调用子智能体
        return await platform.invoke_subagent(
            subagent_name=kwargs.get("subagent_name", subagent_name),
            task=kwargs.get("task", task),
            context=kwargs.get("context", context),
        )

    result = await reflexion_engine.execute_with_reflexion(
        operation=call_subagent,
        initial_input={"subagent_name": subagent_name, "task": task, "context": context},
        operation_type="subagent_call",
    )

    if result["success"]:
        return result["result"]
    else:
        # Fallback：父智能体降级处理
        log_trace(
            span_type="subagent",
            span_name=f"subagent.{subagent_name}.fallback",
            attributes={
                "execution_mode": "fallback",
                "fallback_reason": result["fallback_reason"],
                "attempts": result["attempts"],
            },
        )
        return await parent_agent_self_handle(task, context)
```

## 失败模式与调整策略对照

预先定义常见失败模式的调整策略（避免每次都调 LLM）：

```python
ADJUSTMENT_STRATEGIES = {
    "platform_not_supported": {
        "strategy": "改用 MCP server 工具替代子智能体",
        "alternative_tool": "check_integrity",  # 如 emotional 子智能体失败，改用 check_integrity 做简化校验
    },
    "timeout": {
        "strategy": "简化任务描述，减少上下文",
        "context_reduction": "只保留核心信息，去除历史",
    },
    "format_error": {
        "strategy": "在 prompt 中加入更明确的输出格式要求",
        "format_spec": "返回 JSON 格式：{...}",
    },
    "schema_validation_failed": {
        "strategy": "放宽 schema 要求，允许部分字段缺失",
        "required_fields_relaxed": True,
    },
    "empty_response": {"strategy": "重新表述问题，加入 Few-shot 示例", "add_few_shot": True},
    "context_too_long": {"strategy": "分段处理，先处理最关键部分", "chunk_size": 2000},
    "tool_not_found": {"strategy": "降级到平台原生工具", "fallback_to_native": True},
    "invalid_args": {"strategy": "根据 schema 修正参数", "auto_fix_args": True},
    "rate_limit": {"strategy": "等待后重试", "backoff_seconds": 60},
    "transfer_summary_incomplete": {"strategy": "补全缺失字段后重试", "auto_complete_fields": True},
}


def get_predefined_strategy(failure_type: str) -> Optional[dict]:
    """获取预定义的调整策略（快速路径，不调 LLM）"""
    return ADJUSTMENT_STRATEGIES.get(failure_type)


# 在 _reflect 方法中优先使用预定义策略
async def _reflect(self, failure: FailureRecord, operation_type: str) -> Reflection:
    # 快速路径：预定义策略
    predefined = get_predefined_strategy(failure.failure_type)
    if predefined:
        return Reflection(
            failure_pattern=failure.failure_type,
            root_cause=f"预定义失败模式：{failure.failure_type}",
            adjustment_strategy=predefined["strategy"],
            adjusted_prompt=predefined.get("format_spec"),
            adjusted_args={k: v for k, v in predefined.items() if k not in ["strategy"]},
            alternative_approach=predefined.get("alternative_tool"),
        )

    # 慢速路径：LLM 反思
    return await self._llm_reflect(failure, operation_type)
```

## 与 Graphiti 的集成

反思记忆存储到 Graphiti，跨会话学习：

```python
# knowledge/_temporal/reflexion_memory.py（伪代码）


async def record_failure_pattern(agent_name: str, failure_type: str):
    """记录失败模式出现次数"""
    await graphiti.query(
        """
        MERGE (m:ReflexionMemory {agent_name: $agent_name})
        ON CREATE SET m.failure_patterns = {}
        SET m.failure_patterns[$failure_type] = coalesce(m.failure_patterns[$failure_type], 0) + 1
        SET m.last_updated = $now
        """,
        params={"agent_name": agent_name, "failure_type": failure_type, "now": datetime.now()},
    )


async def record_successful_adjustment(
    agent_name: str, failure_type: str, adjustment_strategy: str
):
    """记录成功的调整策略"""
    await graphiti.query(
        """
        MERGE (m:ReflexionMemory {agent_name: $agent_name})
        SET m.successful_adjustments[$failure_type] = $strategy
        SET m.last_updated = $now
        """,
        params={
            "agent_name": agent_name,
            "failure_type": failure_type,
            "strategy": adjustment_strategy,
            "now": datetime.now(),
        },
    )


async def get_reflexion_memory(agent_name: str) -> ReflexionMemory:
    """获取反思记忆"""
    result = await graphiti.query(
        """
        MATCH (m:ReflexionMemory {agent_name: $agent_name})
        RETURN m
        """,
        params={"agent_name": agent_name},
    )
    if not result:
        return ReflexionMemory(
            agent_name=agent_name,
            failure_patterns={},
            successful_adjustments={},
            last_updated=datetime.now(),
        )
    return ReflexionMemory(**result[0])
```

## 与 trace 的联动

```
subagent_span (death-aftercare-emotional)
├── tool_span (reflexion.attempt_1)  ← 第 1 次失败
│   └── attributes: failure_type, root_cause, adjustment_strategy
├── tool_span (reflexion.attempt_2)  ← 第 2 次失败
│   └── attributes: failure_type, root_cause, adjustment_strategy
├── tool_span (reflexion.attempt_3)  ← 第 3 次失败
│   └── attributes: failure_type, root_cause, adjustment_strategy
└── event: fallback_triggered       ← 进入 fallback
    └── attributes: fallback_reason, total_attempts
```

## Reflexion 上报指标

```python
def reflexion_metrics(failures: list, reflections: list, final_result: dict) -> dict:
    return {
        "total_attempts": len(failures),
        "failure_types_encountered": list(set(f.failure_type for f in failures)),
        "reflexion_succeeded": final_result.get("success", False),
        "fallback_triggered": final_result.get("fallback", False),
        "common_failure_pattern": most_common([f.failure_type for f in failures]),
        "successful_adjustment_used": (
            reflections[-1].adjustment_strategy
            if final_result.get("success") and len(reflections) > 0
            else None
        ),
    }
```

## 与 TEAM.md 的集成

扩展 TEAM.md "子智能体调用失败处理"章节：

```markdown
## 子智能体调用失败处理（扩展版）

### 1. 检测失败
- execution_mode != "success" 即为失败

### 2. Reflexion 重试（新增）
- 第 1 次失败：分析原因，调整后重试
- 第 2 次失败：再次反思，切换策略
- 第 3 次失败：进入 fallback

### 3. Fallback（原有）
- 父智能体降级处理
- 记录到 trace

### 4. 反思记忆（新增）
- 失败模式记录到 Graphiti
- 成功的调整策略记忆
- 下次相同失败优先用历史成功策略

### 不重试的场景
- 涉及 R3 安全危机 → 立即 fallback 到 safety-protocol
- 子智能体输出违反 L0/L1 规则 → 不重试，记录 incident
- 用户主动取消 → 不重试
```

## 评估 Reflexion 效果

```python
# tests/automated/runners/reflexion_eval.py（伪代码）


def evaluate_reflexion():
    """
    评估 Reflexion 是否真的减少了 fallback 率。
    对比：无 Reflexion vs 有 Reflexion
    """
    # 100 个故意会触发失败的 case

    # 无 Reflexion
    no_reflexion_results = [run_case(c, reflexion=False) for c in cases]
    no_reflexion_fallback_rate = sum(1 for r in no_reflexion_results if r["fallback"]) / len(cases)

    # 有 Reflexion
    with_reflexion_results = [run_case(c, reflexion=True) for c in cases]
    with_reflexion_fallback_rate = sum(1 for r in with_reflexion_results if r["fallback"]) / len(
        cases
    )

    return {
        "fallback_rate_without_reflexion": no_reflexion_fallback_rate,
        "fallback_rate_with_reflexion": with_reflexion_fallback_rate,
        "improvement": no_reflexion_fallback_rate - with_reflexion_fallback_rate,
        "avg_attempts_with_reflexion": mean(r["attempts"] for r in with_reflexion_results),
        "cost_overhead": mean(r["cost"] for r in with_reflexion_results)
        - mean(r["cost"] for r in no_reflexion_results),
    }
```

**目标**：
- fallback 率降低 ≥ 20%
- 平均重试次数 ≤ 2（不浪费时间）
- 成本开销 ≤ 30%（重试的额外 LLM 调用）

## 成本估算

| 场景 | 频率 | 单次成本 |
|------|------|---------|
| 子智能体调用失败（需要 Reflexion） | 10% 请求 | $0.02（反思 LLM + 重试） |
| 工具调用失败（需要 Reflexion） | 5% 请求 | $0.01 |
| 转介失败（需要 Reflexion） | 2% 请求 | $0.01 |

按 10000 请求/天：~$30/天，~$900/月。

## 失败回退

若 Reflexion 反而增加延迟和成本（无显著改善 fallback 率）：
1. 减少 MAX_RETRIES 到 2
2. 只对预定义失败模式做 Reflexion（不走 LLM 反思）
3. 完全禁用 Reflexion，回到原 fallback 机制

## 版本
- v1.0 初始 Reflexion 方案（3 类失败场景 + 反思-调整-重试流程 + 预定义策略 + Graphiti 记忆集成 + 评估方法）
