"""P5.2 JIT 短时工具权限 - Just-In-Time 权限发放与验证

借鉴 Zero Trust "默认拒绝 + 按需授权 + 短时失效" 模型，对敏感工具调用
（如 write_file / delete_record / send_notification）要求先获得短时 token
才能执行，避免长期权限滥用。

核心组件：
- JITToken: 单次授权的短时 token（token/tool_name/scope/granted_at/expires_at/
            granted_to）
- JITPermissionManager: 发放 / 验证 / 撤销 / 清理过期 token

Feature flag: DEADMAN_JIT_PERMISSION_ENABLED=0 默认关闭
- 关闭时 grant 返回 None，verify 返回 True（兼容旧路径，不阻断工具调用），
  revoke/cleanup 静默 no-op
- 开启时所有操作生效；token 持久化到 data/jit_tokens.json（重启后仍有效，
  过期 token 在 cleanup_expired 时清理）

降级路径全覆盖：
1. feature flag 关闭 → grant 返回 None，verify 返回 True（不阻断主流程）
2. 持久化文件不可写 → 仅内存操作，记 warning 不抛异常
3. 持久化文件损坏 → 加载时跳过损坏条目，不抛异常
4. clock skew → 用 time.time() 比较 expires_at，过期即拒绝

设计要点：
- token 用 secrets.token_urlsafe(32) 生成（密码学安全随机）
- TTL 默认 300 秒（5 分钟），可在 grant 时覆盖
- 内存 dict 存储 + 持久化到 JSON（重启后 token 仍有效）
- 不引入 cryptography 等重依赖，仅用 stdlib（secrets/time/json）
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
JIT_PERMISSION_ENABLED: bool = os.environ.get("DEADMAN_JIT_PERMISSION_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# 持久化文件路径（settings.project_root.parent = /workspace/deadman/）
DEFAULT_JIT_PATH = Path("data") / "jit_tokens.json"

# 默认 TTL（秒）
DEFAULT_TTL_SECONDS = 300


# =====================================================================
# 数据模型
# =====================================================================


@dataclass
class JITToken:
    """单次 JIT 授权 token

    Attributes:
        token: 随机 token 字符串（secrets.token_urlsafe(32) 生成）
        tool_name: 被授权的工具名
        scope: 授权范围（如 "read" / "write" / "delete"，工具自定义）
        granted_at: 发放时间（unix 时间戳，秒）
        expires_at: 过期时间（unix 时间戳，秒）
        granted_to: 被授权主体（user_id / agent_name，可选）
    """

    token: str
    tool_name: str
    scope: str
    granted_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    granted_to: str = ""

    def is_expired(self, now: float | None = None) -> bool:
        """判断 token 是否已过期"""
        current = now if now is not None else time.time()
        return current >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的 dict"""
        return {
            "token": self.token,
            "tool_name": self.tool_name,
            "scope": self.scope,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "granted_to": self.granted_to,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JITToken:
        """从 dict 反序列化（容错：缺失字段填默认）"""
        return cls(
            token=str(data.get("token", "")),
            tool_name=str(data.get("tool_name", "")),
            scope=str(data.get("scope", "")),
            granted_at=float(data.get("granted_at", 0.0)),
            expires_at=float(data.get("expires_at", 0.0)),
            granted_to=str(data.get("granted_to", "")),
        )


# =====================================================================
# JITPermissionManager
# =====================================================================


class JITPermissionManager:
    """JIT 短时工具权限管理器

    所有写操作在 JIT_PERMISSION_ENABLED=False 时静默 no-op（grant 返回 None）。
    verify 在 JIT_PERMISSION_ENABLED=False 时返回 True（兼容旧路径，不阻断工具调用）。
    """

    def __init__(self, persist_path: str | Path | None = None):
        """Args:
        persist_path: 持久化文件路径；None 用默认 data/jit_tokens.json
        """
        if persist_path is None:
            self._path = settings.project_root.parent / DEFAULT_JIT_PATH
        else:
            self._path = Path(persist_path)
        # 内存存储：token -> JITToken
        self._tokens: dict[str, JITToken] = {}
        # 启动时从磁盘加载已有 token
        self._load_from_disk()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> None:
        """从磁盘加载已有 token（重启后仍有效）

        容错：文件不存在 / JSON 损坏 / 单个 token 字段缺失都不抛异常。
        """
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            tokens_list = data.get("tokens", [])
            if not isinstance(tokens_list, list):
                return
            for item in tokens_list:
                if not isinstance(item, dict):
                    continue
                try:
                    token = JITToken.from_dict(item)
                    if token.token:
                        self._tokens[token.token] = token
                except Exception as e:
                    logger.debug("加载 jit token 失败，跳过: %s", e)
                    continue
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("加载 jit_tokens.json 失败: %s", e)

    def _persist_to_disk(self) -> bool:
        """把当前内存中的 token 持久化到磁盘

        失败时仅 warning，不抛异常。

        Returns:
            True 表示成功写入；False 表示写入失败
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "tokens": [t.to_dict() for t in self._tokens.values()],
                "updated_at": time.time(),
            }
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except OSError as e:
            logger.warning("jit_tokens.json 持久化失败（仅内存）: %s", e)
            return False

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def grant(
        self,
        tool_name: str,
        scope: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        granted_to: str = "",
    ) -> JITToken | None:
        """发放一个短时 JIT token

        Args:
            tool_name: 被授权的工具名
            scope: 授权范围（如 "read" / "write" / "delete"）
            ttl_seconds: TTL 秒数（默认 300 = 5 分钟）
            granted_to: 被授权主体（user_id / agent_name）

        Returns:
            JITToken 实例；feature flag 关闭时返回 None

        降级路径：
        1. JIT_PERMISSION_ENABLED=False → 返回 None
        2. 持久化失败 → 仅内存存储，不抛异常
        """
        if not JIT_PERMISSION_ENABLED:
            logger.debug("jit permission disabled (DEADMAN_JIT_PERMISSION_ENABLED=0), skip")
            return None

        now = time.time()
        token = JITToken(
            token=secrets.token_urlsafe(32),
            tool_name=tool_name,
            scope=scope,
            granted_at=now,
            expires_at=now + max(1, int(ttl_seconds)),
            granted_to=granted_to,
        )
        self._tokens[token.token] = token
        self._persist_to_disk()

        logger.info(
            "jit token granted: tool=%s scope=%s to=%s ttl=%ss (token=%s...)",
            tool_name,
            scope,
            granted_to or "(anonymous)",
            ttl_seconds,
            token.token[:8],
        )
        return token

    def verify(
        self,
        token: str,
        tool_name: str,
        scope: str | None = None,
    ) -> bool:
        """验证 token 是否有效

        Args:
            token: 待验证的 token 字符串
            tool_name: 期望的工具名（必须匹配）
            scope: 期望的 scope（None 不校验 scope；给定则必须匹配）

        Returns:
            True 表示 token 有效且未过期；
            feature flag 关闭时返回 True（兼容旧路径，不阻断工具调用）

        降级路径：
        1. JIT_PERMISSION_ENABLED=False → 返回 True（不阻断工具调用）
        2. token 不存在 / 已过期 / tool_name 不匹配 / scope 不匹配 → 返回 False
        """
        if not JIT_PERMISSION_ENABLED:
            # feature flag 关闭：放行所有调用（兼容旧路径）
            return True

        if not token:
            return False
        t = self._tokens.get(token)
        if t is None:
            return False
        if t.is_expired():
            return False
        if t.tool_name != tool_name:
            return False
        return not (scope is not None and t.scope != scope)

    def revoke(self, token: str) -> bool:
        """撤销一个 token

        Args:
            token: 待撤销的 token 字符串

        Returns:
            True 表示撤销成功（token 之前存在）；
            False 表示 token 不存在或 feature flag 关闭
        """
        if not JIT_PERMISSION_ENABLED:
            return False
        if token not in self._tokens:
            return False
        del self._tokens[token]
        self._persist_to_disk()
        logger.info("jit token revoked: token=%s...", token[:8])
        return True

    def cleanup_expired(self) -> int:
        """清理所有过期 token

        Returns:
            清理的 token 数量；feature flag 关闭返回 0
        """
        if not JIT_PERMISSION_ENABLED:
            return 0
        now = time.time()
        expired_tokens = [t for t in self._tokens if self._tokens[t].is_expired(now)]
        for t in expired_tokens:
            del self._tokens[t]
        if expired_tokens:
            self._persist_to_disk()
            logger.info("jit cleanup: removed %d expired tokens", len(expired_tokens))
        return len(expired_tokens)

    # ------------------------------------------------------------------
    # 辅助方法（主要用于测试和诊断）
    # ------------------------------------------------------------------

    def count(self) -> int:
        """返回当前内存中的 token 总数（feature flag 关闭返回 0）"""
        if not JIT_PERMISSION_ENABLED:
            return 0
        return len(self._tokens)

    def get_token(self, token: str) -> JITToken | None:
        """获取 token 详情（不存在返回 None）"""
        if not JIT_PERMISSION_ENABLED:
            return None
        return self._tokens.get(token)

    def clear(self) -> None:
        """清空所有 token（主要用于测试）"""
        self._tokens.clear()
        if not JIT_PERMISSION_ENABLED:
            return
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError as e:
            logger.warning("清空 jit_tokens.json 失败: %s", e)


# =====================================================================
# 全局单例（延迟初始化）
# =====================================================================

_manager_instance: JITPermissionManager | None = None


def get_jit_manager() -> JITPermissionManager:
    """获取全局 JITPermissionManager 单例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = JITPermissionManager()
    return _manager_instance


def reset_jit_manager() -> None:
    """重置全局单例（主要用于测试）"""
    global _manager_instance
    _manager_instance = None
