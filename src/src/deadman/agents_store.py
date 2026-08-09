"""智能体管理 - 本地配置 + 外部 A2A 发现 + 反馈闭环

设计(对应"举一反三"的 Agents 领域,核心四件套):
  - 本地智能体: agents/*.md 的 frontmatter(name/description/tools)+ A2A AgentCard
  - 外部智能体: A2A v1.0 协议发现远端 agent
      GET {url}/.well-known/agent.json  (官网 2026-07 spec)
  - 手动测试: agent-list 列本地配置 / agent-ping 测远端 A2A 可达性
  - 反馈闭环: 健康状态写 data/agent_health.json + metrics 采集
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import settings

logger = logging.getLogger(__name__)

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


@dataclass
class LocalAgent:
    """本地智能体配置(来自 agents/*.md frontmatter)"""

    name: str
    description: str
    tools: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def load_local_agents() -> dict[str, LocalAgent]:
    """扫描 agents/*.md 加载本地智能体配置

    返回 name -> LocalAgent。无 frontmatter 的跳过。
    """
    agents_dir: Path = settings.project_root / "agents"
    result: dict[str, LocalAgent] = {}
    if not agents_dir.exists():
        return result
    for path in sorted(agents_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("name", path.stem))
        result[name] = LocalAgent(
            name=name,
            description=str(meta.get("description", "")),
            tools=str(meta.get("tools", "")),
            source=str(path),
        )
    return result


async def fetch_remote_agent_card(base_url: str, timeout: float = 10.0) -> dict[str, Any]:
    """获取远端 A2A agent 的 AgentCard

    A2A v1.0 spec: GET {base_url}/.well-known/agent.json
    官网(2026-07): https://developer.a2a.dev

    返回 AgentCard dict;失败返回 {"error": ...}(不抛异常)
    """
    if not _HAS_HTTPX:
        return {"error": "httpx 不可用"}
    url = base_url.rstrip("/") + "/.well-known/agent.json"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}", "url": url}
            return resp.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "url": url}


async def ping_remote_agent(base_url: str, timeout: float = 10.0) -> dict[str, Any]:
    """ping 远端 A2A agent,反馈真实可达性与延迟

    返回 {"reachable": bool, "latency_ms": float, "agent_name": str, "skills": int, "error": str}
    """
    start = time.perf_counter()
    card = await fetch_remote_agent_card(base_url, timeout)
    latency = (time.perf_counter() - start) * 1000
    if "error" in card:
        return {
            "base_url": base_url,
            "reachable": False,
            "latency_ms": round(latency, 1),
            "agent_name": "",
            "skills": 0,
            "error": card["error"],
        }
    return {
        "base_url": base_url,
        "reachable": True,
        "latency_ms": round(latency, 1),
        "agent_name": card.get("name", ""),
        "skills": len(card.get("skills", [])),
        "version": card.get("version", ""),
        "error": "",
    }
