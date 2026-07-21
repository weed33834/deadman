"""测试 deadman.memory - 4 层记忆系统

覆盖点：
  - WorkingMemory 溢出归档（超过 MAX_TURNS 时归档到 episodic）
  - SemanticMemory 矛盾检测（标量字段前后不一致）
  - sanitize_before_store PII 脱敏（identifier/name/phone/address/account_number）
  - MemoryManager.build_context_for_llm 上下文构建
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


from deadman.memory.manager import (
    MemoryManager,
    PII_FIELDS,
    sanitize_before_store,
    _mask_pii,
)
from deadman.memory.semantic import SemanticMemory, UserProfile
from deadman.memory.working import WorkingMemory


# =====================================================================
# WorkingMemory - 溢出归档
# =====================================================================


class TestWorkingMemoryOverflow:
    """测试 WorkingMemory 超过 MAX_TURNS 时归档到 episodic"""

    async def test_overflow_archives_to_episodic(self):
        # 当轮次超过 MAX_TURNS，最老的轮次应被归档
        episodic = MagicMock()
        episodic.archive_turn = AsyncMock()
        wm = WorkingMemory(session_id="s1", max_turns=3)
        wm.set_episodic(episodic)

        # 添加 4 轮，超过 max_turns=3
        for i in range(4):
            await wm.add_turn("user", f"msg-{i}")

        # recent_turns 应只保留最近 3 条
        assert len(wm.recent_turns) == 3
        # 最老的 msg-0 已被归档
        assert wm.recent_turns[0]["content"] == "msg-1"
        # episodic.archive_turn 被调用过（至少一次）
        assert episodic.archive_turn.called
        # 归档的是 msg-0
        archived_call = episodic.archive_turn.call_args_list[0]
        assert archived_call.kwargs.get("session_id") == "s1" or archived_call.args[0] == "s1"

    async def test_no_overflow_no_archive(self):
        # 未超过 MAX_TURNS 时不归档
        episodic = MagicMock()
        episodic.archive_turn = AsyncMock()
        wm = WorkingMemory(session_id="s1", max_turns=10)
        wm.set_episodic(episodic)

        await wm.add_turn("user", "hello")
        await wm.add_turn("assistant", "hi")

        assert len(wm.recent_turns) == 2
        assert not episodic.archive_turn.called

    async def test_add_turn_preserves_role_and_content(self):
        # add_turn 应保留 role 和 content
        wm = WorkingMemory(max_turns=10)
        await wm.add_turn("user", "用户问题", agent="death_aftercare")
        turn = wm.recent_turns[0]
        assert turn["role"] == "user"
        assert turn["content"] == "用户问题"
        assert turn["agent"] == "death_aftercare"

    def test_get_context_window_formats(self):
        # get_context_window 应格式化为 "用户: xxx" / "[agent]: xxx"
        wm = WorkingMemory(max_turns=10)
        wm.recent_turns = [
            {"role": "user", "content": "你好", "agent": "death_aftercare"},
            {"role": "assistant", "content": "您好", "agent": "death_aftercare"},
        ]
        window = wm.get_context_window()
        assert "用户" in window
        assert "你好" in window
        assert "您好" in window

    def test_contradiction_alert_injection(self):
        # add_contradiction_alert 应写入 temp_vars
        wm = WorkingMemory(max_turns=10)
        wm.add_contradiction_alert({"field": "name", "old": "张三", "new": "李四"})
        alerts = wm.temp_vars.get("pending_contradictions", [])
        assert len(alerts) == 1
        assert alerts[0]["field"] == "name"

    def test_clear_contradictions(self):
        wm = WorkingMemory(max_turns=10)
        wm.add_contradiction_alert({"field": "x"})
        wm.clear_contradictions()
        assert wm.temp_vars.get("pending_contradictions", []) == []


# =====================================================================
# SemanticMemory - 矛盾检测
# =====================================================================


class TestSemanticMemoryContradiction:
    """测试 SemanticMemory 标量字段矛盾检测"""

    def test_scalar_contradiction_detected(self):
        # 标量字段前后不一致 → 触发矛盾
        sm = SemanticMemory()
        # 第一次设置 name
        sm.update_user_profile("u1", {"name": "张三"})
        assert sm.get_profile("u1").name == "张三"
        # 第二次设置不同的 name → 矛盾
        sm.update_user_profile("u1", {"name": "李四"})
        contradictions = sm.pending_contradictions
        assert len(contradictions) >= 1
        c = contradictions[0]
        assert c["field"] == "name"
        assert c["old_value"] == "张三"
        assert c["new_value"] == "李四"

    def test_dict_field_merge_no_contradiction(self):
        # dict 字段做字段级合并，相同子键不同值才触发矛盾
        sm = SemanticMemory()
        sm.update_user_profile("u1", {"location": {"city": "北京"}})
        sm.update_user_profile("u1", {"location": {"country": "CN"}})
        # 不同子键 → 合并不触发矛盾
        profile = sm.get_profile("u1")
        assert profile.location == {"city": "北京", "country": "CN"}
        assert sm.pending_contradictions == []

    def test_dict_field_subkey_contradiction(self):
        # dict 子键前后不一致 → 触发矛盾
        sm = SemanticMemory()
        sm.update_user_profile("u1", {"location": {"city": "北京"}})
        sm.update_user_profile("u1", {"location": {"city": "上海"}})
        contradictions = sm.pending_contradictions
        assert len(contradictions) == 1
        assert contradictions[0]["field"] == "location.city"

    def test_list_field_merge_dedup(self):
        # list 字段去重合并
        sm = SemanticMemory()
        sm.update_user_profile("u1", {"pending_tasks": ["task1", "task2"]})
        sm.update_user_profile("u1", {"pending_tasks": ["task2", "task3"]})
        profile = sm.get_profile("u1")
        assert profile.pending_tasks == ["task1", "task2", "task3"]

    def test_no_contradiction_same_value(self):
        # 相同值不触发矛盾
        sm = SemanticMemory()
        sm.update_user_profile("u1", {"name": "张三"})
        sm.update_user_profile("u1", {"name": "张三"})
        assert sm.pending_contradictions == []

    def test_drain_contradictions_clears(self):
        # drain_contradictions 取出并清空
        sm = SemanticMemory()
        sm.update_user_profile("u1", {"name": "张三"})
        sm.update_user_profile("u1", {"name": "李四"})
        drained = sm.drain_contradictions()
        assert len(drained) == 1
        assert sm.pending_contradictions == []

    def test_contradiction_injected_to_working_memory(self):
        # 矛盾告警注入到 working memory
        wm = WorkingMemory(max_turns=10)
        sm = SemanticMemory()
        sm.set_working_memory(wm)

        sm.update_user_profile("u1", {"name": "张三"})
        sm.update_user_profile("u1", {"name": "李四"})

        alerts = wm.temp_vars.get("pending_contradictions", [])
        assert len(alerts) >= 1


# =====================================================================
# sanitize_before_store - PII 脱敏
# =====================================================================


class TestSanitizeBeforeStore:
    """测试 sanitize_before_store PII 脱敏"""

    def test_pii_fields_set(self):
        # 5 个 PII 字段
        assert "identifier" in PII_FIELDS
        assert "name" in PII_FIELDS
        assert "phone" in PII_FIELDS
        assert "address" in PII_FIELDS
        assert "account_number" in PII_FIELDS
        assert len(PII_FIELDS) == 5

    def test_mask_pii_long_string(self):
        # 长字符串：保留首尾 2 字符，中间 ***
        masked = _mask_pii("13800138000")
        # 长度 > 4 → 前 2 + *** + 后 2
        assert masked == "13***00"

    def test_mask_pii_short_string(self):
        # 短字符串（<=4）：全部掩码
        assert _mask_pii("ab") == "***"
        assert _mask_pii("abc") == "***"

    def test_mask_pii_non_string(self):
        # 非字符串 → "***"
        assert _mask_pii(12345) == "***"

    def test_sanitize_name_field(self):
        # name 字段被脱敏
        data = {"name": "张三丰", "age": 30}
        sanitized = sanitize_before_store(data)
        assert sanitized["name"] != "张三丰"
        assert sanitized["age"] == 30  # 非 PII 不动

    def test_sanitize_phone_field(self):
        data = {"phone": "13800138000"}
        sanitized = sanitize_before_store(data)
        assert sanitized["phone"] == "13***00"

    def test_sanitize_nested_dict(self):
        # 嵌套 dict 递归脱敏
        data = {"profile": {"name": "李四", "hobby": "读书"}}
        sanitized = sanitize_before_store(data)
        assert sanitized["profile"]["name"] != "李四"
        assert sanitized["profile"]["hobby"] == "读书"

    def test_sanitize_preserves_non_pii(self):
        # 非 PII 字段保持原值
        data = {"relationship": "子女", "city": "北京"}
        sanitized = sanitize_before_store(data)
        assert sanitized == data

    def test_sanitize_empty_dict(self):
        assert sanitize_before_store({}) == {}


# =====================================================================
# MemoryManager.build_context_for_llm
# =====================================================================


class TestMemoryManagerBuildContext:
    """测试 MemoryManager.build_context_for_llm"""

    async def test_build_context_includes_recent_dialogue(self):
        # 上下文应包含最近对话
        manager = MemoryManager()
        await manager.working.add_turn("user", "你好")
        await manager.working.add_turn("assistant", "您好")

        context = manager.build_context_for_llm("新问题")
        assert "最近对话" in context
        assert "你好" in context
        assert "您好" in context

    async def test_build_context_includes_user_profile(self):
        # 用户画像应注入上下文
        manager = MemoryManager()
        manager.working.temp_vars["user_profile"] = UserProfile(
            user_id="u1", name="张三", relationship_to_deceased="子女"
        )
        context = manager.build_context_for_llm("问题")
        assert "用户画像" in context
        assert "张三" in context

    async def test_build_context_includes_contradictions(self):
        # 待澄清的矛盾应注入上下文
        manager = MemoryManager()
        manager.working.add_contradiction_alert({
            "field": "name",
            "old_value": "张三",
            "new_value": "李四",
        })
        context = manager.build_context_for_llm("问题")
        assert "矛盾" in context or "澄清" in context

    async def test_build_context_empty_when_no_memory(self):
        # 无任何记忆时，仍返回字符串（至少有标题）
        manager = MemoryManager()
        context = manager.build_context_for_llm("问题")
        assert isinstance(context, str)
        assert "最近对话" in context  # 标题始终存在

    def test_memory_manager_has_four_layers(self):
        # MemoryManager 应包含 4 层记忆
        manager = MemoryManager()
        assert manager.working is not None
        assert manager.episodic is not None
        assert manager.semantic is not None
        assert manager.procedural is not None

    def test_memory_manager_cross_injection(self):
        # working 与 episodic / semantic 互相注入
        manager = MemoryManager()
        assert manager.working._episodic is manager.episodic
        assert manager.semantic._working_memory is manager.working

    def test_sanitize_before_store_as_static_method(self):
        # sanitize_before_store 应作为静态方法可调用
        result = MemoryManager.sanitize_before_store({"phone": "13800138000"})
        assert result["phone"] == "13***00"
