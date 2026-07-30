"""P8.1.6 成本路由 - 多模型按成本 + SLA 选优。

设计借鉴:
    - LiteLLM Router:统一多 provider 路由
    - OpenAI Router:按任务复杂度选模型
    - AWS Bedrock Routing:成本最优 + 故障转移

核心策略:
    - SLA 优先:用户 plan = enterprise → 必须用强模型
    - 成本优先:用户 plan = free → 优先用便宜模型
    - 任务复杂度:简单分类用 mini,复杂推理用强模型
    - 故障转移:主 provider 熔断 → fallback

路由决策:
    1. 任务复杂度评分(LLM 评估 or 规则,简化为规则)
    2. 用户 plan 限制(SLA 等级)
    3. 配额状态(已超限 → 强制降级)
    4. 成本最优(同质量选最便宜)
    5. 熔断器状态(熔断的 provider 跳过)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum

from ..infrastructure.circuit_breaker import CircuitBreaker, CircuitState, cb_registry
from ..infrastructure.feature_flags import is_enabled
from .subscription import SubscriptionManager, get_subscription_manager

logger = logging.getLogger(__name__)


class ModelTier(str, Enum):
    """模型层级(按能力 + 成本分级)。

    - TINY: 极便宜,适合分类 / 路由(gpt-4o-mini / claude-haiku)
    - SMALL: 便宜,适合摘要 / 简单任务(gpt-4o-mini / glm-4-flash)
    - MEDIUM: 中等,适合主体响应(gpt-4o / claude-3-5-sonnet / glm-4.6)
    - LARGE: 强,适合复杂推理 / 高 R3 场景(gpt-4o / claude-3-opus / glm-4.6)
    - REASONING: 最强,适合 ToT / Self-Consistency(o1 / claude-3-opus)
    """

    TINY = "tiny"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    REASONING = "reasoning"


class RoutingStrategy(str, Enum):
    """路由策略。"""

    SLA_FIRST = "sla_first"  # SLA 优先(enterprise 用户)
    COST_FIRST = "cost_first"  # 成本优先(free 用户)
    QUALITY_FIRST = "quality_first"  # 质量优先(pro 用户)
    FAILOVER = "failover"  # 故障转移(主 provider 熔断时)


@dataclass(frozen=True)
class ModelChoice:
    """单个模型候选(不可变)。"""

    provider: str  # openai / anthropic / zhipu / deepseek / qwen / ollama
    model: str  # gpt-4o / claude-3-5-sonnet / glm-4.6 / ...
    tier: ModelTier
    # 能力评分(0-1,综合 context_length / reasoning / tool_use / vision)
    capability: float
    # 单价(CNY / 1K tokens,综合 prompt + completion)
    price_per_1k: float
    # 元数据
    supports_tools: bool = False
    supports_vision: bool = False
    supports_json_mode: bool = False
    max_context: int = 8192


@dataclass
class RoutingResult:
    """路由决策结果。"""

    chosen: ModelChoice | None
    reason: str  # 选择理由(便于审计)
    alternatives: list[ModelChoice] = field(default_factory=list)  # 备选(failover 用)
    estimated_cost: float = 0.0  # 预估成本(CNY)
    estimated_latency_ms: int = 0  # 预估延迟(ms)


# 内置模型池(按 tier 分组,可被 env / config 覆盖)
_MODEL_POOL: dict[ModelTier, list[ModelChoice]] = {
    ModelTier.TINY: [
        ModelChoice(
            "openai",
            "gpt-4o-mini",
            ModelTier.TINY,
            0.55,
            0.0015,
            supports_tools=True,
            supports_json_mode=True,
            max_context=128000,
        ),
        ModelChoice(
            "zhipu",
            "glm-4-flash",
            ModelTier.TINY,
            0.5,
            0.001,
            supports_tools=True,
            max_context=128000,
        ),
    ],
    ModelTier.SMALL: [
        ModelChoice(
            "openai",
            "gpt-4o-mini",
            ModelTier.SMALL,
            0.6,
            0.0015,
            supports_tools=True,
            supports_vision=True,
            supports_json_mode=True,
            max_context=128000,
        ),
        ModelChoice(
            "zhipu",
            "glm-4-flash",
            ModelTier.SMALL,
            0.55,
            0.001,
            supports_tools=True,
            max_context=128000,
        ),
        ModelChoice(
            "deepseek",
            "deepseek-chat",
            ModelTier.SMALL,
            0.65,
            0.001,
            supports_tools=True,
            max_context=64000,
        ),
    ],
    ModelTier.MEDIUM: [
        ModelChoice(
            "openai",
            "gpt-4o",
            ModelTier.MEDIUM,
            0.85,
            0.03,
            supports_tools=True,
            supports_vision=True,
            supports_json_mode=True,
            max_context=128000,
        ),
        ModelChoice(
            "anthropic",
            "claude-3-5-sonnet",
            ModelTier.MEDIUM,
            0.88,
            0.03,
            supports_tools=True,
            supports_vision=True,
            max_context=200000,
        ),
        ModelChoice(
            "zhipu",
            "glm-4.6",
            ModelTier.MEDIUM,
            0.82,
            0.005,
            supports_tools=True,
            supports_json_mode=True,
            max_context=128000,
        ),
    ],
    ModelTier.LARGE: [
        ModelChoice(
            "openai",
            "gpt-4o",
            ModelTier.LARGE,
            0.9,
            0.03,
            supports_tools=True,
            supports_vision=True,
            supports_json_mode=True,
            max_context=128000,
        ),
        ModelChoice(
            "anthropic",
            "claude-3-5-sonnet",
            ModelTier.LARGE,
            0.92,
            0.03,
            supports_tools=True,
            supports_vision=True,
            max_context=200000,
        ),
        ModelChoice(
            "zhipu",
            "glm-4.6",
            ModelTier.LARGE,
            0.85,
            0.005,
            supports_tools=True,
            max_context=128000,
        ),
    ],
    ModelTier.REASONING: [
        ModelChoice("openai", "o1", ModelTier.REASONING, 0.98, 0.06, max_context=200000),
        ModelChoice(
            "anthropic",
            "claude-3-opus",
            ModelTier.REASONING,
            0.95,
            0.05,
            supports_tools=True,
            supports_vision=True,
            max_context=200000,
        ),
    ],
}


class CostRouter:
    """成本路由器。

    决策流程:
        1. 用户 plan → 决定 tier 上限(enterprise=REASONING, pro=LARGE, free=SMALL)
        2. 任务复杂度 → 决定 tier 下限(简单分类=TINY, 复杂推理=REASONING)
        3. 配额状态 → 超限强制降级
        4. 熔断器 → 跳过 Open 的 provider
        5. 同 tier 内按策略排序(SLA / 成本 / 质量)
    """

    def __init__(
        self,
        subscriptions: SubscriptionManager | None = None,
    ) -> None:
        self.subscriptions = subscriptions or get_subscription_manager()
        self._lock = threading.RLock()
        # 模型池(可被覆盖)
        self._model_pool: dict[ModelTier, list[ModelChoice]] = {
            tier: list(models) for tier, models in _MODEL_POOL.items()
        }
        # 熔断器缓存:provider → CircuitBreaker
        self._breakers: dict[str, CircuitBreaker] = {}

    # ==================================================================
    # 路由决策
    # ==================================================================

    def route(
        self,
        user_id: str,
        task_complexity: str = "medium",  # tiny / small / medium / large / reasoning
        requires_tools: bool = False,
        requires_vision: bool = False,
        requires_json: bool = False,
        strategy: str | None = None,
        tenant_id: str | None = None,
    ) -> RoutingResult:
        """路由决策。

        Args:
            task_complexity: 任务复杂度(决定 tier 下限)
            requires_tools: 是否需要工具调用
            requires_vision: 是否需要视觉
            requires_json: 是否需要 JSON 输出
            strategy: 路由策略(默认按 plan 决定)

        Returns:
            RoutingResult(chosen / alternatives / estimated_cost)
        """
        if not is_enabled("billing"):
            # billing 关闭:返回默认 medium 模型(透传)
            default = self._model_pool[ModelTier.MEDIUM][0]
            return RoutingResult(
                chosen=default,
                reason="billing_disabled_default",
                alternatives=[],
                estimated_cost=default.price_per_1k * 2,
                estimated_latency_ms=500,
            )

        # 1. 获取用户 plan
        plan = self.subscriptions.get_effective_plan(user_id)
        plan_name = plan.name.value

        # 2. 决定策略
        if strategy is None:
            strategy = self._strategy_for_plan(plan_name)

        # 3. 决定 tier 上限 / 下限
        tier_upper = self._tier_upper_for_plan(plan_name)
        tier_lower = (
            ModelTier(task_complexity)
            if task_complexity in [t.value for t in ModelTier]
            else ModelTier.MEDIUM
        )

        # tier 下限不超过上限
        if self._tier_rank(tier_lower) > self._tier_rank(tier_upper):
            tier_lower = tier_upper

        # 4. 候选筛选:tier 在 [lower, upper] + 满足能力需求 + 熔断器未 Open
        candidates = self._filter_candidates(
            tier_lower, tier_upper, requires_tools, requires_vision, requires_json
        )

        if not candidates:
            # 无可用候选(所有都熔断)→ 返回 None,业务层降级
            logger.warning(
                "No available model for user %s (plan=%s, tier=[%s,%s])",
                user_id,
                plan_name,
                tier_lower.value,
                tier_upper.value,
            )
            return RoutingResult(chosen=None, reason="no_available_model", alternatives=[])

        # 5. 按策略排序
        sorted_candidates = self._sort_by_strategy(candidates, RoutingStrategy(strategy))

        chosen = sorted_candidates[0]
        alternatives = sorted_candidates[1:3]  # 取 2 个备选

        # 6. 估算成本(假设平均 2K tokens)
        avg_tokens = 2000
        estimated_cost = chosen.price_per_1k * (avg_tokens / 1000)
        estimated_latency = self._estimate_latency(chosen)

        return RoutingResult(
            chosen=chosen,
            reason=f"strategy={strategy},tier={tier_lower.value}-{tier_upper.value},candidates={len(candidates)}",
            alternatives=alternatives,
            estimated_cost=estimated_cost,
            estimated_latency_ms=estimated_latency,
        )

    # ==================================================================
    # 故障转移
    # ==================================================================

    def get_failover(
        self, original: ModelChoice, tenant_id: str | None = None
    ) -> ModelChoice | None:
        """获取故障转移候选(主模型熔断时用)。"""
        candidates = self._model_pool.get(original.tier, [])
        for c in candidates:
            if c.provider == original.provider and c.model == original.model:
                continue
            if self._is_breaker_open(c.provider):
                continue
            return c
        return None

    # ==================================================================
    # 配置
    # ==================================================================

    def register_model(self, tier: ModelTier, choice: ModelChoice) -> None:
        """动态注册模型(扩展用)。"""
        with self._lock:
            # 替换同 provider+model 的现有项
            existing = [
                m
                for m in self._model_pool[tier]
                if not (m.provider == choice.provider and m.model == choice.model)
            ]
            existing.append(choice)
            self._model_pool[tier] = existing

    def list_models(self, tier: ModelTier | None = None) -> list[ModelChoice]:
        """列出所有模型(看板用)。"""
        if tier is None:
            result = []
            for models in self._model_pool.values():
                result.extend(models)
            return result
        return list(self._model_pool.get(tier, []))

    # ==================================================================
    # 内部
    # ==================================================================

    def _strategy_for_plan(self, plan_name: str) -> str:
        """按 plan 决定默认策略。"""
        if plan_name == "enterprise":
            return RoutingStrategy.SLA_FIRST.value
        if plan_name == "pro":
            return RoutingStrategy.QUALITY_FIRST.value
        return RoutingStrategy.COST_FIRST.value

    def _tier_upper_for_plan(self, plan_name: str) -> ModelTier:
        """plan → tier 上限。"""
        if plan_name == "enterprise":
            return ModelTier.REASONING
        if plan_name == "pro":
            return ModelTier.LARGE
        return ModelTier.SMALL  # free

    @staticmethod
    def _tier_rank(tier: ModelTier) -> int:
        """tier → 数字(便于比较)。"""
        return {
            ModelTier.TINY: 0,
            ModelTier.SMALL: 1,
            ModelTier.MEDIUM: 2,
            ModelTier.LARGE: 3,
            ModelTier.REASONING: 4,
        }[tier]

    def _filter_candidates(
        self,
        tier_lower: ModelTier,
        tier_upper: ModelTier,
        requires_tools: bool,
        requires_vision: bool,
        requires_json: bool,
    ) -> list[ModelChoice]:
        """筛选满足条件的候选。"""
        candidates: list[ModelChoice] = []
        lower_rank = self._tier_rank(tier_lower)
        upper_rank = self._tier_rank(tier_upper)

        for tier, models in self._model_pool.items():
            rank = self._tier_rank(tier)
            if rank < lower_rank or rank > upper_rank:
                continue
            for m in models:
                if requires_tools and not m.supports_tools:
                    continue
                if requires_vision and not m.supports_vision:
                    continue
                if requires_json and not m.supports_json_mode:
                    continue
                if self._is_breaker_open(m.provider):
                    continue
                candidates.append(m)
        return candidates

    def _sort_by_strategy(
        self, candidates: list[ModelChoice], strategy: RoutingStrategy
    ) -> list[ModelChoice]:
        """按策略排序候选。"""
        if strategy == RoutingStrategy.COST_FIRST:
            return sorted(candidates, key=lambda m: (m.price_per_1k, -m.capability))
        if strategy == RoutingStrategy.QUALITY_FIRST:
            return sorted(candidates, key=lambda m: (-m.capability, m.price_per_1k))
        if strategy == RoutingStrategy.SLA_FIRST:
            # SLA 优先:能力 + 稳定性(用 provider 历史成功率,简化为优先大厂)
            priority = {
                "openai": 0,
                "anthropic": 1,
                "zhipu": 2,
                "deepseek": 3,
                "qwen": 4,
                "ollama": 5,
            }
            return sorted(
                candidates,
                key=lambda m: (priority.get(m.provider, 99), -m.capability, m.price_per_1k),
            )
        return candidates

    def _is_breaker_open(self, provider: str) -> bool:
        """检查 provider 熔断器是否 Open。"""
        if not is_enabled("circuit_breaker"):
            return False
        try:
            breaker = self._get_breaker(provider)
            # 只查不改状态(轻量查询)
            return breaker.state == CircuitState.OPEN
        except Exception:
            return False

    def _get_breaker(self, provider: str) -> CircuitBreaker:
        if provider not in self._breakers:
            self._breakers[provider] = cb_registry.get_or_create(f"llm_{provider}")
        return self._breakers[provider]

    @staticmethod
    def _estimate_latency(model: ModelChoice) -> int:
        """估算延迟(ms)。"""
        # 简化:大厂延迟低,本地延迟高
        latency_map = {
            "openai": 500,
            "anthropic": 600,
            "zhipu": 300,
            "deepseek": 400,
            "qwen": 350,
            "ollama": 2000,  # 本地
        }
        return latency_map.get(model.provider, 1000)


# 全局单例
_cr_instance: CostRouter | None = None
_cr_lock = threading.Lock()


def get_cost_router() -> CostRouter:
    global _cr_instance
    if _cr_instance is None:
        with _cr_lock:
            if _cr_instance is None:
                _cr_instance = CostRouter()
    return _cr_instance
