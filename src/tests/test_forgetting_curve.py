"""测试 deadman.memory.episodic 的 P2.6 遗忘曲线(Ebbinghaus)。

覆盖点(4 个):
    - test_forgetting_score_recent_high: 近期 episode 高分
    - test_forgetting_score_old_low: 老 episode 低分
    - test_forgetting_score_importance_weighted: 重要性加权
    - test_forgetting_disabled_no_change: feature flag 关闭无影响
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deadman.memory.episodic import Episode, EpisodicMemory


def _make_episode(
    timestamp: datetime,
    importance: float = 0.5,
    last_accessed_at: datetime | None = None,
    episode_id: str = "test-eid",
) -> Episode:
    """构造测试用 Episode"""
    return Episode(
        episode_id=episode_id,
        session_id="s1",
        timestamp=timestamp,
        agent="test",
        user_message="用户问题",
        assistant_response="助手回答",
        summary="测试摘要",
        keywords=["测试"],
        last_accessed_at=last_accessed_at or timestamp,
        importance=importance,
    )


# =====================================================================
# 1. 近期 episode 高分
# =====================================================================


class TestForgettingScoreRecentHigh:
    def test_forgetting_score_recent_high(self, monkeypatch):
        # FORGETTING_CURVE_ENABLED=True,近期 episode 应有高分(接近 importance)
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "FORGETTING_CURVE_ENABLED", True)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        ep = _make_episode(now, importance=0.8, last_accessed_at=now)
        score = em.forgetting_score(ep)
        # delta_days ≈ 0,decay ≈ 1.0,score ≈ 0.8
        assert score == pytest.approx(0.8, abs=0.05)


# =====================================================================
# 2. 老 episode 低分
# =====================================================================


class TestForgettingScoreOldLow:
    def test_forgetting_score_old_low(self, monkeypatch):
        # 60 天前的 episode,decay = exp(-60/30) = exp(-2) ≈ 0.135
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "FORGETTING_CURVE_ENABLED", True)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        old_ts = now - timedelta(days=60)
        ep = _make_episode(old_ts, importance=0.8, last_accessed_at=old_ts)
        score = em.forgetting_score(ep)
        # 期望 ≈ 0.8 * 0.135 ≈ 0.108
        assert score < 0.2  # 远低于 importance
        assert score == pytest.approx(0.8 * 0.135, abs=0.02)

    def test_recent_higher_than_old(self, monkeypatch):
        # 同 importance,近期 > 老的
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "FORGETTING_CURVE_ENABLED", True)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        ep_recent = _make_episode(now, importance=0.5, last_accessed_at=now)
        ep_old = _make_episode(
            now - timedelta(days=90),
            importance=0.5,
            last_accessed_at=now - timedelta(days=90),
        )
        assert em.forgetting_score(ep_recent) > em.forgetting_score(ep_old)


# =====================================================================
# 3. 重要性加权
# =====================================================================


class TestForgettingScoreImportanceWeighted:
    def test_forgetting_score_importance_weighted(self, monkeypatch):
        # 同时间,importance 高 → score 高
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "FORGETTING_CURVE_ENABLED", True)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        ep_low_imp = _make_episode(now, importance=0.2, last_accessed_at=now)
        ep_high_imp = _make_episode(now, importance=0.9, last_accessed_at=now)
        score_low = em.forgetting_score(ep_low_imp)
        score_high = em.forgetting_score(ep_high_imp)
        # 高重要性 > 低重要性
        assert score_high > score_low
        # 比例应接近 importance 比例(因为 decay 相同)
        ratio = score_high / score_low if score_low > 0 else float("inf")
        assert ratio == pytest.approx(0.9 / 0.2, rel=0.05)

    def test_importance_clamped_to_range(self, monkeypatch):
        # importance 超出 [0, 1] 应被截断
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "FORGETTING_CURVE_ENABLED", True)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        # 异常 importance
        ep = _make_episode(now, importance=5.0, last_accessed_at=now)
        ep.importance = 5.0  # 强制设置超出范围
        score = em.forgetting_score(ep)
        # 应被截断到 1.0,score ≤ 1.0
        assert 0.0 <= score <= 1.0


# =====================================================================
# 4. feature flag 关闭
# =====================================================================


class TestForgettingDisabled:
    def test_forgetting_disabled_no_change(self, monkeypatch):
        # FORGETTING_CURVE_ENABLED=False → 返回 importance,不受时间影响
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "FORGETTING_CURVE_ENABLED", False)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        ep_recent = _make_episode(now, importance=0.7, last_accessed_at=now)
        ep_old = _make_episode(
            now - timedelta(days=365),
            importance=0.7,
            last_accessed_at=now - timedelta(days=365),
        )
        # 关闭时两者得分相同(都 = importance = 0.7)
        assert em.forgetting_score(ep_recent) == pytest.approx(0.7)
        assert em.forgetting_score(ep_old) == pytest.approx(0.7)
        assert em.forgetting_score(ep_recent) == em.forgetting_score(ep_old)

    def test_recall_by_semantic_uses_forgetting_when_enabled(self, monkeypatch):
        # FORGETTING_CURVE_ENABLED=True 时,recall_by_semantic 排序按 forgetting_score
        # 测试:两个 episode 同样关键词命中,但近期+高重要性应排前
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "FORGETTING_CURVE_ENABLED", True)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        ep_old_high = _make_episode(
            now - timedelta(days=90),
            importance=0.9,
            last_accessed_at=now - timedelta(days=90),
            episode_id="e1",
        )
        ep_recent_low = _make_episode(
            now,
            importance=0.2,
            last_accessed_at=now,
            episode_id="e2",
        )
        # 都用同关键词,保证 overlap 相同
        for ep in (ep_old_high, ep_recent_low):
            ep.keywords = ["户口", "注销"]
            ep.summary = "户口注销流程"
        em._store["e1"] = ep_old_high
        em._store["e2"] = ep_recent_low
        em._by_session["s1"] = ["e1", "e2"]

        results = em.recall_by_semantic("户口注销", top_k=2)
        assert len(results) == 2
        # recent_low 的 forgetting_score = 0.2 * 1.0 = 0.2
        # old_high 的 forgetting_score = 0.9 * exp(-3) ≈ 0.9 * 0.05 = 0.045
        # 所以 recent_low 应排前
        assert results[0].episode_id == "e2"
