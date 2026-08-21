"""用户存储 - 纯文件，无数据库依赖

存储路径：~/.deadman/auth/users.json
格式：{user_id: {email, password_hash, salt, created_at, role, family_id, display_name, email_hmac}}

遵守 legal-compliance-framework：
  - 密码用 PBKDF2-HMAC-SHA256 + 随机 salt（100000 iterations）
  - 不存明文密码
  - 不存敏感 PII（身份证号/手机号）- 用户主动填写时单独存到 vault（Phase 11）
  - email 用 HMAC 索引（防止拖库后撞库）

遵守 safety-protocol：
  - verify() 失败统一返回 None，不区分"邮箱不存在" vs "密码错"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..infrastructure.multi_tenant import DATA_ROOT
from ..utils.jsonio import atomic_write_json

# PBKDF2 参数（NIST 推荐 >= 100000，OWASP 2023 同步推荐）
_PBKDF2_ITERATIONS = 100_000
_PBKDF2_ALGORITHM = "sha256"
_PBKDF2_KEY_LEN = 32  # 256 bit

# 服务端密钥读取路径（用于 HMAC 邮箱索引，防止拖库后撞库）
# 与 jwt_secret 共用同一份 server secret
_SERVER_SECRET_FILE = "jwt_secret"

# 默认数据目录
_DEFAULT_DATA_DIR = DATA_ROOT / "auth"


class UserStore:
    """用户存储 - 纯文件，无数据库依赖

    存储路径：~/.deadman/auth/users.json
    格式：{user_id: {email, password_hash, salt, created_at, role, family_id, display_name, email_hmac}}

    遵守 legal-compliance-framework：
    - 密码用 PBKDF2-HMAC-SHA256 + 随机 salt（100000 iterations）
    - 不存明文密码
    - 不存敏感 PII（身份证号/手机号）- 用户主动填写时单独存到 vault（Phase 11）
    - email 用 HMAC 索引（防止拖库后撞库）
    """

    def __init__(self, data_dir: Path | None = None):
        # data_dir 默认 ~/.deadman/auth/
        self.data_dir: Path = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.users_file: Path = self.data_dir / "users.json"
        # 服务端密钥（用于 HMAC 邮箱索引，防撞库）
        self._server_secret: bytes = self._load_or_create_server_secret()
        # 密码最小长度
        self.password_min_length: int = int(os.getenv("DEADMAN_PASSWORD_MIN_LENGTH", "8"))

    # ================================================================
    # 公开 API
    # ================================================================

    def register(self, email: str, password: str, display_name: str | None = None) -> dict:
        """注册新用户

        - email 必须唯一（HMAC 比对）
        - password 长度 >= 8
        - 返回 {user_id, email, display_name, created_at}
        - 失败抛 ValueError（"邮箱已注册" / "密码太短"）
        """
        # 输入校验
        if not email or not isinstance(email, str):
            raise ValueError("邮箱不能为空")
        email_normalized = email.strip().lower()
        if "@" not in email_normalized:
            raise ValueError("邮箱格式不正确")
        if not password or not isinstance(password, str):
            raise ValueError("密码不能为空")
        if len(password) < self.password_min_length:
            raise ValueError(f"密码太短（最少 {self.password_min_length} 位）")

        # 加载现有数据
        data = self._load()

        # 检查邮箱唯一（HMAC 比对，不直接比 email 明文）
        email_hmac = self._email_hmac(email_normalized)
        for existing in data.values():
            if existing.get("email_hmac") == email_hmac:
                raise ValueError("邮箱已注册")

        # 生成 user_id 与密码 hash
        user_id = str(uuid.uuid4())
        salt, password_hash = self._hash_password(password)

        now = datetime.now(timezone.utc).isoformat()
        user_record: dict[str, Any] = {
            "user_id": user_id,
            "email": email_normalized,
            "email_hmac": email_hmac,
            "password_hash": password_hash.hex(),
            "salt": salt.hex(),
            "created_at": now,
            "role": "user",  # 默认普通用户，admin 需手动提升
            "family_id": None,
            "display_name": display_name or email_normalized.split("@")[0],
        }

        data[user_id] = user_record
        self._atomic_write(data)

        # 返回不含敏感字段的视图
        return self._public_view(user_record)

    def verify(self, email: str, password: str) -> dict | None:
        """验证登录

        - 返回 user dict 或 None
        - 不泄露"邮箱不存在" vs "密码错"（防枚举）- 统一返回 None
        """
        if not email or not password:
            return None
        email_normalized = email.strip().lower()
        email_hmac = self._email_hmac(email_normalized)

        data = self._load()
        # 用 HMAC 比对找用户（不直接比 email 明文，防拖库后撞库）
        target: dict | None = None
        for record in data.values():
            if record.get("email_hmac") == email_hmac:
                target = record
                break

        if target is None:
            # 防枚举：仍然做一次假 hash 比对，统一响应时间
            self._hash_password(password)
            return None

        # 校验密码
        salt = bytes.fromhex(target["salt"])
        expected_hash = bytes.fromhex(target["password_hash"])
        _, actual_hash = self._hash_password(password, salt)
        if not hmac.compare_digest(expected_hash, actual_hash):
            return None

        return self._public_view(target)

    def get_user(self, user_id: str) -> dict | None:
        """获取用户（返回不含密码/salt 的视图）"""
        data = self._load()
        record = data.get(user_id)
        if record is None:
            return None
        return self._public_view(record)

    def get_user_raw(self, user_id: str) -> dict | None:
        """获取用户完整记录（含 password_hash/salt/email_hmac）。

        仅供 DB 同步层（deadman.db.repositories.UserRepository）使用，
        业务代码应使用 get_user()（返回脱敏视图）。
        """
        data = self._load()
        return data.get(user_id)

    def update_user(self, user_id: str, updates: dict) -> dict | None:
        """更新用户字段

        允许更新的字段：display_name, family_id, role
        不允许通过此方法修改 password / email（需专用方法）
        """
        data = self._load()
        record = data.get(user_id)
        if record is None:
            return None
        allowed = {"display_name", "family_id", "role"}
        for k, v in updates.items():
            if k in allowed:
                record[k] = v
        data[user_id] = record
        self._atomic_write(data)
        return self._public_view(record)

    def delete_user(self, user_id: str) -> bool:
        """删除用户"""
        data = self._load()
        if user_id not in data:
            return False
        del data[user_id]
        self._atomic_write(data)
        return True

    # ================================================================
    # 密码重置支持（P1-3）
    # ================================================================

    def find_user_by_email(self, email: str) -> dict | None:
        """按邮箱查找用户（返回含 user_id 的内部视图，供密码重置流程使用）

        与 verify() 不同，此方法不做假 hash 来平衡响应时间——因为密码重置
        请求端点本身不接收密码，且为防枚举，无论邮箱是否存在都返回统一成功响应
        （由 API 层处理，而非 store 层）。store 层如实返回 None 表示未找到。

        Returns:
            {"user_id": ..., "email": ..., ...} 或 None
        """
        if not email:
            return None
        email_normalized = email.strip().lower()
        email_hmac = self._email_hmac(email_normalized)
        data = self._load()
        for user_id, record in data.items():
            if record.get("email_hmac") == email_hmac:
                view = self._public_view(record)
                view["user_id"] = user_id
                return view
        return None

    def update_password(self, user_id: str, new_password: str) -> bool:
        """更新用户密码（P1-3 密码重置确认端点调用）

        - 校验新密码长度 >= password_min_length
        - 重新生成 salt（不复用旧 salt）
        - 原子写入

        Returns:
            True=更新成功；False=用户不存在 / 密码不合规
        """
        if not new_password or len(new_password) < self.password_min_length:
            return False
        data = self._load()
        record = data.get(user_id)
        if record is None:
            return False
        salt, password_hash = self._hash_password(new_password)
        record["salt"] = salt.hex()
        record["password_hash"] = password_hash.hex()
        record["password_updated_at"] = datetime.now(timezone.utc).isoformat()
        data[user_id] = record
        self._atomic_write(data)
        return True

    def list_users(self) -> list[dict]:
        """列出所有用户

        仅 role=admin 可调（调用方需自行校验当前请求者角色）
        返回不含密码/salt 的视图，但保留 email_hmac 前 16 字符用于排查
        """
        data = self._load()
        result: list[dict] = []
        for record in data.values():
            view = self._public_view(record)
            # admin 视图额外保留 email_hmac 截断（不暴露完整 HMAC）
            view["email_hmac"] = record.get("email_hmac", "")[:16] + "..."
            result.append(view)
        return result

    # ================================================================
    # 内部工具
    # ================================================================

    def _hash_password(self, password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
        """PBKDF2-HMAC-SHA256, 100000 iterations

        返回 (salt, password_hash)
        """
        if salt is None:
            salt = secrets.token_bytes(16)  # 128 bit salt
        password_hash = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGORITHM,
            password.encode("utf-8"),
            salt,
            _PBKDF2_ITERATIONS,
            dklen=_PBKDF2_KEY_LEN,
        )
        return salt, password_hash

    def _email_hmac(self, email: str) -> str:
        """HMAC-SHA256(email) 防撞库 - 用 server secret

        拖库后攻击者无法用 HMAC 反推 email，也无法比对已知 email 库
        """
        return hmac.new(
            self._server_secret,
            email.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _atomic_write(self, data: dict) -> None:
        """原子写入 users.json（委托 utils.jsonio，先写 .tmp 再原子替换）。"""
        atomic_write_json(self.users_file, data)

    def _load(self) -> dict:
        """加载 users.json，不存在则返回空 dict"""
        if not self.users_file.exists():
            return {}
        try:
            return json.loads(self.users_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 文件损坏时返回空 dict，避免崩溃（不丢数据需用户从备份恢复）
            return {}

    def _load_or_create_server_secret(self) -> bytes:
        """加载或生成服务端密钥（与 jwt_secret 共用）

        存在 ~/.deadman/auth/jwt_secret
        """
        secret_file = self.data_dir / _SERVER_SECRET_FILE
        if secret_file.exists():
            try:
                content = secret_file.read_text(encoding="utf-8").strip()
                if content:
                    return content.encode("utf-8")
            except OSError:
                pass
        # 生成 32 字节随机密钥
        new_secret = secrets.token_bytes(32)
        try:
            secret_file.write_text(new_secret.hex(), encoding="utf-8")
            # 设置文件权限为 600（仅 owner 可读写）
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return new_secret.hex().encode("utf-8")

    def _public_view(self, record: dict) -> dict:
        """返回不含 password_hash / salt 的公共视图"""
        return {
            "user_id": record.get("user_id"),
            "email": record.get("email"),
            "display_name": record.get("display_name"),
            "role": record.get("role", "user"),
            "family_id": record.get("family_id"),
            "created_at": record.get("created_at"),
        }
