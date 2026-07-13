"""工具调用序列校验 - 量化工具调用准确率

参考 tests/automated/Expected-Tool-Calls.md。

校验维度（5 个指标）：
  1. Tool Selection Accuracy：必须调用的工具中，实际调用了多少
  2. Argument Accuracy：调用了的工具，参数是否符合期望（5 种校验类型）
  3. Tool Call Order Match：工具调用顺序是否正确（子序列匹配）
  4. Unnecessary Tool Calls：实际调用但不在期望列表中的工具（冗余调用）
  5. Tool Call Result Match：工具返回结果是否符合期望

required 字段三态语义：
  - true：必须调用，缺失则 case 失败
  - false：期望但非必须，缺失仅扣分
  - forbidden：禁止调用，调用则 case 失败（严重违规）

参数校验 5 种类型：
  - exact：精确匹配
  - contains：包含子串
  - regex：正则匹配
  - non_empty：非空
  - optional：可有可无（始终通过）

典型用法：

    from legacy.evaluation import validate_tool_calls

    result = await validate_tool_calls(actual_calls, expected_calls)
    # result = {"tool_selection_accuracy": float, "argument_accuracy": float,
    #           "order_match": bool, "unnecessary_calls": int,
    #           "result_match_rate": float, "details": [...]}
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class ArgValidator:
    """工具参数校验器

    支持 5 种校验类型：exact / contains / regex / non_empty / optional。
    """

    def validate(self, value: Any, validation_spec: Any) -> bool:
        """校验单个参数值

        Args:
            value: 实际参数值
            validation_spec: {"type": "exact|contains|regex|non_empty|optional", "value": "..."}
                             非 dict 类型（如标量）会被当作 {"type": "exact", "value": 标量} 处理
                             None 视为无校验要求（通过）

        Returns:
            True 表示校验通过
        """
        # None 视为无校验要求
        if validation_spec is None:
            return True
        # 非 dict 的 spec（如 YAML 中 fields_complete: 7）→ 视为 exact 匹配
        if not isinstance(validation_spec, dict):
            return value == validation_spec

        vtype = validation_spec.get("type", "optional")
        expected = validation_spec.get("value")

        if vtype == "optional":
            # 可有可无，始终通过
            return True

        if vtype == "non_empty":
            # 非空校验：字符串/列表/字典非空，其他类型非 None
            if value is None:
                return False
            if isinstance(value, str):
                return len(value.strip()) > 0
            if isinstance(value, (list, dict, tuple, set)):
                return len(value) > 0
            return True

        if vtype == "exact":
            # 精确匹配
            return value == expected

        if vtype == "contains":
            # 包含子串（字符串/列表）
            if value is None or expected is None:
                return False
            if isinstance(value, str):
                return str(expected) in value
            if isinstance(value, (list, tuple, set)):
                return any(str(expected) in str(item) for item in value)
            return False

        if vtype == "regex":
            # 正则匹配（仅对字符串有效）
            if value is None or not isinstance(value, str):
                return False
            if expected is None:
                return False
            try:
                return re.search(expected, value) is not None
            except re.error as e:
                logger.warning("非法正则校验模式 %r: %s", expected, e)
                return False

        # 未知校验类型 → 不通过（提示 case 定义有误）
        logger.warning("未知参数校验类型: %s", vtype)
        return False


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    """检查 expected 是否是 actual 的子序列（顺序保留，可间隔）

    用于工具调用顺序校验：必须调用的工具的相对顺序应与实际调用顺序一致。
    """
    it = iter(actual)
    return all(item in it for item in expected)


def _match_result(actual_result: dict[str, Any], expected_result: dict[str, Any]) -> bool:
    """校验工具返回结果是否符合期望

    支持的期望字段语义：
      - 标量值：直接相等比较
      - 期望值含 "|"（如 "success|fallback"）：任一分支匹配即可
      - 期望 key 以 "_gte" 结尾（如 "violations_count_gte"）：实际值 >= 期望值

    Args:
        actual_result: 实际工具返回结果
        expected_result: 期望的返回结果

    Returns:
        True 表示所有期望字段都匹配
    """
    if not isinstance(actual_result, dict):
        return False
    if not isinstance(expected_result, dict):
        return actual_result == expected_result

    for key, expected_val in expected_result.items():
        # _gte 后缀：>= 比较（如 violations_count_gte: 1）
        if key.endswith("_gte"):
            real_key = key[:-4]
            actual_val = actual_result.get(real_key)
            try:
                if not (actual_val is not None and actual_val >= expected_val):
                    return False
            except TypeError:
                # 类型不可比较 → 不匹配
                return False
            continue

        # 普通字段
        actual_val = actual_result.get(key)
        if isinstance(expected_val, str) and "|" in expected_val:
            # "success|fallback" → 任一匹配即可
            options = [opt.strip() for opt in expected_val.split("|")]
            if actual_val not in options:
                return False
        else:
            if actual_val != expected_val:
                return False
    return True


def _match_expected_to_actual(
    expected_calls: list[dict[str, Any]],
    actual_calls: list[dict[str, Any]],
) -> dict[int, int | None]:
    """将每个非 forbidden 的 expected_call 匹配到 actual_calls 中的某个调用

    按顺序消费 actual_calls：每个 actual_call 至多被一个 expected_call 匹配。
    这样能正确处理同一工具被多次调用的情况（如 case-20 中 query_knowledge 被调用两次）。

    匹配规则：tool 名称匹配，或 tool 在 alternatives 列表中。

    Returns:
        {expected_idx: actual_idx or None} - expected 调用索引 → 实际调用索引（None 表示未匹配）
    """
    used_actual = [False] * len(actual_calls)
    matches: dict[int, int | None] = {}

    for ei, ec in enumerate(expected_calls):
        # forbidden 调用不参与匹配（单独检查是否被调用）
        if ec.get("required") == "forbidden":
            continue

        tool = ec.get("tool", "")
        alternatives = set(ec.get("alternatives", []) or [])
        candidates = {tool} | alternatives
        candidates.discard("")

        matched_actual_idx: int | None = None
        for ai, ac in enumerate(actual_calls):
            if used_actual[ai]:
                continue
            if ac.get("tool") in candidates:
                matched_actual_idx = ai
                used_actual[ai] = True
                break
        matches[ei] = matched_actual_idx

    return matches


async def validate_tool_calls(
    actual_calls: list[dict[str, Any]],
    expected_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """校验实际工具调用序列是否符合期望

    Args:
        actual_calls: [{"tool": "...", "args": {...}, "result": {...}}]
                      从 trace_spans 提取的实际工具调用列表（按时间排序）
        expected_calls: YAML 中定义的 expected_tool_calls 列表

    Returns:
        {
            "tool_selection_accuracy": float,    # 必须工具的命中率
            "argument_accuracy": float,          # 参数校验通过率
            "order_match": bool,                 # 顺序是否正确
            "unnecessary_calls": int,            # 冗余调用数
            "result_match_rate": float,          # 结果匹配率
            "details": [...],                    # 每个期望调用的详细匹配情况
            "forbidden_violations": [...],       # 禁止工具被调用的违规
            "passed": bool,                      # 综合判定
        }
    """
    arg_validator = ArgValidator()

    # 期望工具集合（含 alternatives），用于判断冗余调用
    expected_tool_set: set[str] = set()
    for ec in expected_calls:
        tool = ec.get("tool", "")
        if tool:
            expected_tool_set.add(tool)
        for alt in ec.get("alternatives", []) or []:
            if alt:
                expected_tool_set.add(alt)

    # 禁止调用的工具及其 purpose
    forbidden_tools: set[str] = set()
    forbidden_purposes: dict[str, str] = {}
    for ec in expected_calls:
        if ec.get("required") == "forbidden":
            tool = ec.get("tool", "")
            if tool:
                forbidden_tools.add(tool)
                forbidden_purposes[tool] = ec.get("purpose", "")

    # 将每个 expected_call 匹配到 actual_call（按顺序消费，避免重复匹配）
    matches = _match_expected_to_actual(expected_calls, actual_calls)

    # === 1. Tool Selection Accuracy ===
    # 必须调用的工具中，实际调用了多少
    required_calls = [
        (i, ec) for i, ec in enumerate(expected_calls) if ec.get("required") is True
    ]
    required_hit = sum(1 for i, _ in required_calls if matches.get(i) is not None)
    selection_accuracy = required_hit / len(required_calls) if required_calls else 1.0

    # === 2. Argument Accuracy ===
    # 调用了的工具，参数是否符合期望（optional 类型不参与统计）
    matched_args = 0
    total_args = 0
    for ei, ec in enumerate(expected_calls):
        if ec.get("required") == "forbidden":
            continue
        ai = matches.get(ei)
        if ai is None:
            continue
        actual = actual_calls[ai]
        args_validation = ec.get("args_validation", {}) or {}
        actual_args = actual.get("args", {}) or {}
        for arg_name, validation in args_validation.items():
            # optional 类型不参与统计
            if isinstance(validation, dict) and validation.get("type") == "optional":
                continue
            total_args += 1
            if arg_validator.validate(actual_args.get(arg_name), validation):
                matched_args += 1
    argument_acc = matched_args / total_args if total_args else 1.0

    # === 3. Tool Call Order Match ===
    # 必须调用工具的期望顺序是否为 actual 顺序的子序列
    expected_order = [
        ec.get("tool", "")
        for ec in expected_calls
        if ec.get("required") is True
    ]
    actual_order = [c.get("tool", "") for c in actual_calls]
    order_ok = _is_subsequence(expected_order, actual_order)

    # === 4. Unnecessary Tool Calls ===
    # 实际调用但不在期望列表中的工具（冗余调用）
    unnecessary_count = 0
    forbidden_violations: list[dict[str, Any]] = []
    for ac in actual_calls:
        tool = ac.get("tool", "")
        if tool not in expected_tool_set:
            unnecessary_count += 1
        if tool in forbidden_tools:
            # 禁止调用的工具被调用 → 严重违规
            forbidden_violations.append(
                {
                    "tool": tool,
                    "reason": "调用了 forbidden 工具",
                    "purpose": forbidden_purposes.get(tool, ""),
                }
            )

    # === 5. Tool Call Result Match ===
    # 工具返回结果是否符合期望（如 found=false）
    result_matches = 0
    result_total = 0
    for ei, ec in enumerate(expected_calls):
        expected_result = ec.get("expected_result")
        if not expected_result:
            continue
        ai = matches.get(ei)
        if ai is None:
            continue
        result_total += 1
        actual = actual_calls[ai]
        actual_result = actual.get("result", {}) or {}
        if _match_result(actual_result, expected_result):
            result_matches += 1
    result_match_rate = result_matches / result_total if result_total else 1.0

    # === 详细信息（每个 expected 调用的匹配情况） ===
    details: list[dict[str, Any]] = []
    for ei, ec in enumerate(expected_calls):
        tool = ec.get("tool", "")
        required = ec.get("required")
        purpose = ec.get("purpose", "")
        step = ec.get("step")

        # forbidden 工具：记录是否被调用
        if required == "forbidden":
            was_called = any(ac.get("tool") == tool for ac in actual_calls)
            details.append(
                {
                    "step": step,
                    "tool": tool,
                    "required": required,
                    "purpose": purpose,
                    "matched": not was_called,  # 期望未调用 → 未被调用才算 matched
                    "was_called": was_called,
                }
            )
            continue

        ai = matches.get(ei)
        detail: dict[str, Any] = {
            "step": step,
            "tool": tool,
            "required": required,
            "purpose": purpose,
            "matched": ai is not None,
        }
        if ai is not None:
            actual = actual_calls[ai]
            # 参数校验详情
            args_validation = ec.get("args_validation", {}) or {}
            actual_args = actual.get("args", {}) or {}
            arg_results: dict[str, bool] = {}
            for arg_name, validation in args_validation.items():
                arg_results[arg_name] = arg_validator.validate(
                    actual_args.get(arg_name), validation
                )
            detail["args_match"] = arg_results
            # 结果校验详情
            expected_result = ec.get("expected_result")
            if expected_result:
                detail["result_match"] = _match_result(
                    actual.get("result", {}) or {}, expected_result
                )
        details.append(detail)

    # === 综合判定（参考 Expected-Tool-Calls.md） ===
    passed = (
        selection_accuracy == 1.0  # 所有必须工具都调用
        and argument_acc >= 0.8  # 参数 80% 正确
        and order_ok  # 顺序正确
        and unnecessary_count <= 1  # 至多 1 个冗余调用
        and len(forbidden_violations) == 0  # 没有调用禁止工具
    )

    return {
        "tool_selection_accuracy": selection_accuracy,
        "argument_accuracy": argument_acc,
        "order_match": order_ok,
        "unnecessary_calls": unnecessary_count,
        "result_match_rate": result_match_rate,
        "forbidden_violations": forbidden_violations,
        "details": details,
        "passed": passed,
    }
