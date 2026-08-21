"""邀请令牌存储 - 机构成员邀请

设计（对齐 auth/password_reset.py 模式）:
  - 单次使用：消费后立即删除（防重放）
  - TTL：默认 24 小时过期
  - 随机足够：secrets.token_urlsafe(32) = 256 bit 熵
  - 绑定 org_id/email/role：消费时校验，防令牌串用

存储路径：{org_data_dir}/invites.json
格式：{token: {org_id, email, role, invited_by, created_at, expires_at}}
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .rbac import ORG_ROLES

_DEFAULT_DATA_DIR = Path.home() / ".deadman" / "org"
_DEFAULT_TTL_HOURS = 24


class InviteStore:
    """机构邀请令牌 - 基于文件的原子读写 + TTL 过期"""

    def __init__(
        self,
        data_dir: Path | None = None,
        ttl_hours: int = _DEFAULT_TTL_HOURS,
    ) -> None:
        self.data_dir: Path = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.invites_file: Path = self.data_dir / "invites.json"
        self.ttl: timedelta = timedelta(hours=ttl_hours)
        self._lock = threading.Lock()

    def create_invite(
        self,
        org_id: str,
        email: str,
        role: str = "viewer",
        invited_by: str | None = None,
    ) -> str:
        """创建邀请令牌。

        Args:
            org_id: 目标机构 ID
            email: 被邀请人邮箱（记录用，消费时校验绑定）
            role: 受邀角色（viewer/consultant/case_manager/org_admin）
            invited_by: 邀请人 user_id

        Returns:
            令牌字符串（URL-safe base64，32 字节熵）
        """
        if not org_id or not email:
            raise ValueError("org_id 与 email 不能为空")
        if role not in ORG_ROLES:
            raise ValueError(f"role 仅支持 {ORG_ROLES}")
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        entry: dict[str, Any] = {
            "org_id": org_id,
            "email": email.strip().lower(),
            "role": role,
            "invited_by": invited_by,
            "created_at": now.isoformat(),
            "expires_at": (now + self.ttl).isoformat(),
        }
        with self._lock:
            data = self._purge_expired(self._load())
            data[token] = entry
            self._atomic_write(data)
        return token

    def consume_invite(self, token: str) -> dict[str, Any] | None:
        """消费令牌（单次使用：成功后立即删除）。

        Returns:
            {"org_id": ..., "email": ..., "role": ...} 或 None（不存在/过期/已用）
        """
        if not token:
            return None
        with self._lock:
            data = self._purge_expired(self._load())
            entry = data.pop(token, None)
            if entry is None:
                return None
            self._atomic_write(data)
            return {
                "org_id": entry.get("org_id"),
                "email": entry.get("email"),
                "role": entry.get("role"),
                "invited_by": entry.get("invited_by"),
            }

    def peek_invite(self, token: str) -> dict[str, Any] | None:
        """查看令牌（不消费，用于测试/调试）。"""
        if not token:
            return None
        with self._lock:
            data = self._purge_expired(self._load())
            entry = data.get(token)
            return dict(entry) if entry else None

    def list_invites(self, org_id: str) -> list[dict[str, Any]]:
        with self._lock:
            data = self._purge_expired(self._load())
            return [{"token": t, **e} for t, e in data.items() if e.get("org_id") == org_id]

    def revoke_invite(self, token: str) -> bool:
        """吊销未使用的邀请令牌。"""
        if not token:
            return False
        with self._lock:
            data = self._purge_expired(self._load())
            if token not in data:
                return False
            del data[token]
            self._atomic_write(data)
            return True

    def purge_all(self) -> int:
        """清空所有令牌（仅测试/紧急场景）。"""
        with self._lock:
            data = self._load()
            count = len(data)
            self._atomic_write({})
            return count

    # ================================================================
    # 内部工具
    # ================================================================

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.invites_file.exists():
            return {}
        try:
            text = self.invites_file.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _atomic_write(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self.invites_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.invites_file)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _is_expired(self, entry: dict[str, Any]) -> bool:
        expires = entry.get("expires_at")
        if not expires:
            return True
        try:
            return self._now() >= datetime.fromisoformat(expires)
        except (ValueError, TypeError):
            return True

    def _purge_expired(self, data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        expired = [t for t, e in data.items() if self._is_expired(e)]
        for token in expired:
            del data[token]
        if expired:
            self._atomic_write(data)
        return data
