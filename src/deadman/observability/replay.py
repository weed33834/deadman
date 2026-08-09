"""P6.5 Replay Debugging - 用新参数重放 trace 中的 LLM 调用，对比响应差异

参考 DEADMAN_UPGRADE_PLAN.md v1.2 P6 实施细化。

工作流程：
  1. 从 trace 加载原始 user_input + system_prompt（ROOT span attributes）
  2. 用新参数（new_prompt / new_model / new_temperature）重新调用 LLM
  3. 对比原始响应 vs 新响应，生成 diff
  4. 判断是否"改进"（new_response 比 original_response 更长 / 更完整）

Feature flag: DEADMAN_REPLAY_ENABLED=0 默认关闭
  - 关闭时 replay 返回空 ReplayResult
  - 开启时执行重放

降级路径全覆盖：
  1. feature flag 关闭 → 返回空 ReplayResult
  2. trace 不存在 → 返回空 ReplayResult
  3. 无 ROOT span → 返回空 ReplayResult
  4. LLM 调用失败 → replayed_response 为空，diff 标记失败原因
"""

from __future__ import annotations

import difflib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
REPLAY_ENABLED: bool = os.environ.get("DEADMAN_REPLAY_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


# =====================================================================
# 数据模型
# =====================================================================


@dataclass
class ReplayRequest:
    """重放请求

    Attributes:
        trace_id: 要重放的 trace ID
        new_prompt: 新 prompt（替换原 user_input；None 表示用原 user_input）
        new_model: 新模型名（None 表示用 LLMClient 默认 model）
        new_temperature: 新 temperature（None 表示用默认 0.3）
    """

    trace_id: str = ""
    new_prompt: str | None = None
    new_model: str | None = None
    new_temperature: float | None = None


@dataclass
class ReplayResult:
    """重放结果

    Attributes:
        original_response: 原始响应（从 trace 提取）
        replayed_response: 重放后的响应
        diff: 原始 vs 重放的 diff 文本（unified diff 格式）
        improved: 重放是否"改进"（响应更完整 / 更长）
    """

    original_response: str = ""
    replayed_response: str = ""
    diff: str = ""
    improved: bool = False
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# =====================================================================
# ReplayDebugger
# =====================================================================


class ReplayDebugger:
    """重放调试器

    用法：

        debugger = ReplayDebugger(tracer, llm_client)
        result = await debugger.replay(
            ReplayRequest(trace_id="xxx", new_temperature=0.7)
        )
        print(result.diff)

    feature flag 关闭时 replay 返回空 ReplayResult。
    """

    def __init__(self, tracer: Any, llm_client: Any) -> None:
        """
        Args:
            tracer: Tracer 实例（用于 get_trace 加载原始 span）
            llm_client: LLMClient 实例（用于重新调用 LLM）
        """
        self.tracer = tracer
        self.llm_client = llm_client

    async def replay(self, request: ReplayRequest) -> ReplayResult:
        """重放 trace 中的 LLM 调用

        Args:
            request: ReplayRequest，指定 trace_id + 可选新参数

        Returns:
            ReplayResult；feature flag 关闭/trace 不存在时返回空结果
        """
        # 1. feature flag 关闭 → 空结果
        if not REPLAY_ENABLED:
            logger.debug(
                "replay disabled (DEADMAN_REPLAY_ENABLED=0), skip trace_id=%s",
                request.trace_id,
            )
            return ReplayResult(metadata={"reason": "replay_disabled"})

        # 2. 加载 trace
        try:
            spans = self.tracer.get_trace(request.trace_id) if self.tracer else []
        except Exception as e:
            logger.warning("加载 trace %s 失败: %s", request.trace_id, e)
            return ReplayResult(metadata={"reason": f"trace_load_error: {e}"})

        if not spans:
            return ReplayResult(metadata={"reason": "trace_not_found"})

        # 3. 提取原始 user_input + system_prompt + 原始响应
        original_user_input, original_system_prompt, original_response = (
            self._extract_original_context(spans)
        )

        if not original_user_input:
            return ReplayResult(
                original_response=original_response,
                metadata={"reason": "no_user_input_in_trace"},
            )

        # 4. 用新参数重放 LLM 调用
        replayed_response, error = await self._replay_llm_call(
            request, original_user_input, original_system_prompt
        )

        # 5. 生成 diff
        diff = self._generate_diff(original_response, replayed_response)

        # 6. 判断是否"改进"
        improved = self._is_improved(original_response, replayed_response, error)

        return ReplayResult(
            original_response=original_response,
            replayed_response=replayed_response,
            diff=diff,
            improved=improved,
            error=error,
            metadata={
                "trace_id": request.trace_id,
                "new_prompt": request.new_prompt,
                "new_model": request.new_model,
                "new_temperature": request.new_temperature,
                "original_user_input": original_user_input[:200],
            },
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_original_context(spans: list[dict[str, Any]]) -> tuple[str, str, str]:
        """从 trace span 树提取原始 user_input / system_prompt / response

        优先级：
          - user_input: ROOT span attributes.user_input > .input > .query
          - system_prompt: ROOT span attributes.system_prompt > .system
          - original_response: ROOT span attributes.output > .response >
                              LLM_JUDGE span attributes.input

        Returns:
            (user_input, system_prompt, original_response)，未提取到时为空字符串
        """
        user_input = ""
        system_prompt = ""
        original_response = ""

        for span in spans:
            span_type = str(span.get("span_type", "")).lower()
            if span_type != "root":
                continue
            attrs = span.get("attributes", {}) or {}
            for key in ("user_input", "input", "query", "message"):
                val = attrs.get(key)
                if val:
                    user_input = str(val)
                    break
            for key in ("system_prompt", "system"):
                val = attrs.get(key)
                if val:
                    system_prompt = str(val)
                    break
            for key in ("output", "response", "result"):
                val = attrs.get(key)
                if val:
                    original_response = str(val)
                    break
            break  # 只取首个 ROOT span

        # 兜底：original_response 从 LLM_JUDGE span attributes.input 提取
        if not original_response:
            for span in spans:
                span_type = str(span.get("span_type", "")).lower()
                if span_type != "llm_judge":
                    continue
                attrs = span.get("attributes", {}) or {}
                val = attrs.get("input") or attrs.get("response")
                if val:
                    original_response = str(val)
                    break

        return user_input, system_prompt, original_response

    async def _replay_llm_call(
        self,
        request: ReplayRequest,
        original_user_input: str,
        original_system_prompt: str,
    ) -> tuple[str, str]:
        """用新参数重新调用 LLM

        Args:
            request: ReplayRequest
            original_user_input: 原始 user_input（若 request.new_prompt 为 None 时使用）
            original_system_prompt: 原始 system_prompt

        Returns:
            (replayed_response, error_message)；error 为空表示成功
        """
        # 构造 messages
        messages: list[dict[str, str]] = []
        if original_system_prompt:
            messages.append({"role": "system", "content": original_system_prompt})
        # new_prompt 优先，否则用原 user_input
        user_content = request.new_prompt if request.new_prompt else original_user_input
        messages.append({"role": "user", "content": user_content})

        # temperature
        temperature = request.new_temperature if request.new_temperature is not None else 0.3

        # 构造 kwargs（new_model 时尝试覆盖 model）
        kwargs: dict[str, Any] = {}
        if request.new_model:
            kwargs["model"] = request.new_model
            # 注意：LLMClient.chat 不直接支持 model 参数；
            # 这里通过 kwargs 传递，若 LLMClient 不接受则忽略

        try:
            # 调用 LLM
            response = await self.llm_client.chat(messages, temperature=temperature, **kwargs)
            return str(response), ""
        except TypeError:
            # kwargs 不被接受（如 model 参数）→ 去掉 kwargs 重试
            try:
                response = await self.llm_client.chat(messages, temperature=temperature)
                return str(response), ""
            except Exception as e:
                logger.warning("重放 LLM 调用失败 (no kwargs): %s", e)
                return "", f"llm_call_failed: {e}"
        except Exception as e:
            logger.warning("重放 LLM 调用失败: %s", e)
            return "", f"llm_call_failed: {e}"

    @staticmethod
    def _generate_diff(original: str, replayed: str) -> str:
        """生成 unified diff 文本

        Args:
            original: 原始响应
            replayed: 重放响应

        Returns:
            unified diff 文本；两者完全相同时返回空字符串
        """
        if not original and not replayed:
            return ""
        original_lines = original.splitlines(keepends=False) if original else []
        replayed_lines = replayed.splitlines(keepends=False) if replayed else []

        diff_lines = list(
            difflib.unified_diff(
                original_lines,
                replayed_lines,
                fromfile="original",
                tofile="replayed",
                lineterm="",
            )
        )
        return "\n".join(diff_lines)

    @staticmethod
    def _is_improved(original: str, replayed: str, error: str) -> bool:
        """判断重放是否"改进"

        简化判定：
          - 有 error → 未改进
          - original 为空但 replayed 非空 → 改进
          - replayed 比 original 更长（>= 1.2 倍）且非空 → 改进
          - 其他 → 未改进

        Returns:
            bool
        """
        if error:
            return False
        if not replayed:
            return False
        if not original and replayed:
            return True
        if not original:
            return False
        # replayed 比 original 长 20% 以上视为改进
        return len(replayed) >= len(original) * 1.2
