"""D11: LLM 能力分级抽象(Capability Tier Abstraction)。

问题:
    LLM 能力快速跃迁(GPT-3.5 → GPT-4 → GPT-4o → GPT-5 → ...),
    每次跃迁带来:
        - 上下文窗口扩大(4K → 128K → 1M → 10M)
        - 推理能力增强(单步 → CoT → Tree of Thoughts → 内置 o1-style)
        - 工具调用进化(无 → function calling → computer use → autonomous agent)
        - 多模态扩展(文本 → vision → audio → video → 3D)

    现有 `llm.py` 的 `use_case` 是扁平字符串(router/summarizer/respond),
    无法表达"模型支持工具调用 / vision / json-mode / streaming"等能力。
    当 LLM 跃迁后,大量代码废弃,需重构。

缓解:
    - CapabilityTier: 显式能力等级(flagship / mid / cheap / nano)
    - ModelCapability: 能力矩阵(支持 vision / tool_use / json_mode / streaming / ...)
    - CapabilityRequirement: 任务对能力的需求(必须支持 vision / 必须 tool_use / ...)
    - CapabilityRouter: 按需求 + budget 选择最合适的模型(不绑定具体 provider)
    - 渐进迁移:老 use_case 接口保留,新接口 capability-aware

设计:
    CapabilityRouter.match(req) -> (provider, model_name, tier)
    自动按能力 + budget 选模型,fallback 到次优。

集成:
    react_loop.py 调用 LLM 前:
        req = CapabilityRequirement(
            needs_tool_use=True,
            needs_vision=False,
            max_latency_ms=2000,
        )
        provider, model, tier = cap_router.match(req)
        llm.set_provider_model(provider, model)

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用,关闭后透传到顶级)。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


class CapabilityTier(str, Enum):
    """LLM 能力等级(从高到低)。

    不同等级对应不同的成本 / 延迟 / 能力水平。
    """

    FLAGSHIP = "flagship"  # 顶级(GPT-4o / Claude-Opus / Gemini-Ultra)
    MID = "mid"  # 中端(GPT-4o-mini / Claude-Sonnet / Gemini-Flash)
    CHEAP = "cheap"  # 低端(GPT-4o-mini / Haiku / Gemini-Nano)
    NANO = "nano"  # 极简(本地 7B / Haiku / nano)


class ModelCapability(str, Enum):
    """LLM 能力维度(支持 / 不支持)。"""

    TEXT = "text"  # 文本生成
    VISION = "vision"  # 图像理解
    AUDIO = "audio"  # 音频理解
    VIDEO = "video"  # 视频理解
    TOOL_USE = "tool_use"  # 工具调用(function calling)
    JSON_MODE = "json_mode"  # 结构化输出
    STREAMING = "streaming"  # 流式输出
    LONG_CONTEXT = "long_context"  # 长上下文(>128K)
    REASONING = "reasoning"  # 推理增强(o1 / R1 style)
    COMPUTER_USE = "computer_use"  # 计算机操作(Claude Computer Use)
    PARALLEL_TOOL = "parallel_tool"  # 并行工具调用


@dataclass
class ModelProfile:
    """模型能力档案。

    一个 (provider, model_name) 对应一份档案。
    """

    provider: str  # openai / anthropic / zhipu / ollama / ...
    model_name: str  # gpt-4o / claude-3-opus / glm-4 / qwen2.5:7b
    tier: CapabilityTier
    capabilities: set[ModelCapability] = field(default_factory=set)
    context_window: int = 4096  # tokens
    max_output_tokens: int = 1024
    # 大致成本(美元 / 1K tokens,用于 cost-aware routing)
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0
    # 大致延迟(ms,用于 latency-aware routing)
    typical_latency_ms: int = 1000
    # 是否本地推理(影响隐私 / 数据驻留)
    is_local: bool = False
    # 自定义元数据(版本 / region / ...)
    metadata: dict[str, Any] = field(default_factory=dict)

    def supports(self, req: CapabilityRequirement) -> bool:
        """是否满足能力需求。"""
        for cap in req.required_capabilities:
            if cap not in self.capabilities:
                return False
        if req.min_context_window and self.context_window < req.min_context_window:
            return False
        if req.max_latency_ms and self.typical_latency_ms > req.max_latency_ms:
            return False
        if req.require_local and not self.is_local:
            return False
        if req.min_tier:
            # 比较 tier 优先级(flagship=0 > mid=1 > cheap=2 > nano=3)
            tier_order = list(CapabilityTier)
            if tier_order.index(self.tier) > tier_order.index(req.min_tier):
                return False
        return True

    def cost_per_call(self, input_tokens: int, output_tokens: int) -> float:
        """估算单次调用成本(美元)。"""
        return (
            self.input_cost_per_1k * input_tokens / 1000
            + self.output_cost_per_1k * output_tokens / 1000
        )


@dataclass
class CapabilityRequirement:
    """任务对 LLM 能力的需求。"""

    required_capabilities: set[ModelCapability] = field(default_factory=set)
    min_context_window: int | None = None
    max_latency_ms: int | None = None
    require_local: bool = False
    min_tier: CapabilityTier | None = None
    # 偏好(软约束,影响排序)
    preferred_tier: CapabilityTier | None = None
    # budget 上限(美元/调用)
    max_cost_per_call: float | None = None
    # 预估 token(影响成本计算)
    estimated_input_tokens: int = 1000
    estimated_output_tokens: int = 500
    # 用途标签(用于审计 / 配额)
    use_case: str = ""


class CapabilityRouter:
    """能力感知的 LLM 路由器。

    用法:
        router = CapabilityRouter()
        router.register(ModelProfile(
            provider="openai", model_name="gpt-4o",
            tier=CapabilityTier.FLAGSHIP,
            capabilities={ModelCapability.TEXT, ModelCapability.VISION, ModelCapability.TOOL_USE, ...},
            context_window=128000,
        ))
        router.register(...)

        req = CapabilityRequirement(
            required_capabilities={ModelCapability.TOOL_USE, ModelCapability.VISION},
            max_latency_ms=3000,
        )
        match = router.match(req)
        if match:
            print(f"Use {match.provider}/{match.model_name}")
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._profiles: dict[str, ModelProfile] = {}  # key = f"{provider}:{model_name}"
        # 缓存(按 req 签名)
        self._cache: dict[str, tuple[ModelProfile | None, float]] = {}
        self._cache_ttl_seconds = 60.0

    def register(self, profile: ModelProfile) -> None:
        """注册模型档案(同 key 会覆盖)。"""
        key = self._key(profile.provider, profile.model_name)
        with self._lock:
            self._profiles[key] = profile
            # 失效缓存
            self._cache.clear()
            logger.info(
                "Registered model profile: %s/%s (tier=%s, caps=%d)",
                profile.provider, profile.model_name, profile.tier.value,
                len(profile.capabilities),
            )

    def unregister(self, provider: str, model_name: str) -> bool:
        key = self._key(provider, model_name)
        with self._lock:
            existed = key in self._profiles
            self._profiles.pop(key, None)
            self._cache.clear()
            return existed

    def list_profiles(self) -> list[ModelProfile]:
        with self._lock:
            return list(self._profiles.values())

    def match(
        self,
        req: CapabilityRequirement,
        *,
        exclude: set[str] | None = None,
    ) -> ModelProfile | None:
        """按需求匹配最合适的模型。

        选择策略:
            1. 先过滤满足 required_capabilities 的模型
            2. 按 (preferred_tier 命中) → (cost) → (latency) 排序
            3. budget 内选最优

        Args:
            req: 能力需求
            exclude: 排除的模型 key 集合(用于 fallback,避免重复尝试已失败的)

        Returns:
            匹配的 ModelProfile,或 None(无可用模型)
        """
        if not is_enabled("defense"):
            # 关闭后:返回第一个 profile(向后兼容)
            with self._lock:
                if self._profiles:
                    return next(iter(self._profiles.values()))
                return None

        cache_key = self._req_signature(req, exclude or set())
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.time() - cached[1] < self._cache_ttl_seconds:
                return cached[0]

        with self._lock:
            candidates = []
            for key, profile in self._profiles.items():
                if exclude and key in exclude:
                    continue
                if profile.supports(req):
                    cost = profile.cost_per_call(
                        req.estimated_input_tokens,
                        req.estimated_output_tokens,
                    )
                    if req.max_cost_per_call and cost > req.max_cost_per_call:
                        continue
                    candidates.append((profile, cost))

            if not candidates:
                self._cache[cache_key] = (None, time.time())
                return None

            # 排序:preferred_tier 命中 → cost → latency → tier 等级
            tier_order = list(CapabilityTier)

            def sort_key(item: tuple[ModelProfile, float]):
                p, cost = item
                preferred_hit = 0 if (req.preferred_tier and p.tier == req.preferred_tier) else 1
                return (
                    preferred_hit,
                    cost,
                    p.typical_latency_ms,
                    tier_order.index(p.tier),
                )

            candidates.sort(key=sort_key)
            best = candidates[0][0]
            self._cache[cache_key] = (best, time.time())
            return best

    def match_chain(
        self,
        req: CapabilityRequirement,
        *,
        max_fallbacks: int = 3,
    ) -> list[ModelProfile]:
        """返回匹配链(主选 + 备选 N 个,按优先级)。

        用于实现"主模型挂 → 备选" 的 fallback 机制。
        """
        if not is_enabled("defense"):
            with self._lock:
                return list(self._profiles.values())[: max_fallbacks + 1]

        chain: list[ModelProfile] = []
        excluded: set[str] = set()
        with self._lock:
            for _ in range(max_fallbacks + 1):
                m = self.match(req, exclude=excluded)
                if m is None:
                    break
                chain.append(m)
                excluded.add(self._key(m.provider, m.model_name))
        return chain

    def get(self, provider: str, model_name: str) -> ModelProfile | None:
        with self._lock:
            return self._profiles.get(self._key(provider, model_name))

    def clear(self) -> None:
        with self._lock:
            self._profiles.clear()
            self._cache.clear()

    # ==================================================================
    # 内部
    # ==================================================================

    @staticmethod
    def _key(provider: str, model_name: str) -> str:
        return f"{provider}:{model_name}"

    @staticmethod
    def _req_signature(req: CapabilityRequirement, exclude: frozenset[str] | set[str]) -> str:
        caps = "|".join(sorted(c.value for c in req.required_capabilities))
        excl = "|".join(sorted(exclude)) if exclude else ""
        return (
            f"caps={caps}|ctx={req.min_context_window}|lat={req.max_latency_ms}"
            f"|local={req.require_local}|tier={req.min_tier}|pref={req.preferred_tier}"
            f"|cost={req.max_cost_per_call}|excl={excl}"
        )


