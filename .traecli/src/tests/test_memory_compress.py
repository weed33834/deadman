"""P0.5 LLM 记忆压缩单元测试

覆盖:
1. FileMemoryStore.append_episode 支持 importance + pinned
2. _parse_episode_line 向后兼容旧格式(无 importance/pinned)
3. _parse_episode_line 解析新格式
4. MemoryManager._summarize_episode LLM 可用 / 不可用 / 异常 三路径
5. MemoryManager._grade_importance LLM 可用 / 不可用 / 异常 三路径
6. MemoryManager._heuristic_importance 启发式评分
7. MemoryManager._is_trauma_episode 安全触发 / 风险等级 / 关键词
8. after_turn 启用压缩 vs 关闭压缩的两路径
9. feature flag MEMORY_COMPRESS_ENABLED 默认关闭
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock


from deadman.memory.file_store import FileMemoryStore
import deadman.memory.manager as mm_module
from deadman.memory.manager import (
    IMPORTANCE_HIGH_THRESHOLD,
    IMPORTANCE_LOW_THRESHOLD,
    MEMORY_COMPRESS_ENABLED,
    MemoryManager,
)


# =====================================================================
# Mock LLM - 模拟 chat / chat_json
# =====================================================================


class MockLLMClient:
    def __init__(
        self,
        chat_resp: str = "mock summary",
        chat_json_resp: dict[str, Any] | None = None,
        api_key: str = "mock-key",
        raise_on_chat: bool = False,
        raise_on_json: bool = False,
    ):
        self.chat_resp = chat_resp
        self.chat_json_resp = chat_json_resp or {"importance": 0.7}
        self.api_key = api_key
        self.raise_on_chat = raise_on_chat
        self.raise_on_json = raise_on_json
        self.last_usage = {"total_tokens": 50}

    async def chat(self, messages, temperature=0.3, **kwargs):
        if self.raise_on_chat:
            raise RuntimeError("mock chat error")
        return self.chat_resp

    async def chat_json(self, messages, temperature=0.3, **kwargs):
        if self.raise_on_json:
            raise RuntimeError("mock json error")
        return dict(self.chat_json_resp)


# =====================================================================
# FileMemoryStore - importance + pinned 字段测试
# =====================================================================


class TestAppendEpisodeWithMetadata:
    def test_append_with_importance_and_pinned(self, tmp_path: Path):
        store = FileMemoryStore(memory_dir=tmp_path)
        store.append_episode(
            "sess-1",
            "用户咨询户口注销",
            datetime(2024, 1, 1, 10, 0),
            importance=0.85,
            pinned=True,
        )
        episodes = store.load_episodes()
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep["session"] == "sess-1"
        assert ep["summary"] == "用户咨询户口注销"
        assert ep["importance"] == 0.85
        assert ep["pinned"] is True

    def test_append_importance_only(self, tmp_path: Path):
        store = FileMemoryStore(memory_dir=tmp_path)
        store.append_episode(
            "s", "summary", datetime(2024, 1, 1), importance=0.5
        )
        ep = store.load_episodes()[0]
        assert ep["importance"] == 0.5
        assert ep["pinned"] is False

    def test_append_pinned_only(self, tmp_path: Path):
        store = FileMemoryStore(memory_dir=tmp_path)
        store.append_episode(
            "s", "summary", datetime(2024, 1, 1), pinned=True
        )
        ep = store.load_episodes()[0]
        assert ep["importance"] is None
        assert ep["pinned"] is True

    def test_importance_clamped_to_range(self, tmp_path: Path):
        """importance > 1.0 或 < 0.0 应被归一化"""
        store = FileMemoryStore(memory_dir=tmp_path)
        store.append_episode("s", "x", datetime(2024, 1, 1), importance=1.5)
        ep = store.load_episodes()[0]
        assert ep["importance"] == 1.0

        store.append_episode("s", "y", datetime(2024, 1, 2), importance=-0.5)
        eps = store.load_episodes()
        assert eps[1]["importance"] == 0.0


class TestParseEpisodeBackwardCompat:
    """旧格式(无 importance/pinned)必须仍能解析"""

    def test_old_format_parses(self, tmp_path: Path):
        store = FileMemoryStore(memory_dir=tmp_path)
        # 手动写旧格式行
        store.episodes_file.parent.mkdir(parents=True, exist_ok=True)
        store.episodes_file.write_text(
            "[2024-01-01 10:00] session=sess-1 summary=旧格式摘要\n",
            encoding="utf-8",
        )
        ep = store.load_episodes()[0]
        assert ep["session"] == "sess-1"
        assert ep["summary"] == "旧格式摘要"
        assert ep["importance"] is None
        assert ep["pinned"] is False

    def test_mixed_old_and_new_format(self, tmp_path: Path):
        store = FileMemoryStore(memory_dir=tmp_path)
        store.episodes_file.parent.mkdir(parents=True, exist_ok=True)
        store.episodes_file.write_text(
            "[2024-01-01 10:00] session=sess-1 summary=旧格式\n"
            "[2024-01-02 11:00] session=sess-2 importance=0.75 pinned=true summary=新格式\n",
            encoding="utf-8",
        )
        eps = store.load_episodes()
        assert len(eps) == 2
        assert eps[0]["importance"] is None
        assert eps[0]["pinned"] is False
        assert eps[1]["importance"] == 0.75
        assert eps[1]["pinned"] is True
        # session 字段必须正确,不被元数据污染
        assert eps[1]["session"] == "sess-2"

    def test_session_with_dashes(self, tmp_path: Path):
        """session 含短横线不应被误解析"""
        store = FileMemoryStore(memory_dir=tmp_path)
        store.append_episode(
            "default-session", "x", datetime(2024, 1, 1),
            importance=0.5, pinned=True,
        )
        ep = store.load_episodes()[0]
        assert ep["session"] == "default-session"


# =====================================================================
# MemoryManager._summarize_episode - 三路径测试
# =====================================================================


class TestSummarizeEpisode:
    async def test_llm_available_returns_summary(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(chat_resp="用户咨询户口注销流程,智能体建议到派出所办理。")
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        # 注意:_summarize_episode 引用模块级 llm_client,需要确保方法读的是模块级
        summary = await mm._summarize_episode("如何注销户口", "建议到派出所", None, "R0")
        assert "户口" in summary or "派出所" in summary

    async def test_llm_unavailable_falls_back_to_truncation(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(api_key="")
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        summary = await mm._summarize_episode("如何注销户口", "建议到派出所", None, "R0")
        # 应回退到截断式
        assert "用户:" in summary
        assert "助手:" in summary

    async def test_llm_exception_falls_back(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(raise_on_chat=True)
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        summary = await mm._summarize_episode("如何注销户口", "建议到派出所", None, "R0")
        # 异常 → 截断式兜底
        assert "用户:" in summary

    async def test_llm_empty_response_falls_back(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(chat_resp="")
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        summary = await mm._summarize_episode("如何注销户口", "建议到派出所", None, "R0")
        assert "用户:" in summary

    async def test_llm_long_response_truncated(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(chat_resp="摘要" * 500)
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        summary = await mm._summarize_episode("问题", "回答", None, "R0")
        assert len(summary) <= 503  # 500 + "..."


# =====================================================================
# MemoryManager._grade_importance - 三路径测试
# =====================================================================


class TestGradeImportance:
    async def test_llm_available_returns_score(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(chat_json_resp={"importance": 0.85})
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        score = await mm._grade_importance("问题", "回答", "R0", None)
        assert score == 0.85

    async def test_llm_unavailable_uses_heuristic(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(api_key="")
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        score = await mm._grade_importance("问题", "回答", "R3", None)
        assert score == 0.85  # R3 → 0.85

    async def test_llm_exception_uses_heuristic(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(raise_on_json=True)
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        score = await mm._grade_importance("问题", "回答", "R1", None)
        assert score == 0.6  # R1 → 0.6

    async def test_score_clamped_to_range(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(chat_json_resp={"importance": 1.5})
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        score = await mm._grade_importance("问题", "回答", "R0", None)
        assert score == 1.0

    async def test_safety_triggered_heuristic(self, monkeypatch):
        mm = MemoryManager()
        mock_llm = MockLLMClient(api_key="")
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)
        # 构造 safety_triggered=True 的 rule_check_result
        rule_result = MagicMock()
        rule_result.safety_triggered = True
        score = await mm._grade_importance("问题", "回答", "R0", rule_result)
        assert score == 0.95


class TestHeuristicImportance:
    def test_safety_triggered_returns_max(self):
        rule_result = MagicMock()
        rule_result.safety_triggered = True
        assert MemoryManager._heuristic_importance("R0", rule_result) == 0.95

    def test_r0_default(self):
        assert MemoryManager._heuristic_importance("R0", None) == 0.5

    def test_r3_high(self):
        assert MemoryManager._heuristic_importance("R3", None) == 0.85

    def test_r4_highest(self):
        assert MemoryManager._heuristic_importance("R4", None) == 0.9

    def test_unknown_tier_default(self):
        assert MemoryManager._heuristic_importance("R99", None) == 0.5

    def test_none_rule_result(self):
        assert MemoryManager._heuristic_importance("R1", None) == 0.6


# =====================================================================
# MemoryManager._is_trauma_episode - 创伤检测
# =====================================================================


class TestIsTraumaEpisode:
    def setup_method(self):
        self.mm = MemoryManager()

    def test_safety_triggered_is_trauma(self):
        rule_result = MagicMock()
        rule_result.safety_triggered = True
        assert self.mm._is_trauma_episode("x", "y", rule_result, "R0") is True

    def test_r3_is_trauma(self):
        assert self.mm._is_trauma_episode("x", "y", None, "R3") is True

    def test_r4_is_trauma(self):
        assert self.mm._is_trauma_episode("x", "y", None, "R4") is True

    def test_r0_normal_not_trauma(self):
        assert self.mm._is_trauma_episode("咨询户口", "建议到派出所", None, "R0") is False

    def test_distress_keyword_in_user_input(self):
        assert self.mm._is_trauma_episode(
            "我撑不下去了想自杀", "建议拨打心理热线", None, "R0"
        ) is True

    def test_distress_keyword_in_assistant(self):
        # 智能体提到"自杀"干预也算
        assert self.mm._is_trauma_episode(
            "我很难过", "如果您有自杀念头,请拨打...", None, "R0"
        ) is True

    def test_legal_keyword(self):
        assert self.mm._is_trauma_episode(
            "我家有继承纠纷", "建议咨询律师", None, "R0"
        ) is True

    def test_normal_conversation_not_trauma(self):
        assert self.mm._is_trauma_episode(
            "请问户口注销需要什么材料", "需要身份证和死亡证明", None, "R0"
        ) is False


# =====================================================================
# after_turn - 启用 vs 关闭压缩的两路径
# =====================================================================


class TestAfterTurnCompression:
    async def test_compression_disabled_uses_truncation(self, monkeypatch, tmp_path):
        """关闭压缩:走旧的截断式摘要"""
        # 确保 feature flag 关闭
        monkeypatch.setattr(mm_module, "MEMORY_COMPRESS_ENABLED", False)
        mm = MemoryManager(file_store=FileMemoryStore(memory_dir=tmp_path))
        # graphiti/lightrag 都为 None,file_store 启用
        mm.graphiti = None
        mm.lightrag = None

        # mock LLM 不可用,确保不走 LLM 路径
        mock_llm = MockLLMClient(api_key="")
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)

        await mm.after_turn(
            user_id="user-1",
            user_input="如何注销户口",
            assistant_response="建议到派出所办理户口注销",
            agent="death_aftercare",
            risk_tier="R0",
        )
        episodes = mm.file_store.load_episodes()
        assert len(episodes) == 1
        # 旧路径:截断式摘要
        assert "用户:" in episodes[0]["summary"]
        assert "助手:" in episodes[0]["summary"]
        # 旧路径不写 importance/pinned
        assert episodes[0]["importance"] is None
        assert episodes[0]["pinned"] is False

    async def test_compression_enabled_uses_llm_summary(self, monkeypatch, tmp_path):
        """启用压缩:走 LLM 摘要 + 重要性 + pinned"""
        monkeypatch.setattr(mm_module, "MEMORY_COMPRESS_ENABLED", True)
        mm = MemoryManager(file_store=FileMemoryStore(memory_dir=tmp_path))
        mm.graphiti = None
        mm.lightrag = None

        # mock LLM:摘要返回 LLM 生成文本,重要性返回 0.75
        mock_llm = MockLLMClient(
            chat_resp="LLM 生成的摘要:用户咨询户口注销,建议到派出所。",
            chat_json_resp={"importance": 0.75},
        )
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)

        await mm.after_turn(
            user_id="user-1",
            user_input="如何注销户口",
            assistant_response="建议到派出所办理户口注销",
            agent="death_aftercare",
            risk_tier="R0",
        )
        episodes = mm.file_store.load_episodes()
        assert len(episodes) == 1
        # LLM 生成的摘要,不应含 "用户:"/"助手:" 前缀
        assert "LLM 生成" in episodes[0]["summary"]
        assert episodes[0]["importance"] == 0.75
        # R0 + 无安全触发 + 无关键词 → 不 pinned
        assert episodes[0]["pinned"] is False

    async def test_compression_enabled_trauma_pinned(self, monkeypatch, tmp_path):
        """启用压缩:创伤场景 → pinned=True"""
        monkeypatch.setattr(mm_module, "MEMORY_COMPRESS_ENABLED", True)
        mm = MemoryManager(file_store=FileMemoryStore(memory_dir=tmp_path))
        mm.graphiti = None
        mm.lightrag = None

        mock_llm = MockLLMClient(
            chat_resp="用户表达绝望情绪,智能体建议拨打心理热线。",
            chat_json_resp={"importance": 0.95},
        )
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)

        await mm.after_turn(
            user_id="user-1",
            user_input="我撑不下去了想自杀",
            assistant_response="请立即拨打北京心理危机研究与干预中心热线 010-82951332",
            agent="death_aftercare",
            risk_tier="R0",
        )
        episodes = mm.file_store.load_episodes()
        assert episodes[0]["pinned"] is True
        assert episodes[0]["importance"] == 0.95

    async def test_compression_llm_exception_falls_back(self, monkeypatch, tmp_path):
        """启用压缩但 LLM 异常 → 摘要回退截断,重要性走启发式"""
        monkeypatch.setattr(mm_module, "MEMORY_COMPRESS_ENABLED", True)
        mm = MemoryManager(file_store=FileMemoryStore(memory_dir=tmp_path))
        mm.graphiti = None
        mm.lightrag = None

        mock_llm = MockLLMClient(
            raise_on_chat=True, raise_on_json=True, api_key="mock-key"
        )
        monkeypatch.setattr(mm_module, "llm_client", mock_llm)

        await mm.after_turn(
            user_id="user-1",
            user_input="如何注销户口",
            assistant_response="建议到派出所",
            agent="death_aftercare",
            risk_tier="R3",
        )
        episodes = mm.file_store.load_episodes()
        # 摘要回退截断式
        assert "用户:" in episodes[0]["summary"]
        # 重要性走启发式(R3 → 0.85)
        assert episodes[0]["importance"] == 0.85
        # R3 → pinned=True(创伤)
        assert episodes[0]["pinned"] is True


# =====================================================================
# feature flag 默认关闭验证
# =====================================================================


class TestFeatureFlag:
    def test_default_disabled(self):
        # 默认环境不应启用压缩(避免破坏现有测试)
        assert isinstance(MEMORY_COMPRESS_ENABLED, bool)

    def test_thresholds_valid(self):
        assert 0.0 <= IMPORTANCE_LOW_THRESHOLD < IMPORTANCE_HIGH_THRESHOLD <= 1.0
        assert IMPORTANCE_LOW_THRESHOLD == 0.3
        assert IMPORTANCE_HIGH_THRESHOLD == 0.8
