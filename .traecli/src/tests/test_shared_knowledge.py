"""测试 deadman.memory.shared_knowledge - P2.4 跨用户匿名知识共享。

覆盖点(5 个):
    - test_add_anonymizes_pii: 添加时 PII 脱敏
    - test_add_without_consent_rejected: 无 consent 拒绝
    - test_query_by_topic: 按 topic 检索
    - test_merge_entries: 合并同主题
    - test_source_user_count_incremented: 样本量累加
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deadman.memory.shared_knowledge import (
    SharedKnowledgeEntry,
    SharedKnowledgeStore,
    SHARED_KNOWLEDGE_ENABLED,
)


@pytest.fixture
def enabled_store(tmp_path: Path, monkeypatch):
    """启用 feature flag + 用 tmp_path 隔离存储"""
    import deadman.memory.shared_knowledge as sk_module

    monkeypatch.setattr(sk_module, "SHARED_KNOWLEDGE_ENABLED", True)
    file_path = tmp_path / "SHARED_KNOWLEDGE.json"
    return SharedKnowledgeStore(file_path=file_path)


# =====================================================================
# 1. PII 脱敏
# =====================================================================

class TestAddAnonymizesPII:
    def test_add_anonymizes_pii(self, enabled_store):
        # user_id 作为 name 字段传入 sanitize_before_store,会被掩码
        result = enabled_store.add(
            user_id="user-001-very-long-id",
            topic="北京户口注销流程",
            content="用户咨询户口注销流程",
            user_consent=True,
        )
        assert result is not None
        # 读回磁盘文件验证 user_id 被掩码
        raw_text = enabled_store.file_path.read_text(encoding="utf-8")
        # 原始 user_id 不应明文出现(name 字段被 sanitize 掩码)
        assert "user-001-very-long-id" not in raw_text
        # 脱敏后的掩码形式应出现(首尾保留 2 字符 + ***)
        assert "***" in raw_text

    def test_user_id_anonymized_in_contributors(self, enabled_store):
        # user_id 作为 name 字段传给 sanitize_before_store,会被掩码
        long_user_id = "user-zhangsan123456"
        enabled_store.add(
            user_id=long_user_id,
            topic="测试主题",
            content="测试内容",
            user_consent=True,
        )
        raw_text = enabled_store.file_path.read_text(encoding="utf-8")
        # 原 user_id 不应明文出现
        assert long_user_id not in raw_text


# =====================================================================
# 2. 无 consent 拒绝
# =====================================================================

class TestAddWithoutConsent:
    def test_add_without_consent_rejected(self, enabled_store):
        result = enabled_store.add(
            user_id="user-x",
            topic="测试主题",
            content="测试内容",
            user_consent=False,
        )
        assert result is None
        # 不应写入文件
        assert not enabled_store.file_path.exists()

    def test_add_with_consent_accepted(self, enabled_store):
        result = enabled_store.add(
            user_id="user-x",
            topic="测试主题",
            content="测试内容",
            user_consent=True,
        )
        assert result is not None


# =====================================================================
# 3. 按 topic 检索
# =====================================================================

class TestQueryByTopic:
    def test_query_by_topic(self, enabled_store):
        enabled_store.add("u1", "北京户口注销", "步骤1:派出所", user_consent=True)
        enabled_store.add("u2", "上海殡仪馆流程", "步骤1:联系殡仪馆", user_consent=True)
        enabled_store.add("u3", "北京户口注销", "步骤2:派出所盖章", user_consent=True)

        results = enabled_store.query("北京户口注销", top_k=5)
        assert len(results) >= 1
        # 全部命中应是北京户口相关
        for r in results:
            assert "北京" in r.topic or "户口" in r.topic

    def test_query_empty_topic(self, enabled_store):
        results = enabled_store.query("", top_k=5)
        assert results == []

    def test_query_no_match(self, enabled_store):
        enabled_store.add("u1", "话题A", "内容A", user_consent=True)
        results = enabled_store.query("完全不相关的话题XYZ", top_k=5)
        assert results == []


# =====================================================================
# 4. 合并同主题
# =====================================================================

class TestMergeEntries:
    def test_merge_entries(self, enabled_store):
        # 同主题多 entry,用长度 > 4 且首尾不同的 user_id 保证掩码后仍可区分
        eid1 = enabled_store.add("alice-001", "继承权", "继承顺序:配偶子女父母", user_consent=True)
        eid2 = enabled_store.add("bob-002", "继承权", "代位继承特殊情形", user_consent=True)
        assert eid1 is not None
        assert eid2 is not None

        merged = enabled_store.merge_entries("继承权")
        assert merged is not None
        # 合并后 content 应包含两条内容
        assert "继承顺序" in merged.content
        assert "代位继承" in merged.content
        # source_user_count 应累加(2 个用户各贡献 1 次 → 2)
        assert merged.source_user_count == 2
        # contributors 应有 2 个用户
        assert len(merged.contributors) == 2

    def test_merge_no_match_returns_none(self, enabled_store):
        enabled_store.add("alice-001", "话题A", "内容A", user_consent=True)
        merged = enabled_store.merge_entries("不存在的话题")
        assert merged is None


# =====================================================================
# 5. source_user_count 累加
# =====================================================================

class TestSourceUserCountIncremented:
    def test_source_user_count_incremented(self, enabled_store):
        # 不同用户贡献同主题 → count 累加
        # 用长度 > 4 且首尾不同的 user_id 保证掩码后仍可区分
        enabled_store.add("alice-001", "经验主题", "经验A", user_consent=True)
        enabled_store.add("bob-002", "经验主题", "经验B", user_consent=True)
        enabled_store.add("carol-003", "经验主题", "经验C", user_consent=True)

        results = enabled_store.query("经验主题", top_k=5)
        assert len(results) >= 1
        # 应有 1 条,且 source_user_count == 3
        assert results[0].source_user_count == 3
        assert len(results[0].contributors) == 3

    def test_same_user_no_count_increment(self, enabled_store):
        # 同一用户多次贡献同主题 → count 不增(只更新 content)
        enabled_store.add("alice-001", "经验主题", "经验A", user_consent=True)
        enabled_store.add("alice-001", "经验主题", "经验B", user_consent=True)

        results = enabled_store.query("经验主题", top_k=5)
        assert len(results) == 1
        assert results[0].source_user_count == 1  # 同 user 不增
        assert len(results[0].contributors) == 1


# =====================================================================
# 6. feature flag 关闭
# =====================================================================

class TestFeatureFlagDisabled:
    def test_disabled_returns_none(self, tmp_path, monkeypatch):
        # feature flag 关闭 → add 返回 None, query 返回空
        import deadman.memory.shared_knowledge as sk_module

        monkeypatch.setattr(sk_module, "SHARED_KNOWLEDGE_ENABLED", False)
        store = SharedKnowledgeStore(file_path=tmp_path / "SK.json")
        assert store.add("u1", "t", "c", user_consent=True) is None
        assert store.query("t") == []
        assert store.count() == 0
