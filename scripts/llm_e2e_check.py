#!/usr/bin/env python3
"""真实 LLM 端到端冒烟检查（单次调用，不烧配额）

用途：部署后验证 LLM 链路真实可用（此前 CI 全走 mock/降级路径）。
设计：仅发一次最小请求；429 时明确提示配额问题而非重试轰炸。

用法：
    export LLM_PROVIDER=openai
    export LLM_BASE_URL=https://your-gateway/v1
    export LLM_API_KEY=sk-xxx
    export LLM_MODEL=your-model
    python scripts/llm_e2e_check.py

退出码：0=通过（真实模型回复）；1=失败（连接/认证/配置问题）；
2=限频（配额耗尽，稍后重试）。
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


async def main() -> int:
    from deadman.llm import llm_client

    print(
        f"provider={llm_client.provider} model={llm_client.model} key={'***' if llm_client.api_key else '缺失'}"
    )
    if not llm_client.api_key:
        print("✗ 未配置 LLM_API_KEY")
        return 1

    try:
        resp = await llm_client.chat(
            [{"role": "user", "content": "请只回复两个字：收到"}],
            max_tokens=16,
        )
    except Exception as exc:
        msg = str(exc)
        if "429" in msg or "rate limit" in msg.lower():
            print("⚠ 网关限频/配额耗尽（未重试轰炸）。稍后单次重跑本脚本。")
            return 2
        print(f"✗ 调用失败: {type(exc).__name__}: {msg[:200]}")
        return 1

    text = (resp.content or "").strip()
    print(f"✓ 真实模型回复: {text[:80]}")
    usage = resp.usage or {}
    print(
        f"  tokens: prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}"
    )
    degraded_hint = not text
    return 1 if degraded_hint else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
