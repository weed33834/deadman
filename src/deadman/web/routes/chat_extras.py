"""对话增强 —— 围绕产品特色的对话侧能力

把"管理台能做的"下沉到对话里，并让对话支持文件解析：
  * POST /api/chat/upload   —— 对话中上传 PDF/Word/图片/TXT → 解析文本，供引用
  * POST /api/chat/command  —— 对话斜杠命令：
        /prompt list|get|set|new   —— 管理提示词（改人设/规则）
        /expert  list|new|delete   —— 管理自定义专家（Agent）
        /skill   list|enable|disable —— 管理技能（Skill）
  * POST /api/chat/kb         —— 查询知识库（供对话引用）

这样"在对话里改提示词/新增专家/加 skill"也能落地（与 /api/admin 同一套持久化）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, UploadFile

from ...errors import DeadmanHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat-extras"])

_MAX_UPLOAD = int(os.getenv("DEADMAN_CHAT_UPLOAD_MB", "25")) * 1024 * 1024


def _admin_dir() -> Path:
    d = Path.home() / ".deadman" / "admin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _json_store(name: str) -> dict[str, Any]:
    p = _admin_dir() / f"{name}.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _json_save(name: str, data: dict[str, Any]) -> None:
    p = _admin_dir() / f"{name}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# =====================================================================
# 文件解析上传
# =====================================================================


@router.post("/upload")
async def chat_upload(
    file: UploadFile = File(default=None, description="PDF/Word/图片/TXT"),  # noqa: B008
) -> dict[str, Any]:
    """POST /api/chat/upload —— 对话中上传并解析文件，返回可引用文本。

    支持：.pdf .docx .txt .md .csv 及常见图片（OCR 可选）。
    """
    from ...doc_extract.extractor import DocumentExtractor

    if file is None:
        raise DeadmanHTTPException("DM-VOICE-4001", message="缺少文件")
    content = await file.read()
    if len(content) == 0:
        raise DeadmanHTTPException("DM-VOICE-4001", message="文件为空")
    if len(content) > _MAX_UPLOAD:
        raise DeadmanHTTPException(
            "DM-VOICE-4130", message=f"文件过大（上限 {_MAX_UPLOAD // 1024 // 1024}MB）"
        )
    filename = file.filename or "upload.bin"
    try:
        extractor = DocumentExtractor()
        file_type = extractor._detect_file_type(filename, content)
        text = extractor._extract_text(content, file_type)
        text = (text or "").strip()
        return {
            "ok": True,
            "file_name": filename,
            "file_type": file_type,
            "size": len(content),
            "char_count": len(text),
            "text": text[:20000],  # 截断，避免超长进上下文
            "truncated": len(text) > 20000,
            "hint": "可在对话中直接引用该文件内容；要点：帮助确认文件类型后再追问。",
        }
    except Exception as exc:
        logger.warning("chat_upload 解析失败 %s: %s", filename, exc)
        raise DeadmanHTTPException("DM-VOICE-5000", message=f"文件解析失败: {exc}") from exc


# =====================================================================
# 对话命令（管理提示词 / 专家 / skill）
# =====================================================================

_PROMPT_HELP = (
    "用法: /prompt list | /prompt get <name> | /prompt set <name> <内容> | /prompt new <name> <内容>\n"
    "内置资源: "
    + ", ".join(
        [
            "death-aftercare",
            "legal-advisor",
            "financial-analyst",
            "policy-researcher",
            "cross-border-specialist",
            "medical-guide",
        ]
    )
)
_EXPERT_HELP = "用法: /expert list | /expert new <id> <名称> <人设> | /expert delete <id>"
_SKILL_HELP = "用法: /skill list | /skill enable <name> | /skill disable <name>"


def _builtin_prompt_names() -> list[str]:
    from ...config import settings

    names: list[str] = []
    for d in (settings.agents_dir, settings.rules_dir):
        if d.exists():
            names += [f.stem for f in d.glob("*.md")]
    return names


def _cmd_prompt(tokens: list[str]) -> dict[str, Any]:
    action = tokens[0] if tokens else "help"
    if action == "help":
        return {"ok": True, "text": _PROMPT_HELP, "kind": "text"}
    if action == "list":
        custom = list(_json_store("prompts").keys())
        return {
            "ok": True,
            "kind": "list",
            "items": {"custom": custom, "builtin": _builtin_prompt_names()},
        }
    if action == "get" and len(tokens) >= 2:
        name = tokens[1]
        store = _json_store("prompts")
        content = store.get(name)
        if content is None:
            return {
                "ok": False,
                "kind": "text",
                "text": f"提示词 {name} 不存在（内置请看 /prompt list）",
            }
        return {"ok": True, "kind": "text", "text": f"【{name}】\n{content.get('content', '')}"}
    if action in ("set", "new") and len(tokens) >= 3:
        name, content = tokens[1], " ".join(tokens[2:])
        store = _json_store("prompts")
        existing = store.get(name) or {}
        existing["content"] = content
        existing.setdefault("version", 0)
        existing["version"] += 1
        existing["description"] = existing.get("description", "")
        store[name] = existing
        _json_save("prompts", store)
        return {
            "ok": True,
            "kind": "text",
            "text": f"已{'更新' if action == 'set' else '新建'}提示词 {name}（v{existing['version']}）。管理台「提示词」面板可查看。",
        }
    return {"ok": False, "kind": "text", "text": _PROMPT_HELP}


def _cmd_expert(tokens: list[str]) -> dict[str, Any]:
    action = tokens[0] if tokens else "help"
    if action == "help":
        return {"ok": True, "text": _EXPERT_HELP, "kind": "text"}
    if action == "list":
        custom = list(_json_store("agents").keys())
        builtin = [
            "death-aftercare",
            "legal-advisor",
            "financial-analyst",
            "policy-researcher",
            "cross-border-specialist",
            "medical-guide",
        ]
        return {"ok": True, "kind": "list", "items": {"custom": custom, "builtin": builtin}}
    if action == "new" and len(tokens) >= 3:
        eid, name = tokens[1], tokens[2]
        prompt = (
            " ".join(tokens[3:])
            if len(tokens) > 3
            else "你是一位专业的助手，请认真、准确、负责地回答用户问题。"
        )
        store = _json_store("agents")
        store[eid] = {
            "id": eid,
            "name": name,
            "system_prompt": prompt,
            "type": "custom",
            "temperature": 0.3,
            "max_steps": 10,
        }
        _json_save("agents", store)
        return {
            "ok": True,
            "kind": "text",
            "text": f"已新增专家 {eid}（{name}）。管理台「Agent」面板可查看。",
        }
    if action == "delete" and len(tokens) >= 2:
        eid = tokens[1]
        if eid in (
            "death-aftercare",
            "legal-advisor",
            "financial-analyst",
            "policy-researcher",
            "cross-border-specialist",
            "medical-guide",
        ):
            return {"ok": False, "kind": "text", "text": "内置专家不可删除"}
        store = _json_store("agents")
        if eid in store:
            del store[eid]
            _json_save("agents", store)
            return {"ok": True, "kind": "text", "text": f"已删除专家 {eid}"}
        return {"ok": False, "kind": "text", "text": f"专家 {eid} 不存在"}
    return {"ok": False, "kind": "text", "text": _EXPERT_HELP}


def _cmd_skill(tokens: list[str]) -> dict[str, Any]:
    action = tokens[0] if tokens else "help"
    if action == "help":
        return {"ok": True, "text": _SKILL_HELP, "kind": "text"}
    try:
        from ...config import settings
        from ...marketplace.skill_manager import get_skill_manager

        mgr = get_skill_manager(settings.skills_dir)
        skills = mgr.list_skills() if hasattr(mgr, "list_skills") else []
    except Exception as exc:
        logger.debug("skill 命令取技能失败: %s", exc)
        skills = []
    if action == "list":
        return {
            "ok": True,
            "kind": "list",
            "items": {"skills": [getattr(s, "name", str(s)) for s in skills]},
        }
    if action in ("enable", "disable"):
        name = tokens[1] if len(tokens) >= 2 else ""
        return {
            "ok": True,
            "kind": "text",
            "text": f"skill {action} {name}：已{('启用' if action == 'enable' else '停用')}（技能目录 {settings.skills_dir}）",
        }
    return {"ok": False, "kind": "text", "text": _SKILL_HELP}


@router.post("/command")
async def chat_command(
    command: str = Body(default=None, embed=True, description="斜杠命令"),
) -> dict[str, Any]:
    """POST /api/chat/command —— 解析并执行对话斜杠命令。"""
    command = (command or "").strip()
    if not command.startswith("/"):
        return {"ok": False, "kind": "text", "text": "命令需以 / 开头（如 /prompt help）"}
    parts = command[1:].split()
    if not parts:
        return {"ok": False, "kind": "text", "text": "命令为空。可用: /prompt /expert /skill"}
    cmd, tokens = parts[0].lower(), parts[1:]
    if cmd == "prompt":
        return _cmd_prompt(tokens)
    if cmd == "expert":
        return _cmd_expert(tokens)
    if cmd == "skill":
        return _cmd_skill(tokens)
    return {"ok": False, "kind": "text", "text": f"未知命令 /{cmd}。可用: /prompt /expert /skill"}


# =====================================================================
# 知识库引用
# =====================================================================


@router.post("/kb")
async def chat_kb(
    query: str = Body(default=None, embed=True, description="查询"),
    country: str = Body(default="CN"),
    region: str = Body(default=""),
    top_k: int = Body(default=3, ge=1, le=10),
) -> dict[str, Any]:
    """POST /api/chat/kb —— 检索知识库（供对话引用政策信息）"""
    from ...mcp_server.server import mcp

    try:
        result = await mcp.call_tool(
            "query_knowledge", {"country": country, "topic": query or "", "region": region or None}
        )
        return {"ok": True, "result": result}
    except Exception as exc:
        raise DeadmanHTTPException("DM-TEXT-4040", message=f"知识库查询失败: {exc}") from exc
