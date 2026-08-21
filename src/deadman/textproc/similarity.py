"""相似度计算 —— deep-spec 20 C.6。

余弦相似度（向量 / 词袋）＋ Jaccard。阈值可配。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable

from .clean import remove_stopwords
from .tokenize import tokenize, tokenize_words


def cosine_similarity(
    vec_a: dict[str, float] | Iterable[float], vec_b: dict[str, float] | Iterable[float]
) -> float:
    """余弦相似度，支持两种输入：
    * dict 词袋（{term: weight}）
    * 数值序列（向量）
    """
    if isinstance(vec_a, dict) and isinstance(vec_b, dict):
        inter = set(vec_a) & set(vec_b)
        dot = sum(vec_a[k] * vec_b[k] for k in inter)
        norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
        norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    else:
        seq_a = list(vec_a)
        seq_b = list(vec_b)
        if len(seq_a) != len(seq_b):
            raise ValueError("向量长度不一致")
        dot = sum(x * y for x, y in zip(seq_a, seq_b, strict=False))
        norm_a = math.sqrt(sum(x * x for x in seq_a))
        norm_b = math.sqrt(sum(y * y for y in seq_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _bow(text: str) -> dict[str, float]:
    words = remove_stopwords(tokenize_words(text))
    cnt = Counter(words)
    total = sum(cnt.values()) or 1
    return {w: c / total for w, c in cnt.items()}


def text_similarity(text_a: str, text_b: str) -> float:
    """词袋余弦相似度（0-1）。"""
    return cosine_similarity(_bow(text_a), _bow(text_b))


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard 集合相似度。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def token_similarity(text_a: str, text_b: str) -> float:
    """token 集合 Jaccard 相似度（与 utils/text_similarity 兼容）。"""
    return jaccard_similarity(tokenize(text_a), tokenize(text_b))
