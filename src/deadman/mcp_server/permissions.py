"""P3.3 工具权限分级 - 把 15 个 MCP 工具按风险等级分类

四档权限：
  - READ_ONLY    : 只读工具（query_knowledge / web_search / read_file 等），无需二次确认
  - WRITE_CONFIRM: 写操作（write_file / init_transfer），调用前需用户确认
  - WRITE_ASYNC  : 异步写/执行（execute_code），需用户确认 + 异步执行
  - DANGEROUS    : 高风险（execute_reflexion 实际触发反思重试），必须二次确认

中性工具（invoke_subagent / initiate_debate / call_external_agent / report_incident）
归为 READ_ONLY 等价档（不强制二次确认），便于缓存层判断"是否可缓存"。

Feature flag:DEADMAN_TOOL_PERMISSIONS_ENABLED=0（默认关闭）
关闭时 check_permission 一律放行，requires_confirmation 一律 False，
保证旧行为完全不变。

降级路径：未知工具默认归为 READ_ONLY，避免误伤动态注册的新工具。
"""

from __future__ import annotations

import os
from enum import Enum

# =====================================================================
# 配置（feature flag，默认关闭）
# =====================================================================

TOOL_PERMISSIONS_ENABLED: bool = os.environ.get(
    "DEADMAN_TOOL_PERMISSIONS_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")


# =====================================================================
# 权限枚举
# =====================================================================


class ToolPermission(str, Enum):
    """工具权限等级（继承 str 便于 JSON 序列化）"""

    READ_ONLY = "read_only"
    WRITE_CONFIRM = "write_confirm"
    WRITE_ASYNC = "write_async"
    DANGEROUS = "dangerous"


# =====================================================================
# 权限注册表 - 16 个工具的默认权限
# =====================================================================

PERMISSION_REGISTRY: dict[str, ToolPermission] = {
    # ---------- read-only ----------
    "query_knowledge": ToolPermission.READ_ONLY,
    "web_search": ToolPermission.READ_ONLY,
    "web_search_official": ToolPermission.READ_ONLY,
    "read_file": ToolPermission.READ_ONLY,
    "check_integrity": ToolPermission.READ_ONLY,
    "check_rules": ToolPermission.READ_ONLY,
    "query_memory": ToolPermission.READ_ONLY,
    # ---------- write-confirm ----------
    "write_file": ToolPermission.WRITE_CONFIRM,
    "init_transfer": ToolPermission.WRITE_CONFIRM,
    # ---------- write-async ----------
    "execute_code": ToolPermission.WRITE_ASYNC,
    "browser_automation": ToolPermission.WRITE_ASYNC,
    # ---------- dangerous ----------
    "execute_reflexion": ToolPermission.DANGEROUS,
    # ---------- 中性（按 read-only 对待：可缓存、无需二次确认）----------
    "invoke_subagent": ToolPermission.READ_ONLY,
    "initiate_debate": ToolPermission.READ_ONLY,
    "call_external_agent": ToolPermission.READ_ONLY,
    "report_incident": ToolPermission.READ_ONLY,
}


# =====================================================================
# 接口
# =====================================================================


def get_permission(tool_name: str) -> ToolPermission:
    """返回工具的权限等级；未知工具默认 READ_ONLY（降级，避免误伤）"""
    return PERMISSION_REGISTRY.get(tool_name, ToolPermission.READ_ONLY)


def check_permission(tool_name: str, action: str) -> bool:
    """检查工具是否允许执行给定 action

    action 取值：
      - "call"          : 普通调用（READ_ONLY / WRITE_CONFIRM / WRITE_ASYNC / DANGEROUS 都允许）
      - "call_confirmed": 已二次确认（DANGEROUS 必须走这个）
      - "cache"         : 是否允许缓存结果（仅 READ_OPTION 允许）
      - "cache_stale_ok": 同 cache

    feature flag 关闭时一律返回 True（保证旧行为不变）。
    """
    if not TOOL_PERMISSIONS_ENABLED:
        return True

    perm = get_permission(tool_name)

    if action == "cache" or action == "cache_stale_ok":
        # 仅只读工具的结果允许缓存（避免缓存副作用工具的执行结果）
        return perm == ToolPermission.READ_ONLY

    if action in ("call", "call_confirmed"):
        # DANGEROUS 必须显式 call_confirmed
        if perm == ToolPermission.DANGEROUS:
            return action == "call_confirmed"
        return True

    # 未知 action：保守放行（避免阻断新 action 类型）
    return True


def requires_confirmation(tool_name: str) -> bool:
    """判断工具是否需要二次确认

    DANGEROUS / WRITE_CONFIRM / WRITE_ASYNC 都需要确认；
    READ_ONLY 不需要。

    feature flag 关闭时一律返回 False（保证旧行为不变）。
    """
    if not TOOL_PERMISSIONS_ENABLED:
        return False

    perm = get_permission(tool_name)
    return perm in (
        ToolPermission.DANGEROUS,
        ToolPermission.WRITE_CONFIRM,
        ToolPermission.WRITE_ASYNC,
    )


def is_read_only(tool_name: str) -> bool:
    """是否只读工具（缓存层用）"""
    if not TOOL_PERMISSIONS_ENABLED:
        return False  # 缓存 feature flag 关闭时不缓存任何工具
    return get_permission(tool_name) == ToolPermission.READ_ONLY
