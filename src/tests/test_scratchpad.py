"""P4.2 Scratchpad - 测试矩阵

覆盖点：
1. test_scratchpad_add_get: 基础 add+get
2. test_scratchpad_clear: 清空
3. test_scratchpad_shared_mode: 共享模式
4. test_scratchpad_independent_mode: 独立模式
5. test_scratchpad_disabled_no_change: feature flag 关闭
"""

from __future__ import annotations

import pytest

import deadman.orchestration.scratchpad as scratchpad_module
from deadman.orchestration.scratchpad import ScratchpadManager
from deadman.orchestration.state import create_initial_state

# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _enable_scratchpad(monkeypatch):
    """每个测试默认开启 scratchpad feature flag"""
    monkeypatch.setattr(scratchpad_module, "SCRATCHPAD_ENABLED", True)
    yield


# =====================================================================
# 1. 基础 add+get
# =====================================================================


class TestScratchpadAddGet:
    def test_scratchpad_add_get(self):
        """add 追加 note，get 读取（返回副本）"""
        mgr = ScratchpadManager()
        mgr.add("legal_advisor", "用户提到有海外资产")
        mgr.add("legal_advisor", "需要查跨境政策")
        notes = mgr.get("legal_advisor")
        assert notes == ["用户提到有海外资产", "需要查跨境政策"]
        # get 返回副本，外部修改不影响内部
        notes.append("外部修改")
        assert mgr.get("legal_advisor") == ["用户提到有海外资产", "需要查跨境政策"]

    def test_add_to_different_agents_isolated(self):
        """不同 agent 的 scratchpad 互相隔离（independent 模式）"""
        mgr = ScratchpadManager(mode="independent")
        mgr.add("legal_advisor", "法律笔记")
        mgr.add("financial_analyst", "财务笔记")
        assert mgr.get("legal_advisor") == ["法律笔记"]
        assert mgr.get("financial_analyst") == ["财务笔记"]

    def test_add_empty_note_is_noop(self):
        """空 note 不追加"""
        mgr = ScratchpadManager()
        mgr.add("a", "")
        mgr.add("a", None)  # type: ignore[arg-type]
        mgr.add("", "x")
        assert mgr.get("a") == []

    def test_count_returns_zero_for_unknown_agent(self):
        mgr = ScratchpadManager()
        assert mgr.count("unknown") == 0

    def test_state_backed_storage(self):
        """state 传入时 scratchpads 写到 state['scratchpads']"""
        state = create_initial_state("x")
        mgr = ScratchpadManager(state=state)
        mgr.add("legal_advisor", "note1")
        assert state["scratchpads"]["legal_advisor"] == ["note1"]


# =====================================================================
# 2. 清空
# =====================================================================


class TestScratchpadClear:
    def test_scratchpad_clear(self):
        """clear 清空指定 agent 的 scratchpad"""
        mgr = ScratchpadManager()
        mgr.add("a", "note1")
        mgr.add("a", "note2")
        mgr.add("b", "other")
        mgr.clear("a")
        assert mgr.get("a") == []
        # 其他 agent 不受影响
        assert mgr.get("b") == ["other"]

    def test_clear_unknown_agent_is_noop(self):
        """clear 不存在的 agent 不报错"""
        mgr = ScratchpadManager()
        mgr.clear("nonexistent")  # 不抛异常


# =====================================================================
# 3. 共享模式
# =====================================================================


class TestScratchpadSharedMode:
    def test_scratchpad_shared_mode(self):
        """shared 模式：所有 agent 读写同一 scratchpad"""
        mgr = ScratchpadManager(mode="shared")
        mgr.add("legal_advisor", "法律笔记")
        # financial_analyst 读到的是 legal_advisor 写的
        assert mgr.get("financial_analyst") == ["法律笔记"]
        # 反过来也成立
        mgr.add("financial_analyst", "财务笔记")
        assert mgr.get("legal_advisor") == ["法律笔记", "财务笔记"]

    def test_shared_mode_share_to_is_noop(self):
        """shared 模式下 share_to 无需复制（已共享）"""
        mgr = ScratchpadManager(mode="shared")
        mgr.add("a", "note")
        mgr.share_to("b", "a")
        # b 已经能读到（共享），share_to 是 no-op，不会重复追加
        assert mgr.get("b") == ["note"]

    def test_shared_mode_list_agents_excludes_internal_key(self):
        """shared 模式下 list_agents 不返回 __shared__ 内部 key"""
        mgr = ScratchpadManager(mode="shared")
        mgr.add("a", "x")
        agents = mgr.list_agents()
        assert "__shared__" not in agents


# =====================================================================
# 4. 独立模式
# =====================================================================


class TestScratchpadIndependentMode:
    def test_scratchpad_independent_mode(self):
        """independent 模式：每 agent 独立，share_to 显式复制"""
        mgr = ScratchpadManager(mode="independent")
        mgr.add("legal_advisor", "法律笔记1")
        mgr.add("legal_advisor", "法律笔记2")
        # 共享前 cross_border 读不到
        assert mgr.get("cross_border_specialist") == []
        # 共享后 cross_border 能读到 legal_advisor 的笔记
        mgr.share_to("cross_border_specialist", "legal_advisor")
        assert mgr.get("cross_border_specialist") == ["法律笔记1", "法律笔记2"]
        # 源 agent 不受影响
        assert mgr.get("legal_advisor") == ["法律笔记1", "法律笔记2"]

    def test_share_to_appends_not_overwrites(self):
        """share_to 追加而非覆盖目标已有 notes"""
        mgr = ScratchpadManager(mode="independent")
        mgr.add("a", "a-note1")
        mgr.add("b", "b-note1")
        mgr.share_to("b", "a")  # 把 a 的笔记追加到 b
        assert mgr.get("b") == ["b-note1", "a-note1"]

    def test_share_to_empty_source_is_noop(self):
        """源 agent 无 notes 时 share_to 无副作用"""
        mgr = ScratchpadManager(mode="independent")
        mgr.add("b", "b-note")
        mgr.share_to("b", "empty_source")
        assert mgr.get("b") == ["b-note"]

    def test_invalid_mode_falls_back_to_default(self):
        """无效 mode 回退到 independent"""
        mgr = ScratchpadManager(mode="invalid_mode")
        assert mgr.mode == "independent"


# =====================================================================
# 5. feature flag 关闭
# =====================================================================


class TestScratchpadDisabledNoChange:
    def test_scratchpad_disabled_no_change(self, monkeypatch):
        """feature flag 关闭：所有写操作 no-op，读操作返回 []"""
        monkeypatch.setattr(scratchpad_module, "SCRATCHPAD_ENABLED", False)
        mgr = ScratchpadManager()
        mgr.add("a", "note")
        mgr.share_to("b", "a")
        # 读返回空
        assert mgr.get("a") == []
        assert mgr.get("b") == []
        # count 返回 0
        assert mgr.count("a") == 0
        # clear 不抛异常
        mgr.clear("a")

    def test_disabled_does_not_modify_state(self, monkeypatch):
        """feature flag 关闭时不修改传入的 state"""
        monkeypatch.setattr(scratchpad_module, "SCRATCHPAD_ENABLED", False)
        state = create_initial_state("x")
        original = dict(state["scratchpads"]) if state.get("scratchpads") else {}
        mgr = ScratchpadManager(state=state)
        mgr.add("a", "note")
        # state["scratchpads"] 保持原样（{} 默认）
        assert state.get("scratchpads", {}) == original
