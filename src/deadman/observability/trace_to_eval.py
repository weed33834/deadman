"""P6.3 Trace→Eval→Deploy 闭环 - 把生产 trace 转为 eval case / redteam payload

参考 DEADMAN_UPGRADE_PLAN.md v1.2 P6 实施细化。

工作流程：
  1. TraceToEvalConverter.convert(trace_jsonl_path)
     - 读取 trace JSONL 文件（每行一个 span dict）
     - 按 trace_id 分组，每个 trace 生成一个 eval case
     - 提取 user_input（从 ROOT span attributes.user_input / input）
     - 提取 expected_behavior（从 LLM_JUDGE span attributes，含 verdict / requirements）
  2. EvalToRedteamConverter.convert(eval_case)
     - 把 eval 失败 case 转为 redteam payload（用于安全回归）

Feature flag: DEADMAN_TRACE_TO_EVAL_ENABLED=0 默认关闭
  - 关闭时 convert 返回空列表
  - 开启时执行转换

降级路径全覆盖：
  1. feature flag 关闭 → 返回空
  2. trace 文件不存在/解析失败 → 返回空
  3. trace 缺 ROOT span → user_input 为空字符串
  4. trace 缺 LLM_JUDGE span → expected_behavior 为空字符串
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
TRACE_TO_EVAL_ENABLED: bool = os.environ.get("DEADMAN_TRACE_TO_EVAL_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


# =====================================================================
# TraceToEvalConverter
# =====================================================================


class TraceToEvalConverter:
    """把生产 trace JSONL 转为 eval case 列表

    用法：

        converter = TraceToEvalConverter()
        cases = converter.convert("/var/log/deadman/trace.jsonl")
        # cases = [{"case_id": ..., "user_input": ..., "expected_behavior": ..., ...}, ...]

    feature flag 关闭时返回空列表。
    """

    def convert(self, trace_jsonl_path: str) -> list[dict[str, Any]]:
        """读取 trace JSONL 文件，按 trace_id 分组转为 eval case

        Args:
            trace_jsonl_path: trace JSONL 文件路径，每行一个 span dict

        Returns:
            eval case 列表；feature flag 关闭或文件读取失败时返回空列表

        每个 case 结构：
            {
                "case_id": str,
                "user_input": str,            # 从 ROOT span 提取
                "expected_behavior": str,     # 从 LLM_JUDGE span 提取
                "trace_spans": list[dict],    # 原始 span 列表
                "metadata": {
                    "trace_id": str,
                    "span_count": int,
                    "source": "trace_jsonl",
                    "source_path": str,
                }
            }
        """
        if not TRACE_TO_EVAL_ENABLED:
            logger.debug("trace_to_eval disabled (DEADMAN_TRACE_TO_EVAL_ENABLED=0), skip")
            return []

        # 1. 读取 trace JSONL 文件
        spans = self._load_spans(trace_jsonl_path)
        if not spans:
            return []

        # 2. 按 trace_id 分组
        traces: dict[str, list[dict[str, Any]]] = {}
        for span in spans:
            trace_id = str(span.get("trace_id", ""))
            if not trace_id:
                continue
            traces.setdefault(trace_id, []).append(span)

        # 3. 每个 trace 生成一个 eval case
        cases: list[dict[str, Any]] = []
        for trace_id, trace_spans in traces.items():
            case = self._build_case(trace_id, trace_spans, trace_jsonl_path)
            cases.append(case)

        return cases

    def convert_from_spans(self, spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """直接从内存 span 列表转为 eval case（无需 JSONL 文件）

        Args:
            spans: span dict 列表（与 JSONL 每行结构一致）

        Returns:
            eval case 列表；feature flag 关闭时返回空列表
        """
        if not TRACE_TO_EVAL_ENABLED:
            return []

        traces: dict[str, list[dict[str, Any]]] = {}
        for span in spans:
            trace_id = str(span.get("trace_id", ""))
            if not trace_id:
                continue
            traces.setdefault(trace_id, []).append(span)

        cases: list[dict[str, Any]] = []
        for trace_id, trace_spans in traces.items():
            case = self._build_case(trace_id, trace_spans, "<memory>")
            cases.append(case)
        return cases

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _load_spans(path: str) -> list[dict[str, Any]]:
        """读取 JSONL 文件，每行解析为 span dict

        文件不存在/解析失败时返回空列表（不抛异常）。
        """
        try:
            spans: list[dict[str, Any]] = []
            with open(path, encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        span = json.loads(line)
                        if isinstance(span, dict):
                            spans.append(span)
                    except json.JSONDecodeError as e:
                        logger.warning("trace JSONL 第 %d 行解析失败: %s", line_no, e)
            return spans
        except FileNotFoundError:
            logger.warning("trace 文件不存在: %s", path)
            return []
        except Exception as e:
            logger.warning("读取 trace 文件失败 %s: %s", path, e)
            return []

    def _build_case(
        self,
        trace_id: str,
        spans: list[dict[str, Any]],
        source_path: str,
    ) -> dict[str, Any]:
        """从一组 span 构造一个 eval case"""
        user_input = self._extract_user_input(spans)
        expected_behavior = self._extract_expected_behavior(spans)

        return {
            "case_id": f"trace-{trace_id[:8]}-{uuid.uuid4().hex[:6]}",
            "user_input": user_input,
            "expected_behavior": expected_behavior,
            "trace_spans": spans,
            "metadata": {
                "trace_id": trace_id,
                "span_count": len(spans),
                "source": "trace_jsonl",
                "source_path": source_path,
            },
        }

    @staticmethod
    def _extract_user_input(spans: list[dict[str, Any]]) -> str:
        """从 ROOT span 提取 user_input

        优先级：attributes.user_input > attributes.input > attributes.query > ""
        """
        for span in spans:
            span_type = str(span.get("span_type", "")).lower()
            if span_type != "root":
                continue
            attrs = span.get("attributes", {}) or {}
            for key in ("user_input", "input", "query", "message"):
                val = attrs.get(key)
                if val:
                    return str(val)
            # ROOT span 无显式 user_input 时返回空字符串
            return ""
        return ""

    @staticmethod
    def _extract_expected_behavior(spans: list[dict[str, Any]]) -> str:
        """从 LLM_JUDGE span 提取 expected_behavior

        优先级：attributes.expected_behavior > attributes.verdict >
                attributes.requirements > attributes.judge_result > ""
        """
        for span in spans:
            span_type = str(span.get("span_type", "")).lower()
            if span_type != "llm_judge":
                continue
            attrs = span.get("attributes", {}) or {}
            for key in (
                "expected_behavior",
                "verdict",
                "requirements",
                "judge_result",
                "overall_verdict",
            ):
                val = attrs.get(key)
                if val:
                    return str(val)
            return ""
        return ""


# =====================================================================
# EvalToRedteamConverter
# =====================================================================


class EvalToRedteamConverter:
    """把 eval 失败 case 转为 redteam payload

    eval 失败 case 通常对应"智能体在某种输入下没有给出期望响应"，
    把这种输入重新包装为 redteam payload，可用于：
    - 把生产失败案例转为安全回归用例
    - 验证修复后智能体是否能正确处理该输入

    Feature flag: DEADMAN_TRACE_TO_EVAL_ENABLED=0 默认关闭
        - 关闭时 convert 返回空 dict
    """

    # 默认 expected_behavior：未指定时假设 eval 期望"安全响应"
    _DEFAULT_EXPECTED = "safe_response"

    def convert(self, eval_case: dict[str, Any]) -> dict[str, Any]:
        """把 eval case 转为 redteam payload

        Args:
            eval_case: eval case dict，至少包含 user_input 和 expected_behavior

        Returns:
            redteam payload dict；feature flag 关闭时返回空 dict

            {
                "payload_id": str,
                "payload": str,              # 原 user_input
                "category": str,             # "eval_regression"
                "expected_behavior": str,    # 转 redteam 期望行为
                "source_case_id": str,
                "metadata": {...}
            }
        """
        if not TRACE_TO_EVAL_ENABLED:
            logger.debug(
                "trace_to_eval disabled (DEADMAN_TRACE_TO_EVAL_ENABLED=0), skip eval_to_redteam"
            )
            return {}

        user_input = str(eval_case.get("user_input", ""))
        expected_behavior = str(eval_case.get("expected_behavior", "")).strip()
        # eval expected_behavior 通常是"通过/失败"判定文本，
        # 转 redteam 时映射到 refuse/no_leak/safe_response 之一
        redteam_expected = self._map_to_redteam_expected(expected_behavior)

        case_id = str(eval_case.get("case_id", ""))
        metadata = eval_case.get("metadata", {}) or {}

        return {
            "payload_id": f"redteam-{case_id[:16]}-{uuid.uuid4().hex[:6]}",
            "payload": user_input,
            "category": metadata.get("category", "eval_regression"),
            "expected_behavior": redteam_expected,
            "source_case_id": case_id,
            "metadata": {
                "source_trace_id": metadata.get("trace_id", ""),
                "source": "eval_to_redteam",
                "original_expected_behavior": expected_behavior,
            },
        }

    @staticmethod
    def _map_to_redteam_expected(expected_behavior: str) -> str:
        """把 eval expected_behavior 映射到 redteam expected_behavior

        redteam expected_behavior 取值（参考 security/redteam.py）：
          - refuse: 应拒绝执行
          - no_leak: 不应泄漏
          - safe_response: 安全响应

        映射规则：
          - 含"refuse/拒绝" → refuse
          - 含"no_leak/不泄漏" → no_leak
          - 其他 → safe_response
        """
        text = expected_behavior.lower()
        if "refuse" in text or "拒绝" in expected_behavior:
            return "refuse"
        if "no_leak" in text or "不泄漏" in expected_behavior or "不泄露" in expected_behavior:
            return "no_leak"
        return EvalToRedteamConverter._DEFAULT_EXPECTED
