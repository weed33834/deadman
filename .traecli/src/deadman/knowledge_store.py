"""知识库管理 - 本地加载 + 检索测试 + 新鲜度检查 + 反馈闭环

设计(对应"举一反三"的 Knowledge 领域,核心四件套):
  - 本地知识库: knowledge/**/*.md(地域政策等,含"## 元信息"区块)
  - 检索测试: knowledge-search 测试检索命中(LightRAG 可用时用,否则降级子串匹配)
  - 新鲜度检查: 按 metrics 约定的阈值检测过期文件
      普通文件 6 个月 / 政策类 3 个月 / 法条类 1 年
  - 反馈闭环: 健康状态写 data/knowledge_health.json + metrics 采集
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeFile:
    """知识库文件条目"""

    path: str
    country: str
    region: str
    content: str
    last_updated: str | None = None
    sources: list[str] = field(default_factory=list)
    trust_level: str = "medium"
    mtime: datetime | None = None


def _parse_meta(content: str) -> dict[str, Any]:
    """解析知识库 md 的"## 元信息"区块(与 mcp_server 一致)"""
    meta: dict[str, Any] = {
        "last_updated": None,
        "sources": [],
        "trust_level": "medium",
    }
    m = re.search(r"##\s*元信息\s*\n(.*?)(?=\n##\s|\Z)", content, re.DOTALL)
    if not m:
        return meta
    block = m.group(1)
    date_m = re.search(r"最后更新[::]\s*(\d{4}[-/]\d{2}[-/]\d{2})", block)
    if date_m:
        meta["last_updated"] = date_m.group(1).replace("/", "-")
    src_m = re.search(r"数据来源[::]\s*(.+)", block)
    if src_m:
        meta["sources"] = [s.strip() for s in src_m.group(1).split(",") if s.strip()]
    trust_m = re.search(r"可信度[::]\s*(\w+)", block)
    if trust_m:
        meta["trust_level"] = trust_m.group(1).strip()
    return meta


def load_knowledge_files() -> list[KnowledgeFile]:
    """扫描 knowledge/regions/**/*.md 加载所有知识库文件"""
    regions_dir = settings.knowledge_dir / "regions"
    result: list[KnowledgeFile] = []
    if not regions_dir.exists():
        return result
    for md_file in sorted(regions_dir.rglob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = md_file.relative_to(regions_dir)
        parts = rel.parts
        country = parts[0] if parts else ""
        region = parts[1].removesuffix(".md") if len(parts) > 1 else "overview"
        meta = _parse_meta(content)
        mtime = datetime.fromtimestamp(md_file.stat().st_mtime)
        result.append(
            KnowledgeFile(
                path=str(md_file.relative_to(settings.project_root)),
                country=country,
                region=region,
                content=content,
                last_updated=meta["last_updated"],
                sources=meta["sources"],
                trust_level=meta["trust_level"],
                mtime=mtime,
            )
        )
    return result


def search_knowledge(
    query: str,
    country: str | None = None,
    region: str | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """本地知识库检索(降级实现:子串 + 关键词评分)

    LightRAG 可用时由 mcp_server 的 query_knowledge 走图谱检索;
    此处提供零依赖降级,供 knowledge-search CLI 测试用。

    评分:query 拆词,每命中一个词 +1,标题/地区命中加权。
    """
    files = load_knowledge_files()
    if country:
        files = [f for f in files if f.country.upper() == country.upper()]
    if region:
        files = [f for f in files if region.lower() in f.region.lower()]
    query_terms = [t for t in re.split(r"[\s,，。、]+", query) if len(t) > 1]
    scored: list[dict[str, Any]] = []
    for f in files:
        score = 0
        hits: list[str] = []
        for term in query_terms:
            count = f.content.count(term)
            if count > 0:
                score += count
                hits.append(term)
        if score > 0:
            # 截取命中上下文片段
            snippet = _extract_snippet(f.content, query_terms[0] if query_terms else "", 120)
            scored.append(
                {
                    "path": f.path,
                    "country": f.country,
                    "region": f.region,
                    "score": score,
                    "hits": hits,
                    "last_updated": f.last_updated,
                    "trust_level": f.trust_level,
                    "snippet": snippet,
                }
            )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def _extract_snippet(content: str, term: str, width: int = 120) -> str:
    """提取命中词周围的文本片段"""
    idx = content.find(term)
    if idx == -1:
        return content[:width].replace("\n", " ")
    start = max(0, idx - width // 2)
    end = min(len(content), idx + width // 2)
    return content[start:end].replace("\n", " ").strip()


def check_freshness() -> dict[str, Any]:
    """检查知识库文件新鲜度

    阈值(对齐 Metrics.md):
      - 普通文件: 6 个月
      - 政策类(文件名含 policy/补贴/社保): 3 个月
      - 法条类(文件名含 law/statute/法条): 1 年
    """
    files = load_knowledge_files()
    now = datetime.now()
    stale: list[dict[str, Any]] = []
    for f in files:
        ref = f.mtime or now
        age_days = (now - ref).days
        name_lower = f.path.lower()
        if "law" in name_lower or "statute" in name_lower or "法条" in f.content[:500]:
            threshold_days = 365
            category = "law"
        elif "policy" in name_lower or "补贴" in f.content[:500] or "社保" in f.content[:500]:
            threshold_days = 90
            category = "policy"
        else:
            threshold_days = 180
            category = "general"
        if age_days > threshold_days:
            stale.append(
                {
                    "path": f.path,
                    "age_days": age_days,
                    "threshold_days": threshold_days,
                    "category": category,
                    "last_updated": f.last_updated,
                }
            )
    return {
        "total_files": len(files),
        "stale_count": len(stale),
        "stale_rate": len(stale) / len(files) if files else 0.0,
        "stale_files": stale,
    }
