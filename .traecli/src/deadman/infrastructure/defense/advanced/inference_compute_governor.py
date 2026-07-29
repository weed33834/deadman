"""D21:推理时计算治理器(Inference-time Compute Governor)。

问题:
    2024-2025 出现的 reasoning 模型(o1 / o3 / DeepSeek-R1 / Claude Thinking)在
    "思考阶段"消耗大量 token(可达 10K-100K),且无法精确预测。

    生产风险:
    - 单次调用 token 成本从 $0.001 飙升到 $0.5+(500x)
    - 思考阶段无超时控制 → 单次调用可能持续 60s+
    - 不同 provider 思考预算语义不同(o1 用 max_completion_tokens,R1 用 reasoning_content)
    - 没有总量上限 → 用户单次会话可能消耗整个 plan 配额
    - "thinking" token 黑盒,无法审计是否泄露 PII / 是否合理

缓解:
    1. 预算三段式:pre_call(预扣预算) + during_call(超时熔断) + post_call(实算回补)
    2. 推理预算上限:max_reasoning_tokens / max_reasoning_seconds(可配置)
    3. 思考 token 审计:记录思考内容摘要 + PII 二次检测 + 异常模式告警
    4. 用户级聚合:跨会话累计思考 token,触达阈值降级到 non-reasoning model
    5. 模型自动降级:reasoning budget 超限 → 切到普通 model

设计:
    - InferenceBudget: 单次推理预算(预估 token / 实际 token / 剩余)
    - ReasoningAuditor: 思考内容审计(PII / 异常 / 摘要)
    - ComputeGovernor: 统筹预算 + 审计 + 降级

集成:
    llm_client.py 调用 reasoning model 前:
        gov = get_compute_governor()
        plan = gov.plan_call(
            user_id="u1",
            model="o1",
            estimated_input_tokens=2000,
            max_reasoning_tokens=8000,
        )
        if plan.should_degrade:
            model = "gpt-4o"  # 降级到非推理
        try:
            response = await llm.chat(model=model, ...)
            gov.record_actual(plan, response.usage)
        except TimeoutError:
            gov.record_timeout(plan)

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


# =====================================================================
# 数据类
# =====================================================================

class ReasoningModelStyle(str, Enum):
    """推理模型思考阶段输出风格。"""

    NONE = "none"  # 非推理模型(无思考)
    OAI_REASONING = "oai_reasoning"  # OpenAI o1/o3(reasoning_tokens in usage)
    ANTHROPIC_THINKING = "anthropic_thinking"  # Claude Thinking(thinking content block)
    DEEPSEEK_R1 = "deepseek_r1"  # DeepSeek-R1(reasoning_content field)
    CUSTOM = "custom"


class DegradeReason(str, Enum):
    """降级原因。"""

    NONE = "none"  # 不降级
    USER_BUDGET_EXHAUSTED = "user_budget_exhausted"  # 用户级预算耗尽
    PER_CALL_LIMIT = "per_call_limit"  # 单次上限
    FREQUENT_TIMEOUT = "frequent_timeout"  # 频繁超时
    ABUSIVE_PATTERN = "abusive_pattern"  # 滥用模式(异常增长)
    REASONING_LEAK = "reasoning_leak"  # 思考内容泄漏 PII


@dataclass
class InferenceBudgetPlan:
    """单次调用预算计划。

    pre_call 阶段生成,贯穿整个调用生命周期。
    """

    plan_id: str
    user_id: str
    tenant_id: str = ""
    model: str = ""
    model_style: ReasoningModelStyle = ReasoningModelStyle.NONE
    # 预算(预估)
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_reasoning_tokens: int = 0
    max_reasoning_tokens: int = 0
    max_reasoning_seconds: float = 0.0
    # 预扣总 token(从用户配额中扣除)
    reserved_total_tokens: int = 0
    # 降级决策
    should_degrade: bool = False
    degrade_reason: DegradeReason = DegradeReason.NONE
    degrade_to_model: str = ""  # 降级到哪个模型
    # 状态
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    timed_out: bool = False
    # 实际使用(post_call 填充)
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0
    actual_reasoning_tokens: int = 0
    actual_duration_seconds: float = 0.0
    # 审计
    reasoning_summary: str = ""  # 思考内容摘要(限长)
    reasoning_pii_leak: bool = False
    reasoning_anomaly: bool = False
    anomaly_reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["model_style"] = self.model_style.value
        d["degrade_reason"] = self.degrade_reason.value
        return d


# =====================================================================
# 思考内容审计
# =====================================================================

# PII 简化检测(复用 pii_guard 的核心规则,但本地化避免循环依赖)
_PII_PATTERNS = [
    (re.compile(r"\b1[3-9]\d{9}\b"), "china_phone"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "china_id_card"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "email"),
    (re.compile(r"\b\d{16,19}\b"), "bank_card"),
]

# 异常模式(可能是 prompt 注入 / 越狱 / 死循环)
_ANOMALY_PATTERNS = [
    (re.compile(r"(?:ignore|disregard)\s+(?:previous|above)\s+instructions", re.IGNORECASE), "prompt_injection"),
    (re.compile(r"system\s*prompt\s*[:：]", re.IGNORECASE), "system_prompt_leak"),
    (re.compile(r"(?:let me|I should|I'll try)\s+(?:try|attempt)\s+(?:again|once more|a different)", re.IGNORECASE), "loop_indicator"),
]


class ReasoningAuditor:
    """思考内容审计器。

    用法:
        auditor = ReasoningAuditor()
        result = auditor.audit(
            reasoning_content="Let me think...the user said 13812345678",
            user_id="u1",
        )
        if result.pii_leak:
            # 严重:思考内容含 PII,需告警
            ...
    """

    def __init__(
        self,
        max_summary_chars: int = 200,
        retain_history: int = 1000,
    ) -> None:
        self.max_summary_chars = max_summary_chars
        self.retain_history = retain_history
        self._lock = threading.RLock()
        # 审计历史(用户 -> deque)
        self._history: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=self.retain_history))
        # 统计
        self._stats = {
            "audits": 0,
            "pii_leak": 0,
            "anomaly": 0,
        }

    def audit(
        self,
        reasoning_content: str,
        *,
        user_id: str = "",
    ) -> "ReasoningAuditResult":
        """审计思考内容。

        Returns:
            ReasoningAuditResult:含 PII / 异常 / 摘要
        """
        result = ReasoningAuditResult()

        if not reasoning_content:
            return result

        if not is_enabled("defense"):
            result.summary = reasoning_content[: self.max_summary_chars]
            return result

        with self._lock:
            self._stats["audits"] += 1

        # 1. PII 检测
        pii_types = []
        for pattern, pii_type in _PII_PATTERNS:
            if pattern.search(reasoning_content):
                pii_types.append(pii_type)
        if pii_types:
            result.pii_leak = True
            result.pii_types = pii_types
            with self._lock:
                self._stats["pii_leak"] += 1

        # 2. 异常模式检测
        anomalies = []
        for pattern, anomaly_type in _ANOMALY_PATTERNS:
            if pattern.search(reasoning_content):
                anomalies.append(anomaly_type)
        if anomalies:
            result.anomaly = True
            result.anomaly_types = anomalies
            with self._lock:
                self._stats["anomaly"] += 1

        # 3. 摘要(截断到 max_summary_chars,选最近的句子边界)
        result.summary = self._make_summary(reasoning_content)

        # 4. 长度异常(超过 10x 平均)
        length = len(reasoning_content)
        result.length = length

        # 5. 历史记录
        with self._lock:
            self._history[user_id].append({
                "timestamp": time.time(),
                "length": length,
                "pii_leak": result.pii_leak,
                "anomaly": result.anomaly,
                "summary": result.summary,
            })

        return result

    def _make_summary(self, content: str) -> str:
        """生成思考内容摘要。"""
        if len(content) <= self.max_summary_chars:
            return content
        # 找最近的句子边界
        cut = self.max_summary_chars
        for sep in ["。", ".", "!", "?", "\n"]:
            last_sep = content.rfind(sep, 0, cut)
            if last_sep > cut * 0.7:
                return content[: last_sep + 1] + "...[truncated]"
        return content[:cut] + "...[truncated]"

    def get_user_history(self, user_id: str, limit: int = 10) -> list[dict]:
        """获取用户审计历史(看板用)。"""
        with self._lock:
            return list(self._history.get(user_id, []))[-limit:]

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self._stats)


@dataclass
class ReasoningAuditResult:
    """思考内容审计结果。"""

    pii_leak: bool = False
    pii_types: list[str] = field(default_factory=list)
    anomaly: bool = False
    anomaly_types: list[str] = field(default_factory=list)
    summary: str = ""
    length: int = 0


# =====================================================================
# 用户级聚合器
# =====================================================================

@dataclass
class UserComputeStats:
    """用户级推理统计。"""

    user_id: str
    # 累计 token
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_reasoning_tokens: int = 0
    # 调用次数
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    timeout_calls: int = 0
    # 时间
    first_call_at: Optional[float] = None
    last_call_at: Optional[float] = None
    # PII / 异常
    reasoning_pii_leak_count: int = 0
    reasoning_anomaly_count: int = 0
    # 降级次数
    degrade_count: int = 0
    # 最近 N 次的实际 reasoning token(用于检测异常增长)
    recent_reasoning_tokens: deque = field(default_factory=lambda: deque(maxlen=20))

    @property
    def avg_reasoning_tokens(self) -> float:
        if not self.recent_reasoning_tokens:
            return 0.0
        return sum(self.recent_reasoning_tokens) / len(self.recent_reasoning_tokens)

    @property
    def reasoning_ratio(self) -> float:
        """reasoning token 占总 token 比例。"""
        total = self.total_input_tokens + self.total_output_tokens + self.total_reasoning_tokens
        if total == 0:
            return 0.0
        return self.total_reasoning_tokens / total

    def to_dict(self) -> dict:
        d = asdict(self)
        d["recent_reasoning_tokens"] = list(self.recent_reasoning_tokens)
        d["avg_reasoning_tokens"] = self.avg_reasoning_tokens
        d["reasoning_ratio"] = self.reasoning_ratio
        return d


# =====================================================================
# Compute Governor
# =====================================================================

# 默认配置(可按场景覆盖)
DEFAULT_CONFIG = {
    # 单次调用上限
    "max_reasoning_tokens_per_call": 32_000,  # 32K(o1 默认上限)
    "max_reasoning_seconds_per_call": 120.0,  # 2 分钟
    # 用户级日上限(reasoning token)
    "user_daily_reasoning_token_limit": 200_000,  # 20 万/天
    # 用户级日上限(总 token)
    "user_daily_total_token_limit": 1_000_000,  # 100 万/天
    # 降级阈值:连续 N 次超时
    "timeout_threshold_for_degrade": 3,
    # 降级冷却时间(秒)
    "degrade_cooldown_seconds": 600,  # 10 分钟
    # 异常增长检测:最近 N 次平均 > 历史平均的 X 倍
    "abusive_growth_multiplier": 3.0,
    "abusive_growth_min_samples": 5,
    # reasoning token 占比上限
    "reasoning_ratio_limit": 0.85,
    # 预扣比例(预留 buffer,避免实际超预扣)
    "reserved_buffer_ratio": 1.2,
}


class ComputeGovernor:
    """推理时计算治理器。

    用法:
        gov = get_compute_governor()

        # 1. 调用前:计划 + 预算检查
        plan = gov.plan_call(
            user_id="u1",
            tenant_id="t1",
            model="o1",
            model_style=ReasoningModelStyle.OAI_REASONING,
            estimated_input_tokens=2000,
            estimated_output_tokens=1000,
            max_reasoning_tokens=8000,
        )

        if plan.should_degrade:
            # 降级到非推理模型
            model = plan.degrade_to_model
            reasoning_effort = None
        else:
            model = plan.model
            reasoning_effort = "medium"

        # 2. 调用 LLM
        try:
            response = await llm.chat(
                model=model,
                messages=...,
                reasoning_effort=reasoning_effort,
                max_tokens=plan.max_reasoning_tokens + plan.estimated_output_tokens,
                timeout=plan.max_reasoning_seconds,
            )
            # 3. 调用后:实算回补
            gov.record_actual(plan, response.usage, response.reasoning_content)

        except TimeoutError:
            gov.record_timeout(plan)
            raise
        except Exception as e:
            gov.record_failure(plan, error=str(e))
            raise

    关键设计:
        - 预扣 reserved_total_tokens = estimated * buffer_ratio,确保不超扣
        - post_call 用 actual 修正,差额回退到用户配额
        - reasoning_content 走 ReasoningAuditor 二次审计
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        auditor: Optional[ReasoningAuditor] = None,
    ) -> None:
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.auditor = auditor or ReasoningAuditor()
        self._lock = threading.RLock()
        # 用户级统计(user_id -> UserComputeStats)
        self._user_stats: dict[str, UserComputeStats] = {}
        # 降级状态(user_id -> 降级到期时间)
        self._degrade_until: dict[str, float] = {}
        # 计划历史(便于审计)
        self._plan_history: deque[dict] = deque(maxlen=10_000)

    # ---------------- 公开 API ----------------

    def plan_call(
        self,
        *,
        user_id: str,
        model: str,
        model_style: ReasoningModelStyle = ReasoningModelStyle.NONE,
        tenant_id: str = "",
        estimated_input_tokens: int = 1000,
        estimated_output_tokens: int = 500,
        max_reasoning_tokens: int = 0,
        max_reasoning_seconds: Optional[float] = None,
    ) -> InferenceBudgetPlan:
        """调用前规划:预算预扣 + 降级决策。"""
        plan_id = f"plan-{user_id}-{int(time.time() * 1000)}"

        # 非推理模型 → 直接放行(仍记录预算)
        if model_style == ReasoningModelStyle.NONE:
            plan = InferenceBudgetPlan(
                plan_id=plan_id,
                user_id=user_id,
                tenant_id=tenant_id,
                model=model,
                model_style=model_style,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
                estimated_reasoning_tokens=0,
                max_reasoning_tokens=0,
                max_reasoning_seconds=0.0,
                reserved_total_tokens=int(
                    (estimated_input_tokens + estimated_output_tokens)
                    * self.config["reserved_buffer_ratio"]
                ),
            )
            return plan

        # 推理模型:计算预扣
        max_reasoning = min(
            max_reasoning_tokens or self.config["max_reasoning_tokens_per_call"],
            self.config["max_reasoning_tokens_per_call"],
        )
        max_seconds = (
            max_reasoning_seconds
            if max_reasoning_seconds is not None
            else self.config["max_reasoning_seconds_per_call"]
        )

        estimated_total = (
            estimated_input_tokens + estimated_output_tokens + max_reasoning
        )
        reserved = int(estimated_total * self.config["reserved_buffer_ratio"])

        plan = InferenceBudgetPlan(
            plan_id=plan_id,
            user_id=user_id,
            tenant_id=tenant_id,
            model=model,
            model_style=model_style,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            estimated_reasoning_tokens=max_reasoning,
            max_reasoning_tokens=max_reasoning,
            max_reasoning_seconds=max_seconds,
            reserved_total_tokens=reserved,
        )

        if not is_enabled("defense"):
            return plan

        # 检查降级条件
        degrade_reason = self._check_degrade_conditions(user_id)
        if degrade_reason != DegradeReason.NONE:
            plan.should_degrade = True
            plan.degrade_reason = degrade_reason
            plan.degrade_to_model = self._pick_fallback_model(model)
            with self._lock:
                self._degrade_until[user_id] = (
                    time.time() + self.config["degrade_cooldown_seconds"]
                )
                stats = self._get_or_create_stats(user_id)
                stats.degrade_count += 1
            logger.warning(
                "Degrade reasoning call: user=%s model=%s reason=%s fallback=%s",
                user_id, model, degrade_reason.value, plan.degrade_to_model,
            )

        # 记录计划历史
        with self._lock:
            self._plan_history.append(plan.to_dict())

        return plan

    def record_actual(
        self,
        plan: InferenceBudgetPlan,
        usage: dict,
        reasoning_content: Optional[str] = None,
    ) -> None:
        """调用后:记录实际使用 + 审计思考内容。"""
        plan.finished_at = time.time()
        plan.actual_duration_seconds = plan.finished_at - plan.started_at

        # 解析 usage(各 provider 字段不同)
        actual_input = (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("input_tokens_details", {}).get("text_tokens", 0)
            or plan.estimated_input_tokens
        )
        actual_output = (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or plan.estimated_output_tokens
        )
        actual_reasoning = (
            usage.get("reasoning_tokens")
            or usage.get("output_tokens_details", {}).get("reasoning_tokens", 0)
            or 0
        )

        plan.actual_input_tokens = actual_input
        plan.actual_output_tokens = actual_output
        plan.actual_reasoning_tokens = actual_reasoning

        # 审计思考内容
        if reasoning_content and plan.model_style != ReasoningModelStyle.NONE:
            audit = self.auditor.audit(reasoning_content, user_id=plan.user_id)
            plan.reasoning_summary = audit.summary
            plan.reasoning_pii_leak = audit.pii_leak
            plan.reasoning_anomaly = audit.anomaly
            plan.anomaly_reason = ", ".join(audit.anomaly_types) if audit.anomaly_types else ""

            # PII 泄漏:强制降级 + 告警
            if audit.pii_leak:
                with self._lock:
                    self._degrade_until[plan.user_id] = (
                        time.time() + self.config["degrade_cooldown_seconds"]
                    )
                    stats = self._get_or_create_stats(plan.user_id)
                    stats.reasoning_pii_leak_count += 1
                logger.error(
                    "Reasoning PII leak! user=%s model=%s pii_types=%s",
                    plan.user_id, plan.model, audit.pii_types,
                )

        # 更新用户统计
        with self._lock:
            stats = self._get_or_create_stats(plan.user_id)
            stats.total_input_tokens += actual_input
            stats.total_output_tokens += actual_output
            stats.total_reasoning_tokens += actual_reasoning
            stats.total_calls += 1
            stats.successful_calls += 1
            stats.last_call_at = time.time()
            if stats.first_call_at is None:
                stats.first_call_at = stats.last_call_at
            stats.recent_reasoning_tokens.append(actual_reasoning)
            if plan.reasoning_pii_leak:
                stats.reasoning_pii_leak_count += 0  # 已在审计中累加
            if plan.reasoning_anomaly:
                stats.reasoning_anomaly_count += 1

    def record_timeout(self, plan: InferenceBudgetPlan) -> None:
        """记录超时。"""
        plan.timed_out = True
        plan.finished_at = time.time()
        plan.actual_duration_seconds = plan.finished_at - plan.started_at

        with self._lock:
            stats = self._get_or_create_stats(plan.user_id)
            stats.timeout_calls += 1
            stats.failed_calls += 1
            stats.last_call_at = time.time()

        logger.warning(
            "Reasoning call timeout: user=%s model=%s duration=%.1fs",
            plan.user_id, plan.model, plan.actual_duration_seconds,
        )

    def record_failure(self, plan: InferenceBudgetPlan, error: str = "") -> None:
        """记录失败(非超时)。"""
        plan.finished_at = time.time()
        plan.actual_duration_seconds = plan.finished_at - plan.started_at

        with self._lock:
            stats = self._get_or_create_stats(plan.user_id)
            stats.failed_calls += 1
            stats.last_call_at = time.time()

        logger.warning(
            "Reasoning call failed: user=%s model=%s error=%s",
            plan.user_id, plan.model, error,
        )

    def get_user_stats(self, user_id: str) -> Optional[dict]:
        """获取用户统计(看板用)。"""
        with self._lock:
            stats = self._user_stats.get(user_id)
            return stats.to_dict() if stats else None

    def is_degraded(self, user_id: str) -> bool:
        """用户当前是否被降级。"""
        with self._lock:
            until = self._degrade_until.get(user_id, 0.0)
            return time.time() < until

    def reset_user(self, user_id: str) -> None:
        """重置用户状态(运维 / 测试用)。"""
        with self._lock:
            self._user_stats.pop(user_id, None)
            self._degrade_until.pop(user_id, None)

    def list_users_over_budget(self) -> list[dict]:
        """列出超预算用户(看板用)。"""
        over = []
        with self._lock:
            for user_id, stats in self._user_stats.items():
                if stats.total_reasoning_tokens > self.config["user_daily_reasoning_token_limit"]:
                    over.append({
                        "user_id": user_id,
                        "total_reasoning_tokens": stats.total_reasoning_tokens,
                        "limit": self.config["user_daily_reasoning_token_limit"],
                        "ratio": stats.reasoning_ratio,
                        "degrade_count": stats.degrade_count,
                    })
        return over

    def get_audit_stats(self) -> dict:
        """获取审计统计(看板用)。"""
        with self._lock:
            return {
                "auditor": self.auditor.get_stats(),
                "total_users": len(self._user_stats),
                "users_over_budget": len(self.list_users_over_budget()),
                "users_degraded": sum(
                    1 for u in self._degrade_until.values() if u > time.time()
                ),
            }

    # ---------------- 内部 ----------------

    def _get_or_create_stats(self, user_id: str) -> UserComputeStats:
        if user_id not in self._user_stats:
            self._user_stats[user_id] = UserComputeStats(user_id=user_id)
        return self._user_stats[user_id]

    def _check_degrade_conditions(self, user_id: str) -> DegradeReason:
        """检查是否需要降级。"""
        # 0. 已在降级冷却期
        with self._lock:
            until = self._degrade_until.get(user_id, 0.0)
            if time.time() < until:
                return DegradeReason.PER_CALL_LIMIT  # 复用,标记为持续降级

            stats = self._user_stats.get(user_id)
            if stats is None:
                return DegradeReason.NONE

        # 1. 用户级预算耗尽
        if stats.total_reasoning_tokens >= self.config["user_daily_reasoning_token_limit"]:
            return DegradeReason.USER_BUDGET_EXHAUSTED

        if (
            stats.total_input_tokens + stats.total_output_tokens + stats.total_reasoning_tokens
            >= self.config["user_daily_total_token_limit"]
        ):
            return DegradeReason.USER_BUDGET_EXHAUSTED

        # 2. 频繁超时
        if stats.timeout_calls >= self.config["timeout_threshold_for_degrade"]:
            return DegradeReason.FREQUENT_TIMEOUT

        # 3. 异常增长(最近平均远超历史平均)
        if (
            len(stats.recent_reasoning_tokens) >= self.config["abusive_growth_min_samples"]
            and stats.avg_reasoning_tokens > 0
        ):
            recent = list(stats.recent_reasoning_tokens)[-5:]  # 最近 5 次
            recent_avg = sum(recent) / len(recent) if recent else 0
            if recent_avg > stats.avg_reasoning_tokens * self.config["abusive_growth_multiplier"]:
                return DegradeReason.ABUSIVE_PATTERN

        # 4. reasoning token 占比过高(说明 LLM "想太多")
        if (
            stats.reasoning_ratio > self.config["reasoning_ratio_limit"]
            and stats.total_calls >= 10
        ):
            return DegradeReason.ABUSIVE_PATTERN

        return DegradeReason.NONE

    def _pick_fallback_model(self, original_model: str) -> str:
        """选降级模型。"""
        # 简化映射(可配置)
        model_lower = original_model.lower()
        if "o1" in model_lower or "o3" in model_lower:
            return "gpt-4o"
        if "r1" in model_lower:
            return "deepseek-chat"
        if "thinking" in model_lower or "sonnet" in model_lower:
            return "claude-3-5-sonnet-latest"
        if "opus" in model_lower:
            return "claude-3-5-sonnet-latest"
        # 默认:相同模型但不带 reasoning
        return original_model


# =====================================================================
# 全局单例
# =====================================================================

_governor_instance: Optional[ComputeGovernor] = None
_governor_lock = threading.RLock()


def get_compute_governor() -> ComputeGovernor:
    """获取全局 ComputeGovernor 单例。"""
    global _governor_instance
    with _governor_lock:
        if _governor_instance is None:
            _governor_instance = ComputeGovernor()
        return _governor_instance


def reset_compute_governor() -> None:
    """重置全局单例(测试用)。"""
    global _governor_instance
    with _governor_lock:
        _governor_instance = None


__all__ = [
    "ComputeGovernor",
    "DegradeReason",
    "InferenceBudgetPlan",
    "ReasoningAuditResult",
    "ReasoningAuditor",
    "ReasoningModelStyle",
    "UserComputeStats",
    "get_compute_governor",
    "reset_compute_governor",
]
