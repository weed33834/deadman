"""LLM 客户端 - 统一封装多厂商 LLM 调用

设计原则：
- 优先使用官方 SDK（openai / anthropic），缺失时降级为 httpx 手写请求
- 支持多厂商 fallback：主 LLM 失败后按 LLM_FALLBACK_CHAIN 顺序重试
- 支持 streaming（stream=True）和 tool_use（function calling）
- tenacity 自动重试（网络错误 / 限流），业务错误不重试
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .config import settings

logger = logging.getLogger(__name__)

# =====================================================================
# 可选依赖 - 官方 SDK（优先），缺失则降级为 httpx
# =====================================================================
try:
    from openai import AsyncOpenAI

    _HAS_OPENAI_SDK = True
except ImportError:
    _HAS_OPENAI_SDK = False

try:
    from anthropic import AsyncAnthropic

    _HAS_ANTHROPIC_SDK = True
except ImportError:
    _HAS_ANTHROPIC_SDK = False

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )

    _HAS_TENACITY = True
except ImportError:
    _HAS_TENACITY = False


# =====================================================================
# 厂商默认配置
# =====================================================================
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "sdk": "openai",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "env_key": "ANTHROPIC_API_KEY",
        "sdk": "anthropic",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
        "sdk": "openai",  # 智谱走 OpenAI 兼容接口
    },
}


@dataclass
class ToolCall:
    """工具调用请求（LLM 返回，待执行）"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """统一 LLM 响应（支持纯文本 + 工具调用）"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


def _retry_config(max_attempts: int = 3):
    """tenacity 重试装饰器工厂；tenacity 不可用时返回无操作装饰器"""
    if not _HAS_TENACITY:
        # 无 tenacity 时退化为不重试
        def _noop(func):
            return func

        return _noop

    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(
            (TimeoutError, ConnectionError, OSError)
        ),
        reraise=True,
    )