# =====================================================================
# 全局单例 + 默认 profile 注册
# =====================================================================

_capability_router: CapabilityRouter | None = None
_router_lock = threading.Lock()


def get_capability_router() -> CapabilityRouter:
    """获取全局 CapabilityRouter 单例(首次调用自动注册常见模型 profile)。"""
    global _capability_router
    if _capability_router is None:
        with _router_lock:
            if _capability_router is None:
                _capability_router = CapabilityRouter()
                _register_default_profiles(_capability_router)
    return _capability_router


def _register_default_profiles(router: CapabilityRouter) -> None:
    """注册常见模型的默认能力档案(2024-2025 数据)。

    注意:这些数据会随时间过时,生产环境应通过 `register` 或配置文件覆盖。
    """
    defaults = [
        # OpenAI
        ModelProfile(
            provider="openai", model_name="gpt-4o",
            tier=CapabilityTier.FLAGSHIP,
            capabilities={
                ModelCapability.TEXT, ModelCapability.VISION, ModelCapability.AUDIO,
                ModelCapability.TOOL_USE, ModelCapability.JSON_MODE,
                ModelCapability.STREAMING, ModelCapability.LONG_CONTEXT,
                ModelCapability.PARALLEL_TOOL,
            },
            context_window=128_000, max_output_tokens=16_384,
            input_cost_per_1k=0.0025, output_cost_per_1k=0.01,
            typical_latency_ms=2000,
        ),
        ModelProfile(
            provider="openai", model_name="gpt-4o-mini",
            tier=CapabilityTier.MID,
            capabilities={
                ModelCapability.TEXT, ModelCapability.VISION,
                ModelCapability.TOOL_USE, ModelCapability.JSON_MODE,
                ModelCapability.STREAMING, ModelCapability.PARALLEL_TOOL,
            },
            context_window=128_000, max_output_tokens=16_384,
            input_cost_per_1k=0.00015, output_cost_per_1k=0.0006,
            typical_latency_ms=1000,
        ),
        ModelProfile(
            provider="openai", model_name="o1-preview",
            tier=CapabilityTier.FLAGSHIP,
            capabilities={
                ModelCapability.TEXT, ModelCapability.VISION,
                ModelCapability.REASONING, ModelCapability.LONG_CONTEXT,
            },
            context_window=128_000, max_output_tokens=32_768,
            input_cost_per_1k=0.015, output_cost_per_1k=0.06,
            typical_latency_ms=10_000,  # o1 慢
        ),
        # Anthropic
        ModelProfile(
            provider="anthropic", model_name="claude-3-5-sonnet-20241022",
            tier=CapabilityTier.MID,
            capabilities={
                ModelCapability.TEXT, ModelCapability.VISION,
                ModelCapability.TOOL_USE, ModelCapability.STREAMING,
                ModelCapability.LONG_CONTEXT, ModelCapability.COMPUTER_USE,
            },
            context_window=200_000, max_output_tokens=8_192,
            input_cost_per_1k=0.003, output_cost_per_1k=0.015,
            typical_latency_ms=1500,
        ),
        ModelProfile(
            provider="anthropic", model_name="claude-3-opus-20240229",
            tier=CapabilityTier.FLAGSHIP,
            capabilities={
                ModelCapability.TEXT, ModelCapability.VISION,
                ModelCapability.TOOL_USE, ModelCapability.STREAMING,
                ModelCapability.LONG_CONTEXT,
            },
            context_window=200_000, max_output_tokens=4_096,
            input_cost_per_1k=0.015, output_cost_per_1k=0.075,
            typical_latency_ms=3000,
        ),
        ModelProfile(
            provider="anthropic", model_name="claude-3-haiku-20240307",
            tier=CapabilityTier.CHEAP,
            capabilities={
                ModelCapability.TEXT, ModelCapability.VISION,
                ModelCapability.TOOL_USE, ModelCapability.STREAMING,
            },
            context_window=200_000, max_output_tokens=4_096,
            input_cost_per_1k=0.00025, output_cost_per_1k=0.00125,
            typical_latency_ms=500,
        ),
        # Zhipu
        ModelProfile(
            provider="zhipu", model_name="glm-4-plus",
            tier=CapabilityTier.MID,
            capabilities={
                ModelCapability.TEXT, ModelCapability.VISION,
                ModelCapability.TOOL_USE, ModelCapability.JSON_MODE,
                ModelCapability.STREAMING,
            },
            context_window=128_000, max_output_tokens=4_096,
            input_cost_per_1k=0.0007, output_cost_per_1k=0.0007,
            typical_latency_ms=1500,
        ),
        # Local
        ModelProfile(
            provider="ollama", model_name="qwen2.5:7b",
            tier=CapabilityTier.NANO,
            capabilities={
                ModelCapability.TEXT, ModelCapability.STREAMING,
                ModelCapability.TOOL_USE,
            },
            context_window=32_000, max_output_tokens=4_096,
            input_cost_per_1k=0.0, output_cost_per_1k=0.0,
            typical_latency_ms=800,
            is_local=True,
        ),
        ModelProfile(
            provider="ollama", model_name="llama3.2-vision:11b",
            tier=CapabilityTier.CHEAP,
            capabilities={
                ModelCapability.TEXT, ModelCapability.VISION,
                ModelCapability.STREAMING,
            },
            context_window=128_000, max_output_tokens=4_096,
            input_cost_per_1k=0.0, output_cost_per_1k=0.0,
            typical_latency_ms=1500,
            is_local=True,
        ),
    ]
    for p in defaults:
        router.register(p)


def reset_capability_router() -> None:
    """重置全局单例(测试用)。"""
    global _capability_router
    with _router_lock:
        _capability_router = None
