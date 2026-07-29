"""跨用户匿名知识共享 - P2.4。

设计目标:
    - 多用户共享"流程经验"(如"北京户口注销流程"),匿名化后入库
    - 严格 PII 脱敏(复用 deadman.memory.manager.sanitize_before_store)
    - 必须 user_consent=True 才入库
    - source_user_count 字段记录样本量,样本量大者优先复用
    - 持久化到 ~/.deadman/memory/SHARED_KNOWLEDGE.json

三大铁律:
    1. feature flag DEADMAN_SHARED_KNOWLEDGE_ENABLED=0 默认关闭
    2. PII 脱敏失败 → 拒绝入库
    3. user_consent=False → 拒绝入库
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# =====================================================================
# feature flag - 默认关闭
# =====================================================================
SHARED_KNOWLEDGE_ENABLED: bool = os.environ.get(
    "DEADMAN_SHARED_KNOWLEDGE_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 默认存储路径:~/.deadman/memory/SHARED_KNOWLEDGE.json
DEFAULT_SHARED_KNOWLEDGE_FILE: Path = (
    Path.home() / ".deadman" / "memory" / "SHARED_KNOWLEDGE.json"
)


@dataclass
class SharedKnowledgeEntry:
    """跨用户共享的一条匿名知识"""

    entry_id: str
    topic: str
    content: str
    # 贡献过此条经验的用户数(样本量)
    source_user_count: int = 1
    anonymized: bool = True
    created_at: str = ""
    last_updated: str = ""
    # 贡献者 user_id 列表(用于去重,不上锁,内部使用)
    contributors: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, content: str) -> None:
    """原子写入:先写 .tmp 再 os.replace"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


