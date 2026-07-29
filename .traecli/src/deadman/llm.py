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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

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
    # 本地模型 - Ollama（OpenAI 兼容接口，默认 11434 端口）
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "env_key": "OLLAMA_API_KEY",  # Ollama 默认无需 key，用占位
        "sdk": "openai",
    },
    # 本地模型 - vLLM（OpenAI 兼容接口，默认 8000 端口）
    "vllm": {
        "base_url": "http://localhost:8000/v1",
        "env_key": "VLLM_API_KEY",  # vLLM 可选 token
        "sdk": "openai",
    },
    # 本地模型 - llama.cpp server（OpenAI 兼容接口，默认 8080 端口）
    "llama_cpp": {
        "base_url": "http://localhost:8080/v1",
        "env_key": "LLAMA_CPP_API_KEY",
        "sdk": "openai",
    },
}


# =====================================================================
# 各厂商最新模型清单（2026-07 官网查证）
# 用于 llm-test CLI 展示可选模型、成本估算
# =====================================================================
PROVIDER_MODELS: dict[str, list[dict[str, Any]]] = {
    "openai": [
        # 数据源: https://platform.openai.com/docs/models (2026-07-14)
        {"id": "gpt-5.5", "name": "GPT-5.5", "context": "1M", "input_price": 5.0, "output_price": 30.0},
        {"id": "gpt-5.4", "name": "GPT-5.4", "context": "1M", "input_price": 2.5, "output_price": 15.0},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini", "context": "400K", "input_price": 0.75, "output_price": 4.5},
        {"id": "gpt-5.4-nano", "name": "GPT-5.4 nano", "context": "400K", "input_price": 0.3, "output_price": 1.8},
    ],
    "anthropic": [
        # 数据源: https://platform.claude.com/docs/en/about-claude/pricing (2026-07-14)
        {"id": "claude-fable-5", "name": "Claude Fable 5", "context": "1M", "input_price": 10.0, "output_price": 50.0},
        {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "context": "1M", "input_price": 5.0, "output_price": 25.0},
        {"id": "claude-sonnet-5", "name": "Claude Sonnet 5", "context": "1M", "input_price": 2.0, "output_price": 10.0},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "context": "200K", "input_price": 1.0, "output_price": 5.0},
    ],
    "zhipu": [
        # 数据源: https://docs.bigmodel.cn/cn/update/new-releases (2026-07-14)
        {"id": "glm-5.2", "name": "GLM-5.2", "context": "1M", "input_price": None, "output_price": None},
        {"id": "glm-5.1", "name": "GLM-5.1", "context": "200K", "input_price": None, "output_price": None},
        {"id": "glm-5", "name": "GLM-5", "context": "200K", "input_price": None, "output_price": None},
        {"id": "glm-4.7", "name": "GLM-4.7", "context": "200K", "input_price": None, "output_price": None},
        {"id": "glm-4.7-flash", "name": "GLM-4.7 Flash (免费)", "context": "200K", "input_price": 0.0, "output_price": 0.0},
        {"id": "glm-4.6", "name": "GLM-4.6", "context": "200K", "input_price": None, "output_price": None},
    ],
    "ollama": [
        # 本地模型，价格均为 0（本地运行）
        {"id": "qwen3:32b", "name": "Qwen3 32B", "context": "128K", "input_price": 0.0, "output_price": 0.0},
        {"id": "qwen3:14b", "name": "Qwen3 14B", "context": "128K", "input_price": 0.0, "output_price": 0.0},
        {"id": "llama3.3:70b", "name": "Llama 3.3 70B", "context": "128K", "input_price": 0.0, "output_price": 0.0},
        {"id": "deepseek-r1:32b", "name": "DeepSeek R1 32B", "context": "128K", "input_price": 0.0, "output_price": 0.0},
    ],
    "vllm": [
        # vLLM 模型由用户自行加载，这里仅占位
        {"id": "custom", "name": "用户自定义模型", "context": "N/A", "input_price": 0.0, "output_price": 0.0},
    ],
    "llama_cpp": [
        {"id": "custom", "name": "用户自定义模型", "context": "N/A", "input_price": 0.0, "output_price": 0.0},
    ],
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

        # P10：记录最近一次成功调用的 token usage，供 TerminationCondition 读取
        # 注意：这是"最近一次"，不是"本轮累计"；本轮累计由 nodes.py 的
        # _accumulate_token_usage helper 把这个值累加到 state["metrics"]["token_usage"]
        self._last_usage: dict[str, int] = {}

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
                resp = await client._call_once(messages, temperature, max_tokens, tools, **kwargs)
                # 成功后记录成本(实际 token 用量→成本)
                self._track_cost(resp)
                # P10：记录最近一次成功调用的 usage，供 TerminationCondition 读取
                self._last_usage = dict(resp.usage or {})
                return resp
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

    @property
    def last_usage(self) -> dict[str, int]:
        """最近一次成功调用的 token usage（副本，只读）

        P10：供 nodes.py 的 _accumulate_token_usage helper 读取，
        累加到 state["metrics"]["token_usage"] 供 TokenUsageTermination 评估。
        """
        return dict(self._last_usage)

    def _track_cost(self, resp: LLMResponse) -> None:
        """把本次调用的 token 用量记入成本追踪器(失败静默,不阻断主流程)"""
        try:
            from .cost import cost_tracker

            usage = resp.usage or {}
            cost_tracker.record_usage(
                provider=self.provider,
                model=self.model,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )
        except Exception:  # pragma: no cover - 成本追踪失败不影响业务
            pass

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

    async def ping_once(
        self,
        messages: list[dict[str, str]] | None = None,
        max_tokens: int = 20,
        **kwargs: Any,
    ) -> LLMResponse:
        """单次直连调用 - 不走 fallback 链、不重试。

        专供 llm-test 等"接入测试"场景使用:测的就是这一个 provider+model 的
        真实可达性与延迟,避免重试/ fallback 掩盖问题。

        Args:
            messages: 测试消息,默认用一个简短 ping
            max_tokens: 最大输出 token,默认 20(接入测试只需短回复)
        """
        if messages is None:
            messages = [{"role": "user", "content": "请只回复四个字:pong ok"}]
        return await self._dispatch(messages, 0.0, max_tokens, None, **kwargs)

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


