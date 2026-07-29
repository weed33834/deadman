"""Web 搜索工具 - 供 MCP / orchestration 调用

借鉴 Hermes Agent (MIT License) 的 web_tools 设计，但按 deadman 身后事场景定位改造：

与 Hermes 差异：
- 不依赖 duckduckgo-search 包（避免重依赖），用 httpx 直连 + 简单 HTML 解析
- 不调用付费 API（Exa / Tavily / Firecrawl / Parallel）
- 每个结果必须标 source_type 和 confidence（retrieval-guardrails 要求）
- 失败返回空列表，不抛异常，不编造结果（integrity-framework 要求）
- query 仅作为 URL params，不拼接到 shell（input-guardrails 要求）

遵守规则文件：
- retrieval-guardrails.md：confidence < 0.5 的结果标记"低可信度，需向官方核实"
- integrity-framework.md：找不到结果返回空 + 提示"未找到，建议打官方热线"
- compliance-framework.md：仅提供信息，不代查
- input-guardrails.md：用户输入仅作为 URL params
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)


def _get_soup(html: str):
    """延迟导入 BeautifulSoup，返回解析后的 soup 对象。

    BeautifulSoup 是 pyproject.toml 声明的正式依赖，但延迟 import
    避免在不使用 web_search 的场景下加载。
    """
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


# =====================================================================
# 数据结构
# =====================================================================


@dataclass
class SearchResult:
    """搜索结果 - 含可信度判定

    confidence 取值参考 retrieval-guardrails.md 的"知识库内容信任等级"：
      - 高可信 (≥0.85): 多个官方源（.gov）确认
      - 中可信 (0.5-0.85): 1 个官方源或多个非官方源一致
      - 低可信 (<0.5): 单一非官方源 / 推断
    """

    title: str
    url: str
    snippet: str
    source_type: str  # official / news / org / blog / forum / unknown
    confidence: float  # 0.0-1.0
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """转 dict 用于 MCP 返回"""
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source_type": self.source_type,
            "confidence": round(self.confidence, 3),
            "retrieved_at": self.retrieved_at.isoformat(),
        }


# =====================================================================
# Provider 抽象
# =====================================================================


class WebSearchProvider(Protocol):
    """搜索 provider 抽象 - 可插拔

    实现方需保证：
    - 失败时不抛异常，返回空列表（integrity-framework）
    - query 仅作为 URL params，不拼接到 shell（input-guardrails）
    - 结果含 source_type 和 confidence（retrieval-guardrails）
    """

    name: str

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """执行搜索，返回结果列表（失败返回空列表）"""
        ...


# =====================================================================
# 已知新闻域名 - 用于 _classify 判定 source_type=news
# =====================================================================

# 国内外主流新闻媒体域名（不冒充官方源，但比 random blog 可信度高）
_KNOWN_NEWS_DOMAINS: frozenset[str] = frozenset(
    {
        # 中国大陆官方/主流媒体
        "people.com.cn",
        "xinhuanet.com",
        "news.cn",
        "chinadaily.com.cn",
        "cctv.com",
        "gov.cn",  # 这个其实算 official，但放这里便于子域名匹配
        # 中国地方门户
        "sina.com.cn",
        "sohu.com",
        "163.com",
        "qq.com",
        "ifeng.com",
        # 国际媒体
        "cnn.com",
        "bbc.com",
        "bbc.co.uk",
        "nytimes.com",
        "washingtonpost.com",
        "reuters.com",
        "apnews.com",
        "bloomberg.com",
        "theguardian.com",
        "wsj.com",
        "ft.com",
        "economist.com",
        # 日本媒体
        "nhk.or.jp",
        "asahi.com",
        "mainichi.jp",
        "yomiuri.co.jp",
        "nikkei.com",
    }
)


# =====================================================================
# DuckDuckGo Provider
# =====================================================================


class DuckDuckGoSearchProvider:
    """DuckDuckGo 搜索 provider - httpx 直连 HTML 解析

    与 Hermes 的 web_tools.py 差异：
    - 不依赖 duckduckgo-search / ddgs 包（避免重依赖）
    - 用 httpx 直连 https://html.duckduckgo.com/html/?q=... 解析 result__a / result__snippet
    - 不调用付费 API
    - 结果必须标 source_type 和 confidence（retrieval-guardrails）

    防注入硬约束（input-guardrails）：
    - query 仅作为 URL params，绝不拼接到 shell
    - URL 编码后由 httpx 透传
    """

    name: str = "duckduckgo"

    # DuckDuckGo HTML 端点（无 JavaScript 版本，便于解析）
    _SEARCH_URL: str = "https://html.duckduckgo.com/html/"
    # 默认请求头（模拟浏览器避免被屏蔽）
    _HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; deadman-bot/1.0; +https://github.com/weed33834/deadman)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(self, http_client: Any | None = None):
        """初始化 - 可注入 http_client 便于测试

        Args:
            http_client: httpx.AsyncClient 实例；为 None 时按需创建
        """
        # httpx 可能未安装，import 延后到首次使用
        self._client: Any = http_client
        self._owns_client: bool = http_client is None  # 标记是否需要自己关闭

    async def _get_client(self) -> Any:
        """获取或创建 httpx.AsyncClient"""
        if self._client is None:
            try:
                import httpx  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "httpx 未安装，无法执行 web search（pip install httpx 后可用）"
                ) from exc
            self._client = httpx.AsyncClient(timeout=10.0, headers=self._HEADERS)
        return self._client

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """执行搜索 - GET https://html.duckduckgo.com/html/?q=...

        解析 result__a 和 result__snippet
        失败返回空列表，不抛异常（integrity-framework：不造假）

        Args:
            query: 搜索查询语句（仅作为 URL params，不拼接到 shell）
            max_results: 最大结果数（默认 5）

        Returns:
            SearchResult 列表，失败返回空列表
        """
        if not query or not query.strip():
            return []

        # 防注入硬约束：query 仅作为 URL params，由 httpx URL 编码
        # 不使用 shell=True / os.system / % 格式化拼接
        try:
            client = await self._get_client()
            # httpx 会自动 URL 编码 params
            response = await client.get(
                self._SEARCH_URL,
                params={"q": query, "kl": "cn-zh"},  # kl=地域
            )
            response.raise_for_status()
            html = response.text
        except Exception as exc:
            # integrity-framework：失败不抛异常，返回空列表 + 日志
            logger.warning("DuckDuckGo 搜索失败（返回空列表）: %s: %s", type(exc).__name__, exc)
            return []

        # 解析 HTML
        try:
            raw_results = self._parse_html(html, max_results)
        except Exception as exc:
            logger.warning("DuckDuckGo HTML 解析失败（返回空列表）: %s: %s", type(exc).__name__, exc)
            return []

        # 对每个结果判定 source_type 和 confidence
        results: list[SearchResult] = []
        for title, url, snippet in raw_results:
            if not url:
                continue
            source_type, confidence = self._classify(url, title)
            results.append(
                SearchResult(
                    title=title or "",
                    url=url,
                    snippet=snippet or "",
                    source_type=source_type,
                    confidence=confidence,
                )
            )
        return results

    def _parse_html(self, html: str, max_results: int) -> list[tuple[str, str, str]]:
        """解析 DuckDuckGo HTML 结果页 - 返回 (title, url, snippet) 列表

        使用 BeautifulSoup 解析（替代手写正则），更稳健地处理 HTML 结构变化。

        DuckDuckGo html 端点的结构：
          <a class="result__a" href="//duckduckgo.com/l/?uddg=ENCODED_URL">title</a>
          <a class="result__snippet" href="...">snippet</a>

        DuckDuckGo 的链接是跳转链接，真实 URL 在 uddg 参数里。
        """
        results: list[tuple[str, str, str]] = []
        soup = _get_soup(html)

        # 提取所有 result__a 链接（title + url）
        title_tags = soup.find_all("a", class_="result__a")
        # 提取所有 snippet
        snippet_tags = soup.find_all("a", class_="result__snippet")

        # 将 snippet 按文档顺序记录位置（soup 的 find_all 已按文档顺序返回）
        snippets_text: list[str] = [s.get_text(strip=True) for s in snippet_tags]

        # 配对：对每个 title，取同序号的 snippet
        for i, tag in enumerate(title_tags):
            raw_url = tag.get("href", "")
            real_url = self._extract_real_url(raw_url)
            title = tag.get_text(strip=True)
            snippet_text = snippets_text[i] if i < len(snippets_text) else ""
            results.append((title, real_url, snippet_text))
            if len(results) >= max_results:
                break

        return results

    def _extract_real_url(self, raw_url: str) -> str:
        """从 DuckDuckGo 跳转链接提取真实 URL

        DuckDuckGo 的链接形如：
          //duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.gov.cn%2Ftest&rut=...
        或直接是真实 URL（少数情况）
        """
        if not raw_url:
            return ""
        # 补全协议
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url

        # 解析 URL 参数
        try:
            parsed = urlparse(raw_url)
            # 检查是否是 DuckDuckGo 跳转链接
            if "duckduckgo.com" in (parsed.netloc or ""):
                qs = parse_qs(parsed.query)
                uddg_list = qs.get("uddg", [])
                if uddg_list:
                    return unquote(uddg_list[0])
            # 不是跳转链接，原样返回（去掉 &rut= 等跟踪参数）
            if raw_url.startswith("https://duckduckgo.com/l/?"):
                # 兜底解析
                qs = parse_qs(parsed.query)
                if "uddg" in qs:
                    return unquote(qs["uddg"][0])
            return raw_url
        except Exception:
            return raw_url

    def _strip_html(self, html_text: str) -> str:
        """剥离 HTML 标签 + 反转义实体（使用 BeautifulSoup）

        保留方法签名以兼容 BaiduSearchProvider / BingCNSearchProvider 的调用。
        """
        if not html_text:
            return ""
        soup = _get_soup(html_text)
        text = soup.get_text(separator=" ", strip=True)
        # 压缩多余空白
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _classify(self, url: str, title: str) -> tuple[str, float]:
        """判定 source_type 和 confidence

        依据 retrieval-guardrails.md 的信任等级：
        - official (.gov.cn / .gov / .gov.uk) → 0.9
        - official (.edu / .edu.cn) → 0.85
        - news (已知新闻域名) → 0.7
        - org (.org) → 0.6
        - blog (blog / wordpress / medium) → 0.4
        - forum (bbs / forum / zhihu / reddit) → 0.4
        - 其他 → unknown, 0.4

        Args:
            url: 结果 URL
            title: 结果标题（仅作辅助判断）

        Returns:
            (source_type, confidence)
        """
        if not url:
            return ("unknown", 0.4)

        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
        except Exception:
            return ("unknown", 0.4)

        if not host:
            return ("unknown", 0.4)

        # 去掉前导 www.
        if host.startswith("www."):
            host[4:]
        else:
            pass

        # 1. 政府域名 → official, 0.9
        #    .gov.cn / .gov / .gov.uk / .gov.au / *.gov
        if host.endswith(".gov.cn") or host.endswith(".gov") or host.endswith(".gov.uk") or host.endswith(".gov.au"):
            return ("official", 0.9)
        # 兜底 gov 子域名
        if ".gov." in host or host == "gov.cn":
            return ("official", 0.9)

        # 2. 教育域名 → official, 0.85
        if host.endswith(".edu") or host.endswith(".edu.cn") or host.endswith(".edu.tw") or host.endswith(".ac.jp") or host.endswith(".ac.uk"):
            return ("official", 0.85)

        # 3. 已知新闻域名 → news, 0.7
        #    完全匹配或为主域名
        for news_domain in _KNOWN_NEWS_DOMAINS:
            if host == news_domain or host.endswith("." + news_domain):
                return ("news", 0.7)

        # 4. .org → org, 0.6
        if host.endswith(".org"):
            return ("org", 0.6)

        # 5. blog 域名 → blog, 0.4
        blog_indicators = ("blog.", ".blogspot.", "wordpress.com", "medium.com", "substack.com")
        for indicator in blog_indicators:
            if host.startswith(indicator) or host.endswith(indicator) or indicator in host:
                return ("blog", 0.4)

        # 6. forum / 社区 → forum, 0.4
        forum_indicators = ("bbs.", ".bbs.", "forum.", "zhihu.com", "reddit.com", "tieba.baidu.com", "douban.com", "quora.com")
        for indicator in forum_indicators:
            if host.startswith(indicator) or host.endswith(indicator) or indicator in host:
                return ("forum", 0.4)

        # 7. 其他 → unknown, 0.4
        return ("unknown", 0.4)

    async def close(self) -> None:
        """关闭内部 http_client（如果是自己创建的）"""
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.debug("DuckDuckGo http_client 关闭失败: %s", e)
            self._client = None


# =====================================================================
# WebSearchTool - 供 MCP / orchestration 调用
# =====================================================================


class WebSearchTool:
    """Web Search 工具 - 供 MCP / orchestration 调用

    遵守 retrieval-guardrails：
    - confidence < 0.5 的结果标记"低可信度，需向官方核实"
    - 找不到结果返回空列表 + 提示"未找到，建议打官方热线"
    - 不编造结果

    遵守 compliance-framework：
    - 仅提供信息，不代查（不调用任何需要用户身份认证的官方接口）
    """

    # confidence 阈值（低于此值视为低可信度）
    LOW_CONFIDENCE_THRESHOLD: float = 0.5

    def __init__(self, provider: WebSearchProvider | None = None):
        """初始化 - 可注入 provider 便于测试

        Args:
            provider: 搜索 provider；为 None 时用 DuckDuckGoSearchProvider
        """
        # 用 cast 而不是直接赋值，因为 Protocol 不能作为实例类型实例化
        self.provider: WebSearchProvider = provider or DuckDuckGoSearchProvider()

    async def search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        """MCP 工具入口

        返回结构：
            {
                "ok": True,
                "results": [SearchResult.to_dict(), ...],
                "low_confidence_count": N,
                "note": "confidence<0.5 的结果需向官方核实",
                "query": "...",
                "max_results": 5
            }

        失败返回：
            {"ok": False, "error": "...", "results": []}

        Args:
            query: 搜索查询语句
            max_results: 最大结果数
        """
        if not query or not query.strip():
            return {
                "ok": False,
                "error": "query 不能为空",
                "results": [],
                "query": query,
                "max_results": max_results,
            }

        try:
            results = await self.provider.search(query, max_results=max_results)
        except Exception as exc:
            # integrity-framework：失败不抛异常，不编造结果
            logger.warning("WebSearchTool 搜索失败: %s: %s", type(exc).__name__, exc)
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "results": [],
                "note": "搜索失败，建议向官方热线核实（12345 政务服务热线 / 12348 法律援助热线）",
                "query": query,
                "max_results": max_results,
            }

        # 计算 low_confidence_count
        low_confidence_count = sum(
            1 for r in results if r.confidence < self.LOW_CONFIDENCE_THRESHOLD
        )

        # 构造 note（retrieval-guardrails 要求）
        note = self._build_note(results, low_confidence_count)

        return {
            "ok": True,
            "results": [r.to_dict() for r in results],
            "low_confidence_count": low_confidence_count,
            "note": note,
            "query": query,
            "max_results": max_results,
            "provider": getattr(self.provider, "name", "unknown"),
        }

    async def search_official(
        self, query: str, max_results: int = 5, min_confidence: float = 0.85
    ) -> dict[str, Any]:
        """只返回官方源（source_type=official 且 confidence ≥ min_confidence）

        供 web_search_official MCP 工具调用

        Args:
            query: 搜索查询语句
            max_results: 最大结果数
            min_confidence: 最低 confidence 阈值（默认 0.85）
        """
        if not query or not query.strip():
            return {
                "ok": False,
                "error": "query 不能为空",
                "results": [],
                "query": query,
                "max_results": max_results,
            }

        try:
            all_results = await self.provider.search(query, max_results=max_results * 3)
        except Exception as exc:
            logger.warning("WebSearchTool.search_official 失败: %s: %s", type(exc).__name__, exc)
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "results": [],
                "note": "搜索失败，建议向官方热线核实（12345 政务服务热线）",
                "query": query,
                "max_results": max_results,
            }

        # 只保留 official + confidence ≥ min_confidence
        official_results = [
            r
            for r in all_results
            if r.source_type == "official" and r.confidence >= min_confidence
        ][:max_results]

        note = (
            "仅返回官方源（.gov / .edu）；若列表为空，建议打官方热线核实"
            if official_results
            else "未找到官方源，建议打 12345 政务服务热线或当地对应机构核实"
        )

        return {
            "ok": True,
            "results": [r.to_dict() for r in official_results],
            "total_found": len(all_results),
            "official_count": len(official_results),
            "note": note,
            "query": query,
            "max_results": max_results,
            "min_confidence": min_confidence,
            "provider": getattr(self.provider, "name", "unknown"),
        }

    def _build_note(self, results: list[SearchResult], low_confidence_count: int) -> str:
        """构造返回 note（retrieval-guardrails 要求）"""
        if not results:
            # integrity-framework：找不到结果，引导用户打官方热线
            return "未找到搜索结果，建议打官方热线核实（12345 政务服务热线 / 12348 法律援助热线 / 12333 社保咨询）"

        if low_confidence_count == len(results):
            return "全部结果为低可信度（confidence<0.5），需向官方核实后再用于用户引导"
        if low_confidence_count > 0:
            return f"{low_confidence_count} 条结果为低可信度（confidence<0.5），需向官方核实"
        return "结果可信度达标；涉及具体金额/时限/法条时仍建议向官方核实"


# =====================================================================
# BaiduSearchProvider - 中国境内搜索引擎
# =====================================================================


class BaiduSearchProvider:
    """百度搜索 provider - httpx 直连 + 简单 HTML 解析

    中国境内访问 DuckDuckGo 不稳定，百度是稳定备选。

    与 DuckDuckGoSearchProvider 一致的约束：
        - 失败不抛异常，返回空列表（integrity-framework）
        - query 仅作为 URL params，不拼 shell（input-guardrails）
        - 结果含 source_type 和 confidence（retrieval-guardrails）
        - 复用 DuckDuckGoSearchProvider 的 _classify 与 _strip_html（避免重复实现）
    """

    name: str = "baidu"

    # 百度搜索 URL（rn 参数控制每页结果数）
    _SEARCH_URL: str = "https://www.baidu.com/s"
    # 模拟浏览器 UA，避免被百度屏蔽
    _HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(self, http_client: Any | None = None):
        """初始化 - 可注入 http_client 便于测试

        Args:
            http_client: httpx.AsyncClient 实例；为 None 时按需创建
        """
        self._client: Any = http_client
        self._owns_client: bool = http_client is None
        # 复用 DuckDuckGoSearchProvider 的 _classify / _strip_html（共享实现，避免重复）
        self._ddg_helper = DuckDuckGoSearchProvider()

    async def _get_client(self) -> Any:
        """获取或创建 httpx.AsyncClient"""
        if self._client is None:
            try:
                import httpx  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "httpx 未安装，无法执行 web search（pip install httpx 后可用）"
                ) from exc
            self._client = httpx.AsyncClient(timeout=10.0, headers=self._HEADERS)
        return self._client

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """执行百度搜索 - GET https://www.baidu.com/s?wd=...&rn=...

        解析 class="result c-container" 块内的 h3 > a 与 content-right_8Zs40 snippet。

        失败返回空列表，不抛异常（integrity-framework：不造假）。

        Args:
            query: 搜索查询语句（仅作为 URL params，不拼接到 shell）
            max_results: 最大结果数

        Returns:
            SearchResult 列表，失败返回空列表
        """
        if not query or not query.strip():
            return []

        try:
            client = await self._get_client()
            response = await client.get(
                self._SEARCH_URL,
                params={"wd": query, "rn": max_results},
            )
            response.raise_for_status()
            html = response.text
        except Exception as exc:
            logger.warning("Baidu 搜索失败（返回空列表）: %s: %s", type(exc).__name__, exc)
            return []

        try:
            raw_results = self._parse_html(html, max_results)
        except Exception as exc:
            logger.warning("Baidu HTML 解析失败（返回空列表）: %s: %s", type(exc).__name__, exc)
            return []

        results: list[SearchResult] = []
        for title, url, snippet in raw_results:
            if not url:
                continue
            source_type, confidence = self._ddg_helper._classify(url, title)
            results.append(
                SearchResult(
                    title=title or "",
                    url=url,
                    snippet=snippet or "",
                    source_type=source_type,
                    confidence=confidence,
                )
            )
        return results

    def _parse_html(self, html: str, max_results: int) -> list[tuple[str, str, str]]:
        """解析百度 HTML 结果页 - 返回 (title, url, snippet) 列表

        使用 BeautifulSoup 解析（替代手写正则），更稳健地处理百度 HTML 结构变化。

        百度搜索结果结构：
            <div class="result c-container ...">
              <h3><a href="http://www.baidu.com/link?url=...">title</a></h3>
              ...
              <span class="content-right_8Zs40">snippet</span>
              ...
            </div>

        百度的链接是跳转链接 http://www.baidu.com/link?url=...
        为简化处理，先保留原链接（调用方如需展开可后续解析 baidu 跳转）。
        """
        results: list[tuple[str, str, str]] = []
        soup = _get_soup(html)

        # 百度每条结果在 class 含 "result" 和 "c-container" 的 div 里
        for block in soup.find_all("div", class_="c-container"):
            # 块内 h3 > a 链接
            h3 = block.find("h3")
            if not h3:
                continue
            a_tag = h3.find("a")
            if not a_tag:
                continue
            raw_url = a_tag.get("href", "")
            title = a_tag.get_text(strip=True)

            # snippet：百度有多种 snippet class，content-right_8Zs40 是较新版本
            snippet_text = ""
            snippet_tag = block.find(class_=re.compile(r"content-right_"))
            if snippet_tag:
                snippet_text = snippet_tag.get_text(strip=True)

            results.append((title, raw_url, snippet_text))
            if len(results) >= max_results:
                break

        return results

    async def close(self) -> None:
        """关闭内部 http_client（如果是自己创建的）"""
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.debug("Baidu http_client 关闭失败: %s", e)
            self._client = None


# =====================================================================
# BingCNSearchProvider - 必应中国
# =====================================================================


class BingCNSearchProvider:
    """必应中国搜索 provider - httpx 直连 + 简单 HTML 解析

    必应中国（cn.bing.com）是中国境内可稳定访问的搜索引擎之一，
    相比百度 HTML 结构更稳定，作为备选 provider。

    与 DuckDuckGoSearchProvider 一致的约束：
        - 失败不抛异常，返回空列表（integrity-framework）
        - query 仅作为 URL params，不拼 shell（input-guardrails）
        - 结果含 source_type 和 confidence（retrieval-guardrails）
        - 复用 DuckDuckGoSearchProvider 的 _classify 与 _strip_html
    """

    name: str = "bing-cn"

    _SEARCH_URL: str = "https://cn.bing.com/search"
    _HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(self, http_client: Any | None = None):
        """初始化 - 可注入 http_client 便于测试

        Args:
            http_client: httpx.AsyncClient 实例；为 None 时按需创建
        """
        self._client: Any = http_client
        self._owns_client: bool = http_client is None
        self._ddg_helper = DuckDuckGoSearchProvider()

    async def _get_client(self) -> Any:
        """获取或创建 httpx.AsyncClient"""
        if self._client is None:
            try:
                import httpx  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "httpx 未安装，无法执行 web search（pip install httpx 后可用）"
                ) from exc
            self._client = httpx.AsyncClient(timeout=10.0, headers=self._HEADERS)
        return self._client

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """执行必应中国搜索 - GET https://cn.bing.com/search?q=...&count=...

        解析 class="b_algo" 块内的 h2 > a 与 <p> snippet。

        失败返回空列表，不抛异常（integrity-framework：不造假）。

        Args:
            query: 搜索查询语句（仅作为 URL params，不拼接到 shell）
            max_results: 最大结果数

        Returns:
            SearchResult 列表，失败返回空列表
        """
        if not query or not query.strip():
            return []

        try:
            client = await self._get_client()
            response = await client.get(
                self._SEARCH_URL,
                params={"q": query, "count": max_results},
            )
            response.raise_for_status()
            html = response.text
        except Exception as exc:
            logger.warning("BingCN 搜索失败（返回空列表）: %s: %s", type(exc).__name__, exc)
            return []

        try:
            raw_results = self._parse_html(html, max_results)
        except Exception as exc:
            logger.warning("BingCN HTML 解析失败（返回空列表）: %s: %s", type(exc).__name__, exc)
            return []

        results: list[SearchResult] = []
        for title, url, snippet in raw_results:
            if not url:
                continue
            source_type, confidence = self._ddg_helper._classify(url, title)
            results.append(
                SearchResult(
                    title=title or "",
                    url=url,
                    snippet=snippet or "",
                    source_type=source_type,
                    confidence=confidence,
                )
            )
        return results

    def _parse_html(self, html: str, max_results: int) -> list[tuple[str, str, str]]:
        """解析必应中国 HTML 结果页 - 返回 (title, url, snippet) 列表

        使用 BeautifulSoup 解析（替代手写正则），更稳健地处理 Bing HTML 结构变化。

        必应中国搜索结果结构：
            <li class="b_algo">
              <h2><a href="https://example.gov.cn/...">title</a></h2>
              <p>snippet text</p>
              ...
            </li>
        """
        results: list[tuple[str, str, str]] = []
        soup = _get_soup(html)

        # 必应每条结果在 class="b_algo" 的 li 里
        for block in soup.find_all("li", class_="b_algo"):
            # 块内 h2 > a 链接
            h2 = block.find("h2")
            if not h2:
                continue
            a_tag = h2.find("a")
            if not a_tag:
                continue
            raw_url = a_tag.get("href", "")
            title = a_tag.get_text(strip=True)

            # 找块内第一个 <p> 当 snippet
            snippet_text = ""
            p_tag = block.find("p")
            if p_tag:
                snippet_text = p_tag.get_text(strip=True)

            results.append((title, raw_url, snippet_text))
            if len(results) >= max_results:
                break

        return results

    async def close(self) -> None:
        """关闭内部 http_client（如果是自己创建的）"""
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.debug("BingCN http_client 关闭失败: %s", e)
            self._client = None


# =====================================================================
# Provider 工厂函数
# =====================================================================


def get_provider(name: str) -> WebSearchProvider:
    """搜索 provider 工厂函数

    支持：
        - "duckduckgo": DuckDuckGoSearchProvider（默认）
        - "baidu": BaiduSearchProvider（中国境内备选）
        - "bing-cn": BingCNSearchProvider（必应中国备选）

    Args:
        name: provider 名称（不区分大小写）

    Returns:
        WebSearchProvider 实例

    Raises:
        ValueError: 未知 name
    """
    normalized = (name or "").strip().lower()
    if normalized in ("", "duckduckgo"):
        return DuckDuckGoSearchProvider()  # type: ignore[return-value]
    if normalized == "baidu":
        return BaiduSearchProvider()  # type: ignore[return-value]
    if normalized in ("bing-cn", "bingcn", "bing_cn"):
        return BingCNSearchProvider()  # type: ignore[return-value]
    raise ValueError(f"未知 web search provider: {name!r}（支持 duckduckgo/baidu/bing-cn）")


async def search(query: str, provider: str | None = None, max_results: int = 5) -> list[SearchResult]:
    """顶层便捷搜索函数

    根据 settings.web_search_provider 配置选择 provider（若 config 无此字段则 fallback 到 duckduckgo）。

    失败返回空列表（integrity-framework：不抛异常）。

    Args:
        query: 搜索查询语句
        provider: 显式指定 provider 名（覆盖 settings 配置）；None 用 settings 配置
        max_results: 最大结果数

    Returns:
        SearchResult 列表，失败返回空列表
    """
    provider_name: str = provider or ""
    if not provider_name:
        # 从 settings 读取配置；若 config 无此字段则 fallback 到 duckduckgo
        try:
            from ..config import settings

            provider_name = getattr(settings, "web_search_provider", "") or "duckduckgo"
        except Exception:
            provider_name = "duckduckgo"

    try:
        p = get_provider(provider_name)
        return await p.search(query, max_results=max_results)
    except ValueError as exc:
        # 未知 provider 名 → 回退到 duckduckgo
        logger.warning("search: provider 名无效 %s，回退 duckduckgo: %s", provider_name, exc)
        try:
            fallback = get_provider("duckduckgo")
            return await fallback.search(query, max_results=max_results)
        except Exception as fallback_exc:
            logger.warning(
                "search duckduckgo fallback 失败（返回空列表）: %s: %s",
                type(fallback_exc).__name__,
                fallback_exc,
            )
            return []
    except Exception as exc:
        # integrity-framework：失败不抛异常，返回空列表
        logger.warning("search 失败（返回空列表）: %s: %s", type(exc).__name__, exc)
        return []
