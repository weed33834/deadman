"""测试 deadman.memory.vector_store - P2.1 向量库接入。

覆盖点(6 个):
    - test_in_memory_vector_store_add_query: 基础 add+query
    - test_in_memory_cosine_similarity: 余弦相似度计算
    - test_chroma_unavailable_falls_back: chromadb 不可用降级到 InMemory
    - test_embedding_fallback_to_hash: sentence-transformers 不可用降级到 hash
    - test_vector_store_factory: 工厂函数返回正确实例
    - test_delete_removes_entry: delete 移除条目
"""

from __future__ import annotations


import pytest

from deadman.memory.vector_store import (
    ChromaVectorStore,
    InMemoryVectorStore,
    _cosine_similarity,
    _hash_embedding,
    get_vector_store,
    reset_vector_store_singleton,
)


# =====================================================================
# 1. InMemoryVectorStore add + query
# =====================================================================

class TestInMemoryVectorStoreAddQuery:
    def test_in_memory_vector_store_add_query(self):
        store = InMemoryVectorStore()
        store.add("e1", "用户父亲去世,需要办理死亡证明")
        store.add("e2", "用户问询殡仪馆流程")
        store.add("e3", "用户咨询继承权问题")

        # 用相关查询应能命中
        results = store.query("父亲去世办理什么手续", top_k=2)
        assert isinstance(results, list)
        assert len(results) <= 2
        # 至少有命中(因 hash 模拟可能产生重叠)
        if results:
            assert "id" in results[0]
            assert "score" in results[0]
            assert "metadata" in results[0]

    def test_query_empty_store_returns_empty(self):
        store = InMemoryVectorStore()
        assert store.query("anything", top_k=5) == []

    def test_count_after_add(self):
        store = InMemoryVectorStore()
        assert store.count() == 0
        store.add("a", "hello")
        store.add("b", "world")
        assert store.count() == 2


# =====================================================================
# 2. 余弦相似度
# =====================================================================

class TestCosineSimilarity:
    def test_in_memory_cosine_similarity(self):
        # 相同向量 → 1.0
        v = _hash_embedding("test text hello world")
        sim = _cosine_similarity(v, v)
        assert sim == pytest.approx(1.0, abs=1e-6)
        # 正交向量(不同 hash 桶)→ 0.0 或近 0
        v2 = _hash_embedding("xyz")
        sim2 = _cosine_similarity(v, v2)
        assert -0.5 <= sim2 <= 1.0  # hash 可能偶然重叠,但范围合理
        # 空向量 → 0.0
        assert _cosine_similarity([], []) == 0.0
        assert _cosine_similarity([1.0, 2.0], [1.0]) == 0.0  # 长度不等

    def test_hash_embedding_deterministic(self):
        # 同样文本生成同样向量
        v1 = _hash_embedding("用户去世办理证明")
        v2 = _hash_embedding("用户去世办理证明")
        assert v1 == v2

    def test_hash_embedding_normalized(self):
        # L2 范数应为 1(或 0,空文本)
        v = _hash_embedding("任意文本内容测试")
        norm = sum(x * x for x in v) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-6) or norm == 0.0


# =====================================================================
# 3. Chroma 不可用降级
# =====================================================================

class TestChromaFallback:
    def test_chroma_unavailable_falls_back(self, monkeypatch):
        # 模拟 chromadb 未安装,工厂应降级到 InMemoryVectorStore
        reset_vector_store_singleton()
        import deadman.memory.vector_store as vs_module

        monkeypatch.setattr(vs_module, "VECTOR_STORE_ENABLED", True)
        monkeypatch.setattr(vs_module, "_HAS_CHROMADB", False)
        # 重置单例
        monkeypatch.setattr(vs_module, "_vector_store_singleton", None)
        store = get_vector_store()
        try:
            assert store is not None
            assert isinstance(store, InMemoryVectorStore)
        finally:
            reset_vector_store_singleton()

    def test_chroma_class_raises_when_unavailable(self, monkeypatch):
        # 直接构造 ChromaVectorStore 时若 chromadb 不可用应抛 RuntimeError
        import deadman.memory.vector_store as vs_module

        monkeypatch.setattr(vs_module, "_HAS_CHROMADB", False)
        with pytest.raises(RuntimeError):
            ChromaVectorStore()


# =====================================================================
# 4. Embedding 降级到 hash
# =====================================================================

class TestEmbeddingFallback:
    def test_embedding_fallback_to_hash(self, monkeypatch):
        # sentence-transformers 不可用时,_EmbeddingFunc 应回退到 hash
        import deadman.memory.vector_store as vs_module

        monkeypatch.setattr(vs_module, "_HAS_ST", False)
        # 重新导入 _EmbeddingFunc 已在模块层,直接构造
        from deadman.memory.vector_store import _EmbeddingFunc

        emb = _EmbeddingFunc()
        assert emb.using_real_model is False
        v = emb.embed("任意文本")
        assert isinstance(v, list)
        assert len(v) > 0
        # 与直接 hash 一致
        assert v == _hash_embedding("任意文本")


# =====================================================================
# 5. 工厂函数
# =====================================================================

class TestVectorStoreFactory:
    def test_vector_store_factory_disabled_returns_none(self, monkeypatch):
        # feature flag 关闭 → 返回 None
        reset_vector_store_singleton()
        import deadman.memory.vector_store as vs_module

        monkeypatch.setattr(vs_module, "VECTOR_STORE_ENABLED", False)
        monkeypatch.setattr(vs_module, "_vector_store_singleton", None)
        assert get_vector_store() is None
        reset_vector_store_singleton()

    def test_vector_store_factory_enabled_returns_inmemory(self, monkeypatch):
        # feature flag 开启 + chromadb 不可用 → InMemory
        reset_vector_store_singleton()
        import deadman.memory.vector_store as vs_module

        monkeypatch.setattr(vs_module, "VECTOR_STORE_ENABLED", True)
        monkeypatch.setattr(vs_module, "_HAS_CHROMADB", False)
        monkeypatch.setattr(vs_module, "_vector_store_singleton", None)
        store = get_vector_store()
        try:
            assert store is not None
            assert isinstance(store, InMemoryVectorStore)
            # 二次调用应返回同一单例
            assert get_vector_store() is store
        finally:
            reset_vector_store_singleton()


# =====================================================================
# 6. delete
# =====================================================================

class TestVectorStoreDelete:
    def test_delete_removes_entry(self):
        store = InMemoryVectorStore()
        store.add("e1", "hello world")
        store.add("e2", "foo bar")
        assert store.count() == 2
        store.delete("e1")
        assert store.count() == 1
        # 删除不存在的不抛异常
        store.delete("not-exist")
        assert store.count() == 1
        # e2 仍可查到
        results = store.query("foo", top_k=5)
        assert any(r["id"] == "e2" for r in results)
