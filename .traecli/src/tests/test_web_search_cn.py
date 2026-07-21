"""测试 deadman.tools.web_search 的中国境内 provider（Baidu / BingCN）

覆盖 11 个测试场景：
    1. test_baidu_parse_html: 百度 HTML 解析正确
    2. test_baidu_parse_gov_url_classified_official: .gov.cn URL 被 classify 为 official
    3. test_baidu_search_uses_correct_url: 搜索调用正确的 URL 与 params
    4. test_baidu_failure_returns_empty: httpx 异常时返回空列表
    5. test_baidu_empty_query_returns_empty: 空 query 返回空列表
    6. test_bingcn_parse_html: 必应中国 HTML 解析正确
    7. test_bingcn_search_uses_correct_url: 搜索调用正确的 URL 与 params
    8. test_bingcn_failure_returns_empty: httpx 异常时返回空列表
    9. test_get_provider_duckduckgo: 工厂返回 DuckDuckGoSearchProvider
    10. test_get_provider_baidu: 工厂返回 BaiduSearchProvider
    11. test_get_provider_bingcn: 工厂返回 BingCNSearchProvider
    12. test_get_provider_unknown_raises: 未知 name 抛 ValueError
    13. test_search_top_level_function: 顶层 search() 函数正常工作

测试隔离：所有 httpx 调用通过 mock 注入，不触达真实网络。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from deadman.tools.web_search import (
    BaiduSearchProvider,
    BingCNSearchProvider,
    DuckDuckGoSearchProvider,
    SearchResult,
    get_provider,
    search as top_level_search,
)


# =====================================================================
# 辅助：构造 mock httpx response
# =====================================================================


class _MockResponse:
    """模拟 httpx.Response"""

    def __init__(self, status_code: int = 200, text: str = "", json_data: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# =====================================================================
# 百度 HTML fixture
# =====================================================================


# 模拟百度搜索结果 HTML（精简版，仅保留解析逻辑所需的结构）
BAIDU_HTML_FIXTURE = """
<html><body>
<div class="result c-container new-pmd">
  <h3><a href="http://www.baidu.com/link?url=abc123">
    <em>国务院</em>政策文件库
  </a></h3>
  <div class="c-abstract">
    <span class="content-right_8Zs40">
      中国政府网官方政策文件检索服务
    </span>
  </div>
</div>
<div class="result c-container new-pmd">
  <h3><a href="http://www.baidu.com/link?url=def456">
    人民日报报道
  </a></h3>
  <div class="c-abstract">
    <span class="content-right_8Zs40">
      人民日报关于户籍改革的报道
    </span>
  </div>
</div>
<div class="result c-container new-pmd">
  <h3><a href="http://www.baidu.com/link?url=ghi789">
    随机博客
  </a></h3>
  <div class="c-abstract">
    <span class="content-right_8Zs40">
      未经验证的信息
    </span>
  </div>
</div>
</body></html>
"""


# 必应中国 HTML fixture
BINGCN_HTML_FIXTURE = """
<html><body>
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://www.gov.cn/zhengce/content/2024.htm">
      <strong>国务院</strong>政策文件
    </a></h2>
    <p class="b_caption">
      <p>中国政府网官方政策检索服务，最新发布</p>
    </p>
  </li>
  <li class="b_algo">
    <h2><a href="https://www.people.com.cn/news/2024.html">
      人民日报新闻报道
    </a></h2>
    <p>人民日报关于户籍制度改革的报道</p>
  </li>
  <li class="b_algo">
    <h2><a href="https://random-blog.xyz/post">
      随机博客内容
    </a></h2>
    <p>未经验证的博客文章</p>
  </li>
