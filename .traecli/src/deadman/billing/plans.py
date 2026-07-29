"""P8.1.1 Plan 定义 - 三档套餐(free / pro / enterprise)+ 自定义 plan。

设计借鉴:
    - OpenAI ChatGPT Plus / Team / Enterprise
    - Anthropic Claude Pro / Team / Enterprise
    - 国内 SaaS 标准(free / pro / enterprise / custom)

每档 plan 包含:
    - 价格(月付 / 年付,年付 8 折)
    - 4 维度配额:llm_tokens / tool_calls / storage_mb / multimodal_calls
    - 特性列表(enabled features,如 debate / handoff / a2a_v12 / multimodal)
    - SLA 等级(无 SLA / 99% / 99.9%)
    - 支持人数(0 / 邮件 / 7x24)
    - 数据保留期(30 天 / 1 年 / 7 年)

向后兼容:
    - free plan 的限制 = P7.7 quota 默认值,关闭 billing 时行为不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PlanName(str, Enum):
    """套餐名(用于序列化与跨模块引用)。"""

    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"
    CUSTOM = "custom"  # 企业自定义


@dataclass(frozen=True)
class PlanLimits:
    """套餐限制(4 维度配额)。

    每维度限制按"每天 / 每月"两窗口:
        - daily_limit: 单日上限(防突发)
        - monthly_limit: 单月上限(成本控制)

    -1 表示无限制(enterprise 默认)。
    """

    llm_tokens_daily: int = 100_000
    llm_tokens_monthly: int = 1_000_000
    tool_calls_daily: int = 1_000
    tool_calls_monthly: int = 10_000
    storage_mb: int = 100  # 存储是累计,不分日 / 月
    multimodal_calls_daily: int = 10  # OCR / ASR / TTS / Vision / ImageGen 合计
    multimodal_calls_monthly: int = 100


@dataclass(frozen=True)
class Plan:
    """套餐定义(不可变,避免运行时被改)。"""

    name: PlanName
    display_name: str  # 用户可见名
    price_monthly: float  # 月付价格(CNY)
    price_yearly: float  # 年付价格(已折扣)
    limits: PlanLimits
    features: tuple[str, ...]  # 启用的 feature flag 名(小写下划线)
    sla_level: str  # none / 99 / 99.9 / 99.99
    support_level: str  # none / email / priority / dedicated
    data_retention_days: int  # 数据保留期
    description: str = ""

    def has_feature(self, feature_name: str) -> bool:
        """判断该 plan 是否启用指定 feature。"""
        return feature_name in self.features


# =====================================================================
# 内置三档套餐(向后兼容 P7.7 quota 默认值)
# =====================================================================

FREE_PLAN = Plan(
    name=PlanName.FREE,
    display_name="免费版",
    price_monthly=0.0,
    price_yearly=0.0,
    limits=PlanLimits(
        llm_tokens_daily=100_000,  # 与 P7.7 quota 默认值一致
        llm_tokens_monthly=1_000_000,
        tool_calls_daily=1_000,
        tool_calls_monthly=10_000,
        storage_mb=100,
        multimodal_calls_daily=10,
        multimodal_calls_monthly=100,
    ),
    features=(
        "debate",
        "ragas_eval",
        "reflexion_persist",
        "react_loop",
        "memory_compress",
        "cot_templates",
    ),
    sla_level="none",
    support_level="none",
    data_retention_days=30,
    description="基础身后事辅助,适合个人用户体验",
)

PRO_PLAN = Plan(
    name=PlanName.PRO,
    display_name="专业版",
    price_monthly=99.0,
    price_yearly=950.0,  # 年付约 8 折
    limits=PlanLimits(
        llm_tokens_daily=1_000_000,  # 10x free
        llm_tokens_monthly=10_000_000,
        tool_calls_daily=10_000,
        tool_calls_monthly=100_000,
        storage_mb=1_000,  # 1GB
        multimodal_calls_daily=100,
        multimodal_calls_monthly=1_000,
    ),
    features=(
        "debate",
        "ragas_eval",
        "reflexion_persist",
        "react_loop",
        "memory_compress",
        "cot_templates",
        "plan_execute",
        "tot",
        "evaluator_optimizer",
        "self_consistency",
        "vector_store",
        "episodic_ttl",
        "memory_snapshot",
        "mcp_gateway",
        "dry_run",
        "tool_permissions",
        "tool_cache",
        "handoff",
        "scratchpad",
        "agent_registry",
        "handoff_audit",
        "audit_chain",
        "guid_sandbox",
        "content_sandbox",
        "root_cause",
        "slo_dashboard",
        "trace_to_eval",
        "drift_detection",
        "circuit_breaker",
        "prompt_versioning",
        "durable_execution",
        "quota",
    ),
    sla_level="99",
    support_level="email",
    data_retention_days=365,
    description="完整智能体能力,适合专业用户和小型律所",
)

ENTERPRISE_PLAN = Plan(
    name=PlanName.ENTERPRISE,
    display_name="企业版",
    price_monthly=999.0,
    price_yearly=9_990.0,
    limits=PlanLimits(
        llm_tokens_daily=-1,  # 无限制
        llm_tokens_monthly=-1,
        tool_calls_daily=-1,
        tool_calls_monthly=-1,
        storage_mb=10_000,  # 10GB
        multimodal_calls_daily=-1,
        multimodal_calls_monthly=-1,
    ),
    features=(
        # 全部 feature 启用
        "debate",
        "ragas_eval",
        "reflexion_persist",
        "react_loop",
        "memory_compress",
        "cot_templates",
        "plan_execute",
        "tot",
        "evaluator_optimizer",
        "self_consistency",
        "react_reflexion",
        "vector_store",
        "episodic_ttl",
        "graphiti_deep",
        "shared_knowledge",
        "memory_snapshot",
        "forgetting_curve",
        "mcp_gateway",
        "dry_run",
        "tool_permissions",
        "tool_cache",
        "dynamic_tool_registration",
        "tool_signing",
        "handoff",
        "scratchpad",
        "agent_registry",
        "a2a_v12",
        "handoff_audit",
        "audit_chain",
        "jit_permission",
        "guid_sandbox",
        "content_sandbox",
        "redteam",
        "honeypot",
        "root_cause",
        "slo_dashboard",
        "trace_to_eval",
        "drift_detection",
        "replay",
        "web_middleware",
        "circuit_breaker",
        "multi_tenant",
        "prompt_versioning",
        "durable_execution",
        "quota",
        "credential_vault",
    ),
    sla_level="99.9",
    support_level="priority",
    data_retention_days=2555,  # 7 年(法规要求)
    description="全功能 + 多租户 + SLA 保障,适合律所 / 殡葬机构 / 政务合作",
)

PLANS: dict[str, Plan] = {
    PlanName.FREE.value: FREE_PLAN,
    PlanName.PRO.value: PRO_PLAN,
    PlanName.ENTERPRISE.value: ENTERPRISE_PLAN,
}


def get_plan(name: str) -> Optional[Plan]:
    """按名查 plan,未知返回 None。"""
    return PLANS.get(name)


def list_plans() -> list[Plan]:
    """列出所有内置 plan(看板用)。"""
    return list(PLANS.values())
