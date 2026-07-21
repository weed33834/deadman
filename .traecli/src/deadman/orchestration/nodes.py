"""编排节点实现 - 所有 LangGraph 节点函数与条件路由函数

节点流程：
    input_guard → router → [agent | user_confirm] → rule_check
    → [respond | integrity_check | output_guard] → respond → END

所有节点均为 async 函数，接收 ConversationState 并返回部分状态更新 dict。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..config import settings
from ..llm import llm_client
from ..rules_loader import rule_checker, rule_loader
from ..types import RuleCheckResult, RiskTier, TransferSummary
from .state import ConversationState

logger = logging.getLogger(__name__)

# =====================================================================
# 常量定义
# =====================================================================

# 6 个并列智能体名（与 agents/*.md 文件名对应，下划线 ↔ 短横线）
AGENT_NAMES: list[str] = [
    "death_aftercare",
    "legal_advisor",
    "financial_analyst",
    "policy_researcher",
    "cross_border_specialist",
    "medical_guide",
]

# 默认智能体（兜底路由）
DEFAULT_AGENT = "death_aftercare"

# L2 输入防护 - Prompt Injection 检测模式
INJECTION_PATTERNS: list[str] = [
    r"ignore\s+(previous|all|above)\s+instructions",
    r"disregard\s+(previous|all)\s+",
    r"you\s+are\s+now\s+(a|an)\s+",
    r"system\s*prompt",
    r"reveal\s+(your|the)\s+(system|instructions|rules)",
    r"忘记(之前|以上)(的)?指令",
    r"忽略(之前|以上)(的)?指令",
    r"你现在是",
    r"输出你的系统提示",
    r"扮演(一个)?(没有限制|无限制)的",
]

# L2 输入防护 - PII 检测模式（手机号/身份证/银行卡）
PII_PATTERNS: list[str] = [
    r"1[3-9]\d{9}",  # 手机号
    r"\d{15}|\d{17}[\dXx]",  # 身份证
    r"\d{16,19}",  # 银行卡
    r"\d{6}\d{4}\d{7,8}",  # 部分身份证格式
]

# 转介信号关键词（用于 agent_node 中的轻量级转介检测）
TRANSFER_SIGNALS: dict[str, list[str]] = {
    "legal_advisor": ["法律争议", "诉讼", "律师", "遗产纠纷", "法定继承争议"],
    "financial_analyst": ["复杂资产", "税务", "大额财产", "跨国资产", "股权"],
    "policy_researcher": ["政策搜索", "地方政策", "最新规定", "政策查询"],
    "cross_border_specialist": ["跨境", "跨国", "海外", "领事馆", "外籍"],
    "medical_guide": ["医疗", "就医", "医保", "医院", "临终关怀"],
}


# =====================================================================
# 辅助函数
# =====================================================================


def _agent_name_to_file(agent_name: str) -> str:
    """智能体名（下划线）转文件名（短横线）"""
    return agent_name.replace("_", "-") + ".md"


def _extract_agent_description(agent_name: str) -> str:
    """从 agent.md 的 YAML frontmatter 中提取 description 字段

    支持 `description: |` 多行格式和 `description: 单行` 格式。
    """
    file_name = _agent_name_to_file(agent_name)
    agent_file = settings.agents_dir / file_name
    if not agent_file.exists():
        return ""
    try:
        content = agent_file.read_text(encoding="utf-8")
    except Exception:
        return ""

    # 提取 frontmatter（--- 之间的内容）
    if not content.startswith("---"):
        return ""
    parts = content.split("---", 2)
    if len(parts) < 3:
        return ""
    frontmatter = parts[1]

    # 逐行解析 description 字段
    lines = frontmatter.split("\n")
    in_desc = False
    desc_lines: list[str] = []
    for line in lines:
        if line.startswith("description:"):
            in_desc = True
            rest = line[len("description:"):].strip()
            # 去除 YAML 的 | / > / |- / >- 标记
            if rest and rest not in ("|", ">", "|-", ">-"):
                desc_lines.append(rest)
            continue
        if in_desc:
            if line.startswith(" ") or line.startswith("\t"):
                desc_lines.append(line.strip())
            else:
                break  # 遇到下一个字段，结束
    return " ".join(desc_lines).strip()


def _load_agent_descriptions() -> str:
    """加载所有智能体的 description，用于 router 的意图分类 prompt"""
    entries: list[str] = []
    for name in AGENT_NAMES:
        desc = _extract_agent_description(name)
        if desc:
            # 截断防止 token 过长
            entries.append(f"- {name}: {desc[:300]}")
        else:
            entries.append(f"- {name}: (描述不可用)")
    return "\n".join(entries)


def _detect_transfer_signals(response: str, current_agent: str) -> str | None:
    """从智能体响应中检测转介信号，返回目标智能体名（无信号返回 None）

    使用关键词匹配，避免额外 LLM 调用。当前智能体本身不触发转介。
    """
    for target_agent, keywords in TRANSFER_SIGNALS.items():
        if target_agent == current_agent:
            continue
        for kw in keywords:
            if kw in response:
                return target_agent
    return None


def _append_trace_span(state: ConversationState, span_type: str, name: str, attributes: dict[str, Any] | None = None) -> None:
    """向 state["trace_spans"] 追加一条 span 记录"""
    spans = state.get("trace_spans", [])
    spans.append({
        "span_type": span_type,
        "name": name,
        "attributes": attributes or {},
    })
    state["trace_spans"] = spans


def _append_metric(state: ConversationState, key: str, value: Any) -> None:
    """向 state["metrics"] 追加一条指标"""
    metrics = state.get("metrics", {})
    metrics[key] = value
    state["metrics"] = metrics


# =====================================================================
# 节点 1: input_guard_node - L2 输入防护
# =====================================================================


async def input_guard_node(state: ConversationState) -> dict[str, Any]:
    """L2 输入防护节点 - 检测 Prompt Injection 和 PII 输入

    - 检测到注入攻击时设置 safety_override=True
    - 检测到 PII 时在 draft_response 中提示用户脱敏
    """
    user_input = state.get("user_input", "")
    injection_detected = False
    pii_detected = False
    detected_patterns: list[str] = []

    # 检测 Prompt Injection
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, user_input, re.IGNORECASE):
            injection_detected = True
            detected_patterns.append(f"注入模式: {pattern}")
            break

    # 检测 PII（仅在未检测到注入时，避免重复告警）
    if not injection_detected:
        for pattern in PII_PATTERNS:
            if re.search(pattern, user_input):
                pii_detected = True
                detected_patterns.append("检测到疑似 PII（手机号/身份证/银行卡）")
                break

    safety_override = False
    draft_response = ""

    if injection_detected:
        # 注入攻击 → 触发安全优先
        safety_override = True
        draft_response = (
            "检测到您的输入可能包含指令注入内容。为了您的账户安全，"
            "我无法处理该请求。如果您有身后事相关的问题，请直接描述您的需求。"
        )
    elif pii_detected:
        # PII → 提示用户脱敏，但不阻断流程
        draft_response = (
            "【温馨提示】您的输入中可能包含敏感个人信息（如手机号/证件号）。"
            "为了保护您的隐私，建议在对话中避免提供完整的证件号码或银行账号。"
            "如需提供，请使用脱敏格式（如：138****5678）。\n\n"
        )

    _append_trace_span(state, "rule", "node.input_guard", {
        "injection_detected": injection_detected,
        "pii_detected": pii_detected,
        "patterns": detected_patterns,
    })

    updates: dict[str, Any] = {
        "safety_override": safety_override,
        "trace_spans": state.get("trace_spans", []),
    }
    if draft_response:
        updates["draft_response"] = draft_response
    if safety_override:
        # 注入时直接设置 rule_check，跳过后续正常校验
        updates["rule_check"] = RuleCheckResult(
            passed=False,
            violations=[{"rule": "input-guardrails", "priority": 2, "violation": p} for p in detected_patterns],
            risk_tier=RiskTier.R3,
            safety_triggered=True,
        )
    return updates


# =====================================================================
# 节点 2: router_node - 意图识别 + 选智能体
# =====================================================================


async def router_node(state: ConversationState) -> dict[str, Any]:
    """意图识别节点 - 用 LLM 分类用户意图，选择最合适的智能体

    - 注入 6 个智能体的 description 到 prompt
    - 若用户在心理危机状态，强制路由到 death_aftercare
    - LLM 不可用时降级到 death_aftercare
    """
    user_input = state.get("user_input", "")
    user_profile = state.get("user_profile", {})

    # 若已有 pending_transfer 且用户已确认，不重新分类（由 route_to_agent 处理）
    pending = state.get("pending_transfer")
    if pending and state.get("transfer_confirmed") is True:
        _append_trace_span(state, "rule", "node.router", {
            "skipped": True,
            "reason": "pending_transfer_confirmed",
        })
        return {"trace_spans": state.get("trace_spans", [])}

    # 若 safety_override 已触发，强制路由到 death_aftercare
    if state.get("safety_override"):
        _append_trace_span(state, "rule", "node.router", {
            "selected_agent": DEFAULT_AGENT,
            "reason": "safety_override",
        })
        return {
            "current_agent": DEFAULT_AGENT,
            "trace_spans": state.get("trace_spans", []),
        }

    # 默认智能体
    selected_agent = DEFAULT_AGENT
    reason = "default_fallback"

    # 尝试用 LLM 做意图分类
    if llm_client and llm_client.api_key:
        try:
            agent_descriptions = _load_agent_descriptions()
            prompt = (
                "你是身后事多智能体平台的路由器。根据用户输入和画像，判定最适合处理的智能体。\n\n"
                f"可选智能体及其职责：\n{agent_descriptions}\n\n"
                f"用户输入：{user_input}\n"
                f"用户画像：{user_profile}\n\n"
                "规则：\n"
                "1. 若用户处于心理危机状态（如提到不想活/想死/自残），强制选择 death_aftercare\n"
                "2. 若用户明确提到医疗/就医/医保，选择 medical_guide\n"
                "3. 若提到法律争议/诉讼/律师，选择 legal_advisor\n"
                "4. 若提到复杂资产/税务，选择 financial_analyst\n"
                "5. 若提到跨境/跨国，选择 cross_border_specialist\n"
                "6. 若需要搜索地方政策，选择 policy_researcher\n"
                "7. 默认选择 death_aftercare\n\n"
                "输出 JSON：{\"agent\": \"智能体名\", \"reason\": \"简短理由\", \"confidence\": 0.0-1.0}"
            )
            result = await llm_client.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            agent = result.get("agent", "").strip()
            if agent in AGENT_NAMES:
                selected_agent = agent
                reason = result.get("reason", "llm_classified")
            else:
                reason = f"llm_returned_invalid_agent: {agent}"
        except Exception as e:
            reason = f"llm_error: {e}"
            logger.warning("路由 LLM 分类失败，降级到默认智能体: %s", e)
    else:
        reason = "llm_unavailable"

    _append_trace_span(state, "rule", "node.router", {
        "selected_agent": selected_agent,
        "reason": reason,
    })

    return {
        "current_agent": selected_agent,
        "trace_spans": state.get("trace_spans", []),
    }


# =====================================================================
# 节点 3: user_confirm_node - 转介确认
# =====================================================================


async def user_confirm_node(state: ConversationState) -> dict[str, Any]:
    """转介确认节点 - 生成转介话术，等待用户确认

    LangGraph 的 interrupt_before=["user_confirm_node"] 会在进入此节点前暂停。
    调用方应在暂停时向用户展示 pending_transfer 信息，获取确认后设置
    transfer_confirmed 并恢复图执行。
    """
    transfer = state.get("pending_transfer")
    if transfer is None:
        # 没有待确认的转介，直接通过
        _append_trace_span(state, "transfer", "node.user_confirm", {
            "had_pending_transfer": False,
        })
        return {"trace_spans": state.get("trace_spans", [])}

    # 若用户已确认/拒绝，处理确认结果
    confirmed = state.get("transfer_confirmed")
    if confirmed is not None:
        transfer_history = state.get("transfer_history", [])
        if confirmed:
            # 用户同意转介：记录历史，切换智能体
            transfer_history.append(transfer)
            _append_trace_span(state, "transfer", "node.user_confirm", {
                "confirmed": True,
                "to_agent": transfer.to_agent,
            })
            return {
                "current_agent": transfer.to_agent,
                "agent_history": state.get("agent_history", []) + [transfer.to_agent],
                "transfer_history": transfer_history,
                "pending_transfer": None,
                "trace_spans": state.get("trace_spans", []),
            }
        else:
            # 用户拒绝转介
            _append_trace_span(state, "transfer", "node.user_confirm", {
                "confirmed": False,
                "declined_to": transfer.to_agent,
            })
            return {
                "pending_transfer": None,
                "trace_spans": state.get("trace_spans", []),
            }

    # 生成转介话术（transfer_confirmed 为 None，即首次询问）
    transfer_message = ""
    if llm_client and llm_client.api_key:
        try:
            prompt = (
                "你是身后事平台的转介引导员。根据转介摘要生成一段简短的转介话术。\n\n"
                f"转介摘要：\n"
                f"- 来源智能体：{transfer.from_agent}\n"
                f"- 目标智能体：{transfer.to_agent}\n"
                f"- 转介原因：{transfer.reason}\n"
                f"- 用户情况：{transfer.user_situation}\n"
                f"- 当前问题：{transfer.current_question}\n\n"
                "要求：\n"
                "1. 简短说明为什么建议转介\n"
                "2. 说明目标智能体能提供什么帮助\n"
                "3. 尊重用户自主权（不强制）\n"
                "4. 提供明确的\"是/否\"选择\n"
                "5. 语气温和克制（tone-framework）"
            )
            transfer_message = await llm_client.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        except Exception as e:
            logger.warning("生成转介话术失败，使用模板: %s", e)
            transfer_message = ""

    # LLM 不可用时使用模板
    if not transfer_message:
        transfer_message = (
            f"根据您的情况，我建议您咨询我们的{transfer.to_agent}智能体，"
            f"它更擅长处理此类问题。\n\n"
            f"转介原因：{transfer.reason}\n\n"
            f"您是否同意转介？（请回复\"是\"或\"否\"）"
        )

    _append_trace_span(state, "transfer", "node.user_confirm", {
        "generated_message": True,
        "to_agent": transfer.to_agent,
    })

    return {
        "draft_response": transfer_message,
        "trace_spans": state.get("trace_spans", []),
    }


# =====================================================================
# 节点 4: agent_node - 通用智能体执行节点
# =====================================================================


async def agent_node(state: ConversationState) -> dict[str, Any]:
    """通用智能体执行节点 - 根据 current_agent 加载 agent.md + rules，调用 LLM 生成响应

    流程：
    1. 读取 current_agent 对应的 agent.md
    2. 加载规则（rule_loader.get_system_prompt_rules）
    3. 构建 system prompt = agent.md + rules
    4. 调用 LLM 生成响应
    5. 检测转介信号，若需要则设置 pending_transfer
    """
    current_agent = state.get("current_agent") or DEFAULT_AGENT
    user_input = state.get("user_input", "")
    user_profile = state.get("user_profile", {})
    turn_count = state.get("turn_count", 0)

    # 若 safety_override 已由前序节点（如 input_guard）触发且已设置 draft_response，
    # 则不覆盖已有响应，直接透传（安全优先，跳过智能体生成）
    if state.get("safety_override") and state.get("draft_response"):
        _append_trace_span(state, "agent", f"node.agent.{current_agent}", {
            "agent": current_agent,
            "skipped": True,
            "reason": "safety_override_with_existing_response",
        })
        return {"trace_spans": state.get("trace_spans", [])}

    # 记录智能体历史
    agent_history = state.get("agent_history", [])
    if not agent_history or agent_history[-1] != current_agent:
        agent_history = agent_history + [current_agent]

    # 加载 agent.md
    agent_file = settings.agents_dir / _agent_name_to_file(current_agent)
    agent_md_content = ""
    if agent_file.exists():
        try:
            agent_md_content = agent_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("加载 agent.md 失败 %s: %s", agent_file, e)

    # 加载规则
    rules_content = ""
    try:
        rules_content = rule_loader.get_system_prompt_rules()
    except Exception as e:
        logger.warning("加载规则失败: %s", e)

    # 构建 system prompt
    system_prompt = (
        f"# 你的智能体定义\n{agent_md_content}\n\n"
        f"# 平台规则（必须遵守）\n{rules_content}\n\n"
        f"# 当前对话上下文\n"
        f"轮次：{turn_count}\n"
        f"用户画像：{user_profile}\n"
    )

    # 保留前序节点（如 input_guard 的 PII 提示）设置的 draft_response 前缀
    existing_prefix = state.get("draft_response", "")

    # 调用 LLM 生成响应
    draft_response = ""
    if llm_client and llm_client.api_key:
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ]
            draft_response = await llm_client.chat(messages, temperature=0.3)
        except Exception as e:
            logger.warning("智能体 LLM 调用失败 [%s]: %s", current_agent, e)
            draft_response = (
                f"抱歉，我在处理您的请求时遇到了技术问题。"
                f"请稍后重试，或直接联系相关机构获取帮助。\n"
                f"（错误信息：{type(e).__name__}）"
            )
    else:
        # LLM 不可用时的降级响应
        draft_response = (
            "当前 LLM 服务未配置（缺少 LLM_API_KEY），无法生成智能回复。\n"
            "请配置 LLM_API_KEY 环境变量后重试。\n\n"
            f"您的问题已转发给 {current_agent} 智能体，"
            "在 LLM 可用后将获得完整回复。"
        )

    # 若前序节点设置了提示前缀（如 PII 警告），前置到生成的响应前
    if existing_prefix:
        draft_response = existing_prefix + draft_response

    # 检测转介信号
    updates: dict[str, Any] = {
        "draft_response": draft_response,
        "agent_history": agent_history,
    }

    transfer_target = _detect_transfer_signals(draft_response, current_agent)
    if transfer_target:
        # 创建转介摘要
        pending_transfer = TransferSummary(
            from_agent=current_agent,
            to_agent=transfer_target,
            reason=f"智能体 {current_agent} 检测到 {transfer_target} 相关信号",
            user_situation=str(user_profile.get("situation", ""))[:500],
            current_question=user_input[:500],
            completed_items=[],
            pending_items=[],
        )
        updates["pending_transfer"] = pending_transfer
        updates["transfer_confirmed"] = None  # 重置确认状态
        _append_trace_span(state, "transfer", "agent.detect_transfer", {
            "from_agent": current_agent,
            "to_agent": transfer_target,
        })

    _append_trace_span(state, "agent", f"node.agent.{current_agent}", {
        "agent": current_agent,
        "response_length": len(draft_response),
        "transfer_detected": transfer_target is not None,
    })
    updates["trace_spans"] = state.get("trace_spans", [])

    return updates


# =====================================================================
# 节点 5: rule_check_node - L0-L8 规则校验
# =====================================================================


async def rule_check_node(state: ConversationState) -> dict[str, Any]:
    """L0-L8 规则优先级链校验节点

    使用 rule_checker 对 draft_response 做规则校验：
    - L0 safety-protocol: 心理危机检测（安全赢一切）
    - L1 integrity-framework: 编造检测（诚信赢温和）
    - L4 risk-tier-framework: 风险分级信号
    - 其他 L2-L8 规则

    若 L0 触发，设置 safety_override=True，跳过后续流程直接响应。
    """
    draft_response = state.get("draft_response", "")

    # 若 safety_override 已由前序节点设置（如 input_guard），直接透传
    if state.get("safety_override"):
        existing_rc = state.get("rule_check")
        if existing_rc and existing_rc.safety_triggered:
            _append_trace_span(state, "rule", "node.rule_check", {
                "skipped": True,
                "reason": "safety_override_already_set",
            })
            return {"trace_spans": state.get("trace_spans", [])}

    # 执行规则校验
    try:
        result = rule_checker.check(
            output_text=draft_response,
            context={
                "user_input": state.get("user_input", ""),
                "current_agent": state.get("current_agent", ""),
            },
        )
    except Exception as e:
        logger.warning("规则校验异常: %s", e)
        result = RuleCheckResult(
            passed=True,
            violations=[],
            risk_tier=RiskTier.R0,
            safety_triggered=False,
        )

    _append_trace_span(state, "rule", "node.rule_check", {
        "passed": result.passed,
        "violations_count": len(result.violations),
        "risk_tier": result.risk_tier.value,
        "safety_triggered": result.safety_triggered,
        "integrity_violations_count": len(result.integrity_violations),
    })

    updates: dict[str, Any] = {
        "rule_check": result,
        "safety_override": result.safety_triggered,
        "trace_spans": state.get("trace_spans", []),
    }

    # 若 L0 安全触发，追加安全响应
    if result.safety_triggered:
        safety_response = (
            "我注意到对话中可能涉及安全问题。您的生命安全是最重要的。\n\n"
            "如果您正在经历心理危机，请立即联系：\n"
            "- 全国心理援助热线：400-161-9995（24小时）\n"
            "- 北京心理危机研究与干预中心：010-82951332\n"
            "- 或拨打 120 / 前往最近医院急诊\n\n"
            "您不是一个人，请先确保安全，身后事的事务可以稍后再处理。"
        )
        updates["draft_response"] = safety_response

    return updates


# =====================================================================
# 节点 6: integrity_check_node - 5 关事实复核
# =====================================================================


async def integrity_check_node(state: ConversationState) -> dict[str, Any]:
    """5 关事实复核节点 - 调用 MCP check_integrity 工具

    校验内容：
    1. 来源校验（claim 是否有来源）
    2. 幻觉校验（编造模式检测）
    3. 时效校验（来源是否过时）
    4. 单源校验（关键 claim 是否有多源）
    5. 越界校验（是否给出专业建议）

    若发现不一致，更新 confidence_labels 并可能重写 draft_response。
    """
    draft_response = state.get("draft_response", "")
    knowledge_results = state.get("knowledge_results", [])

    # 构造 claims_to_verify（从 draft_response 提取关键 claim）
    claims: list[dict[str, Any]] = []
    # 将整个响应作为一个通用 claim
    claims.append({
        "claim": draft_response[:500],
        "source": None,
        "claim_type": "fact",
    })
    # 从知识库结果中提取来源
    for kr in knowledge_results:
        if isinstance(kr, dict):
            source = kr.get("full_file") or kr.get("source")
            if source:
                claims.append({
                    "claim": str(kr.get("content", ""))[:200],
                    "source": source,
                    "claim_type": "fact",
                })

    # 调用 MCP check_integrity 工具
    try:
        from ..mcp_server.server import mcp

        result = await mcp.call_tool("check_integrity", {
            "output_text": draft_response,
            "claims_to_verify": claims,
            "selfcheck_enabled": True,
            "selfcheck_sample_count": settings.selfcheck_sample_count,
        })
    except Exception as e:
        logger.warning("check_integrity 调用失败: %s", e)
        _append_trace_span(state, "rule", "node.integrity_check", {
            "error": str(e),
            "passed": True,
        })
        return {
            "confidence_labels": state.get("confidence_labels", []),
            "trace_spans": state.get("trace_spans", []),
        }

    # 处理校验结果
    passed = result.get("passed", True)
    confidence_labels = result.get("confidence_labels", [])
    check_results = result.get("check_results", {})

    # 若 hallucination_check 失败，追加警示
    hallucination = check_results.get("hallucination_check", {})
    if not hallucination.get("passed", True):
        warning = (
            "\n\n【事实复核提示】本回复中部分内容可能需要进一步核实。"
            "建议您通过官方渠道确认具体信息。"
        )
        draft_response = draft_response + warning

    _append_trace_span(state, "rule", "node.integrity_check", {
        "passed": passed,
        "five_gate_passed": result.get("five_gate_passed", True),
        "confidence_labels_count": len(confidence_labels),
        "hallucination_issues": len(hallucination.get("issues", [])),
    })

    return {
        "draft_response": draft_response,
        "confidence_labels": confidence_labels,
        "trace_spans": state.get("trace_spans", []),
    }


# =====================================================================
# 节点 7: output_guard_node - 输出前最终校验
# =====================================================================


async def output_guard_node(state: ConversationState) -> dict[str, Any]:
    """输出前最终校验节点

    - 确保 AI 身份告知存在（L5 transparency-framework）
    - 追加置信度标注（若有）
    - 确保 PII 未泄漏
    """
    draft_response = state.get("draft_response", "")
    turn_count = state.get("turn_count", 0)

    # 首轮对话追加 AI 身份告知（L5 transparency）
    if turn_count == 0 and "AI" not in draft_response and "人工智能" not in draft_response:
        disclosure = (
            "【身份告知】我是身后事多智能体平台的 AI 助手，"
            "可以为您提供身后事流程引导、政策查询等服务。"
            "我无法替代专业法律/医疗/财务建议，重要决策请咨询专业人士。\n\n"
        )
        draft_response = disclosure + draft_response

    # 追加置信度标注（若有且尚未追加）
    confidence_labels = state.get("confidence_labels", [])
    if confidence_labels:
        existing_labels = [item for item in confidence_labels if isinstance(item, dict)]
        if existing_labels and "【置信度标注】" not in draft_response:
            label_lines: list[str] = ["【置信度标注】"]
            for label in existing_labels:
                claim = label.get("claim", "")[:80]
                conf = label.get("confidence", "未知")
                label_lines.append(f"- {claim}... → 置信度: {conf}")
            draft_response = draft_response + "\n\n" + "\n".join(label_lines)

    # PII 泄漏检测（输出侧）
    for pattern in PII_PATTERNS:
        matches = re.findall(pattern, draft_response)
        if matches:
            # 对输出中的 PII 做掩码
            for match in matches:
                masked = match[:2] + "***" + match[-2:] if len(match) > 4 else "***"
                draft_response = draft_response.replace(match, masked)
            logger.warning("输出中检测到 PII，已脱敏处理")

    _append_trace_span(state, "rule", "node.output_guard", {
        "response_length": len(draft_response),
        "has_disclosure": "身份告知" in draft_response,
        "confidence_labels_count": len(confidence_labels),
    })

    return {
        "draft_response": draft_response,
        "trace_spans": state.get("trace_spans", []),
    }


# =====================================================================
# 节点 8: respond_node - 生成最终响应
# =====================================================================


async def respond_node(state: ConversationState) -> dict[str, Any]:
    """生成最终响应节点

    - 将 draft_response 设为 final_response
    - 更新 metrics（轮次、智能体、规则校验结果等）
    """
    final_response = state.get("draft_response", "")

    # 更新 metrics
    metrics = state.get("metrics", {})
    metrics["turn_count"] = state.get("turn_count", 0) + 1
    metrics["current_agent"] = state.get("current_agent", "")
    metrics["safety_override"] = state.get("safety_override", False)

    rule_check = state.get("rule_check")
    if rule_check:
        metrics["rule_check_passed"] = rule_check.passed
        metrics["risk_tier"] = rule_check.risk_tier.value

    metrics["agent_history"] = state.get("agent_history", [])
    metrics["transfer_triggered"] = state.get("pending_transfer") is not None
    metrics["trace_spans_count"] = len(state.get("trace_spans", []))

    _append_trace_span(state, "root", "node.respond", {
        "final_response_length": len(final_response),
        "metrics_keys": list(metrics.keys()),
    })

    return {
        "final_response": final_response,
        "metrics": metrics,
        "trace_spans": state.get("trace_spans", []),
    }


# =====================================================================
# 条件路由函数
# =====================================================================


def route_to_agent(state: ConversationState) -> str:
    """路由函数 - 根据 current_agent 和转介状态决定路由到哪个智能体节点

    返回值：
    - AGENT_NAMES 中的某个智能体名 → 路由到对应 agent node
    - "await_transfer_confirm" → 路由到 user_confirm_node

    路由优先级：
    1. 待确认转介且用户已确认 → 路由到目标智能体
    2. 待确认转介但未询问 → 路由到用户确认节点
    3. 安全优先触发 → 强制路由到 death_aftercare
    4. 正常路由 → 路由到 current_agent
    """
    # 1. 待确认转介且用户已确认 → 路由到目标智能体
    pending = state.get("pending_transfer")
    if pending and state.get("transfer_confirmed") is True:
        target = pending.to_agent
        if target in AGENT_NAMES:
            return target
        logger.warning("转介目标智能体无效: %s，降级到默认", target)
        return DEFAULT_AGENT

    # 2. 待确认转介但未询问 → 路由到用户确认节点
    if pending and state.get("transfer_confirmed") is None:
        return "await_transfer_confirm"

    # 3. 安全优先触发 → 强制路由到 death_aftercare
    if state.get("safety_override"):
        current = state.get("current_agent", "")
        if current != DEFAULT_AGENT:
            return DEFAULT_AGENT

    # 4. 正常路由 → 路由到 current_agent
    current_agent = state.get("current_agent", "")
    if current_agent in AGENT_NAMES:
        return current_agent

    return DEFAULT_AGENT


def after_rule_check(state: ConversationState) -> str:
    """路由函数 - 规则校验后的路由

    返回值：
    - "safety_override" → 跳过其他校验，直接响应（L0 触发）
    - "needs_integrity_check" → 需要 5 关事实复核（L1 诚信违规）
    - "pass_through" → 直接通过到输出校验
    """
    rc = state.get("rule_check")
    if rc is None:
        return "pass_through"

    # L0 安全触发 → 直接响应
    if rc.safety_triggered:
        return "safety_override"

    # L1 诚信违规 → 需要 5 关事实复核
    if rc.integrity_violations:
        return "needs_integrity_check"

    # 默认通过
    return "pass_through"


def after_user_confirm(state: ConversationState) -> str:
    """路由函数 - 用户确认转介后的路由

    返回值：
    - "proceed_transfer" → 用户同意转介，回到 router 路由到目标智能体
    - "decline_transfer" → 用户拒绝转介，直接响应当前智能体的回复
    """
    confirmed = state.get("transfer_confirmed")
    if confirmed is True:
        return "proceed_transfer"
    return "decline_transfer"
