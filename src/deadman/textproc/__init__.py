"""textproc —— 底层文本处理与检索算法（deep-spec 20 落地）

实现 deep-spec「20-底层基础能力」C（文本处理与算法）与 D（检索算法）的
真实可运行子集，全项目共享：

  * 分词        tokenize.py   —— 中文分词（jieba 可选，缺省退化中英混合分词）
  * 清洗        clean.py      —— 去 HTML / 去 emoji / 全半角归一 / 去零宽字符 / 停用词
  * 关键词提取   keywords.py   —— TF-IDF 基线 + TextRank 增强，输出带权重 Top-N
  * 相似度      similarity.py —— 余弦相似度（支持向量与词袋）＋ Jaccard
  * BM25 检索    bm25.py       —— 关键词侧检索（中文需先分词）
  * 混合检索    hybrid.py     —— BM25 + 向量 RRF 融合，权重可配

设计原则：
  * 零必装依赖（jieba/可选）；纯 Python 兜底，保证离线可测
  * 每个算法独立可测；对外接口统一返回结构化 dict
"""

from .clean import clean_text, normalize_text, remove_stopwords, split_sentences
from .hybrid import hybrid_search
from .keywords import extract_keywords
from .similarity import cosine_similarity, jaccard_similarity, text_similarity
from .tokenize import tokenize, tokenize_words

__all__ = [
    "clean_text",
    "normalize_text",
    "remove_stopwords",
    "split_sentences",
    "extract_keywords",
    "cosine_similarity",
    "jaccard_similarity",
    "text_similarity",
    "tokenize",
    "tokenize_words",
    "hybrid_search",
]
