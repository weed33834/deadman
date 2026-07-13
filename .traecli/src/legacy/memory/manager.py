"""统一记忆管理 - 管理 4 层记忆，对接 LangGraph state。

MemoryManager 把 WorkingMemory / EpisodicMemory / SemanticMemory / ProceduralMemory
统一编排：会话开始时恢复历史记忆，每轮构建选择性召回的上下文，轮末更新各层记忆。
"""

from __future__ import annotations

import logging
from typing import Any

from ..llm import llm_client
from .working import WorkingMemory
from .episodic import EpisodicMemory
from .semantic import SemanticMemory, UserProfile
from .procedural import ProceduralMemory

logger = logging.getLogger(__name__)

# === 可选依赖：Graphiti（时态记忆图）与 LightRAG（知识图谱） ===
# 未安装时降级为 None，记忆系统在纯内存模式下运行。
try:  # pragma: no cover - 可选依赖
    from graphiti import Graphiti as _GraphitiClient  # type: ignore
except Exception:  # pragma: no cover
    _GraphitiClient = None  # type: ignore

try:  # pragma: no cover - 可选依赖
    from lightrag import LightRAG as _LightRAGClient  # type: ignore
except Exception:  # pragma: no cover
    _LightRAGClient = None  # type: ignore


# 需要 PII 脱敏的字段集合
PII_FIELDS = {"identifier", "name", "phone", "address", "account_number"}


def sanitize_before_store(data: dict) -> dict:
    """存储前 PII 脱敏。

    对 identifier/name/phone/address/account_number 字段做掩码处理，
    嵌套 dict 递归处理。
    """
    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        if key in PII_FIELDS:
            sanitized[key] = _mask_pii(value)
        elif isinstance(value, dict):
            sanitized[key] = sanitize_before_store(value)
        else:
            sanitized[key] = value
    return sanitized


def _mask_pii(value: Any) -> Any:
    """脱敏处理：保留首尾 2 字符，中间用 *** 替换；过短则全部掩码"""
    if isinstance(value, str):
        if len(value) > 4:
            return value[:2] + "***" + value[-2:]
        return "***"
    if isinstance(value, dict):
        return {k: _mask_pii(v) for k, v in value.items()}
    return "***"


