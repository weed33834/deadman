"""评估运行器 - 加载 YAML case、调用被测系统、三层判定 + 工具调用校验

参考 tests/automated/README.md（CI Runner 流程）与 Expected-Tool-Calls.md（CI 集成）。

流程：
  1. 加载 YAML case
  2. 调用被测系统（mock 或真实 SUT）获取 response + tool_calls + trace_id
  3. 三层判定（正则 → 关键词 → LLM-as-Judge）
  4. 工具调用序列校验（5 个指标）
  5. 汇总结果

典型用法：

    from deadman.evaluation import CaseRunner, run_all_cases

    # 1. 单 case
    runner = CaseRunner()
    result = await runner.run_case("tests/automated/cases/case-01-no-fabrication.yaml")

    # 2. 跑目录下所有 case
    summary = await run_all_cases("tests/automated/cases")
    # summary = {"total": N, "passed": N, "failed": N, "results": [...]}
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any
from collections.abc import Callable, Coroutine

import yaml

from ..llm import LLMClient
from .three_layer import ThreeLayerEvaluator
from .tool_calls import validate_tool_calls

logger = logging.getLogger(__name__)

# 被测系统回调类型：接收 (user_input, case_yaml)，返回 {"response", "tool_calls", "trace_id"}
SystemUnderTest = Callable[
    [str, dict[str, Any]],
    Coroutine[Any, Any, dict[str, Any]],
]


class CaseRunner:
    """单 case 运行器

    加载 YAML case → 调用被测系统 → 三层判定 → 工具调用序列校验 → 汇总结果。
    """

    def __init__(
        self,
        evaluator: ThreeLayerEvaluator | None = None,
        llm_client: LLMClient | None = None,
        system_under_test: SystemUnderTest | Any | None = None,
    ) -> None:
        """
        Args:
            evaluator: 三层判定器，未提供则用默认
            llm_client: LLM 客户端（保留以兼容依赖注入，当前未直接使用）
            system_under_test: 被测系统（mock 或真实）
                - 若提供，需是 callable 或具有 async call 方法
                - 签名：async def call(user_input: str, case: dict) -> dict
                - 返回 dict 应包含 {"response": str, "tool_calls": [...], "trace_id": str}
                - 若不提供，则用 case_yaml 中的 mock_response / mock_tool_calls
        """
        self.evaluator = evaluator or ThreeLayerEvaluator()
        self.llm_client = llm_client or LLMClient()
        self.system_under_test = system_under_test

    async def run_case(self, case_yaml_path: str | Path) -> dict[str, Any]:
        """运行单个 case

        Args:
            case_yaml_path: YAML case 文件路径

        Returns:
            {
                "case_id": str,
                "passed": bool,
                "layer_reached": "regex|keyword|llm",
                "failures": [...],
                "tool_call_results": {...},
                "three_layer_passed": bool,
                "tool_call_passed": bool,
                "trace_id": str,
                "response": str,
                "details": {...},
            }
        """
        path = Path(case_yaml_path)
        case_yaml = self._load_case(path)

        case_id = case_yaml.get("case_id", path.stem)
        user_input = case_yaml.get("user_input", "")

        # 1. 调用被测系统（mock 或真实）
        sut_result = await self._call_system_under_test(user_input, case_yaml)
        response = sut_result.get("response", "")
        actual_tool_calls = sut_result.get("tool_calls", []) or []
        trace_id = sut_result.get("trace_id") or str(uuid.uuid4())

        # 2. 三层判定
        eval_result = await self.evaluator.evaluate(response, case_yaml)
        layer_reached = eval_result.get("layer", "regex")
        failures = eval_result.get("failures", [])
        three_layer_passed = eval_result.get("passed", False)

        # 3. 工具调用序列校验
        expected_tool_calls = case_yaml.get("expected_tool_calls", []) or []
        if expected_tool_calls:
            tool_call_results = await validate_tool_calls(
                actual_tool_calls, expected_tool_calls
            )
        else:
            # case 未定义期望工具调用 → 默认通过
            tool_call_results = {
                "tool_selection_accuracy": 1.0,
                "argument_accuracy": 1.0,
                "order_match": True,
                "unnecessary_calls": 0,
                "result_match_rate": 1.0,
                "forbidden_violations": [],
                "details": [],
                "passed": True,
            }

        # 4. 综合判定：三层判定 + 工具调用校验都通过
        overall_passed = three_layer_passed and tool_call_results.get("passed", False)

        return {
            "case_id": case_id,
            "name": case_yaml.get("name", ""),
            "category": case_yaml.get("category", ""),
            "priority": case_yaml.get("priority", ""),
            "passed": overall_passed,
            "layer_reached": layer_reached,
            "three_layer_passed": three_layer_passed,
            "tool_call_passed": tool_call_results.get("passed", False),
            "failures": failures,
            "tool_call_results": tool_call_results,
            "trace_id": trace_id,
            "response": response,
            "details": eval_result.get("details", {}),
        }

    def _load_case(self, path: Path) -> dict[str, Any]:
        """加载 YAML case 文件

        Args:
            path: YAML 文件路径

        Returns:
            case 字典

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 文件为空或解析失败
        """
        if not path.is_file():
            raise FileNotFoundError(f"case 文件不存在：{path}")
        with open(path, encoding="utf-8") as f:
            case = yaml.safe_load(f)
        if not case:
            raise ValueError(f"空 case 文件：{path}")
        if not isinstance(case, dict):
            raise ValueError(f"case 文件格式错误（期望 dict）：{path}")
        return case

    async def _call_system_under_test(
        self,
        user_input: str,
        case_yaml: dict[str, Any],
    ) -> dict[str, Any]:
        """调用被测系统

        - 若提供了 system_under_test，调用它
        - 否则返回 case_yaml 中的 mock 数据（用于纯评估场景）

        Returns:
            {"response": str, "tool_calls": [...], "trace_id": str}
        """
        sut = self.system_under_test
        if sut is not None:
            # 1. 直接 callable
            if asyncio.iscoroutinefunction(sut):
                return await sut(user_input, case_yaml)
            if callable(sut) and not hasattr(sut, "call"):
                # 同步 callable（不常见，但兼容）
                result = sut(user_input, case_yaml)
                if asyncio.iscoroutine(result):
                    return await result
                return result
            # 2. 具有 call 方法的对象
            call_method = getattr(sut, "call", None)
            if call_method is None:
                raise TypeError(
                    "system_under_test 必须是 callable 或具有 async call(user_input, case) 方法"
                )
            if asyncio.iscoroutinefunction(call_method):
                return await call_method(user_input, case_yaml)
            result = call_method(user_input, case_yaml)
            if asyncio.iscoroutine(result):
                return await result
            return result

        # 无 SUT → 用 case_yaml 中的 mock 数据（用于纯评估/回归场景）
        return {
            "response": case_yaml.get("mock_response", ""),
            "tool_calls": case_yaml.get("mock_tool_calls", []) or [],
            "trace_id": str(uuid.uuid4()),
        }

    # ==============================================================
    # P6.3: 从 trace JSONL 加载 case（feature flag 开启时启用）
    # ==============================================================

    @staticmethod
    def load_from_trace_jsonl(path: str) -> list[dict[str, Any]]:
        """从 trace JSONL 文件加载 eval case（P6.3 新增）

        把生产 trace 一键转为 eval case，用于：
        - 把生产失败 trace 转为回归 case
        - 把 trace 中的 user_input + LLM_JUDGE 判定结果作为 expected_behavior

        Feature flag DEADMAN_TRACE_TO_EVAL_ENABLED=0 默认关闭：
            - 关闭时返回空列表（不抛异常）
            - 开启时调用 TraceToEvalConverter.convert

        Args:
            path: trace JSONL 文件路径

        Returns:
            eval case 列表；feature flag 关闭/文件读取失败时返回空列表
        """
        try:
            from ..observability.trace_to_eval import TraceToEvalConverter
        except ImportError:
            logger.warning("无法导入 TraceToEvalConverter，跳过 trace 加载")
            return []
        try:
            converter = TraceToEvalConverter()
            return converter.convert(path)
        except Exception as e:
            logger.warning("从 trace JSONL 加载 case 失败: %s", e)
            return []


async def run_all_cases(cases_dir: str | Path) -> dict[str, Any]:
    """运行目录下所有 YAML case

    串行执行每个 case，单 case 异常不会中断整体运行（记为失败）。

    Args:
        cases_dir: case 文件目录

    Returns:
        {
            "total": int,
            "passed": int,
            "failed": int,
            "pass_rate": float,
            "results": [...],   # 每个 case 的运行结果
        }
    """
    dir_path = Path(cases_dir)
    if not dir_path.is_dir():
        raise NotADirectoryError(f"case 目录不存在或不是目录：{dir_path}")

    # 收集所有 .yaml / .yml case（按文件名排序，保证可复现）
    case_files = sorted(list(dir_path.glob("*.yaml")) + list(dir_path.glob("*.yml")))

    runner = CaseRunner()
    results: list[dict[str, Any]] = []

    for case_file in case_files:
        try:
            result = await runner.run_case(case_file)
        except Exception as e:
            # 单 case 失败不影响其他 case
            logger.exception("case %s 运行失败", case_file.name)
            result = {
                "case_id": case_file.stem,
                "name": case_file.name,
                "passed": False,
                "layer_reached": "error",
                "three_layer_passed": False,
                "tool_call_passed": False,
                "failures": [{"reason": f"运行异常：{e}"}],
                "tool_call_results": {},
                "trace_id": None,
                "error": str(e),
            }
        results.append(result)

    total = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    failed = total - passed
    pass_rate = passed / total if total else 0.0

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": pass_rate,
        "results": results,
    }
