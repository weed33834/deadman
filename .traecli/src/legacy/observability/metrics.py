"""MetricsCollector - 多智能体平台指标采集与看板

参考 observability/Metrics.md 设计，覆盖 11 大类 50+ 指标：
质量 / 效率 / 知识库 / 安全 / 跨平台一致性 / 协作 / 记忆 / 互操作 /
对齐 / 韧性 / 幻觉检测。

存储模型：纯内存，按 (category, metric_name, tags) 聚合。
指标命名约定：`<category>.<metric_name>`，例如 `quality.rule_violation_rate`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


# === 11 大类指标分类 ===
# 键为分类前缀，值为 (中文名, 看板名, 该类典型指标列表)
METRIC_CATEGORIES: dict[str, dict[str, Any]] = {
    "quality": {
        "name_cn": "质量",
        "dashboard": "质量看板",
        "description": "智能体做得对不对",
        "metrics": [
            "rule_violation_rate",          # 规则违反率
            "integrity_violation_rate",     # 诚信违规率
            "compliance_violation_rate",    # 合规违规率
            "safety_violation_rate",        # 安全违规率
            "transparency_violation_rate",  # 透明度违规率
            "input_guardrails_bypass_rate", # 输入护栏绕过率
            "transfer_accuracy",            # 转介准确率
            "transfer_summary_completeness",# 转介摘要完整率
            "transfer_user_confirm_rate",   # 转介用户确认率
            "subagent_call_accuracy",       # 子智能体调用准确率
            "subagent_call_failure_rate",   # 子智能体调用失败率
            "subagent_schema_valid_rate",   # 子智能体 schema 合规率
            "tool_selection_accuracy",      # 工具选择准确率
            "tool_argument_accuracy",       # 参数填充准确率
            "tool_sequence_accuracy",       # 调用顺序准确率
            "confidence_labeling_rate",     # 置信度标注率
            "source_passthrough_rate",      # 来源透传率
            "ai_identity_disclosure_rate",  # AI 身份告知率
            "disclaimer_inclusion_rate",    # 免责声明包含率
        ],
    },
    "efficiency": {
        "name_cn": "效率",
        "dashboard": "效率看板",
        "description": "智能体做得快不快",
        "metrics": [
            "first_response_latency_p50",  # 首次响应延迟 P50
            "first_response_latency_p95",  # 首次响应延迟 P95
            "first_response_latency_p99",  # 首次响应延迟 P99
            "full_conversation_latency_p50",
            "full_conversation_latency_p95",
            "full_conversation_latency_p99",
            "subagent_latency_p50",        # 子智能体调用延迟 P50
            "subagent_latency_p95",
            "tool_latency_p50",            # 工具调用延迟 P50
            "tool_latency_p95",
            "transfer_decision_latency_p50",
            "transfer_decision_latency_p95",
            "avg_dialogue_turns",          # 平均对话轮数
            "avg_tool_calls_per_case",     # 平均工具调用次数
            "avg_subagent_calls_per_case", # 平均子智能体调用次数
            "avg_transfers_per_case",      # 平均转介次数
            "cost_per_dialogue_usd",       # 单次对话成本
            "cost_per_tool_call_usd",      # 单次工具调用成本
            "cost_per_subagent_call_usd",  # 单次子智能体调用成本
            "token_input_count",           # 输入 token
            "token_output_count",          # 输出 token
        ],
    },
    "knowledge": {
        "name_cn": "知识库",
        "dashboard": "知识库看板",
        "description": "RAGAS 式检索质量",
        "metrics": [
            "faithfulness",                # 输出忠于检索片段
            "answer_relevance",            # 输出回答了问题
            "context_precision",           # 检索片段精准
            "context_recall",              # 检索覆盖答案所需
            "stale_file_rate_6m",          # 超 6 个月未更新文件率
            "stale_file_rate_3m_policy",   # 超 3 个月未更新（政策类）
            "stale_file_rate_1y_law",      # 超 1 年未更新（法条类）
        ],
    },
    "safety": {
        "name_cn": "安全",
        "dashboard": "安全看板",
        "description": "注入/心理危机/事故",
        "metrics": [
            "injection_detection_rate",    # 注入识别率
            "jailbreak_block_rate",        # 越狱拦截率
            "pii_leak_rate",               # PII 泄露率
            "r3_detection_rate",           # R3 心理危机识别率
            "r3_response_latency_ms",      # R3 响应延迟
            "high_severity_incident_rate", # 高严重度事故率
            "medium_severity_incident_rate",
            "incident_repeat_rate",        # 事故重复率
        ],
    },
    "cross_platform": {
        "name_cn": "跨平台一致性",
        "dashboard": "跨平台一致性看板",
        "description": "13 平台同一 golden case 通过率差异",
        "metrics": [
            "golden_case_pass_rate",       # golden case 通过率
            "cross_platform_pass_rate_diff", # 跨平台通过率差异
            "span_type_consistency",       # span_type 一致性
            "trace_id_propagation_rate",   # trace_id 串联率
        ],
    },
    "collaboration": {
        "name_cn": "协作",
        "dashboard": "协作看板",
        "description": "辩论/投票（Debate-Voting.md）",
        "metrics": [
            "conflict_detection_accuracy", # 冲突检测准确率
            "debate_convergence_rate",     # 辩论收敛率
            "arbitration_accuracy",        # 仲裁准确率
            "debate_avg_rounds",           # 辩论平均轮次
            "debate_avg_latency_ms",       # 辩论平均延迟
            "debate_integrity_violation_rate", # 辩论中诚信违规率
        ],
    },
    "memory": {
        "name_cn": "记忆",
        "dashboard": "记忆看板",
        "description": "分层记忆（Memory-Store.md）",
        "metrics": [
            "cross_session_resume_rate",   # 跨会话续接成功率
            "repeat_question_rate",        # 重复询问率
            "context_recall_accuracy",     # 上下文召回准确率
            "contradiction_detection_rate",# 矛盾检测率
            "memory_query_latency_p95",    # 记忆查询延迟 P95
            "pii_redaction_rate",          # PII 脱敏率
        ],
    },
    "interop": {
        "name_cn": "互操作",
        "dashboard": "互操作看板",
        "description": "A2A 协议（A2A-Protocol.md）",
        "metrics": [
            "a2a_call_success_rate",       # A2A 调用成功率
            "a2a_avg_latency_ms",          # A2A 平均延迟
            "a2a_data_redaction_rate",     # 数据脱敏率
            "a2a_integrity_check_rate",    # 外部结果诚信校验率
            "agent_card_completeness",     # Agent Card 完整度
            "a2a_cross_validation_rate",   # 外部结果交叉验证率
        ],
    },
    "alignment": {
        "name_cn": "对齐",
        "dashboard": "对齐看板",
        "description": "DPO 模型对齐（DPO-Alignment.md）",
        "metrics": [
            "rule_compliance_rate_dpo",    # 规则遵守率（DPO 后）
            "rule_compliance_rate_lift",   # 规则遵守率提升
            "general_capability_degradation", # 通用能力退化
            "integrity_violation_rate_dpo",# 诚信违规率（DPO 后）
            "adversarial_defense_rate",    # 对抗防御率
            "preference_data_quality",     # 偏好数据质量
        ],
    },
    "resilience": {
        "name_cn": "韧性",
        "dashboard": "韧性看板",
        "description": "Reflexion 机制（Reflexion-Mechanism.md）",
        "metrics": [
            "reflexion_trigger_rate",      # Reflexion 触发率
            "reflexion_success_rate",      # Reflexion 成功率
            "fallback_rate",               # Fallback 率
            "fallback_rate_reduction",     # Fallback 率降低
            "avg_retry_count",             # 平均重试次数
            "predefined_strategy_hit_rate",# 预定义策略命中率
        ],
    },
    "hallucination": {
        "name_cn": "幻觉检测",
        "dashboard": "幻觉检测看板",
        "description": "SelfCheckGPT（SelfCheckGPT.md）",
        "metrics": [
            "numeric_claim_extraction_rate", # 数字类 claim 提取率
            "consistency_detection_accuracy",# 一致性检测准确率
            "low_consistency_capture_rate",  # 低一致性 claim 捕获率
            "selfcheck_f1",                  # SelfCheckGPT F1
            "ragas_complement_rate",         # 与 RAGAS faithfulness 互补率
        ],
    },
}


def _category_of(metric_name: str) -> str:
    """从指标名推断分类前缀。

    约定：`<category>.<metric_name>`，例如 `quality.rule_violation_rate`。
    未识别前缀归入 `uncategorized`。
    """
    if not metric_name or "." not in metric_name:
        return "uncategorized"
    prefix = metric_name.split(".", 1)[0]
    if prefix in METRIC_CATEGORIES:
        return prefix
    return "uncategorized"


def _tags_key(tags: Optional[dict[str, Any]]) -> str:
    """将 tags 序列化为稳定字符串键，用于聚合分组。"""
    if not tags:
        return ""
    items = sorted((str(k), str(v)) for k, v in tags.items())
    return "|".join(f"{k}={v}" for k, v in items)


class MetricsCollector:
    """指标采集器 - 内存聚合 + 看板聚合查询。

    存储结构：
        self._records[category][metric_name][tags_key] = [
            {"value": float, "tags": dict, "timestamp": str},
            ...
        ]

    聚合策略：按 (category, metric_name, tags) 分组保留全部原始记录，
    get_dashboard 时计算 count/sum/avg/last/min/max。
    """

    def __init__(self) -> None:
        # category -> metric_name -> tags_key -> [record, ...]
        self._records: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
        for cat in METRIC_CATEGORIES:
            self._records[cat] = {}
        self._records["uncategorized"] = {}

    # === 核心 API ===

    def record_metric(
        self,
        name: str,
        value: float | int | bool,
        tags: Optional[dict[str, Any]] = None,
    ) -> None:
        """记录一个指标值。

        参数：
            name: 指标名，约定 `<category>.<metric>`，如 `quality.rule_violation_rate`
            value: 指标值（数值或布尔，布尔会转为 0/1）
            tags: 可选标签字典，用于细分维度（如 platform/agent_name/risk_tier）
        """
        category = _category_of(name)
        normalized_value: float
        if isinstance(value, bool):
            normalized_value = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            normalized_value = float(value)
        else:
            # 非数值类型转为字符串后跳过数值聚合（保留在记录里便于追溯）
            normalized_value = 0.0

        record = {
            "value": normalized_value,
            "raw_value": value,
            "tags": dict(tags) if tags else {},
            "timestamp": datetime.now().isoformat(),
        }

        cat_bucket = self._records.setdefault(category, {})
        name_bucket = cat_bucket.setdefault(name, {})
        tkey = _tags_key(tags)
        name_bucket.setdefault(tkey, []).append(record)

    def get_metric(
        self,
        name: str,
        tags: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """查询单个指标的聚合统计。

        返回：{count, sum, avg, min, max, last, last_timestamp}
        若指定 tags，仅返回该 tags 维度的统计；否则返回该指标全部记录的合并统计。
        """
        category = _category_of(name)
        cat_bucket = self._records.get(category, {})
        name_bucket = cat_bucket.get(name, {})

        if tags is not None:
            records = name_bucket.get(_tags_key(tags), [])
        else:
            records = []
            for rec_list in name_bucket.values():
                records.extend(rec_list)

        return self._aggregate(records)

    def get_dashboard(self) -> dict[str, dict[str, Any]]:
        """返回各看板的当前值。

        结构：{category: {name_cn, dashboard, description, metrics: {metric_name: stats}}}
        每个 stats 为 {count, sum, avg, min, max, last, last_timestamp}。
        """
        dashboard: dict[str, dict[str, Any]] = {}
        for category, meta in METRIC_CATEGORIES.items():
            cat_bucket = self._records.get(category, {})
            metrics_view: dict[str, Any] = {}
            for metric_name, tag_map in cat_bucket.items():
                all_records: list[dict[str, Any]] = []
                for rec_list in tag_map.values():
                    all_records.extend(rec_list)
                metrics_view[metric_name] = self._aggregate(all_records)
            dashboard[category] = {
                "name_cn": meta["name_cn"],
                "dashboard": meta["dashboard"],
                "description": meta["description"],
                "metrics": metrics_view,
            }
        # uncategorized 也附上（便于发现命名不规范的指标）
        uncategorized = self._records.get("uncategorized", {})
        if uncategorized:
            uc_view: dict[str, Any] = {}
            for metric_name, tag_map in uncategorized.items():
                all_records: list[dict[str, Any]] = []
                for rec_list in tag_map.values():
                    all_records.extend(rec_list)
                uc_view[metric_name] = self._aggregate(all_records)
            dashboard["uncategorized"] = {
                "name_cn": "未分类",
                "dashboard": "未分类看板",
                "description": "命名不符合 <category>.<metric> 约定的指标",
                "metrics": uc_view,
            }
        return dashboard

    def get_category(self, category: str) -> dict[str, Any]:
        """获取单个分类的看板视图。"""
        dashboard = self.get_dashboard()
        return dashboard.get(
            category,
            {
                "name_cn": "未知分类",
                "dashboard": "未知",
                "description": "",
                "metrics": {},
            },
        )

    def list_metrics(self, category: Optional[str] = None) -> list[str]:
        """列出已记录的指标名（可选按分类过滤）。"""
        if category:
            cat_bucket = self._records.get(category, {})
            return list(cat_bucket.keys())
        result: list[str] = []
        for cat_bucket in self._records.values():
            result.extend(cat_bucket.keys())
        return result

    def clear(self) -> None:
        """清空所有指标记录。"""
        for cat in list(self._records.keys()):
            self._records[cat] = {}

    # === 内部工具 ===

    @staticmethod
    def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
        """对一组记录计算聚合统计。"""
        if not records:
            return {
                "count": 0,
                "sum": 0.0,
                "avg": 0.0,
                "min": 0.0,
                "max": 0.0,
                "last": 0.0,
                "last_timestamp": None,
            }
        values = [r["value"] for r in records]
        last_record = records[-1]
        return {
            "count": len(values),
            "sum": sum(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "last": last_record["value"],
            "last_timestamp": last_record["timestamp"],
        }


# === 全局单例 ===
metrics_collector = MetricsCollector()
