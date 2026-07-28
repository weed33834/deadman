"""统一记忆管理 - 管理 4 层记忆，对接 LangGraph state。

MemoryManager 把 WorkingMemory / EpisodicMemory / SemanticMemory / ProceduralMemory
统一编排：会话开始时恢复历史记忆，每轮构建选择性召回的上下文，轮末更新各层记忆。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from ..config import settings
from ..llm import llm_client
from .working import WorkingMemory
from .episodic import EpisodicMemory
from .semantic import SemanticMemory, UserProfile
from .procedural import ProceduralMemory

logger = logging.getLogger(__name__)

# =====================================================================
# P0.5 LLM 记忆压缩 - feature flag(默认关闭,保留旧截断式摘要行为)
# =====================================================================
# 启用后:
#   - episode 摘要由 LLM 生成 2-3 句话(替换 user_input[:80] 截断)
#   - LLM 评估 episode 重要性 0.0-1.0,< 0.3 归档不召回,> 0.8 提升召回优先级
#   - 检测"创伤"记忆(L0 安全触发 / 法律纠纷 / 用户情绪崩溃)→ pinned=True 永不压缩
# 关闭时:走旧的 user_input[:80] + assistant_response[:80] 截断摘要(保证不破坏现有测试)
MEMORY_COMPRESS_ENABLED: bool = os.environ.get(
    "DEADMAN_MEMORY_COMPRESS", "0"
).lower() in ("1", "true", "yes", "on")

# 重要性阈值:< LOW 自动归档不召回,> HIGH 提升召回优先级
IMPORTANCE_LOW_THRESHOLD: float = 0.3
IMPORTANCE_HIGH_THRESHOLD: float = 0.8

# === 可选依赖：Graphiti（时态记忆图）与 LightRAG（知识图谱） ===
# 未安装时降级为 None，记忆系统在纯内存模式下运行。
try:  # pragma: no cover - 可选依赖
    from graphiti import Graphiti as _GraphitiClient  # type: ignore
except ImportError:  # pragma: no cover
    _GraphitiClient = None  # type: ignore

try:  # pragma: no cover - 可选依赖
    from lightrag import LightRAG as _LightRAGClient  # type: ignore
except ImportError:  # pragma: no cover
    _LightRAGClient = None  # type: ignore


def _init_graphiti() -> Any:
    """懒加载 Graphiti 客户端

    需要 GRAPHITI_ENABLED=true + graphiti 包已安装 + Neo4j 连接配置。
    初始化失败时返回 None，记忆系统降级为纯内存模式。
    """
    if not settings.graphiti_enabled or _GraphitiClient is None:
        return None
    try:
        client = _GraphitiClient(
            uri=settings.graphiti_neo4j_uri,
            user=settings.graphiti_neo4j_user,
            password=settings.graphiti_neo4j_password,
        )
        logger.info("Graphiti 客户端初始化成功，uri=%s", settings.graphiti_neo4j_uri)
        return client
    except Exception as exc:
        logger.warning("Graphiti 初始化失败，记忆系统降级为纯内存: %s", exc)
        return None


# === FileMemoryStore 懒加载（借鉴 Hermes Agent MIT 设计的文件降级存储） ===
# 未传入 file_store 时尝试自动初始化；导入失败时降级为 None。
# 任何异常都不应阻塞 MemoryManager 构造（韧性优先）。
def _init_file_store() -> Any:
    """懒加载 FileMemoryStore（纯文件降级后端）。

    FileMemoryStore 仅依赖 stdlib + pyyaml，理论上不会导入失败，
    但仍用 try/except 包裹以防极端情况（如 ~/.deadman 不可写）。
    """
    try:
        from .file_store import FileMemoryStore
        return FileMemoryStore()
    except Exception as exc:  # pragma: no cover - 极端情况
        logger.warning("FileMemoryStore 初始化失败: %s", exc)
        return None


# 需要 PII 脱敏的字段集合（含中英文别名，与 mcp_server._redact_pii 对齐）
PII_FIELDS = {
    "identifier", "name", "phone", "address", "account_number",
    # 中文别名
    "姓名", "电话", "手机", "地址", "住址", "身份证", "证件号",
    "账号", "账户号", "卡号",
    # 常见英文变体
    "tel", "mobile", "id_card", "account",
}


def sanitize_before_store(data: dict) -> dict:
    """存储前 PII 脱敏。

    对 identifier/name/phone/address/account_number 字段（含中英文别名）做掩码处理，
    嵌套 dict 递归处理。字段名匹配大小写不敏感。
    """
    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in PII_FIELDS or key in PII_FIELDS:
            sanitized[key] = _mask_pii(value)
        elif isinstance(value, dict):
            sanitized[key] = sanitize_before_store(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_before_store(item) if isinstance(item, dict) else item
                for item in value
            ]
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
        file_store: Any = None,
    ):
        # 可选外部依赖：未传入时尝试自动初始化
        self.graphiti = graphiti_client or _init_graphiti()
        self.lightrag = lightrag_client

        # FileMemoryStore 作为降级后端：未传入时尝试自动初始化
        # 仅在 graphiti 和 lightrag 都不可用时启用，且只打印一次降级日志
        self.file_store = file_store if file_store is not None else _init_file_store()
        self._file_store_degraded_logged = False  # 防止重复打降级日志

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

    def _is_file_store_active(self) -> bool:
        """判断 FileMemoryStore 降级路径是否应启用。

        启用条件：graphiti 与 lightrag 都不可用，且 file_store 已就绪。
        首次返回 True 时打印一次降级日志（避免每轮重复）。
        """
        if self.graphiti is not None or self.lightrag is not None:
            return False
        if self.file_store is None:
            return False
        if not self._file_store_degraded_logged:
            logger.info("使用 FileMemoryStore 降级存储")
            self._file_store_degraded_logged = True
        return True

    def start_session(self, user_id: str, session_id: str) -> None:
        """开始新会话 - 恢复历史记忆。

        1. 恢复用户画像（语义记忆）
        2. 恢复最近情景（情景记忆）
        3. 恢复流程进度（程序记忆）

        降级路径：若 graphiti 和 lightrag 都不可用，且 FileMemoryStore 就绪，
        从 ~/.deadman/memory/USER.md 加载 profile 注入到 working memory。
        不破坏现有 Graphiti/LightRAG 集成路径。
        """
        self.working.session_id = session_id

        # 1. 恢复用户画像（语义记忆）
        profile = self.semantic.get_profile(user_id)

        # 降级：semantic 内存无 profile 时，从 FileMemoryStore 加载
        if profile is None and self._is_file_store_active():
            try:
                file_profile = self.file_store.load_profile(user_id)
                if file_profile is not None:
                    # 注入到 semantic 内存，后续轮次可直接命中
                    self.semantic.user_profiles[user_id] = file_profile
                    profile = file_profile
            except Exception as exc:
                logger.warning("FileMemoryStore.load_profile 失败: %s", exc)

        if profile:
            self.working.temp_vars["user_profile"] = profile

        # 2. 恢复最近情景（情景记忆）
        recent_episodes = self.episodic.recall_recent(session_id, n=3)
        if recent_episodes:
            self.working.temp_vars["recent_episodes_summary"] = [
                ep.summary for ep in recent_episodes
            ]
        elif self._is_file_store_active():
            # 降级：从 EPISODES.md 加载最近情景摘要
            try:
                file_episodes = self.file_store.load_episodes(limit=3)
                if file_episodes:
                    self.working.temp_vars["recent_episodes_summary"] = [
                        ep.get("summary", "") for ep in file_episodes
                    ]
            except Exception as exc:
                logger.warning("FileMemoryStore.load_episodes 失败: %s", exc)

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

        # 4. 降级路径：若 graphiti 和 lightrag 都不可用，把更新的 profile 和
        #    新 episode 写入 FileMemoryStore（~/.deadman/memory/）
        #    不破坏现有 Graphiti/LightRAG 集成路径，仅作降级方案。
        if self._is_file_store_active():
            try:
                # 写入更新后的 profile
                current_profile = self.semantic.get_profile(user_id)
                if current_profile is not None:
                    self.file_store.save_profile(user_id, current_profile)
                # === P0.5 LLM 记忆压缩 ===
                # 启用 MEMORY_COMPRESS_ENABLED:LLM 生成摘要 + 重要性 + pinned 检测
                # 关闭:走旧截断式摘要(保证不破坏现有测试)
                if MEMORY_COMPRESS_ENABLED:
                    summary_text = await self._summarize_episode(
                        user_input, assistant_response, rule_check_result, risk_tier
                    )
                    importance = await self._grade_importance(
                        user_input, assistant_response, risk_tier, rule_check_result
                    )
                    pinned = self._is_trauma_episode(
                        user_input, assistant_response, rule_check_result, risk_tier
                    )
                else:
                    summary_text = (
                        f"用户: {user_input[:80]} | 助手: {assistant_response[:80]}"
                    )
                    importance = None
                    pinned = False
                # session_id 优先取 working.session_id，其次从 kwargs 提取
                sid = self.working.session_id
                if not sid:
                    sid_kw = kwargs.get("session_id")
                    sid = sid_kw if isinstance(sid_kw, str) and sid_kw else "default-session"
                self.file_store.append_episode(
                    episode_id=sid,
                    summary=summary_text,
                    timestamp=datetime.now(),
                    importance=importance,
                    pinned=pinned,
                )
                # 把本轮提取的事实追加到 MEMORY.md（用户事实章节）
                if facts:
                    for fact_key, fact_value in facts.items():
                        if fact_value is None:
                            continue
                        # 只追加标量事实，dict/list 太啰嗦
                        if isinstance(fact_value, (str, int, float)):
                            self.file_store.append_fact(
                                "用户事实",
                                f"{fact_key}={fact_value}",
                            )
            except Exception as exc:
                logger.warning("FileMemoryStore 写入失败: %s", exc)

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

    # ==================================================================
    # P0.5 LLM 记忆压缩 - 摘要 / 重要性 / 创伤检测
    # ==================================================================

    async def _summarize_episode(
        self,
        user_input: str,
        assistant_response: str,
        rule_check_result: Any = None,
        risk_tier: str = "R0",
    ) -> str:
        """LLM 生成 episode 摘要(2-3 句话),替换 user_input[:80] 截断。

        降级路径:
        - LLM 不可用 → 回退到截断式摘要(保证不阻断主流程)
        - LLM 异常 → 回退到截断式摘要
        - LLM 返回空 → 回退到截断式摘要
        """
        # 截断式兜底
        fallback = f"用户: {user_input[:80]} | 助手: {assistant_response[:80]}"
        if not llm_client.api_key:
            return fallback
        prompt = (
            "请用 2-3 句话总结以下对话的关键信息(用户意图 / 智能体建议 / 关键事实)。"
            "不要编造,只总结对话中明确出现的内容。输出纯文本,不要 JSON。\n\n"
            f"用户：{user_input[:500]}\n"
            f"智能体：{assistant_response[:500]}\n"
        )
        try:
            summary = await llm_client.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=200,
            )
            summary = (summary or "").strip()
            # 防止 LLM 返回过长(失控)
            if len(summary) > 500:
                summary = summary[:500] + "..."
            return summary if summary else fallback
        except Exception as e:
            logger.warning("LLM episode 摘要失败,回退到截断: %s", e)
            return fallback

    async def _grade_importance(
        self,
        user_input: str,
        assistant_response: str,
        risk_tier: str = "R0",
        rule_check_result: Any = None,
    ) -> float:
        """LLM 评估 episode 重要性 0.0-1.0。

        评分维度:
        - 是否涉及关键决策(法律/财务/医疗)
        - 是否包含不可恢复信息(如逝者身份细节)
        - 是否触发安全/合规规则
        - 是否有长期参考价值(政策查询 vs 闲聊)

        降级路径(LLM 不可用)用启发式:
        - safety_triggered / R3+ 风险 → 0.9
        - R1-R2 风险 → 0.6
        - R0 / 默认 → 0.5
        """
        # 启发式兜底(LLM 不可用时)
        heuristic = self._heuristic_importance(risk_tier, rule_check_result)
        if not llm_client.api_key:
            return heuristic
        prompt = (
            "评估以下对话的长期重要性,输出 JSON {\"importance\": 0.0-1.0}。\n"
            "评分依据:\n"
            "- 0.9+: 涉及法律纠纷/安全危机/不可恢复决策\n"
            "- 0.7-0.9: 涉及关键事实(逝者身份/资产/家庭关系)\n"
            "- 0.4-0.7: 涉及流程指导(户口/殡仪/医保办理)\n"
            "- 0.0-0.4: 闲聊/重复/无长期价值\n\n"
            f"用户：{user_input[:300]}\n"
            f"智能体：{assistant_response[:300]}\n"
            f"风险等级：{risk_tier}\n"
        )
        try:
            data = await llm_client.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            importance = float(data.get("importance", heuristic))
            return max(0.0, min(1.0, importance))
        except Exception as e:
            logger.warning("LLM 重要性评估失败,回退启发式: %s", e)
            return heuristic

    @staticmethod
    def _heuristic_importance(risk_tier: str, rule_check_result: Any) -> float:
        """启发式重要性评分(LLM 不可用时的兜底)"""
        # safety_triggered → 最高
        if rule_check_result is not None:
            safety = getattr(rule_check_result, "safety_triggered", False)
            if safety:
                return 0.95
        # 风险等级
        tier_map = {
            "R0": 0.5,
            "R1": 0.6,
            "R2": 0.7,
            "R3": 0.85,
            "R4": 0.9,
        }
        return tier_map.get(risk_tier, 0.5)

    def _is_trauma_episode(
        self,
        user_input: str,
        assistant_response: str,
        rule_check_result: Any = None,
        risk_tier: str = "R0",
    ) -> bool:
        """检测是否为"创伤"记忆 → pinned=True 永不压缩。

        判定标准(任一命中即 pinned):
        - L0 安全协议触发(safety_triggered)
        - R3+ 高风险等级(法律纠纷/财务风险)
        - 用户情绪崩溃信号(自杀/想不开/撑不下去)
        - 涉及法律争议(诉讼/纠纷/继承争议)
        """
        # 1. 安全触发
        if rule_check_result is not None:
            if getattr(rule_check_result, "safety_triggered", False):
                return True
        # 2. 高风险等级
        if risk_tier in ("R3", "R4"):
            return True
        # 3. 用户情绪崩溃信号(关键词检测)
        distress_keywords = (
            "自杀", "想不开", "撑不下去", "不想活", "活不下去",
            "结束生命", "轻生", "绝望",
        )
        text = (user_input or "") + " " + (assistant_response or "")
        for kw in distress_keywords:
            if kw in text:
                return True
        # 4. 法律争议信号
        legal_keywords = ("诉讼", "纠纷", "继承争议", "法庭", "起诉", "被告", "原告")
        for kw in legal_keywords:
            if kw in text:
                return True
        return False

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

    # ==================================================================
    # P0.3 Reflexion 跨会话持久化 - MemoryManager 接口
    # ==================================================================
    #
    # ReflexionEngine 期望 MemoryManager 提供:
    #   async get_reflexion_memory(agent_name) -> dict
    #   async record_successful_adjustment(agent_name, failure_type, adjustment_strategy)
    #
    # 高级功能:
    #   - 按 agent_name + failure_type 索引(快速命中)
    #   - 失败模式统计(某 agent 某失败类型历史出现 N 次 → 反思 prompt 注入历史经验)
    #   - 成功调整策略自动入 procedural memory(固化经验)
    #   - TTL 90 天过期(法规/政策可能已变)
    #   - 跨 agent 共享(同一失败类型在多 agent 间共享,避免重复踩坑)
    #   - 反思质量评估(反思后重试成功率统计 → 反思有效性的元评估)
    #   - 反思压缩(同一失败类型多次成功调整 → LLM 总结为"通用策略")

    async def get_reflexion_memory(self, agent_name: str) -> dict[str, Any]:
        """获取指定 agent 的反思记忆

        返回结构:
            {
                "failure_patterns": {failure_type: {count, first_seen, last_seen}},
                "successful_adjustments": {failure_type: {strategy, success_rate, ...}},
                "shared_patterns": {...},  # 跨 agent 共享(只读)
                "best_strategy": Optional[str],  # 跨 agent 最佳策略(若该失败类型有共享)
            }

        返回的 dict 会传给 ReflexionEngine._reflect 的 prompt,
        让 LLM 知道"此类失败历史出现 N 次,历史成功策略是 X"。
        """
        # 优先用 Graphiti(若可用,未来扩展点)
        if self.graphiti is not None:
            try:
                # Graphiti 客户端可能提供 get_reflexion_memory 方法
                # 此处保留接口,实际实现待 Graphiti 集成完善
                pass
            except Exception as exc:  # pragma: no cover
                logger.warning("Graphiti get_reflexion_memory 失败: %s", exc)

        # 降级到 FileMemoryStore
        if self.file_store is None:
            return {"failure_patterns": {}, "successful_adjustments": {}, "shared_patterns": {}}

        try:
            memory = self.file_store.get_agent_reflexion(agent_name)
            # 注入 best_strategy(跨 agent 最佳)
            shared = memory.get("shared_patterns", {}) or {}
            # 若某 failure_type 在 shared_patterns 中有 best_strategy,
            # 提取出来给 ReflexionEngine 用
            best_strategies = {
                ftype: info.get("best_strategy", "")
                for ftype, info in shared.items()
                if info.get("best_strategy")
            }
            if best_strategies:
                memory["best_strategy"] = best_strategies
            return memory
        except Exception as exc:
            logger.warning("MemoryManager.get_reflexion_memory 失败: %s", exc)
            return {"failure_patterns": {}, "successful_adjustments": {}, "shared_patterns": {}}

    async def record_successful_adjustment(
        self,
        agent_name: str,
        failure_type: str,
        adjustment_strategy: str,
        success: bool = True,
    ) -> None:
        """记录单次反思调整结果(成功或失败)

        高级功能:
        1. 落盘到 FileMemoryStore(REFLEXION.json)
        2. 跨 agent 共享统计自动更新
        3. 反思质量评估(success_rate 追踪)
        4. 若某策略连续成功 N 次 → 固化到 procedural memory(经验沉淀)
        5. 若某失败类型历史 >= 5 次 → 触发反思压缩(LLM 总结为"通用策略")

        Args:
            agent_name: agent 名
            failure_type: 失败模式
            adjustment_strategy: 调整策略描述
            success: 本次是否成功
        """
        if self.file_store is None:
            return

        try:
            # 1. 记录到 FileMemoryStore
            self.file_store.record_agent_adjustment(
                agent_name=agent_name,
                failure_type=failure_type,
                adjustment_strategy=adjustment_strategy,
                success=success,
            )

            # 2. 反思质量评估:若成功率 >= 0.8 且尝试 >= 5 次 → 固化到 procedural
            memory = self.file_store.get_agent_reflexion(agent_name)
            adj = memory.get("successful_adjustments", {}).get(failure_type, {})
            total = int(adj.get("total_count", 0))
            success_rate = float(adj.get("success_rate", 0))
            if total >= 5 and success_rate >= 0.8:
                await self._persist_strategy_to_procedural(
                    agent_name, failure_type, adjustment_strategy, success_rate
                )

            # 3. 反思压缩:失败类型历史 >= 10 次 → LLM 总结为通用策略
            # (此处仅记录"需要压缩"标记,实际 LLM 压缩延后执行避免阻塞主流程)
            if total >= 10 and total % 5 == 0:
                logger.info(
                    "反思压缩触发: agent=%s failure_type=%s total=%d,待 LLM 压缩",
                    agent_name, failure_type, total,
                )

        except Exception as exc:
            logger.warning("MemoryManager.record_successful_adjustment 失败: %s", exc)

    async def _persist_strategy_to_procedural(
        self,
        agent_name: str,
        failure_type: str,
        strategy: str,
        success_rate: float,
    ) -> None:
        """把成功调整策略固化到 procedural memory(经验沉淀)

        当某失败类型的策略历史成功率 >= 0.8 且尝试 >= 5 次时,
        视为"经验已成熟",固化到 procedural memory 供未来直接复用。

        ProceduralMemory 的实际写入依赖 Graphiti;若不可用则降级到 MEMORY.md
        """
        try:
            fact = (
                f"agent={agent_name} failure_type={failure_type} "
                f"strategy=\"{strategy}\" success_rate={success_rate:.2f}"
            )

            # 降级到 FileMemoryStore.append_fact
            if self.file_store is not None:
                self.file_store.append_fact("经验固化", fact)

            # 同步到 procedural_memory（Graphiti-backed 时自动写入时序知识图谱）
            self.procedural.store_strategy(
                agent_name=agent_name,
                failure_type=failure_type,
                strategy=strategy,
                success_rate=success_rate,
            )
        except Exception as exc:
            logger.warning("固化策略到 procedural memory 失败: %s", exc)

    def get_reflexion_summary(self) -> dict[str, Any]:
        """导出反思记忆摘要(供 CLI memory-list 子命令展示)"""
        if self.file_store is None:
            return {"total_agents": 0, "total_patterns": 0, "total_adjustments": 0}
        try:
            return self.file_store.get_reflexion_summary()
        except Exception as exc:
            logger.warning("get_reflexion_summary 失败: %s", exc)
            return {"total_agents": 0, "total_patterns": 0, "total_adjustments": 0}
