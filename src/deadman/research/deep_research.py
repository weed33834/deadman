"""Deep Research 深度研究智能体（P0 功能缺口）。

针对一个研究问题，迭代式检索多源网络信息，做交叉验证，产出带引用的结构化报告。

流水线：
    1. ``decompose_question`` 问题拆解为多个子查询（LLM；无 key 时退化为原问题）。
    2. ``gather_sources`` 逐子查询搜索 + 按 URL 去重 + 截断来源数。
    3. ``cross_verify`` 多源交叉验证（LLM 判定一致性；无 key 时按 confidence 分组）。
    4. ``synthesize_report`` 合成带引用的报告（LLM；无 key 时降级为来源聚合摘要）。

降级原则（对齐项目 zero-invasiveness）：LLM 不可用时不抛异常，输出"来源聚合"
报告并标记 degraded=True，保证功能在无 API key 环境下仍可运行。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 语义缓存：近义问题复用研究结果（省 LLM/检索）
_cache: "SemanticCache | None" = None
_cache_lock = threading.Lock()


def _get_cache() -> "SemanticCache":
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                from ..utils.semantic_cache import SemanticCache

                _cache = SemanticCache(max_entries=128, ttl_seconds=1800, similarity_threshold=0.9)
    return _cache


@dataclass
class ResearchSource:
    """一个研究来源。"""

    title: str
    url: str
    snippet: str = ""
    confidence: float = 0.5  # 0.0-1.0，低可信度需向官方核实
    source_type: str = "web"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchReport:
    """研究结果。"""

    question: str
    findings: str = ""
    sources: list[ResearchSource] = field(default_factory=list)
    sub_queries: list[str] = field(default_factory=list)
    confidence: str = "low"  # high / medium / low
    degraded: bool = False  # LLM 不可用 → 来源聚合降级
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "findings": self.findings,
            "sources": [s.to_dict() for s in self.sources],
            "sub_queries": self.sub_queries,
            "confidence": self.confidence,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


def _llm_available() -> bool:
    from ..llm import llm_client

    return bool(getattr(llm_client, "api_key", None))


async def _llm_text(system: str, user: str, temperature: float = 0.3) -> str | None:
    """调用 LLM 返回纯文本；无 key 或失败返回 None（调用方降级）。"""
    if not _llm_available():
        return None
    from ..llm import llm_client

    try:
        return await llm_client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature,
        )
    except Exception as exc:  # pragma: no cover - 降级路径
        logger.warning("deep_research LLM 调用失败，降级: %s", exc)
        return None


async def decompose_question(question: str, max_sub_queries: int = 4) -> list[str]:
    """把研究问题拆解为多个子查询；LLM 不可用退化为 [原问题]。"""
    if not question:
        return []
    out = await _llm_text(
        "你是研究规划员。把用户研究问题拆解为 {n} 个聚焦的中文子查询，"
        "输出 JSON 字符串数组，不要其他文字。".replace("{n}", str(max_sub_queries)),
        question,
    )
    if out:
        try:
            arr = json.loads(out)
            if isinstance(arr, list):
                cleaned = [str(q).strip() for q in arr if str(q).strip()]
                if cleaned:
                    return cleaned[:max_sub_queries]
        except (json.JSONDecodeError, ValueError):
            logger.debug("拆解输出非 JSON，回退单查询: %r", out[:80])
    return [question]


def _dedup_sources(results: list[Any]) -> list[ResearchSource]:
    """按 URL 去重（保留高 confidence），归一为 ResearchSource。"""
    seen: dict[str, ResearchSource] = {}
    for r in results:
        url = getattr(r, "url", "") or ""
        if not url or url in seen:
            continue
        seen[url] = ResearchSource(
            title=getattr(r, "title", "") or "",
            url=url,
            snippet=getattr(r, "snippet", "") or "",
            confidence=float(getattr(r, "confidence", 0.5) or 0.5),
            source_type=getattr(r, "source_type", "web") or "web",
        )
    return list(seen.values())


async def gather_sources(
    sub_queries: list[str],
    max_sources: int = 8,
    max_results_per_query: int = 4,
) -> list[ResearchSource]:
    """逐子查询搜索并去重、截断。搜索失败不影响整体（跳过该子查询）。"""
    from ..tools.web_search import search

    all_results: list[Any] = []
    for q in sub_queries:
        try:
            found = await search(q, max_results=max_results_per_query)
            all_results.extend(found)
        except Exception as exc:  # pragma: no cover - 单查询失败跳过
            logger.warning("deep_research 子查询失败（跳过）: %s: %s", q, exc)
    return _dedup_sources(all_results)[:max_sources]


async def cross_verify(
    sources: list[ResearchSource],
    question: str,
) -> tuple[str, list[str]]:
    """多源交叉验证：LLM 判定整体可信度；无 LLM 按 sources 平均 confidence 估算。

    Returns:
        (confidence: high|medium|low, warnings: list[str])
    """
    warnings: list[str] = []
    if not sources:
        return "low", ["未检索到可靠来源，建议人工核实或拨打官方热线"]

    if _llm_available():
        src_desc = "\n".join(
            f"- [{i + 1}] {s.title}（{s.url}）：{s.snippet[:80]}" for i, s in enumerate(sources)
        )
        out = await _llm_text(
            "你是事实核查员。根据以下来源，判断对问题的整体可信度（high/medium/low），"
            "并指出明显不一致或需官方核实之处。输出 JSON：{\"confidence\": \"high|medium|low\", \"warnings\": [...]}",
            f"问题：{question}\n来源：\n{src_desc}",
        )
        if out:
            try:
                obj = json.loads(out)
                conf = str(obj.get("confidence", "low")).lower()
                conf = conf if conf in ("high", "medium", "low") else "low"
                w = [str(x) for x in obj.get("warnings", []) if str(x).strip()]
                return conf, w
            except (json.JSONDecodeError, ValueError):
                logger.debug("cross_verify 输出非 JSON，回退估算")

    # 降级：按来源 confidence 平均
    avg = sum(s.confidence for s in sources) / len(sources)
    conf = "high" if avg >= 0.7 else ("medium" if avg >= 0.5 else "low")
    if conf == "low":
        warnings.append("来源可信度偏低，请务必向官方核实后再决策")
    return conf, warnings


async def synthesize_report(
    question: str,
    sources: list[ResearchSource],
    confidence: str,
) -> tuple[str, bool]:
    """合成带引用的研究报告。

    LLM 可用 → 生成结构化、带 [n] 引用的叙述报告。
    LLM 不可用 → 降级为"来源聚合"摘要（列出各来源要点 + 链接），degraded=True。
    """
    if not sources:
        return "未检索到相关来源。建议更换关键词重试，或拨打官方热线核实。", _llm_available()

    if _llm_available():
        src_desc = "\n".join(
            f"[{i + 1}] {s.title}\n   URL: {s.url}\n   摘要: {s.snippet}"
            for i, s in enumerate(sources)
        )
        out = await _llm_text(
            "你是深度研究员。基于给定来源撰写中文研究报告：先一句话概述，"
            "再分点给出关键结论（每条结论末尾标注来源编号 [1][2]...），"
            "最后附'需向官方核实'注意事项。不要编造来源中不存在的事实。",
            f"研究问题：{question}\n可信度：{confidence}\n来源：\n{src_desc}",
            temperature=0.3,
        )
        if out:
            return out, False

    # 降级：来源聚合
    lines = [f"# {question}", "", "（来源聚合摘要 · LLM 未配置，请人工复核）", ""]
    for i, s in enumerate(sources, 1):
        lines.append(f"### [{i}] {s.title}")
        lines.append(f"来源：{s.url}")
        if s.snippet:
            lines.append(f"摘要：{s.snippet}")
        lines.append("")
    return "\n".join(lines), True


async def deep_research(
    question: str,
    max_sources: int = 8,
    max_sub_queries: int = 4,
) -> ResearchReport:
    """深度研究入口：拆解 → 检索 → 交叉验证 → 合成报告。

    语义缓存：近义问题命中直接复用结果（省 LLM/检索）；结果落缓存。
    """
    if not question or not question.strip():
        return ResearchReport(question=question or "", warnings=["问题不能为空"])
    q = question.strip()

    cache = _get_cache()
    cached = cache.get(q)
    if cached is not None:
        cached_report = ResearchReport(**cached) if isinstance(cached, dict) else cached
        cached_report.warnings = [*(cached_report.warnings or []), "(来自语义缓存)"]
        return cached_report

    sub_queries = await decompose_question(q, max_sub_queries=max_sub_queries)
    sources = await gather_sources(sub_queries, max_sources=max_sources)
    confidence, warnings = await cross_verify(sources, q)
    findings, degraded = await synthesize_report(q, sources, confidence)

    report = ResearchReport(
        question=q,
        findings=findings,
        sources=sources,
        sub_queries=sub_queries,
        confidence=confidence,
        degraded=degraded,
        warnings=warnings,
    )
    cache.put(q, report.to_dict())
    return report
