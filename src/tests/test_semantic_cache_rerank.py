"""语义缓存 + 机构文档 RAG 重排测试。"""

from __future__ import annotations

from deadman.utils.semantic_cache import SemanticCache


class TestSemanticCache:
    def test_exact_and_normalized_hit(self):
        c = SemanticCache()
        c.put("北京 身后事  流程", "result")
        # 去空白小写命中
        assert c.get("北京身后事流程") == "result"
        assert c.stats()["hits"] == 1

    def test_similarity_hit(self):
        c = SemanticCache(similarity_threshold=0.6)
        c.put("北京身后事办理需要哪些材料", "R")
        # 近义命中
        assert c.get("北京身后事需要哪些材料") == "R"

    def test_ttl_expiry(self):
        import time as _t

        c = SemanticCache(ttl_seconds=1)
        c.put("q", "v")
        _t.sleep(1.1)
        assert c.get("q") is None

    def test_lru_eviction(self):
        c = SemanticCache(max_entries=2)
        c.put("a", 1)
        c.put("b", 2)
        c.put("c", 3)
        assert c.get("a") is None  # a 被淘汰
        assert c.get("b") == 2


class TestOrgDocRagRerank:
    def test_rerank_prefers_focused_chunk(self, tmp_path):
        from deadman.research.org_doc_rag import OrgDocRag

        rag = OrgDocRag(root=tmp_path)
        # 两个块都含查询词；块2答案集中，块1分散
        rag.index_document(
            "orgA",
            "d1",
            "散",
            "死亡证明需要身份证、户口本、医院档案、派出所记录等多处材料分别说明。",
        )
        rag.index_document(
            "orgA", "d2", "集中", "死亡证明：需到户籍地派出所或医院开具，材料为身份证与户口本。"
        )
        hits = rag.query("orgA", "死亡证明去哪里开具", top_k=5)
        assert hits, "应返回结果"
        assert hits[0]["doc_id"] == "d2", "重排后应优先集中答案块"
        assert "rerank_score" in hits[0]
