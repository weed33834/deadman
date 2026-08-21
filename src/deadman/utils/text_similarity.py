"""文本分词与相似度计算 - 全项目共享工具模块。

统一原先散落在 planner / react_loop / convergence_detector /
memory_integrity_verifier / lightrag_runtime 等模块中的重复实现。

提供:
    - tokenize(text) → set[str]: 中英文混合分词（委托 textproc.tokenize，单一实现）
    - jaccard_similarity(a, b) → float: Jaccard 集合相似度
    - text_similarity(a, b) → float: 基于 tokenize + Jaccard 的文本相似度
    - tokenize_for_embedding(text) → list[str]: hash embedding 专用分词
"""

from __future__ import annotations

import re

from ..textproc.tokenize import tokenize  # 复用统一分词实现

__all__ = ["tokenize", "jaccard_similarity", "text_similarity", "tokenize_for_embedding"]


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard 集合相似度: |A∩B| / |A∪B|。空集返回 0。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def text_similarity(text_a: str, text_b: str) -> float:
    """文本相似度: tokenize + Jaccard，0-1。"""
    return jaccard_similarity(tokenize(text_a), tokenize(text_b))


# 停用词表（hash embedding 用，取各模块并集）
_STOPWORDS = {
    "的",
    "了",
    "是",
    "在",
    "我",
    "你",
    "他",
    "她",
    "它",
    "和",
    "与",
    "及",
    "或",
    "也",
    "都",
    "就",
    "这",
    "那",
    "有",
    "没",
    "不",
    "要",
    "会",
    "能",
    "把",
    "被",
    "让",
    "给",
    "对",
    "向",
    "从",
    "到",
    "一个",
    "什么",
    "怎么",
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "i",
    "you",
    "he",
    "she",
    "it",
    "we",
    "they",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "as",
    "that",
    "this",
}


def tokenize_for_embedding(text: str) -> list[str]:
    """分词用于 hash embedding：中英文 token + 中文 2 字滑动窗口。

    与 tokenize() 不同：
    - 返回 list[str]（有序，允许 bigram）
    - 过滤停用词
    - 中文 ≥4 字符段额外生成 2-gram
    """
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", text.lower())
    out: list[str] = []
    for t in tokens:
        if len(t) < 2 or t in _STOPWORDS:
            continue
        out.append(t)
        if len(t) >= 4 and re.match(r"[\u4e00-\u9fff]+", t):
            for i in range(len(t) - 1):
                window = t[i : i + 2]
                if window not in _STOPWORDS:
                    out.append(window)
    return out