class LLMClient:
    """统一 LLM 客户端 - 支持 OpenAI / Anthropic / 智谱 + fallback 链"""

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.provider = provider or settings.llm_provider
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.llm_api_key
        self.base_url = base_url or settings.llm_base_url
        self.timeout = settings.llm_timeout

        # 构建 fallback 链：主配置 + 环境变量配置的备用模型
        self._fallback_clients: list[LLMClient] = []
        for item in settings.llm_fallback_chain:
            if ":" not in item:
                continue
            fb_provider, fb_model = item.split(":", 1)
            # 主配置已覆盖的跳过
            if fb_provider == self.provider and fb_model == self.model:
                continue
            defaults = _PROVIDER_DEFAULTS.get(fb_provider, {})
            fb_key = os.getenv(defaults.get("env_key", ""), "")
            fb_base = defaults.get("base_url", "")
            if not fb_key:
                logger.warning("fallback provider %s 未配置 API key，跳过", fb_provider)
                continue
            self._fallback_clients.append(
                LLMClient(
                    provider=fb_provider,
                    model=fb_model,
                    api_key=fb_key,
                    base_url=fb_base,
                )
            )

        # 懒初始化的 SDK 客户端（避免构造时连网）
        self._openai_client: Any = None
        self._anthropic_client: Any = None

    # ==================================================================
    # 公开 API
    # ==================================================================

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> str:
        """调用 LLM 对话，返回纯文本。需要工具调用结果时用 chat_with_tools"""
        resp = await self.chat_with_tools(messages, temperature, max_tokens, tools, **kwargs)
        return resp.content

    async def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """调用 LLM，返回含工具调用的完整响应。失败时走 fallback 链"""
        last_error: Exception | None = None
        # 先试主客户端，再试 fallback
        for client in [self, *self._fallback_clients]:
            try:
                return await client._call_once(messages, temperature, max_tokens, tools, **kwargs)
            except Exception as e:
                last_error = e
                logger.warning(
                    "LLM 调用失败 provider=%s model=%s: %s",
                    client.provider,
                    client.model,
                    e,
                )
                continue
        raise RuntimeError(f"所有 LLM 均调用失败，最后错误: {last_error}") from last_error

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式对话，逐 token yield。不支持 fallback（流式中途切换会丢上下文）"""
        async for chunk in self._stream_once(messages, temperature, max_tokens, **kwargs):
            yield chunk

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """调用 LLM 并解析 JSON 输出"""
        response = await self.chat(messages, temperature, **kwargs)
        return self._parse_json(response)

    async def sample_multiple(
        self,
        messages: list[dict[str, str]],
        temperatures: list[float],
        **kwargs: Any,
    ) -> list[str]:
        """多次采样 - SelfCheckGPT 用"""
        tasks = [self.chat(messages, temp, **kwargs) for temp in temperatures]
        return await asyncio.gather(*tasks)

    # ==================================================================
    # 单次调用（含重试，不含 fallback）
    # ==================================================================

    async def _call_once(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
        **kwargs: Any,
    ) -> LLMResponse:
        retry_decorator = _retry_config()
        wrapped = retry_decorator(self._dispatch)
        return await wrapped(messages, temperature, max_tokens, tools, **kwargs)

    async def _dispatch(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
        **kwargs: Any,
    ) -> LLMResponse:
        """按 provider 分发到具体实现"""
        sdk_type = _PROVIDER_DEFAULTS.get(self.provider, {}).get("sdk", "openai")
        if sdk_type == "anthropic":
            return await self._call_anthropic(messages, temperature, max_tokens, tools, **kwargs)
        # openai 兼容（openai / zhipu / 其他）
        return await self._call_openai_compat(messages, temperature, max_tokens, tools, **kwargs)

    # ==================================================================
    # OpenAI 兼容实现（openai / zhipu）
    # ==================================================================

    def _get_openai_client(self) -> Any:
        if self._openai_client is not None:
            return self._openai_client
        base = self.base_url or _PROVIDER_DEFAULTS.get(self.provider, {}).get("base_url", "")
        if _HAS_OPENAI_SDK:
            self._openai_client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=base,
                timeout=self.timeout,
            )
            return self._openai_client
        # 降级：无 SDK 时用 httpx
        if not _HAS_HTTPX:
            raise RuntimeError("openai SDK 和 httpx 均不可用，无法调用 OpenAI 兼容接口")
        return None  # httpx 模式下每次新建 client

    async def _call_openai_compat(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
        **kwargs: Any,
    ) -> LLMResponse:
        client = self._get_openai_client()
        if client is not None:
            return await self._call_openai_sdk(client, messages, temperature, max_tokens, tools, **kwargs)
        return await self._call_openai_httpx(messages, temperature, max_tokens, tools, **kwargs)

    async def _call_openai_sdk(
        self,
        client: Any,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
        **kwargs: Any,
    ) -> LLMResponse:
        """官方 openai SDK"""
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if tools:
            params["tools"] = tools
        resp = await client.chat.completions.create(**params)
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {"_raw": tc.function.arguments}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )

    async def _call_openai_httpx(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
        **kwargs: Any,
    ) -> LLMResponse:
        """httpx 降级实现（无 openai SDK 时）"""
        base = self.base_url or _PROVIDER_DEFAULTS.get(self.provider, {}).get("base_url", "")
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=self.timeout) as http_client:
            resp = await http_client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls = []
        for tc in msg.get("tool_calls", []):
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            tool_calls.append(
                ToolCall(id=tc.get("id", ""), name=tc["function"]["name"], arguments=args)
            )
        return LLMResponse(
            content=msg.get("content", "") or "",
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
        )

    # ==================================================================
    # Anthropic 实现
    # ==================================================================

    def _get_anthropic_client(self) -> Any:
        if self._anthropic_client is not None:
            return self._anthropic_client
        if _HAS_ANTHROPIC_SDK:
            self._anthropic_client = AsyncAnthropic(
                api_key=self.api_key,
                base_url=self.base_url or None,
                timeout=self.timeout,
            )
            return self._anthropic_client
        return None

    async def _call_anthropic(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
        **kwargs: Any,
    ) -> LLMResponse:
        client = self._get_anthropic_client()
        if client is not None:
            return await self._call_anthropic_sdk(client, messages, temperature, max_tokens, tools, **kwargs)
        return await self._call_anthropic_httpx(messages, temperature, max_tokens, tools, **kwargs)

    async def _call_anthropic_sdk(
        self,
        client: Any,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
        **kwargs: Any,
    ) -> LLMResponse:
        """官方 anthropic SDK"""
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_messages = [m for m in messages if m["role"] != "system"]
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
            **kwargs,
        }
        if system:
            params["system"] = system
        if tools:
            # Anthropic 工具格式与 OpenAI 不同，但此处接受统一格式由调用方转换
            params["tools"] = tools
        resp = await client.messages.create(**params)
        content = ""
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=block.input)
                )
        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
                "total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
            }
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=resp.stop_reason or "stop",
            usage=usage,
        )

    async def _call_anthropic_httpx(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None,
        **kwargs: Any,
    ) -> LLMResponse:
        """httpx 降级实现（无 anthropic SDK 时）"""
        base = self.base_url or "https://api.anthropic.com"
        url = f"{base}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_messages = [m for m in messages if m["role"] != "system"]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
            **kwargs,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=self.timeout) as http_client:
            resp = await http_client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        content = ""
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input", {}),
                    )
                )
        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason", "stop"),
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            },
        )

    # ==================================================================
    # 流式实现（仅 SDK，httpx 降级时回退到非流式）
    # ==================================================================

    async def _stream_once(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        sdk_type = _PROVIDER_DEFAULTS.get(self.provider, {}).get("sdk", "openai")
        if sdk_type == "anthropic" and _HAS_ANTHROPIC_SDK:
            async for chunk in self._stream_anthropic(messages, temperature, max_tokens, **kwargs):
                yield chunk
            return
        if _HAS_OPENAI_SDK:
            async for chunk in self._stream_openai(messages, temperature, max_tokens, **kwargs):
                yield chunk
            return
        # 降级：无 SDK 时非流式返回完整结果
        result = await self.chat(messages, temperature, max_tokens, **kwargs)
        yield result

    async def _stream_openai(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        client = self._get_openai_client()
        stream = await client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            **kwargs,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def _stream_anthropic(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        client = self._get_anthropic_client()
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_messages = [m for m in messages if m["role"] != "system"]
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
            **kwargs,
        }
        if system:
            params["system"] = system
        async with client.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                yield text

    # ==================================================================
    # 工具方法
    # ==================================================================

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """从 LLM 输出中解析 JSON"""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                return json.loads(text[start : end + 1])
            raise


# 全局单例
llm_client = LLMClient()
