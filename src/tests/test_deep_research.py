"""Deep Research 深度研究智能体测试。"""

from __future__ import annotations

import pytest

from deadman.research.deep_research import (
    ResearchSource,
    _dedup_sources,
    deep_research,
    gather_sources,
)


class _SR:
    def __init__(self, title, url, confidence=0.7):
        self.title = title
        self.url = url
        self.snippet = "某来源摘要内容"
        self.confidence = confidence
        self.source_type = "web"


def test_dedup_sources_keeps_highest_confidence():
    results = [
        _SR("a", "http://x/1", 0.9),
        _SR("b", "http://x/1", 0.5),  # 同 URL，低置信度
        _SR("c", "http://y/2", 0.7),
    ]
    out = _dedup_sources(results)
    assert len(out) == 2
    by_url = {s.url: s for s in out}
    assert by_url["http://x/1"].confidence == 0.9  # 保留高置信度


@pytest.mark.asyncio
async def test_deep_research_gathers_and_reports(monkeypatch):
    """检索→报告降级聚合流程（LLM 不可用 → degraded）。"""

    async def fake_search(query, max_results=4):
        return [
            _SR("北京身后事指南", "http://a/1", 0.8),
            _SR("民政部身后事", "http://b/2", 0.7),
        ]

    monkeypatch.setattr("deadman.tools.web_search.search", fake_search)
    rep = await deep_research("北京身后事办理材料", max_sources=5)
    assert rep.question == "北京身后事办理材料"
    assert len(rep.sources) == 2
    assert rep.sources[0].url.startswith("http://")
    # LLM 不可用 → 来源聚合降级报告
    assert rep.degraded is True
    assert "http://a/1" in rep.findings
    # 至少给出置信度与警告字段
    assert rep.confidence in ("high", "medium", "low")


@pytest.mark.asyncio
async def test_gather_sources_search_failure_is_graceful(monkeypatch):
    """子查询搜索失败 → 跳过，不抛异常。"""

    async def boom(query, max_results=4):
        raise RuntimeError("search down")

    monkeypatch.setattr("deadman.tools.web_search.search", boom)
    sources = await gather_sources(["q1"], max_sources=5)
    assert sources == []


def test_research_source_to_dict():
    s = ResearchSource(title="t", url="http://x", snippet="s", confidence=0.6)
    d = s.to_dict()
    assert d["title"] == "t" and d["confidence"] == 0.6


@pytest.mark.asyncio
async def test_deep_research_empty_question():
    rep = await deep_research("   ")
    assert rep.findings == ""
    assert rep.warnings
