"""测试 deadman.reflexion - Reflexion 反思重试引擎

覆盖点：
  - execute_with_reflexion 成功路径（首次即成功）
  - execute_with_reflexion 失败重试（首次失败，重试后成功）
  - execute_with_reflexion 全部失败走 fallback
  - ADJUSTMENT_STRATEGIES 预定义策略表查找
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from deadman.reflexion.engine import (
    ADJUSTMENT_STRATEGIES,
    ReflexionEngine,
    get_predefined_strategy,
)

# =====================================================================
# ADJUSTMENT_STRATEGIES - 预定义策略表
# =====================================================================


class TestAdjustmentStrategies:
    """测试预定义调整策略表"""

    def test_strategies_count(self):
        # 10 个预定义策略
        assert len(ADJUSTMENT_STRATEGIES) == 10

    def test_strategies_keys(self):
        # 关键失败模式都覆盖
        expected_keys = {
            "platform_not_supported", "timeout", "format_error",
            "subagent_not_found", "knowledge_not_found", "api_error",
            "rate_limit", "invalid_argument", "permission_denied", "unknown",
        }
        assert set(ADJUSTMENT_STRATEGIES.keys()) == expected_keys

    def test_get_predefined_strategy_hit(self):
        # 命中已知失败模式
        result = get_predefined_strategy("timeout")
        assert result is not None
        assert "strategy" in result
        assert "adjusted_params" in result

    def test_get_predefined_strategy_miss(self):
        # 未命中返回 None
        assert get_predefined_strategy("non_existent_failure") is None

    def test_each_strategy_has_required_fields(self):
        # 每个策略至少含 strategy 和 adjusted_params
        for key, strategy in ADJUSTMENT_STRATEGIES.items():
            assert "strategy" in strategy, f"策略 {key} 缺 strategy 字段"
            assert "adjusted_params" in strategy, f"策略 {key} 缺 adjusted_params 字段"

    def test_timeout_strategy_has_simplification(self):
        # timeout 策略应包含简化任务相关参数
        timeout_strategy = ADJUSTMENT_STRATEGIES["timeout"]
        assert "strategy" in timeout_strategy
        adjusted = timeout_strategy["adjusted_params"]
        assert "simplify_task" in adjusted
        assert "max_context_length" in adjusted

    def test_rate_limit_strategy_has_backoff(self):
        # rate_limit 策略应有 backoff_seconds
        rate_limit_strategy = ADJUSTMENT_STRATEGIES["rate_limit"]
        assert "backoff_seconds" in rate_limit_strategy
        assert rate_limit_strategy["backoff_seconds"] == 60


# =====================================================================
# execute_with_reflexion - 成功路径
# =====================================================================


class TestExecuteWithReflexionSuccess:
    """测试 execute_with_reflexion 成功路径"""

    async def test_success_on_first_attempt(self):
        # 首次即成功 → attempts=1
        async def operation(**kwargs):
            return {"execution_mode": "success"}

        engine = ReflexionEngine(agent_name="test-agent")
        result = await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="subagent",
        )

        assert result["success"] is True
        assert result["attempts"] == 1
        assert "result" in result
        assert "fallback" not in result

    async def test_success_returns_result(self):
        # 成功时 result 字段保留操作返回值
        async def operation(**kwargs):
            return {"execution_mode": "success", "answer": "42"}

        engine = ReflexionEngine()
        result = await engine.execute_with_reflexion(
            operation=operation,
            initial_input={},
            operation_type="subagent",
        )
        assert result["result"]["answer"] == "42"

    async def test_tool_operation_success(self):
        # tool 类型操作成功
        async def operation(**kwargs):
            return {"data": "ok"}

        engine = ReflexionEngine()
        result = await engine.execute_with_reflexion(
            operation=operation,
            initial_input={},
            operation_type="tool",
        )
        assert result["success"] is True

    async def test_transfer_operation_success(self):
        # transfer 类型操作成功
        async def operation(**kwargs):
            return {"accepted": True}

        engine = ReflexionEngine()
        result = await engine.execute_with_reflexion(
            operation=operation,
            initial_input={},
            operation_type="transfer",
        )
        assert result["success"] is True


# =====================================================================
# execute_with_reflexion - 失败重试
# =====================================================================


class TestExecuteWithReflexionRetry:
    """测试 execute_with_reflexion 失败重试"""

    async def test_retry_then_success(self, patch_llm):
        # 首次失败，重试后成功 → attempts >= 2
        call_count = {"n": 0}

        async def operation(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # 首次返回 fallback（视为失败）
                return {"execution_mode": "fallback", "fallback_reason": "api_error"}
            # 重试后成功
            return {"execution_mode": "success"}

        # mock LLM 反思返回 api_error 失败类型（命中预定义策略）
        patch_llm.chat_json = AsyncMock(return_value={
            "failure_type": "api_error",
            "failure_reason": "API 临时错误",
            "adjustment_strategy": "降级",
            "adjusted_params": {"execution_mode": "fallback"},
        })

        engine = ReflexionEngine(agent_name="test-agent")
        result = await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="subagent",
        )

        assert result["success"] is True
        assert result["attempts"] >= 2
        # 应有失败和反思记录
        assert len(engine.failures) >= 1

    async def test_failure_recorded(self, patch_llm):
        # 失败应被记录到 self.failures
        call_count = {"n": 0}

        async def operation(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"execution_mode": "fallback", "fallback_reason": "timeout"}
            return {"execution_mode": "success"}

        patch_llm.chat_json = AsyncMock(return_value={
            "failure_type": "timeout",
            "failure_reason": "超时",
            "adjustment_strategy": "简化任务",
            "adjusted_params": {"simplify_task": True},
        })

        engine = ReflexionEngine()
        await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="subagent",
        )

        assert len(engine.failures) >= 1
        assert engine.failures[0]["failure_type"] in ("timeout", "fallback")
        assert len(engine.reflections) >= 1


# =====================================================================
# execute_with_reflexion - 全部失败走 fallback
# =====================================================================


class TestExecuteWithReflexionFallback:
    """测试 execute_with_reflexion 全部失败走 fallback"""

    async def test_all_fail_returns_fallback(self, patch_llm):
        # 所有重试都失败 → 返回 fallback
        async def operation(**kwargs):
            # 永远返回 fallback
            return {"execution_mode": "fallback", "fallback_reason": "api_error"}

        patch_llm.chat_json = AsyncMock(return_value={
            "failure_type": "api_error",
            "failure_reason": "持续失败",
            "adjustment_strategy": "降级",
            "adjusted_params": {"execution_mode": "fallback"},
        })

        engine = ReflexionEngine(agent_name="always-fail")
        result = await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="subagent",
        )

        # 全部失败 → fallback=True
        assert result["success"] is False
        assert result.get("fallback") is True
        assert result["attempts"] == engine.max_retries
        assert "fallback_reason" in result
        assert "failures" in result
        assert len(result["failures"]) == engine.max_retries

    async def test_exception_treated_as_failure(self, patch_llm):
        # 操作抛异常 → 视为失败
        async def operation(**kwargs):
            raise RuntimeError("boom")

        patch_llm.chat_json = AsyncMock(return_value={
            "failure_type": "unknown",
            "failure_reason": "异常",
            "adjustment_strategy": "简化输入",
            "adjusted_params": {"simplify_input": True},
        })

        engine = ReflexionEngine()
        result = await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="tool",
        )

        assert result["success"] is False
        assert result.get("fallback") is True
        # 失败类型应为 exception
        assert any(f["failure_type"] == "exception" for f in engine.failures)

    async def test_fallback_reason_includes_last_failure(self, patch_llm):
        # fallback_reason 应包含最后一次失败信息
        async def operation(**kwargs):
            return {"execution_mode": "fallback", "fallback_reason": "rate_limit"}

        patch_llm.chat_json = AsyncMock(return_value={
            "failure_type": "rate_limit",
            "failure_reason": "限流",
            "adjustment_strategy": "等待重试",
            "adjusted_params": {"backoff_seconds": 60},
        })

        engine = ReflexionEngine()
        result = await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="subagent",
        )

        reason = result.get("fallback_reason", "")
        # 应提到重试次数
        assert "重试" in reason or str(engine.max_retries) in reason


# =====================================================================
# ReflexionEngine - 基础行为
# =====================================================================


class TestReflexionEngineBasics:
    """测试 ReflexionEngine 基础行为"""

    def test_init_defaults(self):
        # 默认 agent_name="default"
        engine = ReflexionEngine()
        assert engine.agent_name == "default"
        assert engine.memory_store is None
        assert engine.max_retries >= 1

    def test_init_with_custom_name(self):
        engine = ReflexionEngine(agent_name="death_aftercare")
        assert engine.agent_name == "death_aftercare"

    async def test_failures_reset_per_execution(self, patch_llm):
        # 每次 execute_with_reflexion 应清空 failures
        async def operation(**kwargs):
            return {"execution_mode": "success"}

        engine = ReflexionEngine()
        # 预填一些失败记录
        engine.failures.append({"fake": True})
        engine.reflections.append({"fake": True})

        await engine.execute_with_reflexion(
            operation=operation, initial_input={}, operation_type="subagent"
        )
        # 应被清空（成功路径下不应有 failures）
        assert len(engine.failures) == 0
