"""分词 —— 中文分词（jieba 可选，缺省退化中英混合分词）。

deep-spec 20 C.1：中文分词 jieba 级即可，专业领域可挂自定义词典。
"""

from __future__ import annotations

import re

# jieba 可选依赖
try:
    import jieba  # type: ignore

    _HAS_JIEBA = True
except Exception:
    _HAS_JIEBA = False

_EN_WORD = re.compile(r"[a-z0-9]{2,}")
_DIGIT = re.compile(r"\d{3,}")


def tokenize(text: str) -> set[str]:
    """返回去重的 token 集合（中英混合）。

    英文按词(≥2)、中文按单字、3 位以上数字串。与 utils/text_similarity.tokenize 一致，
    供布尔检索/哈希嵌入等需要"集合"的场景使用。
    """
    if not text:
        return set()
    text = text.lower()
    tokens: set[str] = set()
    for m in _EN_WORD.findall(text):
        tokens.add(m)
    for m in _DIGIT.findall(text):
        tokens.add(m)
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            tokens.add(ch)
    return tokens


def tokenize_words(text: str) -> list[str]:
    """返回保序的词序列（供 TF-IDF / BM25 / TextRank 使用）。

    优先 jieba 精确模式分词；jieba 不可用则退化为：
      中文按单字、英文按词(≥2)、数字串。保留顺序与重复。
    """
    if not text:
        return []
    text = text.lower()
    if _HAS_JIEBA:
        return [w.strip() for w in jieba.cut(text) if w.strip()]
    words: list[str] = []
    for m in _EN_WORD.findall(text):
        words.append(m)
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            words.append(ch)
    return words


def jieba_available() -> bool:
    """jieba 是否可用"""
    return _HAS_JIEBA
