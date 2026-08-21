"""清洗与归一化 —— deep-spec 20 C.3/C.4。

清洗管道：去 HTML 标签/实体 → 去 URL → 去 emoji（可配置保留）→ 统一全半角 → 去除零宽字符 → 空白折叠。
归一化：小写（英文）+ 全半角对齐（供检索管道统一使用）。
"""

from __future__ import annotations

import html
import re

# 停用词表（中英常用词，抽取/检索用）
STOPWORDS: frozenset[str] = frozenset(
    {
        # 中文高频虚词
        "的",
        "了",
        "是",
        "在",
        "我",
        "你",
        "他",
        "她",
        "它",
        "我们",
        "你们",
        "他们",
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
        "没有",
        "不",
        "吗",
        "呢",
        "吧",
        "啊",
        "呀",
        "么",
        "什么",
        "怎么",
        "怎样",
        "为",
        "为了",
        "因为",
        "所以",
        "但是",
        "而且",
        "如果",
        "虽然",
        "一个",
        "这个",
        "那个",
        "这些",
        "那些",
        "一",
        "二",
        "三",
        "四",
        "五",
        "六",
        "七",
        "八",
        "九",
        "十",
        "请",
        "请问",
        "能",
        "可以",
        "想",
        "要",
        "会",
        "对",
        "给",
        "让",
        "把",
        "被",
        "更",
        "最",
        "很",
        "太",
        "非常",
        "比较",
        "还",
        "再",
        "又",
        "已",
        "已经",
        "自己",
        "人家",
        "大家",
        "东西",
        "时候",
        "现在",
        "今天",
        "应该",
        "需要",
        # 英文停用词
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "at",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "his",
        "our",
        "their",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "not",
        "no",
        "so",
        "very",
        "just",
    }
)

_HTML_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+|www\.\S+")
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\U00002b00-\U00002bff]+"
)
_WHITESPACE = re.compile(r"[\s\u3000]+")

# 全角 → 半角（标点 + 数字 + 字母）
_FULL_TO_HALF = {
    0x3000: ord(" "),
    0xFF01: ord("!"),
    0xFF08: ord("("),
    0xFF09: ord(")"),
    0xFF0C: ord(","),
    0xFF0E: ord("."),
    0xFF1A: ord(":"),
    0xFF1B: ord(";"),
    0xFF1F: ord("?"),
    0xFF20: ord("@"),
    0xFF3B: ord("["),
    0xFF3D: ord("]"),
    0xFF5B: ord("{"),
    0xFF5D: ord("}"),
}
_FULL_TO_HALF.update({0xFF10 + i: ord("0") + i for i in range(10)})  # ０-９
_FULL_TO_HALF.update({0xFF21 + i: ord("A") + i for i in range(26)})  # Ａ-Ｚ
_FULL_TO_HALF.update({0xFF41 + i: ord("a") + i for i in range(26)})  # ａ-ｚ


def _full_to_half(text: str) -> str:
    return text.translate(_FULL_TO_HALF)


def clean_text(text: str, remove_html: bool = True, keep_emoji: bool = False) -> str:
    """清洗管道：去 HTML → 去 URL → 解码实体 → 统一全半角 → 去零宽 → 空白折叠。

    Args:
        text: 输入文本
        remove_html: 是否去除 HTML 标签
        keep_emoji: 是否保留 emoji（默认去除）

    Returns:
        清洗后的文本
    """
    if not text:
        return ""
    if remove_html:
        text = _HTML_TAG.sub(" ", text)
    text = _URL.sub(" ", text)
    # stdlib html.unescape 覆盖全部 HTML 实体（含数字/命名实体），替代手写映射表
    text = html.unescape(text)
    text = _full_to_half(text)
    text = _ZERO_WIDTH.sub("", text)
    if not keep_emoji:
        text = _EMOJI.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def normalize_text(text: str, lowercase: bool = True) -> str:
    """归一化：小写（英文）+ 全半角 + 空白折叠。供检索管道统一。"""
    text = clean_text(text)
    if lowercase:
        text = text.lower()
    return text


def remove_stopwords(words: list[str]) -> list[str]:
    """过滤停用词（保留顺序），并去除纯标点/空白 token。"""
    out: list[str] = []
    for w in words:
        w = w.strip()
        if not w:
            continue
        if re.fullmatch(r"[\W_]+", w):
            continue
        if w in STOPWORDS:
            continue
        out.append(w)
    return out


def split_sentences(text: str) -> list[str]:
    """按句末标点切句（中文/英文）。"""
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;])\s*", text)
    return [p.strip() for p in parts if p.strip()]