class SharedKnowledgeStore:
    """跨用户匿名知识共享存储。

    所有写入操作:
        1. 检查 SHARED_KNOWLEDGE_ENABLED(关闭时返回空结果)
        2. 检查 user_consent(无授权拒绝)
        3. PII 脱敏(复用 sanitize_before_store)
        4. 写入 SHARED_KNOWLEDGE.json(原子写入)
    """

    def __init__(self, file_path: Optional[Path] = None) -> None:
        self.file_path: Path = (
            file_path if file_path is not None else DEFAULT_SHARED_KNOWLEDGE_FILE
        )
        # 内存缓存:entry_id -> SharedKnowledgeEntry
        self._cache: dict[str, SharedKnowledgeEntry] = {}
        self._loaded: bool = False

    # ==================================================================
    # 持久化
    # ==================================================================
    def _load(self) -> None:
        """从磁盘加载到内存缓存(失败时空缓存,不抛异常)"""
        if self._loaded:
            return
        self._cache = {}
        if not self.file_path.exists():
            self._loaded = True
            return
        try:
            text = self.file_path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
            if isinstance(data, dict):
                entries = data.get("entries", [])
                if isinstance(entries, list):
                    for item in entries:
                        if not isinstance(item, dict):
                            continue
                        try:
                            entry = SharedKnowledgeEntry(
                                entry_id=str(item.get("entry_id") or uuid4()),
                                topic=str(item.get("topic", "")),
                                content=str(item.get("content", "")),
                                source_user_count=int(item.get("source_user_count", 1)),
                                anonymized=bool(item.get("anonymized", True)),
                                created_at=str(item.get("created_at", "")),
                                last_updated=str(item.get("last_updated", "")),
                                contributors=list(item.get("contributors", []) or []),
                            )
                            self._cache[entry.entry_id] = entry
                        except Exception as exc:  # pragma: no cover - 韧性
                            logger.warning("共享知识条目解析失败: %s", exc)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("SharedKnowledgeStore 加载失败: %s", exc)
            self._cache = {}
        self._loaded = True

    def _save(self) -> None:
        """把内存缓存写回磁盘(原子写入)"""
        try:
            data = {
                "version": 1,
                "last_updated": _now_iso(),
                "entries": [asdict(e) for e in self._cache.values()],
            }
            content = json.dumps(data, ensure_ascii=False, indent=2, default=str)
            _atomic_write(self.file_path, content)
        except Exception as exc:
            logger.warning("SharedKnowledgeStore 保存失败: %s", exc)

    # ==================================================================
    # 公共 API
    # ==================================================================
    def add(
        self,
        user_id: str,
        topic: str,
        content: str,
        user_consent: bool = True,
    ) -> Optional[str]:
        """添加一条跨用户共享知识。

        流程:
            1. SHARED_KNOWLEDGE_ENABLED=0 → 返回 None
            2. user_consent=False → 返回 None(拒绝)
            3. PII 脱敏(覆盖 identifier/name/phone/address/account_number)
            4. 同 topic 已存在 + 同 user 已贡献过 → source_user_count 不增,
               仅更新 content/last_updated
            5. 同 topic 已存在 + 新 user → source_user_count += 1,
               contributors 追加,合并 content
            6. 同 topic 不存在 → 新建 entry

        Returns:
            entry_id;若被拒绝(feature flag/consent)返回 None
        """
        if not SHARED_KNOWLEDGE_ENABLED:
            return None
        if not user_consent:
            logger.info("SharedKnowledge 拒绝入库:user_consent=False")
            return None
        if not topic or not content:
            return None

        # PII 脱敏(用 sanitize_before_store 复用逻辑)
        try:
            from .manager import sanitize_before_store

            safe_payload = sanitize_before_store(
                {"topic": topic, "content": content, "name": user_id}
            )
            safe_topic = str(safe_payload.get("topic", topic))
            safe_content = str(safe_payload.get("content", content))
            safe_user_id = str(safe_payload.get("name", user_id))
        except Exception as exc:  # pragma: no cover - 韧性
            logger.warning("PII 脱敏失败,拒绝入库: %s", exc)
            return None

        self._load()

        # 查找同 topic 的现有 entry
        existing: Optional[SharedKnowledgeEntry] = None
        for entry in self._cache.values():
            if entry.topic == safe_topic:
                existing = entry
                break

        now_iso = _now_iso()
        if existing is None:
            # 新建
            entry_id = str(uuid4())
            entry = SharedKnowledgeEntry(
                entry_id=entry_id,
                topic=safe_topic,
                content=safe_content,
                source_user_count=1,
                anonymized=True,
                created_at=now_iso,
                last_updated=now_iso,
                contributors=[safe_user_id],
            )
            self._cache[entry_id] = entry
        else:
            # 已存在:同 topic
            if safe_user_id in existing.contributors:
                # 同 user 已贡献:更新 content(取并集),不增 count
                if safe_content and safe_content not in existing.content:
                    existing.content = existing.content + "\n---\n" + safe_content
                existing.last_updated = now_iso
            else:
                # 新 user:合并 content,count + 1
                if safe_content and safe_content not in existing.content:
                    existing.content = existing.content + "\n---\n" + safe_content
                existing.source_user_count += 1
                existing.contributors.append(safe_user_id)
                existing.last_updated = now_iso
            entry_id = existing.entry_id

        self._save()
        return entry_id

    def query(self, topic: str, top_k: int = 5) -> list[SharedKnowledgeEntry]:
        """按 topic 检索共享知识(精确匹配 + 关键词包含)。

        排序:source_user_count 降序(样本量大优先)。
        """
        if not SHARED_KNOWLEDGE_ENABLED:
            return []
        self._load()
        if not topic:
            return []
        topic_lower = topic.lower()
        matched: list[SharedKnowledgeEntry] = []
        for entry in self._cache.values():
            if entry.topic.lower() == topic_lower:
                matched.append(entry)
            elif topic_lower in entry.topic.lower() or entry.topic.lower() in topic_lower:
                matched.append(entry)
        # 优先精确匹配,其次按 source_user_count 降序
        matched.sort(
            key=lambda e: (
                e.topic.lower() != topic_lower,  # 精确匹配 False=0 排前
                -e.source_user_count,
            )
        )
        return matched[:top_k]

    def merge_entries(self, topic: str) -> Optional[SharedKnowledgeEntry]:
        """合并同主题的多用户经验为一条。

        把所有 topic 匹配的 entry 合并为一条:
            - content 拼接(去重)
            - source_user_count 累加
            - contributors 合并去重
        合并后写入磁盘,原条目删除。

        Returns:
            合并后的 entry;无匹配返回 None
        """
        if not SHARED_KNOWLEDGE_ENABLED:
            return None
        self._load()
        topic_lower = topic.lower()
        to_merge: list[SharedKnowledgeEntry] = []
        for entry in list(self._cache.values()):
            if entry.topic.lower() == topic_lower:
                to_merge.append(entry)
        if not to_merge:
            return None
        # 合并
        merged_id = str(uuid4())
        contents: list[str] = []
        contributors: list[str] = []
        total_count = 0
        earliest = ""
        latest = ""
        for e in to_merge:
            if e.content and e.content not in contents:
                contents.append(e.content)
            for u in e.contributors:
                if u not in contributors:
                    contributors.append(u)
            total_count += e.source_user_count
            if not earliest or (e.created_at and e.created_at < earliest):
                earliest = e.created_at
            if not latest or (e.last_updated and e.last_updated > latest):
                latest = e.last_updated
        merged = SharedKnowledgeEntry(
            entry_id=merged_id,
            topic=to_merge[0].topic,
            content="\n---\n".join(contents),
            source_user_count=total_count,
            anonymized=True,
            created_at=earliest or _now_iso(),
            last_updated=latest or _now_iso(),
            contributors=contributors,
        )
        # 删除原条目,写入合并条目
        for e in to_merge:
            self._cache.pop(e.entry_id, None)
        self._cache[merged_id] = merged
        self._save()
        return merged

    def count(self) -> int:
        """当前缓存的条目数(测试/调试用)"""
        if not SHARED_KNOWLEDGE_ENABLED:
            return 0
        self._load()
        return len(self._cache)


__all__ = [
    "SharedKnowledgeEntry",
    "SharedKnowledgeStore",
    "SHARED_KNOWLEDGE_ENABLED",
    "DEFAULT_SHARED_KNOWLEDGE_FILE",
]
