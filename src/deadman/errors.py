"""统一错误码体系 —— deep-spec 21「错误码体系三段式」落地

三段式错误码：``服务-模块-序号``，例如 ``DM-PROMPT-4001``。
每个错误码携带：HTTP 状态、人话 message、严重级别；全局注册表便于统一管理与
管理台可视化（/api/admin/error-codes）。

设计原则：
  * 单一来源：所有错误码集中在 ErrorRegistry，禁止散落硬编码
  * 结构化：任何 DeadmanError / DeadmanHTTPException 都会以
    ``{error, code, message, severity, request_id, detail}`` 返回给客户端
  * 兼容：HTTPException（FastAPI）与普通 Exception 仍走原降级路径
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorCode:
    """一个错误码定义"""

    code: str  # 三段式：DM-<模块>-<序号>
    http_status: int
    message: str
    severity: str = "error"  # error | warn


class ErrorRegistry:
    """错误码注册表（进程内单例）"""

    _codes: dict[str, ErrorCode] = {}

    @classmethod
    def register(cls, code: str, http_status: int, message: str, severity: str = "error") -> None:
        cls._codes[code] = ErrorCode(code, http_status, message, severity)

    @classmethod
    def get(cls, code: str) -> ErrorCode | None:
        return cls._codes.get(code)

    @classmethod
    def all(cls) -> list[dict[str, Any]]:
        return [
            {
                "code": c.code,
                "http_status": c.http_status,
                "message": c.message,
                "severity": c.severity,
            }
            for c in sorted(cls._codes.values(), key=lambda x: x.code)
        ]


# =====================================================================
# 内置错误码（按模块分组注册）
# =====================================================================


def _register_defaults() -> None:
    # 通用
    ErrorRegistry.register("DM-GENERAL-4000", 400, "请求无效", "warn")
    ErrorRegistry.register("DM-GENERAL-4040", 404, "资源不存在")
    ErrorRegistry.register("DM-GENERAL-4090", 409, "资源冲突")
    # 校验
    ErrorRegistry.register("DM-VALID-4001", 422, "参数校验失败", "warn")
    ErrorRegistry.register("DM-VALID-4002", 422, "缺少必填字段", "warn")
    # 认证
    ErrorRegistry.register("DM-AUTH-4010", 401, "未认证或登录已过期")
    ErrorRegistry.register("DM-AUTH-4030", 403, "无权限执行此操作")
    # 提示词
    ErrorRegistry.register("DM-PROMPT-4001", 400, "提示词名称或内容缺失")
    ErrorRegistry.register("DM-PROMPT-4040", 404, "提示词不存在")
    ErrorRegistry.register("DM-PROMPT-4090", 409, "内置提示词不可删除或修改")
    ErrorRegistry.register("DM-PROMPT-5000", 500, "AI 生成提示词失败")
    # 工具
    ErrorRegistry.register("DM-TOOL-4001", 400, "工具名缺失")
    ErrorRegistry.register("DM-TOOL-4040", 404, "工具不存在")
    ErrorRegistry.register("DM-TOOL-4030", 403, "工具已被禁用")
    ErrorRegistry.register("DM-TOOL-5000", 500, "工具执行失败")
    # 模型
    ErrorRegistry.register("DM-MODEL-4001", 400, "模型配置缺失")
    ErrorRegistry.register("DM-MODEL-5000", 500, "模型连通性测试失败")
    # Agent
    ErrorRegistry.register("DM-AGENT-4001", 400, "Agent 配置缺失")
    ErrorRegistry.register("DM-AGENT-4040", 404, "Agent 不存在")
    ErrorRegistry.register("DM-AGENT-4090", 409, "内置 Agent 不可删除")
    # 语音
    ErrorRegistry.register("DM-VOICE-4001", 400, "缺少音频文件")
    ErrorRegistry.register("DM-VOICE-4040", 404, "音色资源不存在")
    ErrorRegistry.register("DM-VOICE-4090", 409, "预置音色不可删除或修改")
    ErrorRegistry.register("DM-VOICE-4150", 415, "不支持的音频格式")
    ErrorRegistry.register("DM-VOICE-4130", 413, "音频文件过大")
    ErrorRegistry.register("DM-VOICE-5030", 503, "语音转写/合成未启用")
    ErrorRegistry.register("DM-VOICE-5000", 500, "语音转写失败")
    # MCP 客户端
    ErrorRegistry.register("DM-MCP-4001", 400, "外部 MCP Server 配置不合法")
    ErrorRegistry.register("DM-MCP-4040", 404, "外部 MCP Server 未配置")
    ErrorRegistry.register("DM-MCP-5000", 500, "外部 MCP Server 连接失败")
    # 文本处理
    ErrorRegistry.register("DM-TEXT-4040", 404, "知识库为空，无法检索")
    # 备份
    ErrorRegistry.register("DM-BACKUP-4001", 400, "备份导入失败")
    # 设置
    ErrorRegistry.register("DM-SETTINGS-4001", 400, "设置写入失败")
    # 内部
    ErrorRegistry.register("DM-INTERNAL-5000", 500, "服务器内部错误")


_register_defaults()


# =====================================================================
# 异常类型
# =====================================================================


class DeadmanError(Exception):
    """携带错误码的业务异常（可在任意业务层抛出，由全局处理器转为 JSON）。"""

    def __init__(self, code: str, message: str | None = None, details: Any = None):
        super().__init__(message or code)
        self.code = code
        self.message = (
            message or ErrorRegistry.get(code).message
            if ErrorRegistry.get(code)
            else (message or code)
        )
        self.details = details

    @property
    def http_status(self) -> int:
        ec = ErrorRegistry.get(self.code)
        return ec.http_status if ec else 500

    def to_dict(self, request_id: str = "-") -> dict[str, Any]:
        ec = ErrorRegistry.get(self.code)
        payload: dict[str, Any] = {
            "error": self.code,
            "code": self.code,
            "message": self.message,
            "severity": ec.severity if ec else "error",
            "request_id": request_id,
        }
        if self.details is not None:
            payload["detail"] = self.details
        return payload


class DeadmanHTTPException(DeadmanError):
    """兼容 FastAPI HTTPException 语义的 DeadmanError（供路由直接 raise）。"""
