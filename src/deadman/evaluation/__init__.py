"""自动化评估框架 - 三层判定 + 工具调用序列校验 + case 运行器

参考：
  - tests/automated/README.md（三层判定 + CI 集成）
  - tests/automated/LLM-as-Judge.md（G-Eval CoT + 跨模型共识）
  - tests/automated/Expected-Tool-Calls.md（5 校验类型 + 5 指标）

模块组成：
  - three_layer.py：三层判定（正则黑名单 → 关键词必中 → LLM-as-Judge）
  - tool_calls.py：工具调用序列校验（5 个指标 + 5 种参数校验类型）
  - runner.py：评估运行器（加载 YAML case → 调用 SUT → 三层判定 → 工具调用校验）

典型用法：

    from deadman.evaluation import (
        ThreeLayerEvaluator,
        RegexChecker,
        KeywordChecker,
        LLMJudge,
        validate_tool_calls,
        ArgValidator,
        CaseRunner,
        run_all_cases,
    )

    # 1. 单 case 三层判定
    evaluator = ThreeLayerEvaluator()
    result = await evaluator.evaluate(response, case_yaml)

    # 2. 工具调用序列校验
    tool_result = await validate_tool_calls(actual_calls, expected_calls)

    # 3. 跑目录下所有 case
    summary = await run_all_cases("tests/automated/cases")
    # summary = {"total": N, "passed": N, "failed": N, "results": [...]}
"""

from __future__ import annotations

from .runner import CaseRunner, run_all_cases
from .three_layer import (
    GENERAL_JUDGE_PROMPT,
    KeywordChecker,
    LLMJudge,
    RegexChecker,
    ThreeLayerEvaluator,
)
from .tool_calls import ArgValidator, validate_tool_calls

__all__ = [
    # 三层判定
    "RegexChecker",
    "KeywordChecker",
    "LLMJudge",
    "ThreeLayerEvaluator",
    "GENERAL_JUDGE_PROMPT",
    # 工具调用校验
    "ArgValidator",
    "validate_tool_calls",
    # 运行器
    "CaseRunner",
    "run_all_cases",
]
