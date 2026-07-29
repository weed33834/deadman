"""D18:任务复杂度路由(Task Complexity Routing)。

问题:
    现有 `orchestration/planner.py` 的 `is_complex_query` 仅基于规则:
        - keywords >= 3 OR length > 100 → complex
    问题:
        - 短查询也可能是复杂任务("继承纠纷" 仅 4 字但需多 agent)
        - 长查询也可能是简单任务("请帮我查一下电话号码 ..." 100+ 字但只是查表)
        - 未考虑 cost-aware:复杂任务用旗舰模型,简单任务用便宜模型
        - 未考虑 model tier 选择(planner 始终用单个配置的 LLM)
        - 未考虑 budget 余量(预算紧张时强制降级)
        - 未考虑 agent 数量(复杂任务才需要多 agent 协作)

缓解:
    - TaskComplexity: 简单 / 中等 / 复杂 / 极复杂(4 级)
    - ComplexityClassifier: 规则 + LLM 意图分类(可降级到纯规则)
    - ComplexityRouter: 按复杂度 + budget 选 (model_tier, max_agents, strategy)
    - cost-aware:预算紧张时强制降级到 cheap tier

设计:
    classifier = ComplexityClassifier()
    complexity = classifier.classify(query, user_budget_remaining=0.05)
    routing = ComplexityRouter.route(complexity, budget_remaining=0.05)

    # routing 包含:
    #   - model_tier: FLAGSHIP / MID / CHEAP / NANO
    #   - max_agents: 1 / 3 / 6 / 12
    #   - strategy: react / plan_execute / debate / multi_agent
    #   - max_steps: 5 / 15 / 25 / 50
    #   - max_cost_per_call: 0.001 / 0.01 / 0.05 / 0.2

集成:
    graph.py router_node 调用 ComplexityRouter 选择模型 + agent 数。

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class TaskComplexity(str, Enum):
    """任务复杂度等级。"""

    SIMPLE = "simple"  # 简单:查表 / 通知 / 单步
    MODERATE = "moderate"  # 中等:多步推理 / 单 agent
    COMPLEX = "complex"  # 复杂:多 agent / 需辩论
    EXTREME = "extreme"  # 极复杂:多轮辩论 / 跨域知识 / 高风险


class RoutingStrategy(str, Enum):
    """路由策略。"""

    REACT = "react"  # ReAct 单 agent
    PLAN_EXECUTE = "plan_execute"  # Plan-Execute
    DEBATE = "debate"  # 多 agent 辩论
    MULTI_AGENT = "multi_agent"  # 多 agent 协作
    LOOKUP = "lookup"  # 直接查表(无需 LLM)


# 复杂度信号(规则匹配)
_COMPLEXITY_SIGNALS = {
    TaskComplexity.EXTREME: [
        "继承纠纷", "跨国", "跨境", "多国", "争议", "诉讼", "仲裁",
        "遗产税", "多继承人", "海外资产", "信托", "复杂股权",
    ],
    TaskComplexity.COMPLEX: [
        "继承", "税务", "债务", "房产过户", "股权", "保险理赔",
        "辩论", "对比", "分析", "评估", "建议", "方案",
    ],
    TaskComplexity.MODERATE: [
        "流程", "步骤", "怎么办理", "如何", "需要什么",
        "材料", "证明", "申请",
    ],
}

# 直接查表信号(无需 LLM)
_LOOKUP_SIGNALS = [
    "电话", "热线", "地址", "网址", "官网", "营业时间",
    "查询", "查一下", "查电话",
]


@dataclass
class ComplexitySignals:
    """复杂度信号(规则 + LLM)。"""

    keyword_count: int = 0
    text_length: int = 0
    has_multi_domain: bool = False  # 涉及多领域
    has_legal_terms: bool = False  # 含法律术语
    has_cross_border: bool = False  # 跨境
    has_high_risk: bool = False  # 高风险(医疗 / 法律 / 财务决策)
    needs_multi_step: bool = False  # 需多步推理
    needs_tool_use: bool = False  # 需工具调用
    needs_debate: bool = False  # 需辩论
    llm_intent: str | None = None  # LLM 分类结果
    confidence: float = 0.0  # LLM 分类置信度


@dataclass
class RoutingDecision:
    """路由决策。"""

    complexity: TaskComplexity
    strategy: RoutingStrategy
    model_tier: str  # "flagship" / "mid" / "cheap" / "nano"
    max_agents: int
    max_steps: int
    max_cost_per_call: float  # 美元
    estimated_total_cost: float  # 估算总成本
    reason: str = ""


class ComplexityClassifier:
    """任务复杂度分类器。

    用法:
        classifier = ComplexityClassifier()
        complexity, signals = classifier.classify("我父亲去世,继承北京和海外房产")
        # complexity = TaskComplexity.EXTREME
        # signals.has_cross_border = True
    """

    # 默认阈值(env 可覆盖)
    MIN_KEYWORDS_COMPLEX = 3
    MIN_LENGTH_COMPLEX = 100

    def __init__(
        self,
        llm_classifier: callable | None = None,
        use_llm: bool = False,
    ) -> None:
        """
        Args:
            llm_classifier: async callable(query: str) -> (intent: str, confidence: float)
            use_llm: 是否使用 LLM 分类(默认 False,纯规则)
        """
        self.llm_classifier = llm_classifier
        self.use_llm = use_llm
        self._lock = threading.RLock()

    def classify(
        self,
        query: str,
        *,
        context: dict | None = None,
    ) -> tuple[TaskComplexity, ComplexitySignals]:
        """分类任务复杂度。

        Args:
            query: 用户查询
            context: 上下文(可含 user_id / tenant_id / 历史)

        Returns:
            (complexity, signals)
        """
        if not query:
            return TaskComplexity.SIMPLE, ComplexitySignals(text_length=0)

        signals = self._extract_signals(query, context or {})

        # 1. 先检查是否直接查表
        if any(sig in query for sig in _LOOKUP_SIGNALS):
            # 仅查表 → SIMPLE
            return TaskComplexity.SIMPLE, signals

        # 2. 按 EXTREME → COMPLEX → MODERATE → SIMPLE 优先级匹配
        for complexity in [
            TaskComplexity.EXTREME,
            TaskComplexity.COMPLEX,
            TaskComplexity.MODERATE,
        ]:
            signals_for_tier = _COMPLEXITY_SIGNALS.get(complexity, [])
            matched = [sig for sig in signals_for_tier if sig in query]
            if matched:
                # 多个信号 → 升一级(更复杂)
                if len(matched) >= 2 and complexity != TaskComplexity.EXTREME:
                    # 升级:complexity_index + 1(向 EXTREME 靠拢)
                    order = list(TaskComplexity)  # [SIMPLE, MODERATE, COMPLEX, EXTREME]
                    next_idx = min(len(order) - 1, order.index(complexity) + 1)
                    return order[next_idx], signals
                return complexity, signals

        # 3. 退化到规则(关键词 / 长度)
        if signals.keyword_count >= self.MIN_KEYWORDS_COMPLEX or signals.text_length > self.MIN_LENGTH_COMPLEX:
            return TaskComplexity.COMPLEX, signals
        elif signals.keyword_count >= 1 or signals.text_length > 30:
            return TaskComplexity.MODERATE, signals
        else:
            return TaskComplexity.SIMPLE, signals

    def classify_with_llm(
        self,
        query: str,
        context: dict | None = None,
    ) -> tuple[TaskComplexity, ComplexitySignals, float]:
        """LLM 增强分类(若配置了 llm_classifier)。

        Returns:
            (complexity, signals, confidence)
        """
        complexity, signals = self.classify(query, context=context)
        if not self.use_llm or self.llm_classifier is None:
            return complexity, signals, 0.5

        try:
            intent, confidence = self.llm_classifier(query)
            signals.llm_intent = intent
            signals.confidence = confidence

            # LLM 与规则不一致时,按置信度选择
            llm_complexity = self._intent_to_complexity(intent)
            if llm_complexity != complexity and confidence > 0.8:
                # 高置信度 LLM 优先
                return llm_complexity, signals, confidence
            return complexity, signals, confidence
        except Exception as e:
            logger.warning("LLM classifier failed: %s, fallback to rule", e)
            return complexity, signals, 0.5

    # ==================================================================
    # 内部
    # ==================================================================

    def _extract_signals(self, query: str, context: dict) -> ComplexitySignals:
        signals = ComplexitySignals()
        signals.text_length = len(query)

        # 关键词数(简单的中文分词:按 2-4 字短语)
        keywords = re.findall(r"[\u4e00-\u9fa5]{2,4}|[a-zA-Z]+", query)
        signals.keyword_count = len(set(keywords))

        # 多领域信号
        domains_in_query = []
        domain_keywords = {
            "legal": ["继承", "遗嘱", "法律", "诉讼"],
            "financial": ["税务", "债务", "资产", "银行"],
            "medical": ["医疗", "保险", "健康"],
            "real_estate": ["房产", "过户", "不动产"],
            "digital": ["数字", "账号", "密码", "在线"],
        }
        for domain, kws in domain_keywords.items():
            if any(kw in query for kw in kws):
                domains_in_query.append(domain)
        signals.has_multi_domain = len(domains_in_query) >= 2

        # 法律术语
        legal_terms = ["继承", "遗嘱", "诉讼", "仲裁", "判决", "法律", "法规"]
        signals.has_legal_terms = any(t in query for t in legal_terms)

        # 跨境
        cross_border_terms = ["海外", "跨国", "跨境", "国外", "境外", "国际"]
        signals.has_cross_border = any(t in query for t in cross_border_terms)

        # 高风险(涉及医疗 / 法律 / 财务决策)
        risk_terms = ["医疗决策", "法律建议", "投资", "大额", "高额"]
        signals.has_high_risk = any(t in query for t in risk_terms)

        # 多步推理
        multi_step_terms = ["然后", "接着", "之后", "步骤", "流程", "首先"]
        signals.needs_multi_step = any(t in query for t in multi_step_terms)

        # 工具调用
        tool_terms = ["查询", "搜索", "计算", "对比"]
        signals.needs_tool_use = any(t in query for t in tool_terms)

        # 辩论
        debate_terms = ["辩论", "对比", "哪个更好", "优缺点", "争议"]
        signals.needs_debate = any(t in query for t in debate_terms)

        return signals

    @staticmethod
    def _intent_to_complexity(intent: str) -> TaskComplexity:
        intent_lower = (intent or "").lower()
        if "extreme" in intent_lower or "very_complex" in intent_lower:
            return TaskComplexity.EXTREME
        if "complex" in intent_lower or "multi_agent" in intent_lower:
            return TaskComplexity.COMPLEX
        if "moderate" in intent_lower or "multi_step" in intent_lower:
            return TaskComplexity.MODERATE
        return TaskComplexity.SIMPLE


class ComplexityRouter:
    """复杂度感知的路由器。

    用法:
        router = ComplexityRouter()
        complexity, signals = classifier.classify(query)
        decision = router.route(complexity, signals, budget_remaining=0.05)
        # decision.model_tier = "flagship" (复杂任务)
        # decision.max_agents = 6 (复杂任务)
        # decision.strategy = RoutingStrategy.DEBATE
    """

    # 复杂度 → 默认配置
    DEFAULT_ROUTING = {
        TaskComplexity.SIMPLE: RoutingDecision(
            complexity=TaskComplexity.SIMPLE,
            strategy=RoutingStrategy.LOOKUP,
            model_tier="nano",
            max_agents=1,
            max_steps=3,
            max_cost_per_call=0.0005,
            estimated_total_cost=0.001,
            reason="简单查询:直接查表",
        ),
        TaskComplexity.MODERATE: RoutingDecision(
            complexity=TaskComplexity.MODERATE,
            strategy=RoutingStrategy.REACT,
            model_tier="cheap",
            max_agents=1,
            max_steps=10,
            max_cost_per_call=0.005,
            estimated_total_cost=0.02,
            reason="中等复杂:ReAct 单 agent",
        ),
        TaskComplexity.COMPLEX: RoutingDecision(
            complexity=TaskComplexity.COMPLEX,
            strategy=RoutingStrategy.PLAN_EXECUTE,
            model_tier="mid",
            max_agents=3,
            max_steps=20,
            max_cost_per_call=0.02,
            estimated_total_cost=0.10,
            reason="复杂任务:Plan-Execute + 多 agent",
        ),
        TaskComplexity.EXTREME: RoutingDecision(
            complexity=TaskComplexity.EXTREME,
            strategy=RoutingStrategy.MULTI_AGENT,
            model_tier="flagship",
            max_agents=6,
            max_steps=50,
            max_cost_per_call=0.05,
            estimated_total_cost=0.50,
            reason="极复杂:多 agent 协作 + 辩论",
        ),
    }

    # budget 阈值(美元):低于此值强制降级
    BUDGET_DEGRADE_THRESHOLDS = {
        "flagship_to_mid": 0.10,  # budget < $0.10 → flagship 降到 mid
        "mid_to_cheap": 0.05,
        "cheap_to_nano": 0.01,
    }

    def route(
        self,
        complexity: TaskComplexity,
        signals: ComplexitySignals | None = None,
        *,
        budget_remaining: float | None = None,
        budget_per_call_limit: float | None = None,
    ) -> RoutingDecision:
        """按复杂度 + budget 路由。"""
        decision = self.DEFAULT_ROUTING.get(
            complexity,
            self.DEFAULT_ROUTING[TaskComplexity.SIMPLE],
        )

        # 信号增强(升级策略)
        if signals:
            if signals.needs_debate and decision.strategy != RoutingStrategy.MULTI_AGENT:
                decision = RoutingDecision(
                    **{**decision.__dict__, "strategy": RoutingStrategy.DEBATE}
                )
            if signals.has_cross_border and decision.max_agents < 6:
                decision = RoutingDecision(
                    **{**decision.__dict__, "max_agents": 6, "model_tier": "flagship"}
                )

        # budget 感知降级
        if budget_remaining is not None and budget_remaining < decision.estimated_total_cost:
            decision = self._degrade_for_budget(decision, budget_remaining)

        # 单次调用成本上限
        if budget_per_call_limit and decision.max_cost_per_call > budget_per_call_limit:
            decision = RoutingDecision(
                **{**decision.__dict__, "max_cost_per_call": budget_per_call_limit}
            )

        return decision

    def _degrade_for_budget(
        self,
        decision: RoutingDecision,
        budget_remaining: float,
    ) -> RoutingDecision:
        """根据剩余 budget 降级。"""
        tier_order = ["flagship", "mid", "cheap", "nano"]
        current_tier_idx = tier_order.index(decision.model_tier) if decision.model_tier in tier_order else 1

        # 按 budget 阈值降级
        if budget_remaining < self.BUDGET_DEGRADE_THRESHOLDS["cheap_to_nano"]:
            target_idx = 3  # nano
        elif budget_remaining < self.BUDGET_DEGRADE_THRESHOLDS["mid_to_cheap"]:
            target_idx = 2  # cheap
        elif budget_remaining < self.BUDGET_DEGRADE_THRESHOLDS["flagship_to_mid"]:
            target_idx = 1  # mid
        else:
            target_idx = current_tier_idx

        if target_idx > current_tier_idx:
            new_tier = tier_order[target_idx]
            # 降级时减少 agent 数 / 步数 / 成本上限
            new_max_agents = max(1, decision.max_agents - 2)
            new_max_steps = max(3, decision.max_steps - 10)
            new_max_cost = decision.max_cost_per_call * 0.3
            decision = RoutingDecision(
                **{
                    **decision.__dict__,
                    "model_tier": new_tier,
                    "max_agents": new_max_agents,
                    "max_steps": new_max_steps,
                    "max_cost_per_call": new_max_cost,
                    "reason": f"{decision.reason} | budget-aware degraded to {new_tier}",
                }
            )
        return decision


# =====================================================================
# 全局单例
# =====================================================================

_classifier: ComplexityClassifier | None = None
_router: ComplexityRouter | None = None
_lock = threading.Lock()


def get_complexity_classifier() -> ComplexityClassifier:
    global _classifier
    with _lock:
        if _classifier is None:
            _classifier = ComplexityClassifier()
        return _classifier


def get_complexity_router() -> ComplexityRouter:
    global _router
    with _lock:
        if _router is None:
            _router = ComplexityRouter()
        return _router


def reset_complexity_router() -> None:
    """重置单例(测试用)。"""
    global _classifier, _router
    with _lock:
        _classifier = None
        _router = None
