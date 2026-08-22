"""OpenAI 兼容层 - /v1/chat/completions（生态互通拼图）

让任何 OpenAI-SDK 生态客户端（Cherry Studio / LobeChat / OpenWebUI /
Cursor 自定义模型 / 各类 Agent 框架）直接把 deadman 当成一个"模型"接入。

实现要点：
- 协议：OpenAI Chat Completions（stream=true 走 SSE chunk；false 走一次性 JSON）
- 语义映射：messages 最后一条 user 消息 → 编排图 query；
  model 字段接受 deadman 智能体名（如 death-aftercare，缺省默认智能体）
- 认证：复用 JWT Bearer（Authorization: Bearer <deadman token>），
  匿名请求按平台既有降级路径处理
- 流式格式：chat.completion.chunk（id/choices/delta/content + [DONE] 终止帧）
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from ..deps import bearer_scheme, get_optional_user
from ..services.chat import handle_chat, stream_chat_events

router = APIRouter(prefix="/v1", tags=["openai-compat"])

_DEFAULT_AGENT = "death-aftercare"
_KNOWN_AGENTS = {
    "death-aftercare",
    "legal-advisor",
    "financial-analyst",
    "policy-researcher",
    "cross-border-specialist",
    "medical-guide",
    "deep-researcher",
    "data-analyst",
}


def _extract_query_and_agent(messages: list[dict[str, Any]], model: str | None) -> tuple[str, str]:
    """从 OpenAI messages 提取 (query, agent)。

    agent 解析优先级：model 字段命中已知智能体名 > system 消息中的
    ``agent:<name>`` 标记 > 默认智能体。
    """
    agent = _DEFAULT_AGENT
    if model and model.lower() in _KNOWN_AGENTS:
        agent = model.lower()

    query = ""
    for msg in messages or []:
        role = (msg.get("role") or "").lower()
        content = msg.get("content") or ""
        if isinstance(content, list):  # 多模态 content 数组取文本段拼接
            content = " ".join(seg.get("text", "") for seg in content if isinstance(seg, dict))
        if role == "system":
            text = str(content).strip()
            if text.startswith("agent:"):
                candidate = text.split(":", 1)[1].strip().lower()
                if candidate in _KNOWN_AGENTS:
                    agent = candidate
            continue
        if role == "user":
            query = str(content).strip()  # 取最后一条 user
    return query, agent


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    """OpenAI 兼容对话补全端点（流式/非流式双模）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "invalid JSON body", "type": "invalid_request_error"}},
        )

    messages = body.get("messages") or []
    stream = bool(body.get("stream", False))
    model = body.get("model")
    query, agent = _extract_query_and_agent(messages, model)

    if not query:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "messages 必须包含至少一条非空 user 消息",
                    "type": "invalid_request_error",
                }
            },
        )

    # 可选认证：带合法 token 则绑定 user_id（匿名走平台既有降级）
    user = get_optional_user(cred)
    user_id = (user or {}).get("user_id")

    completion_id = _completion_id()
    created = int(time.time())
    model_name = f"deadman/{agent}"

    if not stream:
        result = await handle_chat(agent=agent, query=query, history=[], user_id=user_id)
        return JSONResponse(
            content={
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.get("response", "")},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                # 平台扩展字段（OpenAI 客户端会忽略未知字段）
                "deadman": {
                    "degraded": result.get("degraded"),
                    "risk_tier": result.get("risk_tier"),
                    "disclaimer": result.get("disclaimer"),
                },
            }
        )

    # ---------- SSE 流式 ----------
    async def sse_gen():
        # 首 chunk 发角色
        first = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"

        async for line in stream_chat_events(agent=agent, query=query, user_id=user_id):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            chunk_text = payload.get("chunk")
            done = payload.get("done") is not None or payload.get("has_trace") is not None
            # 错误事件透传为内容并以 stop 终止（OpenAI 协议无独立错误帧）
            err_text = payload.get("error")
            if err_text and chunk_text is None:
                chunk_text = f"[服务暂不可用] {err_text}"
                done = True
            if chunk_text is None and not done:
                continue
            delta: dict[str, Any] = {}
            if chunk_text:
                delta["content"] = chunk_text
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": "stop" if done else None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            if done:
                break

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
