"""P5.6 Honeypot 工具 - 暴露假工具诱捕恶意调用

借鉴 Canaries / Honeytokens 的思路，向 LLM 工具列表注册几个"假工具"
（如 delete_all_files / grant_admin_access / disable_safety / dump_credentials），
正常用户和合规 agent 永远不会调用这些工具；一旦被调用，说明：
1. LLM 被 prompt injection 攻击者操纵，或
2. Agent 决策逻辑出错，或
3. 有恶意用户尝试通过 LLM 触发危险操作

被调用时立即：
- 告警（logger.warning）
- 记录到审计链（event_type=security_alert，feature flag 控制）
- 返回拒绝响应（不实际执行任何危险操作）

核心组件：
- HONEYPOT_TOOLS: 假工具名列表
- HONEYPOT_TOOL_DEFINITIONS: 假工具的 description / input_schema（用于注册）
- HoneypotManager: 判断 / 触发告警 / 注册到 MCP server

Feature flag: DEADMAN_HONEYPOT_ENABLED=0 默认关闭
- 关闭时 is_honeypot 返回 False，trigger 静默 no-op，
  register_honeypot_tools 不注册任何工具
- 开启时所有操作生效

降级路径全覆盖：
1. feature flag 关闭 → is_honeypot=False, trigger no-op, register 不注册
2. 审计链不可用（AUDIT_CHAIN_ENABLED=False）→ 仅 logger.warning，不抛异常
3. MCP server register_tool 失败 → 仅 warning，不抛异常
4. trigger 本身异常 → 仅 warning，不阻断调用方

设计要点：
- 假工具的 handler 始终返回拒绝响应（永不执行危险操作）
- trigger 记录 caller 信息（agent_name / user_id / tool_name）便于追溯
- 不引入新依赖，仅用 stdlib + 已有 audit 模块
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
HONEYPOT_ENABLED: bool = os.environ.get("DEADMAN_HONEYPOT_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# =====================================================================
# 假工具定义
# =====================================================================

HONEYPOT_TOOLS: list[str] = [
    "delete_all_files",
    "grant_admin_access",
    "disable_safety",
    "dump_credentials",
    "execute_arbitrary_command",
    "bypass_authentication",
    "delete_user_data",
    "read_system_secrets",
]


def _build_honeypot_tool_definitions() -> list[dict[str, Any]]:
    """构造假工具的定义列表（用于注册到 MCP server）

    每个定义包含 name / description / input_schema / handler。
    handler 始终返回拒绝响应（永不执行危险操作）。
    """
    definitions: list[dict[str, Any]] = []
    for tool_name in HONEYPOT_TOOLS:
        definitions.append(
            {
                "name": tool_name,
                "description": (
                    f"[HONEYPOT] {tool_name} - 此工具是蜜罐，正常流程不应调用。"
                    f"被调用即触发安全告警。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "操作目标"},
                        "reason": {"type": "string", "description": "调用原因"},
                    },
                    "required": [],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "blocked": {"type": "boolean"},
                        "alert_id": {"type": "string"},
                    },
                },
            }
        )
    return definitions


HONEYPOT_TOOL_DEFINITIONS: list[dict[str, Any]] = _build_honeypot_tool_definitions()


# =====================================================================
# HoneypotManager
# =====================================================================


class HoneypotManager:
    """Honeypot 假工具管理器

    所有操作在 HONEYPOT_ENABLED=False 时静默 no-op。
    """

    def __init__(self):
        self._triggered: list[dict[str, Any]] = []  # 内存记录被触发的蜜罐（测试用）

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def is_honeypot(self, tool_name: str) -> bool:
        """判断工具名是否是蜜罐工具

        Args:
            tool_name: 工具名

        Returns:
            True 表示是蜜罐工具；
            feature flag 关闭时返回 False（所有工具都视为非蜜罐）
        """
        if not HONEYPOT_ENABLED:
            return False
        return tool_name in HONEYPOT_TOOLS

    def trigger(self, tool_name: str, caller: str = "") -> None:
        """蜜罐被调用时触发告警

        Args:
            tool_name: 被调用的蜜罐工具名
            caller: 调用者（agent_name / user_id，可选）

        降级路径：
        1. HONEYPOT_ENABLED=False → 静默 no-op
        2. 审计链不可用 → 仅 logger.warning，不抛异常
        3. trigger 本身异常 → 仅 warning，不阻断调用方
        """
        if not HONEYPOT_ENABLED:
            return

        # 告警日志
        logger.warning(
            "HONEYPOT TRIGGERED: tool=%s caller=%s — 此工具是蜜罐，"
            "正常流程不应调用，可能存在 prompt injection 或 agent 决策异常",
            tool_name,
            caller or "(unknown)",
        )

        # 记录到内存（测试用）
        self._triggered.append(
            {
                "tool_name": tool_name,
                "caller": caller,
            }
        )

        # 记录到审计链（feature flag 控制）
        # 审计链本身也是 feature flag 控制，关闭时 get_audit_chain().append 返回 None
        try:
            from .audit import get_audit_chain

            get_audit_chain().append(
                event_type="security_alert",
                actor=caller or "(unknown)",
                action="honeypot_triggered",
                target=tool_name,
                metadata={
                    "alert_type": "honeypot",
                    "tool_name": tool_name,
                    "caller": caller,
                },
            )
        except Exception as e:
            # 审计链失败不阻断告警（已 logger.warning）
            logger.warning("honeypot trigger 记录审计链失败: %s", e)

    def register_honeypot_tools(self, mcp_server: Any) -> int:
        """把蜜罐工具注册到 MCP server

        Args:
            mcp_server: MCP server 实例（需有 register_tool 方法）

        Returns:
            成功注册的工具数量；feature flag 关闭返回 0

        降级路径：
        1. HONEYPOT_ENABLED=False → 不注册，返回 0
        2. mcp_server 无 register_tool 方法 → 仅 warning，返回 0
        3. 单个工具注册失败 → 跳过该工具，继续注册其他
        """
        if not HONEYPOT_ENABLED:
            return 0

        if mcp_server is None or not hasattr(mcp_server, "register_tool"):
            logger.warning("mcp_server 无 register_tool 方法，无法注册蜜罐工具")
            return 0

        registered = 0
        for defn in HONEYPOT_TOOL_DEFINITIONS:
            try:
                # 构造拒绝 handler（始终返回 blocked=True）
                tool_name = defn["name"]
                handler = self._make_honeypot_handler(tool_name)
                mcp_server.register_tool(
                    name=tool_name,
                    description=defn["description"],
                    input_schema=defn["input_schema"],
                    handler=handler,
                    output_schema=defn.get("output_schema"),
                )
                registered += 1
            except Exception as e:
                logger.warning("注册蜜罐工具 %s 失败: %s", defn.get("name"), e)
                continue

        logger.info("HONEYPOT: 已注册 %d 个蜜罐工具到 MCP server", registered)
        return registered

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _make_honeypot_handler(self, tool_name: str):
        """为蜜罐工具构造 handler（始终返回拒绝响应）

        handler 被调用时：
        1. 触发告警（trigger）
        2. 返回 blocked=True 的拒绝响应（不执行任何危险操作）
        """
        manager = self

        async def _honeypot_handler(**kwargs: Any) -> dict[str, Any]:
            # 提取 caller（从 kwargs 或用默认值）
            caller = str(kwargs.get("caller", "")) or "(unknown)"
            manager.trigger(tool_name, caller)
            return {
                "ok": False,
                "blocked": True,
                "alert_id": f"honeypot-{tool_name}",
                "message": (f"工具 {tool_name} 是蜜罐，已被阻断。此调用已触发安全告警。"),
            }

        return _honeypot_handler

    # ------------------------------------------------------------------
    # 测试辅助方法
    # ------------------------------------------------------------------

    def get_triggered(self) -> list[dict[str, Any]]:
        """返回被触发的蜜罐记录（主要用于测试）"""
        return list(self._triggered)

    def clear_triggered(self) -> None:
        """清空触发记录（主要用于测试）"""
        self._triggered.clear()


# =====================================================================
# 全局单例（延迟初始化）
# =====================================================================

_manager_instance: HoneypotManager | None = None


def get_honeypot_manager() -> HoneypotManager:
    """获取全局 HoneypotManager 单例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = HoneypotManager()
    return _manager_instance


def reset_honeypot_manager() -> None:
    """重置全局单例（主要用于测试）"""
    global _manager_instance
    _manager_instance = None
