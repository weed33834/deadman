"""P8.7 Mixture-of-Experts (MoE) 路由器。

为 deadman 5 大领域(法律 / 医疗 / 情感 / 财务 / 通用 / 代码)分别注册专家,
查询时按 (specialization_match × (1 - load_ratio) × success_rate) 加权选择。

设计要点:
    - 加权选择:综合领域匹配度 / 负载水位 / 历史成功率
    - 容量保护:已达 capacity 的专家跳过(防止过载)
    - Fallback:无可用专家时回退到 default_expert
    - 在线学习:record_result 更新 success_rate(EMA 平滑)

参考: Switch Transformer (Fedus et al., 2021) 的负载均衡思路。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# =====================================================================
# 专家类型
# =====================================================================
class ExpertSpecialization(str, Enum):
    """专家专长领域。"""

    LEGAL = "legal"
    MEDICAL = "medical"
    EMOTIONAL = "emotional"
    FINANCIAL = "financial"
    CODE = "code"
    GENERAL = "general"


# 领域关键词(用于 query 分类)
_SPECIALIZATION_KEYWORDS: dict[ExpertSpecialization, list[str]] = {
    ExpertSpecialization.LEGAL: [
        "法律", "遗嘱", "继承", "监护", "诉讼", "合同", "律师", "法规", "条款",
        "legal", "will", "inheritance", "guardian", "lawsuit", "contract",
    ],
    ExpertSpecialization.MEDICAL: [
        "医疗", "病情", "诊断", "治疗", "疼痛", "临终", "安宁疗护", "医生",
        "medical", "diagnosis", "treatment", "hospice", "pain",
    ],
    ExpertSpecialization.EMOTIONAL: [
        "情感", "哀伤", "悲痛", "心理", "辅导", "情绪", "抑郁", "焦虑",
        "emotional", "grief", "bereavement", "counseling", "depression",
    ],
    ExpertSpecialization.FINANCIAL: [
        "财务", "遗产", "税务", "投资", "资产", "分配", "信托", "银行",
        "financial", "estate", "tax", "investment", "trust",
    ],
    ExpertSpecialization.CODE: [
        "代码", "编程", "函数", "bug", "调试", "python", "javascript",
        "code", "programming", "function", "debug",
    ],
    ExpertSpecialization.GENERAL: [
        "通用", "帮助", "导航", "faq", "general", "help", "guide",
    ],
}


# =====================================================================
# 数据类
# =====================================================================
@dataclass
class Expert:
    """单个专家。

    Attributes:
        name: 唯一名(如 "legal-qwen-7b")
        specialization: 专长领域
        capacity: 最大并发容量
        current_load: 当前负载(并发请求数)
        success_rate: 历史成功率(0-1,EMA)
        model_name: 对应的模型名(可选,与 LocalLLMClient 关联)
        total_requests: 累计请求数
        total_success: 累计成功数
    """

    name: str
    specialization: ExpertSpecialization = ExpertSpecialization.GENERAL
    capacity: int = 10
    current_load: int = 0
    success_rate: float = 1.0
    model_name: str = ""
    total_requests: int = 0
    total_success: int = 0

    @property
    def load_ratio(self) -> float:
        """负载水位(0-1)。"""
        if self.capacity <= 0:
            return 1.0
        return min(1.0, self.current_load / self.capacity)

    @property
    def is_at_capacity(self) -> bool:
        """是否已达容量上限。"""
        return self.current_load >= self.capacity

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["specialization"] = self.specialization.value
        d["load_ratio"] = self.load_ratio
        d["is_at_capacity"] = self.is_at_capacity
        return d


@dataclass
class MoEConfig:
    """MoE 路由器配置。

    Attributes:
        max_experts: 最大专家数(超过则拒绝注册)
        default_expert: 默认专家名(fallback 用)
        fallback_to_default: 无可用专家时是否回退到 default
        load_weight: 负载因素权重
        success_weight: 成功率权重
        match_weight: 领域匹配权重
        success_ema_alpha: success_rate EMA 平滑系数(0-1)
    """

    max_experts: int = 20
    default_expert: str = "general-default"
    fallback_to_default: bool = True
    load_weight: float = 1.0
    success_weight: float = 1.0
    match_weight: float = 2.0
    success_ema_alpha: float = 0.1


# =====================================================================
# MoERouter
# =====================================================================
class MoERouter:
    """Mixture-of-Experts 路由器。

    用法:
        router = MoERouter()
        router.register_expert(Expert(name="legal-1", specialization=ExpertSpecialization.LEGAL))
        expert = router.route("如何立遗嘱?", context={})
        router.update_load("legal-1", +1)
        # ... 调用 expert ...
        router.update_load("legal-1", -1)
        router.record_result("legal-1", success=True)
    """

    def __init__(self, config: MoEConfig | None = None) -> None:
        self.config = config or MoEConfig()
        self._lock = threading.RLock()
        self._experts: dict[str, Expert] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register_expert(self, expert: Expert) -> bool:
        """注册专家。重名覆盖。"""
        with self._lock:
            if (
                expert.name not in self._experts
                and len(self._experts) >= self.config.max_experts
            ):
                logger.warning(
                    "MoE expert register rejected: max_experts=%d reached",
                    self.config.max_experts,
                )
                return False
            self._experts[expert.name] = expert
            logger.info(
                "MoE expert registered: %s (%s)",
                expert.name, expert.specialization.value,
            )
            return True

    def unregister_expert(self, name: str) -> bool:
        with self._lock:
            if name in self._experts:
                del self._experts[name]
                return True
            return False

    def get_expert(self, name: str) -> Expert | None:
        with self._lock:
            return self._experts.get(name)

    def list_experts(self) -> list[Expert]:
        with self._lock:
            return list(self._experts.values())

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def route(self, query: str, context: dict[str, Any] | None = None) -> Expert:
        """选择最佳专家。

        策略:
            1. 计算每个专家的 specialization_match(基于 query 关键词)
            2. 跳过 is_at_capacity 的专家
            3. score = match_weight × match + load_weight × (1 - load_ratio) + success_weight × success_rate
            4. 选 score 最高的;若全部 at_capacity → fallback_to_default
        """
        context = context or {}
        # 1. 分类 query
        query_specialization = self._classify_query(query, context)

        with self._lock:
            candidates: list[tuple[float, Expert]] = []
            for expert in self._experts.values():
                # 容量保护
                if expert.is_at_capacity:
                    continue
                # 匹配度
                match = 1.0 if expert.specialization == query_specialization else 0.1
                # 加权
                score = (
                    self.config.match_weight * match
                    + self.config.load_weight * (1.0 - expert.load_ratio)
                    + self.config.success_weight * expert.success_rate
                )
                candidates.append((score, expert))

            if candidates:
                # 选最高分(tie-break: 较低 load)
                candidates.sort(key=lambda x: (-x[0], x[1].current_load))
                chosen = candidates[0][1]
                return chosen

            # 全部 at_capacity → fallback
            if self.config.fallback_to_default:
                default = self._experts.get(self.config.default_expert)
                if default is not None:
                    logger.warning(
                        "MoE all experts at capacity, fallback to default: %s",
                        self.config.default_expert,
                    )
                    return default
                # 还是没有 → 返回任意一个
                if self._experts:
                    any_expert = next(iter(self._experts.values()))
                    logger.warning(
                        "MoE no default expert, fallback to: %s", any_expert.name
                    )
                    return any_expert

            # 彻底没有专家 → 返回一个临时 default Expert(避免 None)
            logger.warning("MoE no experts registered, returning placeholder")
            return Expert(
                name=self.config.default_expert,
                specialization=ExpertSpecialization.GENERAL,
            )

    # ------------------------------------------------------------------
    # 负载更新
    # ------------------------------------------------------------------
    def update_load(self, expert_name: str, delta: int) -> bool:
        """增减专家负载(delta 可正可负)。

        Returns:
            True 更新成功 / False 专家不存在
        """
        with self._lock:
            expert = self._experts.get(expert_name)
            if expert is None:
                return False
            expert.current_load = max(0, expert.current_load + delta)
            return True

    # ------------------------------------------------------------------
    # 结果记录
    # ------------------------------------------------------------------
    def record_result(self, expert_name: str, success: bool) -> bool:
        """记录专家调用结果,更新 success_rate(EMA)。

        EMA: new_rate = (1 - α) × old_rate + α × (1 if success else 0)

        Returns:
            True 更新成功 / False 专家不存在
        """
        with self._lock:
            expert = self._experts.get(expert_name)
            if expert is None:
                return False
            alpha = self.config.success_ema_alpha
            observation = 1.0 if success else 0.0
            expert.success_rate = (1 - alpha) * expert.success_rate + alpha * observation
            expert.success_rate = max(0.0, min(1.0, expert.success_rate))
            expert.total_requests += 1
            if success:
                expert.total_success += 1
            return True

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def get_stats(self) -> dict[str, Any]:
        """返回所有专家的负载 / 容量 / 成功率统计。"""
        with self._lock:
            return {
                "total_experts": len(self._experts),
                "default_expert": self.config.default_expert,
                "experts": [e.to_dict() for e in self._experts.values()],
            }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _classify_query(
        self, query: str, context: dict[str, Any]
    ) -> ExpertSpecialization:
        """基于关键词匹配对 query 分类。

        Args:
            query: 用户查询文本
            context: 上下文(可显式指定 task_type,优先级最高)

        Returns:
            最佳匹配的 ExpertSpecialization(无匹配 → GENERAL)
        """
        # context 显式指定 → 直接用
        explicit = context.get("task_type") or context.get("specialization")
        if explicit:
            if isinstance(explicit, ExpertSpecialization):
                return explicit
            if isinstance(explicit, str):
                try:
                    return ExpertSpecialization(explicit)
                except ValueError:
                    pass

        # 关键词匹配(计票)
        query_lower = query.lower()
        scores: dict[ExpertSpecialization, int] = {}
        for spec, keywords in _SPECIALIZATION_KEYWORDS.items():
            count = sum(1 for kw in keywords if kw.lower() in query_lower)
            if count > 0:
                scores[spec] = count

        if not scores:
            return ExpertSpecialization.GENERAL
        return max(scores.items(), key=lambda x: x[1])[0]
