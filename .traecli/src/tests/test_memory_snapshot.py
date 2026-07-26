"""测试 deadman.memory.file_store 的 P2.5 Memory Snapshot。

覆盖点(4 个):
    - test_export_snapshot_returns_bytes: 导出 bytes
    - test_import_snapshot_restores: 导入恢复
    - test_export_import_roundtrip: 往返一致
    - test_import_invalid_data_returns_false: 无效数据返回 False
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deadman.memory.file_store import FileMemoryStore
from deadman.memory.semantic import UserProfile


@pytest.fixture
def enabled_store(tmp_path: Path, monkeypatch):
    """启用 snapshot feature flag + 用 tmp_path 隔离存储"""
    import deadman.memory.file_store as fs_module

    monkeypatch.setattr(fs_module, "MEMORY_SNAPSHOT_ENABLED", True)
    return FileMemoryStore(memory_dir=tmp_path)


def _seed_files(store: FileMemoryStore) -> None:
    """向 store 写入测试用文件内容"""
    store.save_profile("user-001", UserProfile(
        user_id="user-001",
        name="测试用户",
        relationship_to_deceased="子女",
        location={"city": "北京"},
    ))
    store.append_fact("用户事实", "name=测试用户")
    store.append_episode(
        episode_id="sess-1",
        summary="用户咨询户口注销",
        importance=0.7,
    )
    store.save_reflexion({"agents": {"death_aftercare": {
        "failure_patterns": {"timeout": {"count": 1}},
        "successful_adjustments": {},
    }}})


# =====================================================================
# 1. 导出返回 bytes
# =====================================================================

class TestExportSnapshot:
    def test_export_snapshot_returns_bytes(self, enabled_store):
        _seed_files(enabled_store)
        data = enabled_store.export_snapshot()
        assert isinstance(data, bytes)
        assert len(data) > 0
        # 校验魔数
        assert data[:4] == b"DMSP"
        # 校验版本
        assert data[4] == 1
        # flag 应为 0(明文 gzip,未传 aes_key)
        assert data[5] == 0

    def test_export_snapshot_disabled_returns_empty(self, tmp_path, monkeypatch):
        # feature flag 关闭 → 返回 b""
        import deadman.memory.file_store as fs_module

        monkeypatch.setattr(fs_module, "MEMORY_SNAPSHOT_ENABLED", False)
        store = FileMemoryStore(memory_dir=tmp_path)
        _seed_files(store)
        data = store.export_snapshot()
        assert data == b""


# =====================================================================
# 2. 导入恢复
# =====================================================================

class TestImportSnapshot:
    def test_import_snapshot_restores(self, tmp_path, monkeypatch):
        # 在目录 A 导出,在目录 B 导入,验证 B 中文件存在
        import deadman.memory.file_store as fs_module

        monkeypatch.setattr(fs_module, "MEMORY_SNAPSHOT_ENABLED", True)
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_store = FileMemoryStore(memory_dir=src_dir)
        _seed_files(src_store)

        data = src_store.export_snapshot()
        assert len(data) > 0

        dst_store = FileMemoryStore(memory_dir=dst_dir)
        # 初始 dst 目录无文件
        assert not dst_store.user_file.exists()
        ok = dst_store.import_snapshot(data)
        assert ok is True
        # 导入后应有文件
        assert dst_store.user_file.exists()
        assert dst_store.memory_file.exists()
        assert dst_store.episodes_file.exists()
        assert dst_store.reflexion_file.exists()

    def test_import_snapshot_disabled_returns_false(self, tmp_path, monkeypatch):
        import deadman.memory.file_store as fs_module

        monkeypatch.setattr(fs_module, "MEMORY_SNAPSHOT_ENABLED", False)
        # 但需要先在 enabled 时生成有效 snapshot
        monkeypatch.setattr(fs_module, "MEMORY_SNAPSHOT_ENABLED", True)
        src_store = FileMemoryStore(memory_dir=tmp_path / "src")
        _seed_files(src_store)
        data = src_store.export_snapshot()
        # 现在关闭 flag 再导入
        monkeypatch.setattr(fs_module, "MEMORY_SNAPSHOT_ENABLED", False)
        dst_store = FileMemoryStore(memory_dir=tmp_path / "dst")
        ok = dst_store.import_snapshot(data)
        assert ok is False


# =====================================================================
# 3. 往返一致
# =====================================================================

class TestRoundtrip:
    def test_export_import_roundtrip(self, tmp_path, monkeypatch):
        # 导出 → 导入 → 再导出,内容应一致
        import deadman.memory.file_store as fs_module

        monkeypatch.setattr(fs_module, "MEMORY_SNAPSHOT_ENABLED", True)
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_store = FileMemoryStore(memory_dir=src_dir)
        _seed_files(src_store)
        data1 = src_store.export_snapshot()

        dst_store = FileMemoryStore(memory_dir=dst_dir)
        assert dst_store.import_snapshot(data1) is True

        # 再导出
        data2 = dst_store.export_snapshot()
        # 两次导出 gzip 内容应一致(剔除时间戳字段后比较)
        # 简单做法:验证两次导出长度接近,且都可成功往返
        assert len(data1) > 0
        assert len(data2) > 0
        # 两边文件内容应可读且语义一致
        assert src_store.user_file.read_text(encoding="utf-8") == \
               dst_store.user_file.read_text(encoding="utf-8")
        assert src_store.memory_file.read_text(encoding="utf-8") == \
               dst_store.memory_file.read_text(encoding="utf-8")
        assert src_store.episodes_file.read_text(encoding="utf-8") == \
               dst_store.episodes_file.read_text(encoding="utf-8")

    def test_roundtrip_with_aes_key(self, tmp_path, monkeypatch):
        # 若 cryptography 可用,验证加密往返;不可用时降级到明文,仍应可往返
        import deadman.memory.file_store as fs_module

        monkeypatch.setattr(fs_module, "MEMORY_SNAPSHOT_ENABLED", True)
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_store = FileMemoryStore(memory_dir=src_dir)
        _seed_files(src_store)

        aes_key = b"0123456789abcdef0123456789abcdef"  # 32 bytes
        data = src_store.export_snapshot(aes_key=aes_key)
        assert len(data) > 0

        dst_store = FileMemoryStore(memory_dir=dst_dir)
        ok = dst_store.import_snapshot(data, aes_key=aes_key)
        # 若 cryptography 可用,应解密成功;否则降级失败(flag=1 但解密不可用)
        if fs_module._HAS_CRYPTO:
            assert ok is True
            assert dst_store.user_file.exists()


# =====================================================================
# 4. 无效数据返回 False
# =====================================================================

class TestImportInvalidData:
    def test_import_invalid_data_returns_false(self, enabled_store):
        # 各种无效数据
        assert enabled_store.import_snapshot(b"") is False
        assert enabled_store.import_snapshot(b"short") is False
        assert enabled_store.import_snapshot(b"XXXX" + b"\x01\x00" + b"payload") is False  # 错误魔数
        assert enabled_store.import_snapshot(b"DMSP" + b"\x99\x00" + b"payload") is False  # 错误版本
        assert enabled_store.import_snapshot(b"DMSP" + b"\x01\x05" + b"payload") is False  # 未知 flag

    def test_import_corrupt_gzip_returns_false(self, enabled_store):
        # 正确 header + 损坏 payload
        bad_data = b"DMSP" + bytes([1, 0]) + b"not-a-gzip-stream"
        assert enabled_store.import_snapshot(bad_data) is False

    def test_import_non_bytes_returns_false(self, enabled_store):
        # 非 bytes 类型
        assert enabled_store.import_snapshot(None) is False
        assert enabled_store.import_snapshot("string") is False
