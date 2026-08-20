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
    from jinja2 import Template as _JinjaTemplate  # type: ignore[import-not-found]

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
        metadata={
            k: v for k, v in meta.items() if k not in ("name", "description", "inputs", "model")
        },
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


# =====================================================================
# P1.6 CoT 推理模板 - reasoning / planning / verification / reflection
# =====================================================================
# 设计（对齐 v1.2 计划文档 P1.6）：
#   - 4 类模板覆盖显式 CoT 推理全流程：推理 → 规划 → 验证 → 反思
#   - 模板用 Jinja2（已装则用）或简单 {{var}} 替换（参考 render_template）
#   - get_cot_template(name, **vars) 统一入口
#   - 未知模板名返回空字符串（韧性优先，不抛异常）
# 这些模板供 P1.1/P1.2/P1.3/P1.5 模块复用，亦可被 agent_node 注入 system_prompt。
# 无独立 feature flag（模板本身是被动资源，仅在调用方启用对应 feature flag 时生效）。

COT_TEMPLATES: dict[str, str] = {
    "reasoning": (
        "请对以下问题进行显式 Chain-of-Thought 推理。\n"
        "\n"
        "问题：{{ question }}\n"
        "上下文：{{ context }}\n"
        "\n"
        "请按以下步骤推理（让我一步步思考）：\n"
        "1. 理解问题核心与已知信息\n"
        "2. 列出关键事实与约束\n"
        "3. 逐步推导中间结论\n"
        "4. 给出最终答案\n"
        "\n"
        "推理过程："
    ),
    "planning": (
        "将以下问题拆解为可执行的子步骤（Plan-and-Execute 风格）。\n"
        "\n"
        "问题：{{ question }}\n"
        "\n"
        "请输出 JSON（步骤数 1-5，depends_on 只能引用前序 step_id）：\n"
        "{\n"
        '  "steps": [\n'
        '    {"step_id": "s1", "action": "动作描述", "tool_hint": "工具名", '
        '"depends_on": [], "expected_output": "预期输出"}\n'
        "  ]\n"
        "}\n"
        "\n"
        "只输出 JSON，不要其他文本："
    ),
    "verification": (
        "请验证以下回答是否正确、完整（Self-Verification）。\n"
        "\n"
        "问题：{{ question }}\n"
        "我的回答：{{ answer }}\n"
        "\n"
        "请按以下维度检查：\n"
        "1. 事实准确性\n"
        "2. 逻辑一致性\n"
        "3. 完整性\n"
        "4. 是否有编造内容\n"
        "\n"
        '输出 JSON：{"passed": true|false, "score": 0.0-1.0, "issues": ["问题1", "问题2"]}'
    ),
    "reflection": (
        "上次尝试失败，请反思原因并生成调整策略（Reflexion）。\n"
        "\n"
        "任务：{{ task }}\n"
        "失败原因：{{ failure_reason }}\n"
        "上次输出：{{ previous_output }}\n"
        "\n"
        "请反思：\n"
        "1. 失败的根本原因（不要只看表面）\n"
        "2. 应如何调整 prompt / 参数 / 策略\n"
        "3. 下次尝试的具体调整\n"
        "\n"
        '输出 JSON：{"failure_type": "...", "reason": "...", "adjustment": "...", '
        '"adjusted_params": {}}'
    ),
}


def get_cot_template(name: str, **vars: Any) -> str:
    """按名称取 CoT 模板，并用 vars 渲染。

    Args:
        name: 模板名（reasoning / planning / verification / reflection）
        **vars: 模板变量（如 question="..." / answer="..."）

    Returns:
        渲染后的字符串；未知模板名返回空字符串（不抛异常）。
    """
    template = COT_TEMPLATES.get(name)
    if template is None:
        return ""
    return render_template(template, vars)


# 全局单例
local_prompt_store = LocalPromptStore()
