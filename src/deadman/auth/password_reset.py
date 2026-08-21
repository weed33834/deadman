"""密码重置令牌存储（P1-3）

设计原则：
    - 单次使用：令牌确认后立即删除（防重放）
    - 短 TTL：默认 30 分钟过期（行业最佳实践 15-60 分钟）
    - 随机足够：secrets.token_urlsafe(32) = 256 bit 熵
    - 绑定 user_id：确认时校验令牌对应的 user_id，防令牌串用
    - 文件持久化：进程重启不丢失待用令牌（虽 30 分钟内自然过期）

存储路径：~/.deadman/auth/password_reset_tokens.json
格式：{token: {"user_id": ..., "email": ..., "created_at": iso, "expires_at": iso}}

无外部依赖，纯 stdlib。
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..infrastructure.multi_tenant import DATA_ROOT

_DEFAULT_DATA_DIR = DATA_ROOT / "auth"
_DEFAULT_TTL_MINUTES = 30


class PasswordResetTokenStore:
    """密码重置令牌存储 - 基于文件的原子读写 + TTL 过期"""

    def __init__(
        self,
        data_dir: Path | None = None,
        ttl_minutes: int = _DEFAULT_TTL_MINUTES,
    ) -> None:
        self.data_dir: Path = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tokens_file: Path = self.data_dir / "password_reset_tokens.json"
        self.ttl: timedelta = timedelta(minutes=ttl_minutes)
        # 文件读写锁（多线程/多进程并发安全；多进程靠原子 rename 兜底）
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        """加载令牌字典（不存在 / 损坏时返回空 dict）"""
        if not self.tokens_file.exists():
            return {}
        try:
            text = self.tokens_file.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _atomic_write(self, data: dict[str, dict[str, Any]]) -> None:
        """原子写入（先写临时文件再 rename，防中途崩溃损坏）"""
        tmp = self.tokens_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.tokens_file)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _is_expired(self, entry: dict[str, Any]) -> bool:
        expires_at_str = entry.get("expires_at")
        if not expires_at_str:
            return True
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            return self._now() >= expires_at
        except (ValueError, TypeError):
            return True

    def _purge_expired(self, data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """删除所有过期令牌（原地修改 + 返回）"""
        expired_tokens = [t for t, e in data.items() if self._is_expired(e)]
        for token in expired_tokens:
            del data[token]
        if expired_tokens:
            self._atomic_write(data)
        return data

    def create_token(self, user_id: str, email: str) -> str:
        """为用户创建密码重置令牌

        Args:
            user_id: 用户 ID
            email: 用户邮箱（记录用，确认时不依赖此字段）

        Returns:
            令牌字符串（URL-safe base64，32 字节熵）
        """
        token = secrets.token_urlsafe(32)
        now = self._now()
        entry = {
            "user_id": user_id,
            "email": email,
            "created_at": now.isoformat(),
            "expires_at": (now + self.ttl).isoformat(),
        }
        with self._lock:
            data = self._purge_expired(self._load())
            data[token] = entry
            self._atomic_write(data)
        return token

    def consume_token(self, token: str) -> dict[str, Any] | None:
        """消费令牌（单次使用：成功后立即删除）

        Args:
            token: 令牌字符串

        Returns:
            {"user_id": ..., "email": ...} —— 令牌有效且未过期
            None —— 令牌不存在 / 已过期 / 已使用
        """
        if not token:
            return None
        with self._lock:
            data = self._purge_expired(self._load())
            entry = data.pop(token, None)
            if entry is None:
                # 即使无变更也要写回（purge 可能已删除部分）
                return None
            self._atomic_write(data)
            return {
                "user_id": entry.get("user_id"),
                "email": entry.get("email"),
            }

    def peek_token(self, token: str) -> dict[str, Any] | None:
        """查看令牌（不消费，用于测试 / 调试）

        Returns:
            令牌详情或 None（不存在 / 已过期）
        """
        if not token:
            return None
        with self._lock:
            data = self._purge_expired(self._load())
            entry = data.get(token)
            if entry is None:
                return None
            return dict(entry)

    def purge_all(self) -> int:
        """清空所有令牌（仅测试 / 紧急重置用）

        Returns:
            删除的令牌数量
        """
        with self._lock:
            data = self._load()
            count = len(data)
            self._atomic_write({})
            return count
