"""textproc 测试：底层文本处理与检索算法（deep-spec 20 C/D）+ /api/text API"""

from __future__ import annotations

import pytest

from deadman.textproc import (
    clean_text,
    cosine_similarity,
    extract_keywords,
    hybrid_search,
    normalize_text,
    remove_stopwords,
    split_sentences,
    text_similarity,
)
from deadman.textproc.bm25 import Bm25Index


class TestClean:
    def test_clean_removes_html_and_entity(self):
        out = clean_text("<b>你好</b> 世界 &nbsp; ok")
        assert "<b>" not in out and "&nbsp;" not in out and "你好" in out and "ok" in out

    def test_full_to_half(self):
        out = clean_text("全角：ａｂｃ １２３ ！")
        assert "abc" in out and "123" in out and "!" in out

    def test_remove_stopwords(self):
        assert remove_stopwords(["的", "办理", "是", "死亡", "证明"]) == ["办理", "死亡", "证明"]

    def test_split_sentences(self):
        assert split_sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]

    def test_normalize(self):
        assert normalize_text("  ABC 中  文  ") == "abc 中 文"


class TestKeywords:
    def test_extract_keywords_nonempty(self):
        text = "身后事办理流程包括死亡证明、户口注销、遗产继承与债务清偿。医疗导航含医保报销。"
        kws = extract_keywords(text, top_n=5)
        assert kws and len(kws) <= 5
        assert all("word" in k and "weight" in k for k in kws)
        assert [k["weight"] for k in kws] == sorted((k["weight"] for k in kws), reverse=True)

    def test_extract_keywords_empty_input(self):
        assert extract_keywords("") == []


class TestSimilarity:
    def test_cosine_dict(self):
        assert cosine_similarity({"a": 1.0, "b": 1.0}, {"a": 1.0, "b": 1.0}) == pytest.approx(1.0)
        assert cosine_similarity({"a": 1.0}, {"b": 1.0}) == 0.0

    def test_cosine_vector(self):
        assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_text_similarity_same(self):
        assert text_similarity("死亡证明办理", "死亡证明办理") > 0.9


class TestBm25:
    def test_search_returns_ranked(self):
        idx = Bm25Index()
        idx.add("d1", "身后事办理流程与死亡证明")
        idx.add("d2", "医保报销大病救助申请")
        idx.add("d3", "遗嘱与遗产继承债务清偿")
        res = idx.search("死亡证明怎么办", top_k=5)
        assert res and res[0]["id"] == "d1" and res[0]["score"] > 0

    def test_remove_and_empty(self):
        idx = Bm25Index()
        idx.add("d1", "死亡证明办理")
        assert idx.remove("d1") is True and idx.search("死亡") == []
        idx.add("d2", "文本")
        assert idx.search("") == []


class TestHybrid:
    def test_rrf_fusion(self):
        idx = Bm25Index()
        idx.add("d1", "死亡证明办理流程")
        idx.add("d2", "医保报销")
        idx.add("d3", "遗产继承")

        def vec_fn(query, top_k=10):
            return [{"id": "d3", "score": 0.9, "text": ""}, {"id": "d1", "score": 0.6, "text": ""}]

        h = hybrid_search("死亡证明继承", idx, vec_fn, top_k=3)
        assert h["bm25_hits"] >= 1 and h["vector_hits"] == 2 and h["results"][0]["id"] == "d1"


class TestTextAPI:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from deadman.web.routes import text as text_routes

        fresh = FastAPI()
        fresh.include_router(text_routes.router)
        return TestClient(fresh)

    def test_keywords_api(self):
        r = self._client().post(
            "/api/text/keywords",
            json={"text": "身后事办理流程包括死亡证明、户口注销、遗产继承", "top_n": 5},
        )
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["keywords"]

    def test_analyze_api(self):
        d = (
            self._client()
            .post("/api/text/analyze", json={"text": "办理死亡证明需要哪些材料？医保报销流程。"})
            .json()
        )
        assert d["sentence_count"] >= 1 and d["word_count"] >= 1

    def test_status_api(self):
        assert "jieba_available" in self._client().get("/api/text/status").json()
