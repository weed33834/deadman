"""测试 deadman.knowledge 模块 - P8.3 知识图谱 + RAG 框架。

覆盖点(38 个):
  GraphitiRuntime:
    - test_graphiti_add_episode_returns_id
    - test_graphiti_search_by_keyword
    - test_graphiti_search_bfs_traversal
    - test_graphiti_get_temporal_valid_node
    - test_graphiti_get_temporal_invalid_node
    - test_graphiti_temporal_invalidate_node
    - test_graphiti_persistence_roundtrip

  LightRAGRuntime:
    - test_lightrag_add_and_search
    - test_lightrag_search_topk_limit
    - test_lightrag_search_returns_score

  KnowledgeFreshness:
    - test_freshness_check_fresh
    - test_freshness_check_stale_law
    - test_freshness_check_stale_ai_generated
    - test_freshness_archive_outdated
    - test_freshness_register_source
    - test_freshness_watch_changes

  TrustScorer:
    - test_trust_score_official_law
    - test_trust_score_court_case
    - test_trust_score_user_experience
    - test_trust_score_ai_generated
    - test_trust_score_unverified
    - test_trust_update_with_delta
    - test_trust_aggregate_weighted

  KnowledgeFusion:
    - test_fusion_single_source
    - test_fusion_multi_source_agreement
    - test_fusion_conflict_detection
    - test_fusion_confidence_propagation

  PrivateGraph:
    - test_private_graph_add_and_query
    - test_private_graph_tenant_isolation
    - test_private_graph_user_isolation

  Anonymizer:
    - test_anonymizer_k_anonymity_generalization
    - test_anonymizer_l_diversity_check
    - test_anonymizer_can_share_threshold
    - test_anonymizer_content_redaction

  KnowledgeManager:
    - test_manager_add_knowledge_pii_redaction
    - test_manager_query_end_to_end
    - test_manager_check_freshness_all
    - test_manager_disabled_raises_error

设计:
  - 全部测试在 DEADMAN_KNOWLEDGE_GRAPH_ENABLED=1 下运行
  - 使用 tmp_path 隔离文件,不污染真实数据
  - 不依赖外部 LLM / chromadb / sentence-transformers
"""

from __future__ import annotations

import os
import time

import pytest

# 强制开 feature flag(在 import deadman.knowledge 之前)
os.environ["DEADMAN_DEFENSE_ENABLED"] = "1"
os.environ["DEADMAN_KNOWLEDGE_GRAPH_ENABLED"] = "1"

