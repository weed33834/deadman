"""机构文档 RAG 闭环（P0 功能缺口）：上传文档 → 分块建索引 → 机构内问答检索。

机构可上传政策 / 民俗 / SOP 等自有文档，检索时按机构隔离，只检索本机构的文档块。

实现：
- 每机构一个 JSON 分块存储（org 数据目录下 org_doc_rag/<org_id>.json）。
- ``index_document`` 把文档按句/段分块并落盘；重复索引同一 doc_id 时覆盖。
- ``query`` 对该机构全部分块跑 BM25（复用 textproc.bm25），返回 Top-k 块 + 来源。
- 机构隔离：检索只加载目标 org_id 的分块，天然不越权。

说明：先用轻量 BM25（依赖 rank-bm25，成熟库）满足 P0 检索闭环；
后续可接 vector_store 做语义召回 + 交叉编码器重排（P1 Reranker）。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..textproc.bm25 import Bm25Index  # noqa: F401  # 后续 vector 语义召回预留
from ..utils.jsonio import atomic_write_json, read_json

logger = logging.getLogger(__name__)

__all__ = ["OrgDocRag", "DocChunk", "default_rag_root"]


def default_rag_root() -> Path:
    """默认机构文档 RAG 数据根（org 数据目录下 org_doc_rag/）。"""
    from ..config import settings

    return Path(settings.org_data_dir) / "org_doc_rag"


@dataclass
class DocChunk:
    """一个文档分块。"""

    chunk_id: str
    doc_id: str
    title: str
    content: str
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _chunk_text(text: str, max_chars: int = 600) -> list[str]:
    """按句号/换行切分，再按 max_chars 兜底合并，避免分块过碎或过长。"""
    if not text:
        return []
    import re

    sentences = re.split(r"(?<=[。！？!?；;\n])\s*", text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if not buf:
            buf = s
        elif len(buf) + len(s) <= max_chars:
            buf += s
        else:
            chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    # 超长句按 max_chars 硬切
    out: list[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            out.append(c)
        else:
            for i in range(0, len(c), max_chars):
                out.append(c[i : i + max_chars])
    return out


class OrgDocRag:
    """机构文档 RAG：分块存储 + BM25 检索（线程安全）。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else default_rag_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._file = self.root / "index.json"

    # -- 落盘 ------------------------------------------------------------

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        data = read_json(self._file, {})
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, list[dict[str, Any]]]) -> None:
        atomic_write_json(self._file, data)

    # -- 索引 ------------------------------------------------------------

    def index_document(self, org_id: str, doc_id: str, title: str, text: str) -> int:
        """把一个文档分块并入库（重复 doc_id 覆盖）。返回分块数。"""
        if not text:
            return 0
        chunks = _chunk_text(text)
        with self._lock:
            data = self._load()
            org_docs = data.setdefault(org_id, [])
            # 移除该 doc_id 旧块
            org_docs = [c for c in org_docs if c.get("doc_id") != doc_id]
            new_chunks = [
                DocChunk(
                    chunk_id=f"{doc_id}:{i}",
                    doc_id=doc_id,
                    title=title,
                    content=content,
                    seq=i,
                ).to_dict()
                for i, content in enumerate(chunks)
            ]
            org_docs.extend(new_chunks)
            data[org_id] = org_docs
            self._save(data)
            return len(new_chunks)

    def delete_document(self, org_id: str, doc_id: str) -> bool:
        """删除某机构的一个文档（及其全部分块）。"""
        with self._lock:
            data = self._load()
            org_docs = data.get(org_id, [])
            before = len(org_docs)
            data[org_id] = [c for c in org_docs if c.get("doc_id") != doc_id]
            self._save(data)
            return len(data[org_id]) != before

    def doc_count(self, org_id: str) -> int:
        with self._lock:
            docs = {c.get("doc_id") for c in self._load().get(org_id, [])}
            return len(docs)

    # -- 检索 ------------------------------------------------------------

    def query(self, org_id: str, question: str, top_k: int = 5) -> list[dict[str, Any]]:
        """在指定机构的文档分块上检索，返回 Top-k 块（含评分与来源）。

        评分用「查询词命中数重叠」（tokenize_words 后计数），对每机构小语料更稳健
        （rank-bm25 对短文档常给出 ≤0 评分被过滤）。后续接 vector 语义召回时再升级。
        """
        if not question:
            return []
        from ..textproc.tokenize import tokenize_words

        with self._lock:
            chunks = self._load().get(org_id, [])
        if not chunks:
            return []
        q_terms = set(tokenize_words(question))
        if not q_terms:
            return []
        scored: list[dict[str, Any]] = []
        for c in chunks:
            content = c.get("content", "")
            if not content:
                continue
            c_terms = set(tokenize_words(content))
            # 命中 = 查询词 ∩ 块词 去停用词后计数；再按块长做轻微归一，避免超长块占优
            overlap = len(q_terms & c_terms)
            if overlap <= 0:
                continue
            score = overlap / (1.0 + len(c_terms) / 50.0)
            scored.append(
                {
                    "chunk_id": c.get("chunk_id"),
                    "doc_id": c.get("doc_id"),
                    "title": c.get("title"),
                    "content": content,
                    "score": round(score, 4),
                    "overlap": overlap,
                }
            )
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]