class MemoryManager:
    """统一记忆管理 - 管理 4 层记忆，对接 LangGraph state"""

    # 把模块级脱敏函数挂为静态方法，便于以方法形式调用
    sanitize_before_store = staticmethod(sanitize_before_store)

    def __init__(
        self,
        working_memory: WorkingMemory | None = None,
        episodic_memory: EpisodicMemory | None = None,
        semantic_memory: SemanticMemory | None = None,
        procedural_memory: ProceduralMemory | None = None,
        graphiti_client: Any = None,
        lightrag_client: Any = None,
    ):
        # 可选外部依赖
        self.graphiti = graphiti_client
        self.lightrag = lightrag_client

        # 初始化 4 层记忆
        self.working = working_memory or WorkingMemory()
        self.episodic = episodic_memory or EpisodicMemory(graphiti_client=self.graphiti)
        self.semantic = semantic_memory or SemanticMemory(
            graphiti_client=self.graphiti, lightrag_client=self.lightrag
        )
        self.procedural = procedural_memory or ProceduralMemory(
            graphiti_client=self.graphiti
        )

        # 注入层间引用：
        # - working 溢出时归档到 episodic
        # - semantic 检测到矛盾时告警注入到 working.temp_vars
        self.working.set_episodic(self.episodic)
        self.semantic.set_working_memory(self.working)

    def start_session(self, user_id: str, session_id: str) -> None:
        """开始新会话 - 恢复历史记忆。

        1. 恢复用户画像（语义记忆）
        2. 恢复最近情景（情景记忆）
        3. 恢复流程进度（程序记忆）
        """
        self.working.session_id = session_id

        # 1. 恢复用户画像（语义记忆）
        profile = self.semantic.get_profile(user_id)
        if profile:
            self.working.temp_vars["user_profile"] = profile

        # 2. 恢复最近情景（情景记忆）
        recent_episodes = self.episodic.recall_recent(session_id, n=3)
        if recent_episodes:
            self.working.temp_vars["recent_episodes_summary"] = [
                ep.summary for ep in recent_episodes
            ]

        # 3. 恢复流程进度（程序记忆）
        if profile and profile.current_stage:
            procedures = self.procedural.get_procedures_by_stage(profile.current_stage)
            resumed: list[dict] = []
            for proc in procedures:
                progress = self.procedural.get_user_progress(user_id, proc.procedure_id)
                if progress:
                    resumed.append({
                        "procedure": proc.procedure_name,
                        "procedure_id": proc.procedure_id,
                        "current_step": progress.current_step,
                        "completed_steps": list(progress.completed_steps),
                    })
            if resumed:
                self.working.temp_vars["resumed_progress"] = resumed

    def build_context_for_llm(self, user_input: str) -> str:
        """为 LLM 构建完整的上下文（选择性召回，非塞入全部历史）。

        召回顺序：
            1. 工作记忆（最近 N 轮）
            2. 语义召回（与当前输入相关的历史片段）
            3. 用户画像
            4. 当前流程进度
            5. 待处理的矛盾
        """
        context_parts: list[str] = []

        # 1. 工作记忆（最近 N 轮）
        context_parts.append("=== 最近对话 ===")
        window = self.working.get_context_window()
        if window:
            context_parts.append(window)

        # 2. 语义召回（与当前输入相关的历史片段）
        relevant_episodes = self.episodic.recall_by_semantic(user_input, top_k=3)
        if relevant_episodes:
            context_parts.append("\n=== 相关历史 ===")
            for ep in relevant_episodes:
                ts = ep.timestamp.strftime("%Y-%m-%d %H:%M") if ep.timestamp else ""
                context_parts.append(f"[{ts}] {ep.summary}")

        # 3. 用户画像
        profile = self.working.temp_vars.get("user_profile")
        if profile:
            context_parts.append("\n=== 用户画像 ===")
            context_parts.append(self._format_profile(profile))

        # 4. 当前流程进度
        resumed = self.working.temp_vars.get("resumed_progress")
        if resumed:
            context_parts.append("\n=== 当前进度 ===")
            for item in resumed:
                context_parts.append(
                    f"流程：{item['procedure']}（当前第 {item['current_step']} 步，"
                    f"已完成：{item['completed_steps']}）"
                )

        # 5. 待处理的矛盾
        contradictions = self.working.temp_vars.get("pending_contradictions", [])
        if contradictions:
            context_parts.append("\n=== 待澄清的矛盾 ===")
            for c in contradictions:
                context_parts.append(
                    f"用户之前说 {c['field']}={c['old_value']!r}，"
                    f"现在说 {c['new_value']!r}，需要澄清"
                )

        return "\n".join(context_parts)

    async def after_turn(
        self,
        user_id: str,
        user_input: str,
        assistant_response: str,
        agent: str = "death-aftercare",
        **kwargs: Any,
    ) -> None:
        """一轮对话结束后，更新各层记忆。

        1. 写入工作记忆（溢出自动归档到情景记忆）
        2. 提取事实，脱敏后更新语义记忆
        3. 更新流程进度
        """
        transfer_triggered = bool(kwargs.get("transfer_triggered"))
        subagents_called = kwargs.get("subagents_called", [])
        rule_check_result = kwargs.get("rule_check_result")
        risk_tier = kwargs.get("risk_tier", "R0")

        # 1. 写入工作记忆（溢出自动归档到情景记忆）
        await self.working.add_turn("user", user_input)
        await self.working.add_turn(
            "assistant",
            assistant_response,
            agent=agent,
            transfer_triggered=transfer_triggered,
            subagent_called=subagents_called,
            rule_check_result=rule_check_result,
            risk_tier=risk_tier,
        )

        # 2. 提取事实，脱敏后更新语义记忆
        facts = await self._extract_facts(user_input, assistant_response)
        if facts:
            safe_facts = sanitize_before_store(facts)
            self.semantic.update_user_profile(user_id, safe_facts)
            # 同步画像到工作记忆
            self.working.temp_vars["user_profile"] = self.semantic.get_profile(user_id)

        # 3. 更新流程进度
        step_completed = kwargs.get("step_completed")
        procedure_id = kwargs.get("procedure_id")
        if step_completed is not None and procedure_id:
            self.procedural.update_user_progress(
                user_id, procedure_id, step_completed
            )

    async def _extract_facts(self, user_input: str, assistant_response: str) -> dict:
        """从对话中提取事实（LLM，返回可更新 UserProfile 的字段 dict）"""
        if not llm_client.api_key:
            return {}
        prompt = (
            "从以下对话中提取用户的事实信息，输出 JSON。只包含明确提到的，不要猜测，"
            "未提及的字段设为 null。可选字段：\n"
            "name, relationship_to_deceased, location{country,city}, "
            "deceased_info{name,death_date,death_location,cause,nationality,domicile}, "
            "family_structure{...}, assets_summary{...}\n\n"
            f"用户：{user_input}\n"
            f"智能体：{assistant_response}\n"
        )
        try:
            data = await llm_client.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            if not isinstance(data, dict):
                return {}
            # 过滤 null 值
            return {k: v for k, v in data.items() if v is not None}
        except Exception as e:
            logger.warning(f"提取事实失败: {e}")
            return {}

    @staticmethod
    def _format_profile(profile: UserProfile) -> str:
        """格式化用户画像"""
        lines: list[str] = []
        if profile.name:
            lines.append(f"姓名：{profile.name}")
        if profile.relationship_to_deceased:
            lines.append(f"关系：{profile.relationship_to_deceased}")
        if profile.location:
            city = profile.location.get("city") if isinstance(profile.location, dict) else None
            lines.append(f"地点：{city or profile.location}")
        if profile.deceased_info:
            d = profile.deceased_info
            lines.append(
                f"逝者：{d.get('name', '未知')}, "
                f"去世日期：{d.get('death_date', '未知')}"
            )
        if profile.family_structure:
            lines.append(f"家庭：{profile.family_structure}")
        if profile.current_stage:
            lines.append(f"当前阶段：第 {profile.current_stage} 阶段")
        if profile.completed_stages:
            lines.append(f"已完成阶段：{profile.completed_stages}")
        return "\n".join(lines)