# =====================================================================
# 模型清单同步 - 定期 fetch 各 provider /models 端点拿真实可用模型
# =====================================================================
async def fetch_provider_models(provider: str) -> list[dict[str, Any]]:
    """从 provider 的 /models 端点拉取真实可用模型清单

    各厂商端点:
      - OpenAI 兼容(openai/zhipu/ollama/vllm/llama_cpp): GET {base_url}/models
      - Anthropic: GET https://api.anthropic.com/v1/models (需 x-api-key + anthropic-version)

    返回 [{"id": ..., "owned_by": ...}, ...],失败返回空列表(不抛异常)

    用于 llm-sync-models CLI:把线上真实模型与本地 PROVIDER_MODELS 对比,
    发现新模型/下线模型,避免用本地旧数据。
    """
    defaults = _PROVIDER_DEFAULTS.get(provider, {})
    base_url = defaults.get("base_url", "")
    env_key = defaults.get("env_key", "")
    api_key = os.getenv(env_key, "") if env_key else ""

    if not _HAS_HTTPX:
        logger.warning("httpx 不可用,无法 fetch %s 模型清单", provider)
        return []

    if provider == "anthropic":
        url = "https://api.anthropic.com/v1/models?limit=100"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        url = f"{base_url}/models"
        headers = {"Authorization": f"Bearer {api_key or 'none'}"}

    try:
        async with httpx.AsyncClient(timeout=15) as http_client:
            resp = await http_client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.info("fetch %s models 返回 %s", provider, resp.status_code)
                return []
            data = resp.json()
        # OpenAI 兼容: data["data"] = [{"id": ...}]
        # Anthropic: data["data"] = [{"id": ...}]
        items = data.get("data", []) if isinstance(data, dict) else []
        return [
            {"id": item.get("id", ""), "owned_by": item.get("owned_by", "")}
            for item in items
            if item.get("id")
        ]
    except Exception as e:
        logger.info("fetch %s models 失败: %s", provider, e)
        return []


# 全局单例
llm_client = LLMClient()


# =====================================================================
# P7: 多模型分工（借鉴 OpenDeepResearch configuration.py）
# =====================================================================
# use_case → LLMClient 缓存，避免每次构造都创建新的 SDK client
_llm_client_cache: dict[str, LLMClient] = {}


def get_llm_for_use_case(use_case: str) -> LLMClient:
    """按用例获取 LLM 客户端（借鉴 OpenDeepResearch 多模型分工）

    不同任务用不同模型以平衡成本与质量：
    - "router": 意图分类（用 LLM_MODEL_ROUTER 或回退主模型）
    - "summarizer": 摘要/记忆压缩（用 LLM_MODEL_SUMMARIZER 或回退主模型）
    - "respond": 主响应（用 LLM_MODEL_RESPOND 或回退主模型）
    - 其他/默认: 主 LLM

    所有 use_case 共享主 LLM 的 fallback 链；专用模型未配置时全部回退到主 LLM。
    缓存按 use_case 复用 LLMClient 实例，避免重复创建 SDK client。

    Args:
        use_case: "router" / "summarizer" / "respond" / 其他

    Returns:
        对应的 LLMClient 实例（已缓存）
    """
    if use_case in _llm_client_cache:
        return _llm_client_cache[use_case]

    # 按 use_case 取专用模型配置
    model_spec = ""
    if use_case == "router":
        model_spec = settings.llm_model_router
    elif use_case == "summarizer":
        model_spec = settings.llm_model_summarizer
    elif use_case == "respond":
        model_spec = settings.llm_model_respond

    # 未配置专用模型 → 回退主 LLM（避免无谓的 client 构造）
    if not model_spec:
        _llm_client_cache[use_case] = llm_client
        return llm_client

    # 解析 "provider:model" 格式
    if ":" not in model_spec:
        # 仅 model 名，沿用主 provider
        client = LLMClient(
            provider=settings.llm_provider,
            model=model_spec,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
    else:
        provider, model = model_spec.split(":", 1)
        defaults = _PROVIDER_DEFAULTS.get(provider, {})
        env_key = defaults.get("env_key", "")
        api_key = os.getenv(env_key, "") if env_key else settings.llm_api_key
        base_url = defaults.get("base_url", "") or settings.llm_base_url
        if not api_key:
            logger.warning(
                "use_case=%s 配置的 provider=%s 未设置 API key (%s)，回退主 LLM",
                use_case, provider, env_key,
            )
            _llm_client_cache[use_case] = llm_client
            return llm_client
        client = LLMClient(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

    logger.info(
        "P7 多模型分工: use_case=%s → provider=%s model=%s",
        use_case, client.provider, client.model,
    )
    _llm_client_cache[use_case] = client
    return client