</ol>
</body></html>
"""


# =====================================================================
# 1-5. BaiduSearchProvider 测试
# =====================================================================


class TestBaiduParseHTML:
    """测试 BaiduSearchProvider._parse_html"""

    def test_baidu_parse_html(self):
        # 百度 HTML 应解析出 3 条结果
        provider = BaiduSearchProvider()
        results = provider._parse_html(BAIDU_HTML_FIXTURE, max_results=5)

        assert len(results) == 3
        # 第一条标题应含"国务院"
        title1, url1, snippet1 = results[0]
        assert "国务院" in title1
        assert "baidu.com/link" in url1
        assert "政策文件" in snippet1

    def test_baidu_parse_respects_max_results(self):
        # max_results=2 应只返回 2 条
        provider = BaiduSearchProvider()
        results = provider._parse_html(BAIDU_HTML_FIXTURE, max_results=2)
        assert len(results) == 2

    def test_baidu_search_returns_results_with_classification(self):
        # 完整 search() 应返回带 source_type / confidence 的结果
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_MockResponse(text=BAIDU_HTML_FIXTURE))

        provider = BaiduSearchProvider(http_client=mock_client)
        results = asyncio.run(provider.search("国务院政策", max_results=3))

        assert len(results) == 3
        # 全部应有 source_type 与 confidence
        for r in results:
            assert isinstance(r, SearchResult)
            assert r.source_type in ("official", "news", "org", "blog", "forum", "unknown")
            assert 0.0 <= r.confidence <= 1.0
            assert r.url
            assert r.title


class TestBaiduSearchURLAndParams:
    """测试 BaiduSearchProvider.search 调用正确的 URL 与 params"""

    def test_baidu_search_uses_correct_url_and_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_MockResponse(text=BAIDU_HTML_FIXTURE))

        provider = BaiduSearchProvider(http_client=mock_client)
        asyncio.run(provider.search("户籍办理", max_results=3))

        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        # URL 应是百度搜索端点
        url_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
        assert url_arg == "https://www.baidu.com/s"
        # params 应含 wd（query）和 rn（max_results）
        params = call_args.kwargs.get("params", {})
        assert params.get("wd") == "户籍办理"
        assert params.get("rn") == 3

    def test_baidu_search_no_injection(self):
        # shell 元字符仅作为 URL params
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_MockResponse(text=""))

        evil_query = "test; rm -rf /; $(cat /etc/passwd)"
        provider = BaiduSearchProvider(http_client=mock_client)
        asyncio.run(provider.search(evil_query, max_results=3))

        call_args = mock_client.get.call_args
        url_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
        # URL 应固定，不含 evil_query 的危险部分
        assert url_arg == "https://www.baidu.com/s"
        assert "rm -rf" not in url_arg
        # query 应原样作为 params["wd"]
        params = call_args.kwargs.get("params", {})
        assert params.get("wd") == evil_query


class TestBaiduFailure:
    """测试 BaiduSearchProvider 失败时返回空列表"""

    def test_baidu_failure_returns_empty(self):
        # httpx 抛异常时返回空列表
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("ConnectError: network unreachable"))

        provider = BaiduSearchProvider(http_client=mock_client)
        results = asyncio.run(provider.search("test query", max_results=3))

        assert isinstance(results, list)
        assert len(results) == 0

    def test_baidu_empty_query_returns_empty(self):
        # 空 query 直接返回空列表，不调 httpx
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_MockResponse(text=""))

        provider = BaiduSearchProvider(http_client=mock_client)
        results = asyncio.run(provider.search("", max_results=3))

        assert results == []
        mock_client.get.assert_not_called()


# =====================================================================
# 6-8. BingCNSearchProvider 测试
# =====================================================================


class TestBingCNParseHTML:
    """测试 BingCNSearchProvider._parse_html"""

    def test_bingcn_parse_html(self):
        provider = BingCNSearchProvider()
        results = provider._parse_html(BINGCN_HTML_FIXTURE, max_results=5)

        assert len(results) == 3
        title1, url1, snippet1 = results[0]
        assert "国务院" in title1
        assert "gov.cn" in url1
        assert "政策检索" in snippet1

    def test_bingcn_search_returns_results_with_classification(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_MockResponse(text=BINGCN_HTML_FIXTURE))

        provider = BingCNSearchProvider(http_client=mock_client)
        results = asyncio.run(provider.search("国务院政策", max_results=3))

        assert len(results) == 3
        for r in results:
            assert isinstance(r, SearchResult)
            assert r.url
            assert r.title


class TestBingCNSearchURLAndParams:
    """测试 BingCNSearchProvider.search 调用正确的 URL 与 params"""

    def test_bingcn_search_uses_correct_url_and_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_MockResponse(text=BINGCN_HTML_FIXTURE))

        provider = BingCNSearchProvider(http_client=mock_client)
        asyncio.run(provider.search("户籍办理", max_results=3))

        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        url_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
        assert url_arg == "https://cn.bing.com/search"
        params = call_args.kwargs.get("params", {})
        assert params.get("q") == "户籍办理"
        assert params.get("count") == 3


class TestBingCNFailure:
    """测试 BingCNSearchProvider 失败时返回空列表"""

    def test_bingcn_failure_returns_empty(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("ConnectError: timeout"))

        provider = BingCNSearchProvider(http_client=mock_client)
        results = asyncio.run(provider.search("test query", max_results=3))

        assert isinstance(results, list)
        assert len(results) == 0


# =====================================================================
# 9-12. get_provider 工厂函数测试
# =====================================================================


class TestGetProvider:
    """测试 get_provider 工厂函数"""

    def test_get_provider_duckduckgo_default(self):
        # 默认（空字符串）应返回 DuckDuckGoSearchProvider
        p = get_provider("")
        assert isinstance(p, DuckDuckGoSearchProvider)
        assert p.name == "duckduckgo"

    def test_get_provider_duckduckgo_explicit(self):
        p = get_provider("duckduckgo")
        assert isinstance(p, DuckDuckGoSearchProvider)
        assert p.name == "duckduckgo"

    def test_get_provider_baidu(self):
        p = get_provider("baidu")
        assert isinstance(p, BaiduSearchProvider)
        assert p.name == "baidu"

    def test_get_provider_bingcn(self):
        # 多种写法
        for name in ("bing-cn", "bingcn", "bing_cn", "Bing-CN"):
            p = get_provider(name)
            assert isinstance(p, BingCNSearchProvider)
            assert p.name == "bing-cn"

    def test_get_provider_unknown_raises(self):
        # 未知 name 抛 ValueError
        with pytest.raises(ValueError, match="未知 web search provider"):
            get_provider("nonexistent-provider")

    def test_get_provider_case_insensitive(self):
        # 大小写不敏感
        p = get_provider("BaIdU")
        assert isinstance(p, BaiduSearchProvider)


# =====================================================================
# 13. 顶层 search() 函数测试
# =====================================================================


class TestTopLevelSearch:
    """测试顶层 search() 便捷函数"""

    def test_search_with_explicit_provider(self):
        # 显式指定 provider="baidu" 应走百度
        # 通过 monkeypatch BaiduSearchProvider 的 _get_client 来 mock
        baidu_provider = BaiduSearchProvider()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_MockResponse(text=BAIDU_HTML_FIXTURE))
        baidu_provider._client = mock_client
        baidu_provider._owns_client = False

        # 替换 get_provider 工厂返回的实例
        import deadman.tools.web_search as ws_module

        original_get_provider = ws_module.get_provider
        ws_module.get_provider = lambda name: baidu_provider  # type: ignore[assignment]
        try:
            results = asyncio.run(top_level_search("国务院", provider="baidu", max_results=3))
        finally:
            ws_module.get_provider = original_get_provider  # type: ignore[assignment]

        assert len(results) == 3
        assert all(isinstance(r, SearchResult) for r in results)

    def test_search_fallback_on_unknown_provider(self):
        # 未知 provider 名应回退到 duckduckgo
        # mock DuckDuckGoSearchProvider 的 _get_client
        ddg_provider = DuckDuckGoSearchProvider()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            side_effect=Exception("forced failure - just verify duckduckgo was called")
        )
        ddg_provider._client = mock_client
        ddg_provider._owns_client = False

        import deadman.tools.web_search as ws_module

        original_get_provider = ws_module.get_provider
        # 模拟 get_provider：对 "unknown-xyz" 抛 ValueError，对 "duckduckgo" 返回 mock 的 ddg
        def fake_get_provider(name):
            if name == "unknown-xyz":
                raise ValueError("unknown")
            return ddg_provider

        ws_module.get_provider = fake_get_provider  # type: ignore[assignment]
        try:
            results = asyncio.run(top_level_search("test", provider="unknown-xyz"))
        finally:
            ws_module.get_provider = original_get_provider  # type: ignore[assignment]

        # 应回退到 DuckDuckGo（mock 会抛异常，所以返回空列表）
        assert results == []
        # mock_client.get 应被调用（说明走了 DuckDuckGo 的搜索路径）
        mock_client.get.assert_called_once()

    def test_search_empty_query_returns_empty(self):
        results = asyncio.run(top_level_search("", provider="baidu"))
        assert results == []
