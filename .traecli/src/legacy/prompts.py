"""提示词管理 - 本地模板 + 线上仓库 + 渲染 + 反馈闭环

设计(对应用户"举一反三"的提示词领域,核心四件套):
  - 本地提示词: prompts/*.prompty 与 agents/*.md(均 YAML frontmatter + body)
    格式对齐 Prompty 开放标准(https://prompty.ai/specification/file-format)
  - 线上提示词: LangSmith Hub(主,需 API key)+ deepset PromptHub(备,免认证)
    数据源官网查证(2026-07):
      - LangSmith Hub: https://smith.langchain.com/hub , API https://api.smith.langchain.com
      - deepset PromptHub: https://prompthub.deepset.ai , API https://api.prompthub.deepset.ai
  - 手动测试: prompt-test CLI 渲染 + 发 LLM,反馈真实结果
  - 反馈闭环: 健康状态写 data/prompt_health.json + metrics 采集

模板变量语法:
  - 优先 Jinja2({{ var }} / {% if %}),缺失时降级为 {{var}} 简单替换
  - 跨平台兼容性最好的是 Mustache {{var}}(LangSmith/PromptHub/Prompty 都支持)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import settings

logger = logging.getLogger(__name__)

# === 可选依赖: Jinja2(优先),缺失降级为简单 {{var}} 替换 ===
try:
    from jinja2 import Template as _JinjaTemplate

    _HAS_JINJA = True
except ImportError:
    _HAS_JINJA = False

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


# =====================================================================
# 渲染
# =====================================================================
_VAR_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_template(template_str: str, variables: dict[str, Any] | None = None) -> str:
    """渲染模板 - Jinja2 优先,缺失降级为 {{var}} 替换

    Args:
        template_str: 模板字符串(含 {{ var }} 占位)
        variables: 变量字典

    Returns:
        渲染后的字符串
    """
    variables = variables or {}
    if _HAS_JINJA:
        try:
            return _JinjaTemplate(template_str).render(**variables)
        except Exception as e:
            logger.warning("Jinja2 渲染失败,降级简单替换: %s", e)
    # 降级: 逐个替换 {{var}}
    def _repl(m: re.Match) -> str:
        key = m.group(1)
        return str(variables.get(key, m.group(0)))

    return _VAR_PATTERN.sub(_repl, template_str)


def extract_variables(template_str: str) -> list[str]:
    """从模板中提取所有 {{var}} 变量名(去重保序)"""
    seen: list[str] = []
    for m in _VAR_PATTERN.finditer(template_str):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


# =====================================================================
# 本地提示词
# =====================================================================
@dataclass
class LocalPrompt:
    """本地提示词条目

    对齐 Prompty 标准: frontmatter 含 name/description/inputs/model,
    body 是模板正文(含 {{ var }} 占位)。
    """

    name: str
    description: str
    template: str
    inputs: list[str] = field(default_factory=list)
    model: str = ""
    source: str = ""  # 文件路径或线上来源
    metadata: dict[str, Any] = field(default_factory=dict)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 YAML frontmatter + body

    支持 `---\n...\n---\nbody` 格式。无 frontmatter 时返回 ({}, text)
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta if isinstance(meta, dict) else {}, parts[2].lstrip("\n")


def load_prompt_file(path: Path) -> LocalPrompt | None:
    """加载单个 .prompty / .md 文件为 LocalPrompt

    无 frontmatter 或解析失败返回 None。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("读取提示词文件失败 %s: %s", path, e)
        return None
    meta, body = _parse_frontmatter(text)
    if not meta:
        return None
    name = meta.get("name", path.stem)
    description = meta.get("description", "")
    # inputs 优先用 frontmatter 声明,缺失则从 body 提取
    declared_inputs = meta.get("inputs") or []
    if isinstance(declared_inputs, list):
        inputs = [str(x) for x in declared_inputs]
    elif isinstance(declared_inputs, dict):
        inputs = list(declared_inputs.keys())
    else:
        inputs = []
    if not inputs:
        inputs = extract_variables(body)
    model = meta.get("model", "")
    return LocalPrompt(
        name=str(name),
        description=str(description),
        template=body,
        inputs=inputs,
        model=str(model),
        source=str(path),
        metadata={k: v for k, v in meta.items() if k not in ("name", "description", "inputs", "model")},
    )


