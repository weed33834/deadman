"""LLM 客户端 - 统一封装多厂商 LLM 调用"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class LLMClient:
    """统一 LLM 客户端 - 支持 OpenAI/Anthropic/智谱 等"""

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

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> str:
        """调用 LLM 对话"""
        if self.provider == "openai":
            return await self._call_openai(messages, temperature, max_tokens, **kwargs)
        elif self.provider == "anthropic":
            return await self._call_anthropic(messages, temperature, max_tokens, **kwargs)
        elif self.provider == "zhipu":
            return await self._call_zhipu(messages, temperature, max_tokens, **kwargs)
        else:
            # 默认走 OpenAI 兼容接口
            return await self._call_openai(messages, temperature, max_tokens, **kwargs)

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

    async def _call_openai(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> str:
        """OpenAI 兼容接口"""
        url = f"{self.base_url}/v1/chat/completions" or "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def _call_anthropic(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> str:
        """Anthropic 接口"""
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_messages = [m for m in messages if m["role"] != "system"]
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": user_messages,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]

    async def _call_zhipu(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> str:
        """智谱接口（OpenAI 兼容）"""
        if not self.base_url:
            self.base_url = "https://open.bigmodel.cn/api/paas"
        return await self._call_openai(messages, temperature, max_tokens, **kwargs)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """从 LLM 输出中解析 JSON"""
        # 去除可能的 markdown 代码块标记
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试找到第一个 { 和最后一个 }
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                return json.loads(text[start : end + 1])
            raise


# 全局单例
llm_client = LLMClient()
