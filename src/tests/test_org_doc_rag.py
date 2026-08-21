"""机构文档 RAG 闭环测试（上传→分块索引→机构内检索）。"""

from __future__ import annotations

import pytest

from deadman.research.org_doc_rag import DocChunk, OrgDocRag, _chunk_text


@pytest.fixture()
def rag(tmp_path) -> OrgDocRag:
    return OrgDocRag(root=tmp_path)


def test_chunk_text_splits_by_sentence_and_bounds():
    chunks = _chunk_text("第一句。第二句。第三句。")
    assert len(chunks) >= 1
    assert "".join(chunks) == "第一句。第二句。第三句。"
    # 超长内容硬切
    long = "字" * 2000
    assert all(len(c) <= 600 for c in _chunk_text(long))


def test_index_document_chunks_and_overwrites(rag: OrgDocRag):
    n1 = rag.index_document(
        "orgA", "d1", "标题", "北京死亡证明需到派出所开具。火化证明需到殡仪馆。"
    )
    assert n1 >= 1
    assert rag.doc_count("orgA") == 1
    # 重复索引覆盖
    rag.index_document("orgA", "d1", "标题", "新内容。")
    assert rag.doc_count("orgA") == 1


def test_query_returns_matching_chunks(rag: OrgDocRag):
    rag.index_document(
        "orgA", "d1", "北京流程", "北京身后事办理需死亡证明、户口注销、火化证明等材料。"
    )
    rag.index_document("orgA", "d2", "上海流程", "上海身后事办理需先到殡仪馆登记。")
    hits = rag.query("orgA", "死亡证明怎么开")
    assert hits and hits[0]["doc_id"] == "d1"
    # 完全无关 → 空
    assert rag.query("orgA", "今天天气真好") == []


def test_query_tenant_isolation(rag: OrgDocRag):
    rag.index_document("orgA", "d1", "A", "死亡证明相关材料说明。")
    rag.index_document("orgB", "d2", "B", "死亡证明相关材料说明。")
    hits_a = rag.query("orgA", "死亡证明")
    hits_b = rag.query("orgB", "死亡证明")
    assert all(h["doc_id"] == "d1" for h in hits_a)
    assert all(h["doc_id"] == "d2" for h in hits_b)


def test_delete_document(rag: OrgDocRag):
    rag.index_document("orgA", "d1", "A", "死亡证明材料。")
    # d2 用词避开查询词的全部单字（含 jieba 缺失时单字分词的退化场景）
    rag.index_document("orgA", "d2", "B", "火化流程与费用标准。")
    assert rag.delete_document("orgA", "d1") is True
    assert rag.doc_count("orgA") == 1
    assert rag.query("orgA", "死亡证明") == []  # 已删除 d1，d2 无重叠字


def test_doc_chunk_to_dict():
    c = DocChunk(chunk_id="c", doc_id="d", title="t", content="x", seq=0)
    d = c.to_dict()
    assert d["doc_id"] == "d" and d["content"] == "x"
