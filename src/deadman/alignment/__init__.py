"""P8.7 模型微调与私有化部署框架。

模块结构:
    - dpo_trainer.py       Direct Preference Optimization 训练器(mock)
    - sft_dataset.py       SFT 数据集构建(5 大领域 + PII 脱敏 + 多格式导出)
    - local_llm.py         本地 LLM 客户端(Qwen / DeepSeek / Llama / Ollama / vLLM)
    - moe_router.py        Mixture-of-Experts 路由器
    - continuous_learn.py  持续学习(用户反馈 + Reflexion 集成)
    - manager.py           顶层 AlignmentManager 单例编排

设计原则:
    - NO actual LLM training:仅模拟训练流程,产出 mock 指标
    - PII redaction:所有训练数据强制过 defense.pii_guard.PIIRedactor
    - 无外部依赖:不依赖 torch / transformers / trl
    - Feature flag:DEADMAN_ALIGNMENT_ENABLED=0 默认关闭(关闭时抛 AlignmentDisabledError)
    - 多租户:数据按 tenant_id 分目录
    - 线程安全 + 原子写

用法:
    from deadman.alignment import (
        DPOTrainer, SFTDataset, LocalLLMClient, MoERouter,
        ContinuousLearner, AlignmentDisabledError, get_alignment_manager,
    )

    manager = get_alignment_manager()  # 默认关闭 → 抛 AlignmentDisabledError
"""

from __future__ import annotations

from .continuous_learn import ContinuousLearner, FeedbackEvent, WeeklyReport
from .dpo_trainer import (
    DPOConfig,
    DPOTrainer,
    EvalReport,
    PreferenceExample,
    PreferenceSource,
    TrainingReport,
    TrustScoreTracker,
)
from .local_llm import LocalLLMClient, LocalLLMConfig, LocalLLMProvider
from .manager import (
    AlignmentDisabledError,
    AlignmentManager,
    PipelineReport,
    get_alignment_manager,
    reset_alignment_manager,
)
from .moe_router import Expert, ExpertSpecialization, MoEConfig, MoERouter
from .sft_dataset import (
    ExportFormat,
    SFTDataset,
    SFTExample,
    SFTSource,
    TaskType,
)

__all__ = [
    # dpo_trainer
    "DPOConfig",
    "DPOTrainer",
    "EvalReport",
    "PreferenceExample",
    "PreferenceSource",
    "TrainingReport",
    "TrustScoreTracker",
    # sft_dataset
    "SFTDataset",
    "SFTExample",
    "SFTSource",
    "TaskType",
    "ExportFormat",
    # local_llm
    "LocalLLMClient",
    "LocalLLMConfig",
    "LocalLLMProvider",
    # moe_router
    "MoERouter",
    "MoEConfig",
    "Expert",
    "ExpertSpecialization",
    # continuous_learn
    "ContinuousLearner",
    "FeedbackEvent",
    "WeeklyReport",
    # manager
    "AlignmentManager",
    "AlignmentDisabledError",
    "PipelineReport",
    "get_alignment_manager",
    "reset_alignment_manager",
]
