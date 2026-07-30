"""测试 deadman.memory.episodic 的 P2.2 TTL + LRU。

覆盖点(6 个):
    - test_ttl_marks_archived: TTL 过期标记 archived
    - test_ttl_physical_delete_after_30_days: 30 天后物理删
    - test_lru_eviction_when_over_limit: 超容量 LRU 淘汰
    - test_recall_updates_last_accessed: 召回更新 last_accessed_at
    - test_ttl_disabled_no_change: feature flag 关闭无 TTL
    - test_archived_not_recalled: archived 不召回
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from deadman.memory.episodic import Episode, EpisodicMemory


def _make_episode(
    eid: str,
    timestamp: datetime,
    session_id: str = "s1",
    importance: float = 0.5,
    last_accessed_at: datetime | None = None,
) -> Episode:
    """构造测试用 Episode"""
    return Episode(
        episode_id=eid,
        session_id=session_id,
        timestamp=timestamp,
        agent="test-agent",
        user_message="用户问题",
        assistant_response="助手回答",
        summary="测试摘要",
        keywords=["测试"],
        last_accessed_at=last_accessed_at or timestamp,
        importance=importance,
    )


# =====================================================================
# 1. TTL 标记 archived
# =====================================================================


class TestTTLMarksArchived:
    def test_ttl_marks_archived(self, monkeypatch):
        # EPISODE_TTL_DAYS=10,episode 时间在 20 天前 → 应被标记 archived
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "EPISODIC_TTL_ENABLED", True)
        monkeypatch.setattr(ep_module, "EPISODE_TTL_DAYS", 10)

        em = EpisodicMemory()
        old_ts = datetime.now(timezone.utc) - timedelta(days=20)
        ep = _make_episode("e1", old_ts)
        em._store["e1"] = ep
        em._by_session["s1"] = ["e1"]

        marked = em._apply_ttl_filter()
        assert marked == 1
        assert em._store["e1"].archived is True
        assert em._store["e1"].archived_at is not None


# =====================================================================
# 2. 30 天后物理删
# =====================================================================


class TestTTLPhysicalDelete:
    def test_ttl_physical_delete_after_30_days(self, monkeypatch):
        # archived + archived_at 在 31 天前 → 物理删
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "EPISODIC_TTL_ENABLED", True)
        monkeypatch.setattr(ep_module, "EPISODE_TTL_DAYS", 10)
        monkeypatch.setattr(ep_module, "EPISODE_ARCHIVE_GRACE_DAYS", 30)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        ep = _make_episode("e1", now - timedelta(days=60))
        ep.archived = True
        ep.archived_at = now - timedelta(days=31)  # 已超 grace 期
        em._store["e1"] = ep
        em._by_session["s1"] = ["e1"]

        em._apply_ttl_filter()
        # 应被物理删除
        assert "e1" not in em._store
        assert "s1" not in em._by_session  # session 索引空了应清理

    def test_archived_within_grace_not_purged(self, monkeypatch):
        # archived 但 archived_at 在 5 天前(< 30 天)→ 不删
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "EPISODIC_TTL_ENABLED", True)
        monkeypatch.setattr(ep_module, "EPISODE_TTL_DAYS", 10)
        monkeypatch.setattr(ep_module, "EPISODE_ARCHIVE_GRACE_DAYS", 30)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        ep = _make_episode("e1", now - timedelta(days=60))
        ep.archived = True
        ep.archived_at = now - timedelta(days=5)
        em._store["e1"] = ep
        em._by_session["s1"] = ["e1"]

        em._apply_ttl_filter()
        assert "e1" in em._store  # 仍在


# =====================================================================
# 3. LRU 淘汰
# =====================================================================


class TestLRUEviction:
    def test_lru_eviction_when_over_limit(self, monkeypatch):
        # EPISODE_MAX_COUNT=3,加 5 个 episode → 应淘汰 2 个最久未访问的
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "EPISODIC_TTL_ENABLED", True)
        monkeypatch.setattr(ep_module, "EPISODE_MAX_COUNT", 3)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        # 5 个 episode,按 last_accessed_at 从远到近
        for i in range(5):
            ts = now - timedelta(days=5 - i)  # e0 最久,e4 最近
            ep = _make_episode(f"e{i}", ts, last_accessed_at=ts)
            em._store[f"e{i}"] = ep
            em._by_session["s1"].append(
                f"e{i}"
            ) if "s1" in em._by_session else em._by_session.setdefault("s1", []).append(f"e{i}")

        evicted = em._apply_lru_eviction()
        assert evicted == 2
        assert len(em._store) == 3
        # 保留的应是 e2/e3/e4(最近访问的三个)
        assert "e2" in em._store
        assert "e3" in em._store
        assert "e4" in em._store
        assert "e0" not in em._store
        assert "e1" not in em._store


# =====================================================================
# 4. recall 更新 last_accessed_at
# =====================================================================


class TestRecallUpdatesLastAccessed:
    def test_recall_updates_last_accessed(self, monkeypatch):
        # EPISODIC_TTL_ENABLED=True 时,recall_recent 应更新 last_accessed_at
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "EPISODIC_TTL_ENABLED", True)

        em = EpisodicMemory()
        old_ts = datetime.now(timezone.utc) - timedelta(days=10)
        ep = _make_episode("e1", old_ts, last_accessed_at=old_ts)
        em._store["e1"] = ep
        em._by_session["s1"] = ["e1"]

        before = em._store["e1"].last_accessed_at
        em.recall_recent("s1", n=5)
        after = em._store["e1"].last_accessed_at
        assert after > before  # 应被更新到更近的时间

    def test_recall_by_semantic_updates_last_accessed(self, monkeypatch):
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "EPISODIC_TTL_ENABLED", True)

        em = EpisodicMemory()
        old_ts = datetime.now(timezone.utc) - timedelta(days=10)
        ep = _make_episode("e1", old_ts, last_accessed_at=old_ts)
        # 用一个有 match 的 keyword
        ep.keywords = ["户口", "注销"]
        ep.summary = "用户问户口注销流程"
        em._store["e1"] = ep
        em._by_session["s1"] = ["e1"]

        before = em._store["e1"].last_accessed_at
        em.recall_by_semantic("户口注销", top_k=5)
        after = em._store["e1"].last_accessed_at
        assert after > before


# =====================================================================
# 5. feature flag 关闭无 TTL
# =====================================================================


class TestTTLDisabled:
    def test_ttl_disabled_no_change(self, monkeypatch):
        # EPISODIC_TTL_ENABLED=False → _apply_ttl_filter 返回 0,无变化
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "EPISODIC_TTL_ENABLED", False)

        em = EpisodicMemory()
        old_ts = datetime.now(timezone.utc) - timedelta(days=365 * 5)
        ep = _make_episode("e1", old_ts)
        em._store["e1"] = ep

        marked = em._apply_ttl_filter()
        evicted = em._apply_lru_eviction()
        assert marked == 0
        assert evicted == 0
        # 未标记 archived
        assert em._store["e1"].archived is False
        # recall 仍能取到
        em._by_session["s1"] = ["e1"]
        recents = em.recall_recent("s1", n=5)
        assert len(recents) == 1
        # recall 不应更新 last_accessed_at(flag 关闭时 _touch_access 不动)
        before = em._store["e1"].last_accessed_at
        em.recall_recent("s1", n=5)
        assert em._store["e1"].last_accessed_at == before


# =====================================================================
# 6. archived 不召回
# =====================================================================


class TestArchivedNotRecalled:
    def test_archived_not_recalled(self, monkeypatch):
        # archived=True 的 episode 不应出现在 recall 结果中
        import deadman.memory.episodic as ep_module

        monkeypatch.setattr(ep_module, "EPISODIC_TTL_ENABLED", True)

        em = EpisodicMemory()
        now = datetime.now(timezone.utc)
        ep_active = _make_episode("e1", now, last_accessed_at=now)
        ep_archived = _make_episode("e2", now, last_accessed_at=now)
        ep_archived.archived = True
        ep_archived.keywords = ["户口", "注销"]
        ep_active.keywords = ["户口", "注销"]
        em._store["e1"] = ep_active
        em._store["e2"] = ep_archived
        em._by_session["s1"] = ["e1", "e2"]

        # recall_recent
        recents = em.recall_recent("s1", n=5)
        assert all(not e.archived for e in recents)
        assert len(recents) == 1
        assert recents[0].episode_id == "e1"

        # recall_by_semantic
        results = em.recall_by_semantic("户口注销", top_k=5)
        assert all(not e.archived for e in results)
        assert len(results) == 1
        assert results[0].episode_id == "e1"