class LocalPromptStore:
    """本地提示词仓库 - 扫描 prompts/ 与 agents/ 目录加载所有提示词

    prompts/ 目录放 .prompty 文件(纯提示词模板);
    agents/ 目录的 .md 也可加载(智能体系统提示词,frontmatter 有 name/description)。
    """

    def __init__(self) -> None:
        self._prompts: dict[str, LocalPrompt] = {}

    def load_all(self) -> dict[str, LocalPrompt]:
        """扫描所有本地提示词目录并加载,返回 name -> LocalPrompt"""
        self._prompts.clear()
        search_dirs = [
            settings.project_root / "prompts",
            settings.project_root / "agents",
        ]
        for d in search_dirs:
            if not d.exists():
                continue
            for path in sorted(d.glob("*.prompty")) + sorted(d.glob("*.md")):
                prompt = load_prompt_file(path)
                if prompt and prompt.name not in self._prompts:
                    self._prompts[prompt.name] = prompt
        return self._prompts

    def get(self, name: str) -> LocalPrompt | None:
        """按 name 取提示词(未加载时自动 load_all)"""
        if not self._prompts:
            self.load_all()
        return self._prompts.get(name)

    def list_names(self) -> list[str]:
        if not self._prompts:
            self.load_all()
        return sorted(self._prompts.keys())


# =====================================================================
# 线上提示词仓库
# =====================================================================
async def fetch_langsmith_prompts(query: str = "") -> list[dict[str, Any]]:
    """从 LangSmith Hub 拉公开提示词

    官网(2026-07): https://smith.langchain.com/hub
    API: GET https://api.smith.langchain.com/api/v1/repos?public=true
    认证: X-Api-Key 头(读 LANGSMITH_API_KEY / LANGCHAIN_API_KEY)

    无 key 或 httpx 不可用时返回空列表(不抛异常)。
    """
    if not _HAS_HTTPX:
        logger.info("httpx 不可用,跳过 LangSmith fetch")
        return []
    api_key = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY", "")
    if not api_key:
        logger.info("未配置 LANGSMITH_API_KEY,跳过 LangSmith fetch")
        return []
    url = "https://api.smith.langchain.com/api/v1/repos"
    params: dict[str, Any] = {"public": "true", "limit": 20}
    if query:
        params["query"] = query
    headers = {"X-Api-Key": api_key}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                logger.info("LangSmith fetch 返回 %s", resp.status_code)
                return []
            data = resp.json()
        repos = data.get("repos", []) if isinstance(data, dict) else []
        return [
            {
                "name": r.get("name", ""),
                "owner": r.get("owner", ""),
                "description": r.get("description", ""),
                "full_name": f"{r.get('owner', '')}/{r.get('name', '')}",
                "source": "langsmith",
            }
            for r in repos
        ]
    except Exception as e:
        logger.info("LangSmith fetch 失败: %s", e)
        return []


async def fetch_deepset_prompts() -> list[dict[str, Any]]:
    """从 deepset PromptHub 拉公开提示词(免认证)

    官网(2026-07): https://prompthub.deepset.ai
    API: GET https://api.prompthub.deepset.ai/prompts
    """
    if not _HAS_HTTPX:
        return []
    url = "https://api.prompthub.deepset.ai/prompts"
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.info("deepset PromptHub fetch 返回 %s", resp.status_code)
                return []
            data = resp.json()
        items = data if isinstance(data, list) else data.get("prompts", [])
        return [
            {
                "name": p.get("name", ""),
                "description": p.get("description", ""),
                "prompt_text": (p.get("prompt") or p.get("text", ""))[:200],
                "source": "deepset",
            }
            for p in items
        ]
    except Exception as e:
        logger.info("deepset PromptHub fetch 失败: %s", e)
        return []


# 全局单例
local_prompt_store = LocalPromptStore()
