"""浏览器自动化工具 - Playwright 薄适配层（外部开源拼图）

能力由微软官方开源库 `playwright <https://github.com/microsoft/playwright-python>`_
提供；本模块只做工具化封装：
    - lazy import：未安装 playwright 时返回 ok=False + 安装提示（不阻断进程启动）
    - URL 白名单：仅 http/https（阻断 file:// / 内网协议，input-guardrails）
    - 单动作接口：navigate / get_text / screenshot / click / fill（computer-use 风格）
    - 会话复用：进程级 browser 实例 + 每次调用独立 page，用完即关
    - 全程 headless

Feature flag: DEADMAN_BROWSER_TOOL_ENABLED=0 默认关闭（浏览器属高敏面，
由部署方显式开启）。安装：pip install 'deadman[browser]' && playwright install chromium。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import threading
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

BROWSER_TOOL_ENABLED: bool = os.environ.get("DEADMAN_BROWSER_TOOL_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

#: 单页操作默认超时（秒）
DEFAULT_TIMEOUT_SECONDS = 30
#: 提取文本最大字符数（防 token 爆炸）
MAX_TEXT_CHARS = 50_000
#: 截图最长边像素上限
_MAX_VIEWPORT = {"width": 1280, "height": 720}

_ACTIONS = ("navigate", "get_text", "screenshot", "click", "fill")

_playwright_mod: Any = None
_pw_driver: Any = None  # async_playwright().start() 得到的驱动实例
_probe_done = False


def _probe_playwright() -> tuple[bool, str]:
    """探测 playwright 是否可导入。结果缓存，避免每次调用重复 import 开销。"""
    global _playwright_mod, _probe_done
    if not _probe_done:
        try:
            from playwright.async_api import async_playwright as _pw

            _playwright_mod = _pw
        except ImportError:
            _playwright_mod = None
        _probe_done = True
    if _playwright_mod is None:
        return False, (
            "playwright 未安装。请执行: pip install 'playwright>=1.0' "
            "&& python -m playwright install chromium"
        )
    return True, ""


def _validate_url(url: str) -> str | None:
    """仅允许 http/https；返回错误信息或 None"""
    if not url or not url.strip():
        return "url 不能为空"
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return f"仅支持 http/https URL，收到 scheme={parsed.scheme!r}"
    if not parsed.netloc:
        return "URL 缺少主机名"
    return None


class BrowserSessionPool:
    """进程级浏览器会话池（单例复用 browser，page 用完即弃）"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._browser: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def _get_browser(self) -> Any:
        with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            ok, err = _probe_playwright()
            if not ok:
                raise RuntimeError(err)
            global _pw_driver
            if _pw_driver is None:
                _pw_driver = await _playwright_mod().start()
            pw_browser = await _pw_driver.chromium.launch(headless=True)
            self._browser = pw_browser
            return self._browser

    def discard(self) -> None:
        """丢弃浏览器引用（同步；连接由进程退出回收，测试用 reset_pool）"""
        with self._lock:
            self._browser = None


_pool = BrowserSessionPool()


async def run_browser_action(
    action: str,
    url: str = "",
    selector: str = "",
    text: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """执行单个浏览器动作，返回统一 envelope。

    Args:
        action: navigate | get_text | screenshot | click | fill
        url: navigate 目标（navigate 必填）
        selector: CSS 选择器（click/fill 必填）
        text: fill 的输入内容
        timeout_seconds: 页面操作超时
    """
    # ---------- 入参校验 ----------
    if not BROWSER_TOOL_ENABLED:
        return {
            "ok": False,
            "error": "browser_automation 未启用（DEADMAN_BROWSER_TOOL_ENABLED=1 开启）",
        }
    if action not in _ACTIONS:
        return {"ok": False, "error": f"action 仅支持 {'/'.join(_ACTIONS)}，收到 {action!r}"}
    timeout_ms = max(1_000, min(120, int(timeout_seconds))) * 1000

    page_url = ""
    if action in ("navigate",):
        err = _validate_url(url)
        if err:
            return {"ok": False, "error": err}
        page_url = url.strip()

    # ---------- 执行 ----------
    ok_probe, probe_err = _probe_playwright()
    if not ok_probe:
        return {"ok": False, "error": probe_err}

    try:
        browser = await _pool._get_browser()
        context = await browser.new_context(viewport=_MAX_VIEWPORT)
        page = await context.new_page()
        try:
            page.set_default_timeout(timeout_ms)

            if action == "navigate":
                resp = await page.goto(page_url, wait_until="domcontentloaded")
                return {
                    "ok": True,
                    "action": action,
                    "url": page.url,
                    "status": resp.status if resp else None,
                    "title": await page.title(),
                }

            if action == "get_text":
                body_text = await page.inner_text("body")
                truncated = len(body_text) > MAX_TEXT_CHARS
                return {
                    "ok": True,
                    "action": action,
                    "url": page.url,
                    "title": await page.title(),
                    "text": body_text[:MAX_TEXT_CHARS],
                    "truncated": truncated,
                    "total_chars": len(body_text),
                }

            if action == "screenshot":
                shot = await page.screenshot(type="png")
                return {
                    "ok": True,
                    "action": action,
                    "url": page.url,
                    "format": "png",
                    "bytes": len(shot),
                    "image_base64": base64.b64encode(shot).decode("ascii"),
                }

            if action == "click":
                if not selector:
                    return {"ok": False, "error": "click 需要 selector 参数"}
                await page.click(selector)
                return {"ok": True, "action": action, "url": page.url}

            # fill
            if not selector:
                return {"ok": False, "error": "fill 需要 selector 参数"}
            await page.fill(selector, text)
            return {"ok": True, "action": action, "url": page.url}
        finally:
            await context.close()
    except Exception as exc:
        logger.warning("browser_automation %s 失败: %s: %s", action, type(exc).__name__, exc)
        return {"ok": False, "action": action, "error": f"{type(exc).__name__}: {exc}"}


async def reset_pool() -> None:
    """关闭并重置进程级浏览器实例（运维/测试用）"""
    with _pool._lock:
        browser = _pool._browser
        _pool._browser = None
    if browser is not None:
        with contextlib.suppress(Exception):
            await browser.close()
