"""关键 API 请求体的 Pydantic 校验模型。

在 ``web/server.py`` 的 ``do_POST`` 路由层对入参做结构化校验，校验失败返回
``422 Unprocessable Entity`` 并附带字段级错误详情，避免无效请求进入业务逻辑。

模型设计遵循「向后兼容」原则：
* 仅约束关键字段的类型/必填，额外字段默认忽略（不报错），保留现有 API 行为。
* 校验通过后，调用方仍使用原始解析出的 dict 传给各 ``_handle_*`` 方法，
  确保不改变既有处理函数的入参契约。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = ["ChatRequest", "RegisterRequest", "LoginRequest", "validate_body"]


class ChatRequest(BaseModel):
    """POST /api/chat 请求体。

    ``query`` 为必填；``agent`` / ``history`` 可选，缺省时由路由层填充默认值，
    与既有行为一致。
    """

    query: str = Field(..., description="用户输入文本")
    agent: str | None = Field(default=None, description="目标智能体 ID")
    history: list[Any] | None = Field(default=None, description="对话历史")

    model_config = {"extra": "ignore"}


class RegisterRequest(BaseModel):
    """POST /api/auth/register 请求体。"""

    email: str = Field(..., description="注册邮箱")
    password: str = Field(..., description="登录密码")
    display_name: str = Field(..., description="显示名称")

    model_config = {"extra": "ignore"}


class LoginRequest(BaseModel):
    """POST /api/auth/login 请求体。"""

    email: str = Field(..., description="登录邮箱")
    password: str = Field(..., description="登录密码")

    model_config = {"extra": "ignore"}


def validate_body(model_cls: type[BaseModel], data: dict) -> tuple[bool, list]:
    """用 ``model_cls`` 校验 ``data``。

    Returns
    -------
    (ok, errors)
        ``ok`` 为 ``True`` 表示校验通过，``errors`` 为空列表；
        ``ok`` 为 ``False`` 表示校验失败，``errors`` 为可 JSON 序列化的错误详情列表
        （兼容 Pydantic v1/v2）。
    """
    try:
        model_cls(**data)
        return True, []
    except Exception as exc:  # noqa: BLE001 - 统一捕获 ValidationError
        # pydantic v1/v2 均提供 .json()，输出可安全 JSON 序列化的结构
        try:
            errors = exc.errors()
        except Exception:  # noqa: BLE001
            errors = [{"msg": str(exc)}]
        # errors 中可能含不可直接 json 序列化的对象（如 pydantic v2 的 url/input）
        # 经 json 往返确保安全
        import json as _json

        try:
            errors = _json.loads(_json.dumps(errors, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            errors = [{"msg": str(exc)}]
        return False, errors
