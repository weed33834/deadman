"""关键词提取 —— deep-spec 20 C.2（用户点名必做）。

三层：TF-IDF 基线 → TextRank 增强 → LLM 精修（可选用）。输出 Top-N 带权重。
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from .clean import remove_stopwords
from .tokenize import tokenize_words


def _tfidf_scores(docs: list[list[str]]) -> list[dict[str, float]]:
    """对一批文档（每篇=词列表）计算每篇的 TF-IDF 权重。"""
    n = len(docs)
    if n == 0:
        return []
    df: Counter = Counter()
    for words in docs:
        for w in set(words):
            df[w] += 1
    scores_list: list[dict[str, float]] = []
    for words in docs:
        tf = Counter(words)
        total = sum(tf.values()) or 1
        scores: dict[str, float] = {}
        for w, c in tf.items():
            idf = math.log((n + 1) / (df.get(w, 0) + 1)) + 1.0
            scores[w] = (c / total) * idf
        scores_list.append(scores)
    return scores_list


def _textrank(
    text: str, words: list[str], top_k: int = 10, damping: float = 0.85
) -> dict[str, float]:
    """TextRank：基于词共现构建无向图，迭代 PageRank。

    Args:
        text: 原文本（用于窗口共现）
        words: 已分词停用词后的词列表
        top_k: 候选窗口大小
        damping: 阻尼系数（默认 0.85）

    Returns:
        {词: 权重}
    """
    if not words:
        return {}
    graph: dict[str, set[str]] = {w: set() for w in set(words)}
    window = top_k
    for i, w in enumerate(words):
        for j in range(i + 1, min(i + window, len(words))):
            other = words[j]
            if other != w:
                graph[w].add(other)
                graph[other].add(w)
    scores = dict.fromkeys(graph, 1.0)
    for _ in range(20):
        new_scores: dict[str, float] = {}
        for w, nbrs in graph.items():
            s = 1.0 - damping
            for n in nbrs:
                denom = len(graph.get(n, set())) or 1
                s += damping * (scores[n] / denom)
            new_scores[w] = s
        scores = new_scores
    return scores


def extract_keywords(
    text: str,
    top_n: int = 5,
    use_textrank: bool = True,
    min_len: int = 2,
) -> list[dict[str, Any]]:
    """从单段文本提取关键词。

    Args:
        text: 输入文本
        top_n: 返回数量（默认 5）
        use_textrank: 是否用 TextRank 增强（默认开；关则仅 TF-IDF 自对比）
        min_len: 关键词最短长度（仅 jieba 可用时按真实词过滤；退化单字分词时保留单字）

    Returns:
        [{"word": ..., "weight": ..., "method": "textrank"|"tfidf"}, ...] 按权重降序
    """
    if not text:
        return []
    words = remove_stopwords(tokenize_words(text))
    from .tokenize import jieba_available

    if jieba_available():
        words = [w for w in words if len(w) >= min_len]
    if not words:
        return []

    scores = _tfidf_scores([words])[0]
    weights: dict[str, float] = dict(scores)

    method = "tfidf"
    if use_textrank:
        tr = _textrank(text, words)
        max_tf = max(weights.values()) if weights else 1.0
        max_tr = max(tr.values()) if tr else 1.0
        merged: dict[str, float] = {}
        for w in set(weights) | set(tr):
            tf = (weights.get(w, 0) / max_tf) if max_tf else 0.0
            tr_ = (tr.get(w, 0) / max_tr) if max_tr else 0.0
            merged[w] = 0.5 * tf + 0.5 * tr_
        weights = merged
        method = "textrank+tfidf"

    ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [{"word": w, "weight": round(v, 4), "method": method} for w, v in ranked]
