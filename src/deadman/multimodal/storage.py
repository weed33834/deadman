"""Multimodal 文件存储 - 多模态文件持久化。

设计:
    - MultimodalStorage: 多模态文件存储(图片 / 音频 / 生成图片)
    - 路径:~/.deadman/multimodal/{user_id}/{type}/{uuid}.{ext}
      (通过 multi_tenant.resolve_data_path 路由,自动按租户隔离)
    - 元数据跟踪:created_at / type / size / source_user / content_hash
    - TTL 自动清理(temp 类型默认 30 天)

元数据存储:
    - 每个文件旁边存一个 .meta.json,记录元数据
    - 索引文件 ~/.deadman/multimodal/{user_id}/index.json 记录所有文件清单
    - 原子写:tmp + os.replace

文件类型(file_type):
    - image: 用户上传的原始图片
    - audio: 用户上传的音频
    - generated: AI 生成的图片
    - temp: 临时文件(TTL 30 天后自动清理)

feature flag:`DEADMAN_MULTIMODAL_ENABLED=0`(默认 OFF)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id, resolve_data_path

logger = logging.getLogger(__name__)


# 临时文件 TTL(秒):30 天
TEMP_TTL_SECONDS = 30 * 24 * 3600

# 支持的文件类型
SUPPORTED_FILE_TYPES: tuple[str, ...] = ("image", "audio", "generated", "temp")


@dataclass
class FileMetadata:
    """多模态文件元数据。

    Attributes:
        file_id: UUID
        file_type: image / audio / generated / temp
        filename: 文件名(含扩展)
        size: 字节大小
        content_hash: sha256 内容哈希(去重 / 完整性校验)
        source_user: 上传者 user_id
        tenant_id: 租户 ID
        created_at: 创建时间戳
        expires_at: 过期时间戳(temp 类型 = created_at + TEMP_TTL_SECONDS,None 表示永久)
        tags: 自定义标签
    """

    file_id: str
    file_type: str
    filename: str
    size: int
    content_hash: str
    source_user: str
    tenant_id: str
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["expires_at"] = self.expires_at
        return d


class MultimodalStorage:
    """多模态文件存储。

    用法:
        store = MultimodalStorage()
        if store.is_enabled():
            meta = store.store(b"image-bytes", "image", "user_123", ext="png")
            data = store.retrieve(meta.file_id)
            store.delete(meta.file_id)
            store.cleanup_expired()  # 定期清理过期文件
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._lock = threading.RLock()
        # base_dir 默认走 multi_tenant.resolve_data_path("multimodal")
        self._base_dir_override = base_dir
        self._index_cache: dict[str, dict[str, FileMetadata]] = {}  # {user_id: {file_id: meta}}

    def is_enabled(self) -> bool:
        return is_enabled("multimodal")

    def _base_dir(self, user_id: str) -> Path:
        """获取用户的多模态文件根目录(走多租户路由)。"""
        if self._base_dir_override is not None:
            return self._base_dir_override / user_id
        # resolve_data_path("multimodal/{user_id}") 会自动按租户路由
        return resolve_data_path(f"multimodal/{user_id}")

    def _index_path(self, user_id: str) -> Path:
        return self._base_dir(user_id) / "index.json"

    def _file_path(self, user_id: str, file_id: str, ext: str, file_type: str) -> Path:
        return self._base_dir(user_id) / file_type / f"{file_id}.{ext}"

    # ==================================================================
    # 主操作:store / retrieve / delete
    # ==================================================================

    def store(
        self,
        data: bytes,
        file_type: str,
        source_user: str,
        ext: str = "bin",
        tags: list[str] | None = None,
        tenant_id: str | None = None,
    ) -> FileMetadata:
        """持久化多模态文件。

        Args:
            data: 文件字节
            file_type: image / audio / generated / temp
            source_user: 上传者 user_id
            ext: 文件扩展(png/mp3/wav/...)
            tags: 自定义标签
            tenant_id: 显式租户 ID(默认从 ContextVar 取)

        Returns:
            FileMetadata
        """
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError(
                "Multimodal storage disabled (DEADMAN_MULTIMODAL_ENABLED=0)"
            )

        if file_type not in SUPPORTED_FILE_TYPES:
            raise ValueError(f"Unsupported file_type: {file_type}")

        if not isinstance(data, bytes | bytearray):
            raise TypeError("data must be bytes")

        tid = tenant_id or get_current_tenant_id()
        file_id = uuid.uuid4().hex
        content_hash = hashlib.sha256(data).hexdigest()
        created_at = time.time()
        expires_at: float | None = None
        if file_type == "temp":
            expires_at = created_at + TEMP_TTL_SECONDS

        meta = FileMetadata(
            file_id=file_id,
            file_type=file_type,
            filename=f"{file_id}.{ext}",
            size=len(data),
            content_hash=content_hash,
            source_user=source_user,
            tenant_id=tid,
            created_at=created_at,
            expires_at=expires_at,
            tags=list(tags or []),
        )

        with self._lock:
            file_path = self._file_path(source_user, file_id, ext, file_type)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写:tmp + os.replace
            tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
            tmp_path.write_bytes(bytes(data))
            os.replace(tmp_path, file_path)
            # 更新索引
            self._update_index(source_user, meta)

        logger.info(
            "Multimodal file stored: file_id=%s type=%s size=%d user=%s",
            file_id,
            file_type,
            len(data),
            source_user,
        )
        return meta

    def retrieve(self, file_id: str, source_user: str) -> bytes | None:
        """读取文件内容(不存在返回 None)。"""
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError(
                "Multimodal storage disabled (DEADMAN_MULTIMODAL_ENABLED=0)"
            )

        with self._lock:
            meta = self._lookup(source_user, file_id)
            if meta is None:
                return None
            file_path = self._base_dir(source_user) / meta.file_type / meta.filename
            if not file_path.exists():
                return None
            return file_path.read_bytes()

    def get_metadata(self, file_id: str, source_user: str) -> FileMetadata | None:
        """查询文件元数据(不读取内容)。"""
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError(
                "Multimodal storage disabled (DEADMAN_MULTIMODAL_ENABLED=0)"
            )
        with self._lock:
            return self._lookup(source_user, file_id)

    def delete(self, file_id: str, source_user: str) -> bool:
        """删除文件(同时清理索引)。"""
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError(
                "Multimodal storage disabled (DEADMAN_MULTIMODAL_ENABLED=0)"
            )

        with self._lock:
            meta = self._lookup(source_user, file_id)
            if meta is None:
                return False
            file_path = self._base_dir(source_user) / meta.file_type / meta.filename
            try:
                if file_path.exists():
                    file_path.unlink()
            except OSError as e:
                logger.warning("Failed to delete file %s: %s", file_path, e)
            self._remove_from_index(source_user, file_id)
        logger.info("Multimodal file deleted: file_id=%s user=%s", file_id, source_user)
        return True

    def list_files(
        self,
        source_user: str,
        file_type: str | None = None,
    ) -> list[FileMetadata]:
        """列出用户的所有文件(可按 file_type 过滤)。"""
        if not self.is_enabled():
            from .pipeline import MultimodalDisabledError

            raise MultimodalDisabledError(
                "Multimodal storage disabled (DEADMAN_MULTIMODAL_ENABLED=0)"
            )

        with self._lock:
            index = self._load_index(source_user)
            metas = list(index.values())
        if file_type:
            metas = [m for m in metas if m.file_type == file_type]
        metas.sort(key=lambda m: m.created_at, reverse=True)
        return metas

    # ==================================================================
    # TTL 清理
    # ==================================================================

    def cleanup_expired(self, source_user: str | None = None) -> int:
        """清理过期文件(temp 类型超过 TTL)。

        Args:
            source_user: 指定用户;None 则遍历所有用户目录

        Returns:
            删除的文件数
        """
        if not self.is_enabled():
            return 0

        now = time.time()
        deleted = 0

        with self._lock:
            user_ids = [source_user] if source_user else self._list_all_users()
            for uid in user_ids:
                index = self._load_index(uid)
                expired_ids: list[str] = []
                for fid, meta in index.items():
                    if meta.expires_at is not None and meta.expires_at < now:
                        expired_ids.append(fid)
                for fid in expired_ids:
                    meta = index[fid]
                    file_path = self._base_dir(uid) / meta.file_type / meta.filename
                    try:
                        if file_path.exists():
                            file_path.unlink()
                    except OSError as e:
                        logger.warning("Cleanup failed for %s: %s", file_path, e)
                    self._remove_from_index(uid, fid)
                    deleted += 1

        if deleted > 0:
            logger.info("Cleanup expired multimodal files: %d deleted", deleted)
        return deleted

    # ==================================================================
    # 内部:索引管理(原子写)
    # ==================================================================

    def _list_all_users(self) -> list[str]:
        """遍历所有用户目录(用于 cleanup_expired 全局扫描)。"""
        if self._base_dir_override is not None:
            base = self._base_dir_override
        else:
            base = resolve_data_path("multimodal")
        if not base.exists():
            return []
        return [p.name for p in base.iterdir() if p.is_dir()]

    def _load_index(self, user_id: str) -> dict[str, FileMetadata]:
        """加载用户索引(带缓存)。"""
        if user_id in self._index_cache:
            return dict(self._index_cache[user_id])

        index_path = self._index_path(user_id)
        result: dict[str, FileMetadata] = {}
        try:
            if index_path.exists():
                data = json.loads(index_path.read_text(encoding="utf-8"))
                for fid, meta_dict in data.get("files", {}).items():
                    result[fid] = FileMetadata(**meta_dict)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Load index failed for user=%s: %s", user_id, e)

        self._index_cache[user_id] = dict(result)
        return result

    def _save_index(self, user_id: str, index: dict[str, FileMetadata]) -> None:
        """原子写索引文件。"""
        index_path = self._index_path(user_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "updated_at": time.time(),
            "files": {fid: m.to_dict() for fid, m in index.items()},
        }
        tmp_path = index_path.with_suffix(index_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, index_path)
        self._index_cache[user_id] = dict(index)

    def _update_index(self, user_id: str, meta: FileMetadata) -> None:
        index = self._load_index(user_id)
        index[meta.file_id] = meta
        self._save_index(user_id, index)

    def _remove_from_index(self, user_id: str, file_id: str) -> None:
        index = self._load_index(user_id)
        if file_id in index:
            del index[file_id]
            self._save_index(user_id, index)

    def _lookup(self, user_id: str, file_id: str) -> FileMetadata | None:
        index = self._load_index(user_id)
        return index.get(file_id)


# 全局单例
_storage_instance: MultimodalStorage | None = None
_storage_lock = threading.Lock()


def get_multimodal_storage() -> MultimodalStorage:
    """获取全局 MultimodalStorage 单例。"""
    global _storage_instance
    if _storage_instance is None:
        with _storage_lock:
            if _storage_instance is None:
                _storage_instance = MultimodalStorage()
    return _storage_instance
