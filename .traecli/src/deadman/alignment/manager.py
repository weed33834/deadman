"""P8.7 AlignmentManager - 对齐框架顶层编排单例。

编排 5 个子组件:
    - DPOTrainer        偏好优化
    - SFTDataset        监督微调数据集
    - LocalLLMClient    本地推理
    - MoERouter         专家路由
    - ContinuousLearner 持续学习

主要入口:
    - submit_feedback(event)      端到端反馈处理
    - run_training_pipeline()     SFT → DPO 训练流水线
    - route_query(query)          返回 (model_name, expert)

Feature flag:
    DEADMAN_ALIGNMENT_ENABLED=0 默认关闭。
    关闭时 get_alignment_manager() / AlignmentManager.__init__ 抛 AlignmentDisabledError。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled
from .continuous_learn import ContinuousLearner, FeedbackEvent
from .dpo_trainer import DPOConfig, DPOTrainer, TrainingReport
from .local_llm import LocalLLMClient, LocalLLMConfig
from .moe_router import Expert, MoEConfig, MoERouter
from .sft_dataset import SFTDataset

logger = logging.getLogger(__name__)


# =====================================================================
# 异常
# =====================================================================
class AlignmentDisabledError(RuntimeError):
    """Alignment feature flag 未启用时抛出。

    Feature flag:DEADMAN_ALIGNMENT_ENABLED=0(默认关闭)
    """

    def __init__(self, msg: Optional[str] = None) -> None:
        super().__init__(
            msg
            or "Alignment module disabled. Set DEADMAN_ALIGNMENT_ENABLED=1 to enable."
        )


# =====================================================================
# Pipeline Report
# =====================================================================
@dataclass
class PipelineReport:
    """训练流水线报告(SFT → DPO)。"""

    sft_samples: int = 0
    dpo_samples: int = 0
    sft_skipped: bool = False
    dpo_skipped: bool = False
    dpo_report: Optional[TrainingReport] = None
    sft_validation: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    completed: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.dpo_report is not None:
            d["dpo_report"] = self.dpo_report.to_dict()
        return d


# =====================================================================
# AlignmentManager
# =====================================================================
class AlignmentManager:
    """对齐框架顶层管理器(单例,通过 get_alignment_manager 获取)。

    用法:
        manager = get_alignment_manager()  # 若 flag 关闭 → 抛 AlignmentDisabledError
        manager.submit_feedback(feedback_event)
        report = manager.run_training_pipeline()
        model_name, expert = manager.route_query("如何立遗嘱?")
    """

    def __init__(
        self,
        dpo_trainer: Optional[DPOTrainer] = None,
        sft_dataset: Optional[SFTDataset] = None,
        local_llm: Optional[LocalLLMClient] = None,
        moe_router: Optional[MoERouter] = None,
        continuous_learner: Optional[ContinuousLearner] = None,
        dpo_config: Optional[DPOConfig] = None,
        moe_config: Optional[MoEConfig] = None,
    ) -> None:
        # 1. feature flag 检查(默认关闭)
        if not is_enabled("alignment"):
            raise AlignmentDisabledError()

        self._lock = threading.RLock()
        # 子组件(惰性创建,允许注入便于测试)
        self.dpo_trainer = dpo_trainer or DPOTrainer()
        self.sft_dataset = sft_dataset or SFTDataset()
        self.local_llm = local_llm  # 默认 None,按需创建
        self.moe_router = moe_router or MoERouter(moe_config or MoEConfig())
        self.continuous_learner = continuous_learner or ContinuousLearner()
        self.dpo_config = dpo_config or DPOConfig()

        # 注册默认 expert(MoE)
        self._register_default_experts()

        logger.info("AlignmentManager initialized (alignment feature flag enabled)")

    # ------------------------------------------------------------------
    # 默认 experts
    # ------------------------------------------------------------------
    def _register_default_experts(self) -> None:
        """注册 deadman 5 大领域默认 experts。"""
        from .moe_router import Expert, ExpertSpecialization

        defaults = [
            (self.moe_router.config.default_expert, ExpertSpecialization.GENERAL),
            ("legal-expert", ExpertSpecialization.LEGAL),
            ("medical-expert", ExpertSpecialization.MEDICAL),
            ("emotional-expert", ExpertSpecialization.EMOTIONAL),
            ("financial-expert", ExpertSpecialization.FINANCIAL),
        ]
        for name, spec in defaults:
            if self.moe_router.get_expert(name) is None:
                self.moe_router.register_expert(
                    Expert(name=name, specialization=spec, capacity=20)
                )

    # ------------------------------------------------------------------
    # 端到端反馈处理
    # ------------------------------------------------------------------
    def submit_feedback(self, event: FeedbackEvent) -> dict[str, Any]:
        """端到端处理一条反馈:

            1. ContinuousLearner.record_feedback
            2. ContinuousLearner.extract_preference_pair
            3. DPOTrainer.add_preference(若成功提取)
            4. (可选)auto_promote_to_sft

        Returns:
            {"recorded": bool, "preference_added": bool, "preference_rejected": bool}
        """
        # 防御:flag 关闭
        if not is_enabled("alignment"):
            raise AlignmentDisabledError()

        result = {
            "recorded": False,
            "preference_added": False,
            "preference_rejected": False,
        }

        # 1. 记录
        self.continuous_learner.record_feedback(event)
        result["recorded"] = True

        # 2. 提取偏好对
        pref = self.continuous_learner.extract_preference_pair(event)
        if pref is not None:
            added = self.dpo_trainer.add_preference(pref)
            if added:
                result["preference_added"] = True
            else:
                result["preference_rejected"] = True

        return result

    # ------------------------------------------------------------------
    # 训练流水线
    # ------------------------------------------------------------------
    def run_training_pipeline(self) -> PipelineReport:
        """运行 SFT → DPO 训练流水线(mock)。

        流程:
            1. ContinuousLearner.auto_promote_to_sft(高质量反馈 → SFT 数据集)
            2. SFT 数据集校验(validate)
            3. ContinuousLearner.extract_all_preference_pairs → DPOTrainer.add_preference
            4. DPOTrainer.train(config)

        Returns:
            PipelineReport
        """
        if not is_enabled("alignment"):
            raise AlignmentDisabledError()

        import time as _time
        start = _time.time()
        report = PipelineReport()

        try:
            # 1. SFT: 自动晋升高质量反馈
            promoted = self.continuous_learner.auto_promote_to_sft(
                self.sft_dataset,
                min_quality_score=0.8,
                min_rating=4,
            )
            report.sft_samples = self.sft_dataset.count()

            # 2. SFT 校验
            validation = self.sft_dataset.validate()
            report.sft_validation = validation
            if not validation["valid"]:
                logger.warning(
                    "SFT validation failed: %s", validation["errors"][:3]
                )

            # 3. DPO: 提取所有偏好对
            pairs = self.continuous_learner.extract_all_preference_pairs()
            for pair in pairs:
                self.dpo_trainer.add_preference(pair)
            report.dpo_samples = self.dpo_trainer.preference_count()

            # 4. DPO 训练
            if self.dpo_trainer.preference_count() > 0:
                dpo_report = self.dpo_trainer.train(self.dpo_config)
                report.dpo_report = dpo_report
            else:
                report.dpo_skipped = True

            report.completed = True
        except Exception as e:
            report.error = str(e)
            logger.exception("Training pipeline failed: %s", e)

        report.duration_seconds = _time.time() - start
        return report

    # ------------------------------------------------------------------
    # 查询路由
    # ------------------------------------------------------------------
    def route_query(
        self, query: str, context: Optional[dict[str, Any]] = None
    ) -> tuple[str, Expert]:
        """路由查询到最佳 expert + 对应模型名。

        Returns:
            (model_name, expert)
        """
        if not is_enabled("alignment"):
            raise AlignmentDisabledError()

        expert = self.moe_router.route(query, context or {})
        # 模型名:优先 expert.model_name,否则用 default
        model_name = expert.model_name or f"deadman-{expert.specialization.value}"
        return model_name, expert

    # ------------------------------------------------------------------
    # 本地 LLM 接入(惰性)
    # ------------------------------------------------------------------
    def attach_local_llm(self, config: LocalLLMConfig) -> LocalLLMClient:
        """挂载本地 LLM 客户端。"""
        if not is_enabled("alignment"):
            raise AlignmentDisabledError()
        with self._lock:
            self.local_llm = LocalLLMClient(config)
            return self.local_llm

    def chat(self, query: str, **kwargs: Any) -> str:
        """便捷方法:route + local_llm.chat。

        若未挂载 local_llm → 返回 mock 响应。
        """
        if not is_enabled("alignment"):
            raise AlignmentDisabledError()

        model_name, expert = self.route_query(query)
        if self.local_llm is None:
            return f"[no-llm-attached] routed to {expert.name} ({model_name})"

        messages = kwargs.pop(
            "messages",
            [{"role": "user", "content": query}],
        )
        return self.local_llm.chat(messages, model=model_name, **kwargs)

    # ------------------------------------------------------------------
    # GDPR 被遗忘权
    # ------------------------------------------------------------------
    def forget_user(self, user_id: str) -> dict[str, int]:
        """跨组件删除某用户数据。

        Returns:
            {"feedback_removed": int, "preferences_remaining": int}
        """
        if not is_enabled("alignment"):
            raise AlignmentDisabledError()

        removed = self.continuous_learner.forget_user(user_id)
        # DPO 偏好样本按 user_id 过滤
        with self.dpo_trainer._lock:  # 直接访问内部,简化
            before = len(self.dpo_trainer._preferences)
            self.dpo_trainer._preferences = [
                p for p in self.dpo_trainer._preferences if p.user_id != user_id
            ]
        return {
            "feedback_removed": removed,
            "preferences_remaining": self.dpo_trainer.preference_count(),
        }

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """聚合所有子组件的统计。"""
        if not is_enabled("alignment"):
            raise AlignmentDisabledError()
        return {
            "dpo": {
                "preferences": self.dpo_trainer.preference_count(),
                "trust_snapshot": self.dpo_trainer.trust_tracker.snapshot(),
            },
            "sft": self.sft_dataset.stats(),
            "moe": self.moe_router.get_stats(),
            "continuous_learning": {
                "events": self.continuous_learner.event_count(),
                "has_reflexion": self.continuous_learner.has_reflexion,
            },
            "local_llm": self.local_llm.get_stats() if self.local_llm else None,
        }


# =====================================================================
# 单例
# =====================================================================
_alignment_manager_instance: Optional[AlignmentManager] = None
_alignment_manager_lock = threading.Lock()


def get_alignment_manager() -> AlignmentManager:
    """获取全局 AlignmentManager 单例。

    若 DEADMAN_ALIGNMENT_ENABLED=0 → 抛 AlignmentDisabledError。
    """
    global _alignment_manager_instance
    if not is_enabled("alignment"):
        raise AlignmentDisabledError()
    if _alignment_manager_instance is None:
        with _alignment_manager_lock:
            if _alignment_manager_instance is None:
                _alignment_manager_instance = AlignmentManager()
    return _alignment_manager_instance


def reset_alignment_manager() -> None:
    """重置单例(测试用)。"""
    global _alignment_manager_instance
    with _alignment_manager_lock:
        _alignment_manager_instance = None