from deadman.knowledge import (
    Anonymizer,
    Episode,
    ExternalSource,
    FusionResult,
    GraphitiRuntime,
    KGEdge,
    KGNode,
    KnowledgeDisabledError,
    KnowledgeFreshness,
    KnowledgeFusion,
    KnowledgeManager,
    LightRAGRuntime,
    PrivateGraph,
    TenantIsolationError,
    TrustScorer,
    get_knowledge_manager,
    reset_knowledge_manager,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def graphiti(tmp_path):
    return GraphitiRuntime(persist_path=tmp_path / "graphiti.json")


@pytest.fixture
def lightrag(tmp_path):
    return LightRAGRuntime(persist_path=tmp_path / "lightrag.json")


@pytest.fixture
def trust_scorer(tmp_path):
    return TrustScorer(persist_path=tmp_path / "trust.json")


@pytest.fixture
def freshness(tmp_path):
    return KnowledgeFreshness(persist_path=tmp_path / "freshness.json")


@pytest.fixture
def fusion(graphiti, lightrag, trust_scorer):
    return KnowledgeFusion(
        graphiti=graphiti,
        lightrag=lightrag,
        trust_scorer=trust_scorer,
    )


@pytest.fixture
def anonymizer():
    return Anonymizer()


@pytest.fixture
def km(tmp_path):
    """独立的 KnowledgeManager(避免污染单例)。"""
    return KnowledgeManager(persist_root=tmp_path / "km")


# =====================================================================
# GraphitiRuntime tests
# =====================================================================

class TestGraphitiRuntime:
    def test_add_episode_returns_id(self, graphiti):
        """add_episode 应返回字符串 episode id。"""
        ep = Episode(content="北京户口注销流程", source="official_law:cn")
        ep_id = graphiti.add_episode(ep)
        assert isinstance(ep_id, str)
        assert len(ep_id) > 0
        assert graphiti.count_nodes() == 1

    def test_search_by_keyword(self, graphiti):
        """search 应返回 content 含 query 的节点。"""
        graphiti.add_episode(Episode(content="北京户口注销流程", source="official_law:cn"))
        graphiti.add_episode(Episode(content="上海医保政策", source="government_doc:sh"))
        nodes = graphiti.search("北京", max_depth=1)
        assert len(nodes) == 1
        assert "北京" in nodes[0].content

    def test_search_bfs_traversal(self, graphiti):
        """search 应通过 BFS 找到邻居节点。"""
        # n1 -> n2 -> n3
        graphiti.add_node(KGNode(id="n1", content="alpha"))
        graphiti.add_node(KGNode(id="n2", content="beta"))
        graphiti.add_node(KGNode(id="n3", content="gamma"))
        graphiti.add_edge(KGEdge(from_id="n1", to_id="n2", type="related_to"))
        graphiti.add_edge(KGEdge(from_id="n2", to_id="n3", type="related_to"))
        # BFS from n1, max_depth=2 → 应找到 n1, n2, n3
        nodes = graphiti.search("alpha", max_depth=2)
        ids = [n.id for n in nodes]
        assert "n1" in ids
        assert "n2" in ids
        assert "n3" in ids

    def test_get_temporal_valid_node(self, graphiti):
        """get_temporal 应返回当时有效的节点。"""
        ts = time.time() - 100  # 100 秒前
        graphiti.add_node(KGNode(
            id="tn1",
            content="past fact",
            valid_from=ts - 50,
            valid_to=ts + 100,  # 仍然有效
        ))
        result = graphiti.get_temporal("tn1", at_time=ts)
        assert result is not None
        assert result.id == "tn1"

    def test_get_temporal_invalid_node(self, graphiti):
        """get_temporal 应在节点未生效或已失效时返回 None。"""
        ts = time.time()
        graphiti.add_node(KGNode(
            id="tn2",
            content="future fact",
            valid_from=ts + 1000,  # 未来才生效
        ))
        # 查询当前时间 → 节点尚未生效
        result = graphiti.get_temporal("tn2", at_time=ts)
        assert result is None

    def test_temporal_invalidate_node(self, graphiti):
        """invalidate_node 应将节点置为失效(valid_to=now)。"""
        ts = time.time()
        graphiti.add_node(KGNode(id="inv1", content="law v1", valid_from=ts - 10))
        # 置失效
        ok = graphiti.invalidate_node("inv1", at_time=ts)
        assert ok is True
        # 之后查询应失效
        result = graphiti.get_temporal("inv1", at_time=ts + 1)
        assert result is None

    def test_persistence_roundtrip(self, tmp_path):
        """持久化到磁盘后重新加载,节点应保留。"""
        path = tmp_path / "g_persist.json"
        rt1 = GraphitiRuntime(persist_path=path)
        rt1.add_node(KGNode(id="p1", content="persisted content"))
        assert path.exists()
        # 新实例从同一文件加载
        rt2 = GraphitiRuntime(persist_path=path)
        assert rt2.count_nodes() == 1
        node = rt2.get_node("p1")
        assert node is not None
        assert node.content == "persisted content"


# =====================================================================
# LightRAGRuntime tests
# =====================================================================

class TestLightRAGRuntime:
    def test_add_and_search(self, lightrag):
        """add 后 search 应返回相似节点。"""
        lightrag.add(content="北京户口注销流程", source="official_law:cn")
        lightrag.add(content="上海社保政策", source="government_doc:sh")
        results = lightrag.search("北京户口", top_k=2)
        assert len(results) >= 1
        # 相似度最高的应含"北京"
        assert "北京" in results[0].node.content or "户口" in results[0].node.content

    def test_search_topk_limit(self, lightrag):
        """search top_k 应限制返回数。"""
        for i in range(10):
            lightrag.add(content=f"知识条目 {i}", source=f"user:user{i}")
        results = lightrag.search("知识", top_k=3)
        assert len(results) == 3

    def test_search_returns_score(self, lightrag):
        """search 结果应包含相似度分数。"""
        lightrag.add(content="北京户口注销", source="official_law:cn")
        results = lightrag.search("北京户口", top_k=1)
        assert len(results) == 1
        assert isinstance(results[0].score, float)
        assert 0.0 <= results[0].score <= 1.0


# =====================================================================
# KnowledgeFreshness tests
# =====================================================================

class TestKnowledgeFreshness:
    def test_check_fresh(self, freshness):
        """check 应返回 is_stale=False 当未超 TTL。"""
        kid = "node-fresh"
        now = time.time()
        freshness.touch(kid, source="official_law:cn", ts=now)
        report = freshness.check(kid)
        assert report.is_stale is False
        assert report.knowledge_id == kid
        assert report.source == "official_law:cn"

    def test_check_stale_law(self, freshness):
        """law 类别 TTL=365 天,超过应 stale。"""
        kid = "node-stale-law"
        old_ts = time.time() - 400 * 86400  # 400 天前
        freshness.touch(kid, source="official_law:cn", ts=old_ts)
        report = freshness.check(kid)
        assert report.is_stale is True
        assert "law" in report.staleness_reason

    def test_check_stale_ai_generated(self, freshness):
        """ai_generated 类别 TTL=30 天,超过应 stale。"""
        kid = "node-stale-ai"
        old_ts = time.time() - 60 * 86400  # 60 天前
        freshness.touch(kid, source="ai_generated:summary", ts=old_ts)
        report = freshness.check(kid)
        assert report.is_stale is True
        assert "ai_generated" in report.staleness_reason

    def test_archive_outdated(self, freshness):
        """archive_outdated 应返回归档数。"""
        # 1 fresh + 2 stale
        now = time.time()
        freshness.touch("fresh1", source="official_law:cn", ts=now)
        freshness.touch("stale1", source="ai_generated:x", ts=now - 60 * 86400)
        freshness.touch("stale2", source="ai_generated:y", ts=now - 60 * 86400)
        archived = freshness.archive_outdated()
        assert archived == 2
        # 剩余 fresh1
        assert freshness.check("fresh1").is_stale is False

    def test_register_source(self, freshness):
        """register_source 应返回 ExternalSource 并可列出。"""
        src = freshness.register_source(
            name="国家法律法规数据库",
            url="https://flk.npc.gov.cn",
            check_interval=86400,
            parser="parse_law_html",
        )
        assert isinstance(src, ExternalSource)
        assert src.name == "国家法律法规数据库"
        assert src.url == "https://flk.npc.gov.cn"
        # 可列出
        sources = freshness.list_sources()
        assert len(sources) == 1
        assert sources[0].name == "国家法律法规数据库"

    def test_watch_changes(self, freshness):
        """watch_changes 应将 source 加入 watched 集合。"""
        freshness.watch_changes("official_law:cn")
        watched = freshness.get_watched_sources()
        assert "official_law:cn" in watched


# =====================================================================
# TrustScorer tests
# =====================================================================

class TestTrustScorer:
    def test_score_official_law(self, trust_scorer):
        """OFFICIAL_LAW 默认分数 = 0.95。"""
        assert trust_scorer.score("official_law:cn") == 0.95
        assert trust_scorer.score("law:cn") == 0.95

    def test_score_court_case(self, trust_scorer):
        """COURT_CASE 默认分数 = 0.85。"""
        assert trust_scorer.score("court_case:bj-2024-001") == 0.85
        assert trust_scorer.score("case:xyz") == 0.85

    def test_score_government_doc(self, trust_scorer):
        """GOVERNMENT_DOC 默认分数 = 0.90。"""
        assert trust_scorer.score("government_doc:moh") == 0.90
        assert trust_scorer.score("gov:moh") == 0.90

    def test_score_lawyer_verified(self, trust_scorer):
        """LAWYER_VERIFIED 默认分数 = 0.80。"""
        assert trust_scorer.score("lawyer_verified:zhang") == 0.80
        assert trust_scorer.score("lawyer:zhang") == 0.80

    def test_score_user_experience(self, trust_scorer):
        """USER_EXPERIENCE 默认分数 = 0.50。"""
        assert trust_scorer.score("user_experience:u1") == 0.50
        assert trust_scorer.score("user:u1") == 0.50

    def test_score_ai_generated(self, trust_scorer):
        """AI_GENERATED 默认分数 = 0.40。"""
        assert trust_scorer.score("ai_generated:summary") == 0.40
        assert trust_scorer.score("ai:summary") == 0.40

    def test_score_unverified(self, trust_scorer):
        """UNVERIFIED 默认分数 = 0.20。"""
        # 未知前缀
        assert trust_scorer.score("random_source:xxx") == 0.20
        # 空字符串
        assert trust_scorer.score("") == 0.20

    def test_update_with_delta(self, trust_scorer):
        """update 调整后 score 应变化,并记录历史。"""
        # 起始:official_law 默认 0.95
        new_score = trust_scorer.update("official_law:test", delta=-0.3, reason="用户反馈错误")
        assert 0.65 - 0.001 <= new_score <= 0.65 + 0.001
        # 再次查询应返回调整后分数
        assert trust_scorer.score("official_law:test") == pytest.approx(0.65, abs=0.001)
        # history 有记录
        record = trust_scorer.get_record("official_law:test")
        assert record is not None
        assert len(record.history) == 1
        assert record.history[0]["reason"] == "用户反馈错误"

    def test_aggregate_weighted(self, trust_scorer):
        """aggregate 加权聚合,空列表 = 0,单元素 = 自身。"""
        assert trust_scorer.aggregate([]) == 0.0
        assert trust_scorer.aggregate([0.5]) == 0.5
        # 多元素加权聚合应在 min/max 之间
        agg = trust_scorer.aggregate([0.95, 0.85, 0.50])
        assert 0.50 <= agg <= 0.95
        # 边界值
        assert trust_scorer.aggregate([0.0, 1.0]) == pytest.approx(0.5, abs=0.01)


# =====================================================================
# KnowledgeFusion tests
# =====================================================================

class TestKnowledgeFusion:
    def test_single_source(self, fusion, graphiti):
        """单源融合:仅 graphiti 有候选 → 结果含 1 个 contributing source。"""
        graphiti.add_episode(Episode(
            content="北京户口注销流程:1. 死亡证明 2. 户口本",
            source="official_law:cn",
        ))
        result = fusion.fuse("北京户口")
        assert isinstance(result, FusionResult)
        assert "official_law:cn" in result.contributing_sources
        assert result.confidence > 0.0
        assert len(result.nodes) >= 1

    def test_multi_source_agreement(self, fusion, graphiti, lightrag):
        """多源一致:graphiti + lightrag 都有候选,confidence 应聚合。"""
        # 两源都加同样的内容
        graphiti.add_episode(Episode(
            content="社保政策:全国统一",
            source="official_law:cn",
        ))
        lightrag.add(content="社保政策:全国统一", source="official_law:other")
        result = fusion.fuse("社保政策")
        # 两源都贡献
        assert len(result.contributing_sources) >= 1
        assert result.confidence > 0.0
        assert len(result.nodes) >= 1

    def test_conflict_detection(self, fusion, graphiti, lightrag):
        """冲突检测:不同类别来源给出不同内容 → conflicts 非空。"""
        # 官方法律说 A(含 "事项办理时间" 关键词)
        graphiti.add_episode(Episode(
            content="事项办理时间法律条文 A:需 30 天",
            source="official_law:cn",
        ))
        # 用户经验说 B(完全不同内容,含相同关键词)
        lightrag.add(content="事项办理时间用户实际操作 60 天才完成", source="user_experience:u1")
        result = fusion.fuse("事项办理时间")
        # 不同类别来源 → 应触发冲突
        assert len(result.conflicts) >= 1
        assert result.conflicts[0].fact == "content_disagreement"

    def test_confidence_propagation(self, fusion, graphiti):
        """置信度传播:仅高信任来源时 confidence 应较高。"""
        graphiti.add_episode(Episode(
            content="权威法律条文内容",
            source="official_law:cn",
        ))
        result = fusion.fuse("权威法律")
        # 单源且高信任 → confidence 应接近 0.95
        assert result.confidence >= 0.5


# =====================================================================
# PrivateGraph tests
# =====================================================================

class TestPrivateGraph:
    def test_add_and_query(self, tmp_path):
        """私有图:可添加并按关键词查询。"""
        pg = PrivateGraph(
            tenant_id="t1",
            user_id="u1",
            persist_root=tmp_path / "private",
        )
        pg.add_node(KGNode(id="n1", content="我的私有知识:上海社保"))
        results = pg.query("上海")
        assert len(results) == 1
        assert results[0].id == "n1"
        assert "上海" in results[0].content
        # count
        assert pg.count() == 1

    def test_tenant_isolation(self, tmp_path):
        """跨租户查询应抛 TenantIsolationError。"""
        pg = PrivateGraph(
            tenant_id="t1",
            user_id="u1",
            persist_root=tmp_path / "private",
        )
        pg.add_node(KGNode(id="n1", content="tenant1 user1 私有知识"))
        # 试图跨租户查询
        with pytest.raises(TenantIsolationError):
            pg.cross_tenant_query("t2", "anything")

    def test_user_isolation(self, tmp_path):
        """跨用户(同租户)查询应抛 TenantIsolationError。"""
        pg = PrivateGraph(
            tenant_id="t1",
            user_id="u1",
            persist_root=tmp_path / "private",
        )
        with pytest.raises(TenantIsolationError):
            pg.cross_user_query("u2", "anything")

    def test_user_query_does_not_leak_other_users(self, tmp_path):
        """同租户不同用户的私有图应隔离存储。"""
        pg1 = PrivateGraph(
            tenant_id="t1",
            user_id="u1",
            persist_root=tmp_path / "private",
        )
        pg1.add_node(KGNode(id="u1-n1", content="user1 私有知识"))
        pg2 = PrivateGraph(
            tenant_id="t1",
            user_id="u2",
            persist_root=tmp_path / "private",
        )
        pg2.add_node(KGNode(id="u2-n1", content="user2 私有知识"))
        # u1 的图查询不应返回 u2 的节点
        u1_results = pg1.query("私有")
        assert all(n.properties.get("_user_id") == "u1" for n in u1_results)
        assert len(u1_results) == 1


# =====================================================================
# Anonymizer tests
# =====================================================================

class TestAnonymizer:
    def test_k_anonymity_generalization(self, anonymizer):
        """k-匿名:准标识符(如 location)应被泛化。"""
        node = KGNode(
            id="a1",
            content="某人的继承经验",
            properties={
                "location": "北京市朝阳区",
                "age": 35,
                "birthdate": "1989-05-20",
                "name": "张三",
            },
        )
        result = anonymizer.anonymize(node, k=5, l=2)
        # 原节点不应被修改
        assert node.properties["location"] == "北京市朝阳区"
        # 泛化后节点应被处理
        assert result.node.properties["location"] != "北京市朝阳区"
        # 应记录泛化规则
        assert len(result.generalizations) >= 1
        # age 应转为 range
        assert "30-39" in str(result.node.properties.get("age", ""))
        # birthdate 应转为 YYYY-MM
        bd = result.node.properties.get("birthdate", "")
        assert "1989-05" in bd or "1989-05" == bd

    def test_l_diversity_check(self, anonymizer):
        """l-多样性:同等价类节点敏感属性应有 >= l 个不同值。"""
        # 构造 3 个节点,2 个不同敏感属性值
        nodes = [
            KGNode(id="d1", content="x", properties={"income": "low"}),
            KGNode(id="d2", content="x", properties={"income": "high"}),
            KGNode(id="d3", content="x", properties={"income": "low"}),
        ]
        # l=2 → 应通过(2 个不同值)
        assert Anonymizer.check_l_diversity(nodes, l=2) is True
        # l=3 → 应失败(只有 2 个不同值)
        assert Anonymizer.check_l_diversity(nodes, l=3) is False

    def test_can_share_threshold(self, anonymizer):
        """can_share:other_users_count < k 应返回 False。"""
        node = KGNode(id="s1", content="test", properties={
            "_anonymized": True,
            "income": "low",
        })
        # other_users_count=3 < k=5 → False
        assert anonymizer.can_share(node, other_users_count=3, k=5, l=1) is False
        # other_users_count=10 >= k=5 且 _anonymized → True(若 l=1)
        assert anonymizer.can_share(node, other_users_count=10, k=5, l=1) is True

    def test_can_share_requires_anonymized(self, anonymizer):
        """can_share:未匿名化的节点应返回 False。"""
        node = KGNode(id="s2", content="test", properties={})
        # _anonymized 未设置 → False
        assert anonymizer.can_share(node, other_users_count=100, k=5, l=1) is False

    def test_content_redaction(self, anonymizer):
        """content 内嵌的 PII(身份证 / 手机号 / email)应被脱敏。"""
        node = KGNode(
            id="r1",
            content="用户 13800138000 联系邮箱 test@example.com, 身份证 11010119900307234X",
            properties={"location": "北京市"},
        )
        result = anonymizer.anonymize(node, k=5, l=2)
        assert "[REDACTED-PHONE]" in result.node.content
        assert "[REDACTED-EMAIL]" in result.node.content
        assert "[REDACTED-ID]" in result.node.content
        # 原 content 不应被修改
        assert "13800138000" in node.content


# =====================================================================
# KnowledgeManager tests
# =====================================================================

class TestKnowledgeManager:
    def test_add_knowledge_pii_redaction(self, km):
        """add_knowledge 应在存储前做 PII 脱敏。"""
        # 输入含手机号
        kid = km.add_knowledge(
            content="联系 13800138000 咨询户口注销",
            source="user_experience:u1",
            tenant_id="t1",
        )
        assert kid.startswith("k-")
        # graphiti / lightrag 中存储的 content 应已脱敏
        kg_nodes = km.graphiti.all_nodes()
        assert len(kg_nodes) >= 1
        for n in kg_nodes:
            # 不应含原始手机号
            assert "13800138000" not in n.content
            assert "[REDACTED" in n.content or "138****8000" in n.content or "13800138000" not in n.content

    def test_query_end_to_end(self, km):
        """端到端 query 应返回 FusionResult。"""
        km.add_knowledge(
            content="北京户口注销流程的第一步是取得死亡证明",
            source="official_law:cn",
            tenant_id="t1",
        )
        result = km.query("北京户口注销", user_id="u1", tenant_id="t1")
        assert isinstance(result, FusionResult)
        assert result.answer  # 非空
        assert len(result.contributing_sources) >= 1
        assert result.confidence > 0.0

    def test_check_freshness_all(self, km):
        """check_freshness_all 应返回所有知识时效报告。"""
        km.add_knowledge(
            content="社保政策 A",
            source="official_law:cn",
            tenant_id="t1",
        )
        reports = km.check_freshness_all()
        assert len(reports) >= 1
        # 新添加的应为 fresh
        assert any(not r.is_stale for r in reports)

    def test_disabled_raises_error(self, monkeypatch, tmp_path):
        """flag 关闭时 add_knowledge 应抛 KnowledgeDisabledError。"""
        from deadman.knowledge import manager as mod
        monkeypatch.setattr(mod, "is_enabled", lambda name: False)
        km = KnowledgeManager(persist_root=tmp_path / "km_disabled")
        with pytest.raises(KnowledgeDisabledError):
            km.add_knowledge("content", source="official_law:cn")

    def test_disabled_query_raises_error(self, monkeypatch, tmp_path):
        """flag 关闭时 query 应抛 KnowledgeDisabledError。"""
        from deadman.knowledge import manager as mod
        monkeypatch.setattr(mod, "is_enabled", lambda name: False)
        km = KnowledgeManager(persist_root=tmp_path / "km_disabled2")
        with pytest.raises(KnowledgeDisabledError):
            km.query("question")

    def test_private_graph_via_manager(self, km):
        """通过 manager 添加含 user_id 的知识,应同步写入私有图。"""
        km.add_knowledge(
            content="我的私有操作经验",
            source="user_experience:u1",
            tenant_id="t1",
            user_id="u1",
        )
        pg = km.get_private_graph("t1", "u1")
        results = pg.query("私有")
        assert len(results) >= 1

    def test_singleton_get_knowledge_manager(self):
        """get_knowledge_manager 应返回单例。"""
        reset_knowledge_manager()
        km1 = get_knowledge_manager()
        km2 = get_knowledge_manager()
        assert km1 is km2
        reset_knowledge_manager()


# =====================================================================
# Disabled state (module-level) tests
# =====================================================================

class TestDisabledState:
    def test_graphiti_returns_empty_when_disabled(self, monkeypatch, tmp_path):
        """flag 关闭时 graphiti.search 返回空列表。"""
        from deadman.knowledge import graphiti_runtime as mod
        monkeypatch.setattr(mod, "is_enabled", lambda name: False)
        rt = GraphitiRuntime(persist_path=tmp_path / "g_disabled.json")
        # add_episode 返回 ID 但不实际入库
        ep_id = rt.add_episode(Episode(content="test", source="s"))
        assert isinstance(ep_id, str)
        # search 返回空
        assert rt.search("test") == []
        # get_temporal 返回 None
        assert rt.get_temporal("any") is None

    def test_lightrag_returns_empty_when_disabled(self, monkeypatch, tmp_path):
        """flag 关闭时 lightrag.search 返回空列表。"""
        from deadman.knowledge import lightrag_runtime as mod
        monkeypatch.setattr(mod, "is_enabled", lambda name: False)
        rt = LightRAGRuntime(persist_path=tmp_path / "l_disabled.json")
        # add 返回 ID 但不实际入库
        nid = rt.add(content="test", source="s")
        assert isinstance(nid, str)
        # search 返回空
        assert rt.search("test") == []

    def test_freshness_check_when_disabled(self, monkeypatch, tmp_path):
        """flag 关闭时 freshness.check 返回非 stale 报告。"""
        from deadman.knowledge import freshness as mod
        monkeypatch.setattr(mod, "is_enabled", lambda name: False)
        fr = KnowledgeFreshness(persist_path=tmp_path / "f_disabled.json")
        report = fr.check("any-id")
        assert report.is_stale is False
        assert "disabled" in report.staleness_reason

    def test_fusion_returns_empty_when_disabled(self, monkeypatch, tmp_path):
        """flag 关闭时 fusion.fuse 返回空结果。"""
        from deadman.knowledge import fusion as mod
        monkeypatch.setattr(mod, "is_enabled", lambda name: False)
        f = KnowledgeFusion(
            graphiti=GraphitiRuntime(persist_path=tmp_path / "g.json"),
            lightrag=LightRAGRuntime(persist_path=tmp_path / "l.json"),
        )
        result = f.fuse("any query")
        assert result.answer == ""
        assert result.confidence == 0.0
