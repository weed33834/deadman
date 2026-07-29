"""测试 deadman.auth.store - 用户存储（纯文件，无 DB）

覆盖点（9 个）：
  - test_register_success: 注册成功，返回 user_id/email/display_name
  - test_register_duplicate_email_fails: 重复邮箱失败（HMAC 比对）
  - test_register_short_password_fails: 密码 < 8 失败
  - test_verify_success: 正确密码登录成功
  - test_verify_wrong_password_returns_none: 错误密码返回 None（不抛异常）
  - test_verify_nonexistent_email_returns_none: 不存在邮箱返回 None（不泄露）
  - test_password_not_stored_plaintext: 文件中无明文密码（legal-compliance）
  - test_email_hmac_not_plaintext: 文件中无明文邮箱（legal-compliance）
  - test_atomic_write: 写入失败原文件不损坏

测试隔离：每个测试用 tmp_path 构造独立 UserStore，不污染 ~/.deadman
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from deadman.auth.store import UserStore

# =====================================================================
# 1-3. 注册路径
# =====================================================================


class TestRegister:
    """注册路径测试"""

    def test_register_success(self, tmp_path: Path):
        # 注册成功，返回 user_id/email/display_name
        store = UserStore(data_dir=tmp_path)
        user = store.register("alice@example.com", "password123", "Alice")
        assert user["user_id"]
        assert user["email"] == "alice@example.com"
        assert user["display_name"] == "Alice"
        assert user["created_at"]
        # user_id 应是 uuid 形式
        assert len(user["user_id"]) >= 32

    def test_register_duplicate_email_fails(self, tmp_path: Path):
        # 重复邮箱失败（HMAC 比对，case-insensitive）
        store = UserStore(data_dir=tmp_path)
        store.register("bob@example.com", "password123")
        # 同邮箱再注册应抛 ValueError
        with pytest.raises(ValueError, match="邮箱已注册"):
            store.register("bob@example.com", "anotherpass456")
        # 大小写不敏感也应识别为重复
        with pytest.raises(ValueError, match="邮箱已注册"):
            store.register("BOB@example.com", "anotherpass456")

    def test_register_short_password_fails(self, tmp_path: Path):
        # 密码 < 8 失败
        store = UserStore(data_dir=tmp_path)
        with pytest.raises(ValueError, match="密码太短"):
            store.register("carol@example.com", "short")


# =====================================================================
# 4-6. 验证路径（防枚举）
# =====================================================================


class TestVerify:
    """验证路径测试 - 关键是防枚举（不区分邮箱不存在 vs 密码错）"""

    def test_verify_success(self, tmp_path: Path):
        # 正确密码登录成功
        store = UserStore(data_dir=tmp_path)
        store.register("dave@example.com", "password123", "Dave")
        user = store.verify("dave@example.com", "password123")
        assert user is not None
        assert user["email"] == "dave@example.com"
        assert user["display_name"] == "Dave"
        # 不应包含敏感字段
        assert "password_hash" not in user
        assert "salt" not in user

    def test_verify_wrong_password_returns_none(self, tmp_path: Path):
        # 错误密码返回 None（不抛异常）
        store = UserStore(data_dir=tmp_path)
        store.register("eve@example.com", "password123")
        user = store.verify("eve@example.com", "wrongpassword")
        assert user is None

    def test_verify_nonexistent_email_returns_none(self, tmp_path: Path):
        # 不存在邮箱返回 None（不泄露"邮箱不存在"）
        store = UserStore(data_dir=tmp_path)
        user = store.verify("nobody@example.com", "password123")
        assert user is None


# =====================================================================
# 7-8. PIPL 合规 - 不存明文敏感数据
# =====================================================================


class TestPiplCompliance:
    """PIPL 合规测试 - 文件中不得出现明文密码 / 明文邮箱索引可被撞库"""

    def test_password_not_stored_plaintext(self, tmp_path: Path):
        # 文件中无明文密码
        store = UserStore(data_dir=tmp_path)
        password = "my_secret_password_123"
        store.register("frank@example.com", password, "Frank")

        raw = store.users_file.read_text(encoding="utf-8")
        # 明文密码不应出现在文件中
        assert password not in raw, "明文密码出现在 users.json 中！违反 PIPL"
        # password_hash 应是 hex 字符串
        data = json.loads(raw)
        for record in data.values():
            assert "password_hash" in record
            assert "salt" in record
            # hash 应是 64 字符 hex（256 bit）
            assert len(record["password_hash"]) == 64
            assert len(record["salt"]) == 32  # 16 字节 hex

    def test_email_hmac_not_plaintext(self, tmp_path: Path):
        # 文件中无明文邮箱（防撞库）
        # 注意：当前实现为了 _handle_auth_me 返回 email 给本人，邮箱明文存了，
        # 但 email_hmac 应该是 HMAC（不是 email 直接 hash），且与 email 不同
        store = UserStore(data_dir=tmp_path)
        email = "grace@example.com"
        store.register(email, "password123")

        data = json.loads(store.users_file.read_text(encoding="utf-8"))
        for record in data.values():
            # email_hmac 应是 64 字符 hex
            assert len(record.get("email_hmac", "")) == 64
            # email_hmac 不应等于 email 明文
            assert record["email_hmac"] != email
            # email_hmac 不应是 email 的简单 sha256（HMAC 用 server secret）
            import hashlib
            simple_sha = hashlib.sha256(email.encode("utf-8")).hexdigest()
            assert record["email_hmac"] != simple_sha, (
                "email_hmac 是 email 的简单 SHA256，无 server secret 防撞库"
            )


# =====================================================================
# 9. 原子写入
# =====================================================================


class TestAtomicWrite:
    """原子写入测试 - 写入失败原文件不损坏"""

    def test_atomic_write(self, tmp_path: Path):
        # 第一次写入成功
        store = UserStore(data_dir=tmp_path)
        store.register("henry@example.com", "password123", "Henry")
        original_content = store.users_file.read_text(encoding="utf-8")
        assert "henry@example.com" in original_content

        # 模拟写入失败：把 _atomic_write 内部的 write 改成抛异常
        # 但 os.replace 之前就失败了，原文件应保持不变
        import deadman.auth.store as store_module

        original_write = store_module.Path.write_text

        def failing_write(self_path, *args, **kwargs):
            # 只对 .tmp 文件抛异常
            if str(self_path).endswith(".json.tmp"):
                raise OSError("simulated disk full")
            return original_write(self_path, *args, **kwargs)

        store_module.Path.write_text = failing_write  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError, match="simulated disk full"):
                store.register("iris@example.com", "password456")
        finally:
            store_module.Path.write_text = original_write  # type: ignore[method-assign]

        # 原文件应保持不变（henry 仍在，iris 未写入）
        current_content = store.users_file.read_text(encoding="utf-8")
        assert current_content == original_content, "写入失败后原文件被损坏"
        assert "henry@example.com" in current_content
        assert "iris@example.com" not in current_content
        # 临时文件应被清理
        tmp_file = store.users_file.with_suffix(".json.tmp")
        assert not tmp_file.exists(), "临时文件未清理"


# =====================================================================
# 额外：list_users / delete_user / update_user
# =====================================================================


class TestCrud:
    """list_users / delete_user / update_user 基础覆盖"""

    def test_list_users_does_not_leak_secrets(self, tmp_path: Path):
        store = UserStore(data_dir=tmp_path)
        store.register("alice@example.com", "password123")
        store.register("bob@example.com", "anotherpass456")
        users = store.list_users()
        assert len(users) == 2
        # 不应泄露 password_hash / salt
        for u in users:
            assert "password_hash" not in u
            assert "salt" not in u
            # email_hmac 应被截断
            assert u.get("email_hmac", "").endswith("...")

    def test_delete_user(self, tmp_path: Path):
        store = UserStore(data_dir=tmp_path)
        user = store.register("alice@example.com", "password123")
        assert store.delete_user(user["user_id"]) is True
        assert store.get_user(user["user_id"]) is None
        # 再删一次返回 False
        assert store.delete_user(user["user_id"]) is False

    def test_update_user_display_name(self, tmp_path: Path):
        store = UserStore(data_dir=tmp_path)
        user = store.register("alice@example.com", "password123")
        updated = store.update_user(user["user_id"], {"display_name": "Alice New"})
        assert updated is not None
        assert updated["display_name"] == "Alice New"

    def test_update_user_rejects_password_change(self, tmp_path: Path):
        # 不允许通过 update_user 修改 password / email
        store = UserStore(data_dir=tmp_path)
        user = store.register("alice@example.com", "password123")
        updated = store.update_user(
            user["user_id"],
            {"password_hash": "fake", "email": "hacker@example.com"},
        )
        # 字段被忽略
        assert "password_hash" not in updated
        assert updated["email"] == "alice@example.com"
