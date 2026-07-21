"""DecedentRegistry - 遗码通（逝者案例管理）

参考重庆"渝逝有安"遗码通：逝者唯一标识贯穿全流程。

重要边界：
    - deadman 是信息引导平台，不存储逝者敏感 PII
    - case_id 是 deadman 内部 ID，不冒充官方编号
    - 不与任何官方系统对接
    - 不存储：真实姓名/身份证号/死亡证明编号

设计要点：
    - 数据目录：~/.deadman/cases/{user_id}/cases.json
    - case_id 格式：case-{uuid12}（与官方编号无关）
    - events 时间线由各 agent 追加（含 event/timestamp/agent/notes）
    - 涉及自杀/非正常死亡时由调用方触发 safety-protocol L0

遵守：
    - rules/legal-compliance-framework.md 第五章 PIPL：不存敏感 PII
    - rules/integrity-framework.md：case_id 是内部 ID
    - rules/service-boundary-framework.md：不与官方系统对接
    - rules/safety-protocol.md：涉及自杀/非正常死亡触发 L0
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# =====================================================================
# 案例状态常量
# =====================================================================
STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"
STATUS_ARCHIVED = "archived"

_VALID_STATUSES = {STATUS_ACTIVE, STATUS_CLOSED, STATUS_ARCHIVED}

# 允许的关系类型
_VALID_RELATIONSHIPS = {"配偶", "子女", "父母", "兄弟姐妹", "祖父母", "孙辈", "其他"}


# =====================================================================
# DecedentRecord 数据结构
# =====================================================================
@dataclass
class DecedentRecord:
    """逝者案例记录

    重要：本结构不存储逝者的真实姓名/身份证号/死亡证明编号等敏感 PII。
    decedent_alias 是用户给的化名（如"我父亲""张奶奶"），不要求是真实姓名。

    case_id 是 deadman 内部 ID，不与官方系统对接，不冒充官方编号。
    """
    case_id: str
    owner_user_id: str
    decedent_alias: str
    relationship: str
    events: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    status: str = STATUS_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("created_at", "updated_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        # events 内的 timestamp 已经是字符串
        return d


# =====================================================================
# DecedentRegistry
# =====================================================================
class DecedentRegistry:
    """遗码通 - 逝者案例管理

    遵守：
        - PIPL: 不存敏感 PII（真实姓名/身份证号/死亡证明编号）
        - integrity: case_id 是内部 ID，不冒充官方编号
        - service-boundary: 不与官方系统对接，仅 deadman 内部使用
        - safety-protocol: 涉及自杀/非正常死亡触发 L0（由调用方触发）
    """

    # 敏感 PII 字段黑名单 —— 不允许出现在 events/notes 中
    PII_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
        # 身份证号 18 位（前 6 + 8 + 3 + 1）
        ("id_card", re.compile(r"\b\d{6}\d{8}\d{3}[\dXx]\b"), "[已脱敏:身份证号]"),
        # 手机号 11 位
        ("phone", re.compile(r"\b1[3-9]\d{9}\b"), "[已脱敏:手机号]"),
        # 银行账号 16-19 位
        ("bank_account", re.compile(r"\b\d{16,19}\b"), "[已脱敏:银行账号]"),
    )

    def __init__(self, data_dir: Path | None = None) -> None:
        """初始化案例注册表。

        Args:
            data_dir: 数据根目录，默认 ~/.deadman/cases/
        """
        if data_dir is None:
            data_dir = Path.home() / ".deadman" / "cases"
        self.data_dir: Path = Path(data_dir)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("DecedentRegistry 创建数据目录失败 %s: %s", self.data_dir, exc)

    # ==================================================================
    # 文件读写
    # ==================================================================
    def _cases_file(self, user_id: str) -> Path:
        return self.data_dir / user_id / "cases.json"

    def _read_cases(self, user_id: str) -> dict[str, dict[str, Any]]:
        path = self._cases_file(user_id)
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("DecedentRegistry 读取案例失败 %s: %s", path, exc)
            return {}

    def _write_cases(self, user_id: str, cases: dict[str, dict[str, Any]]) -> None:
        path = self._cases_file(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cases, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("DecedentRegistry 写入案例失败 %s: %s", path, exc)

    # ==================================================================
    # PII 脱敏（写入前）
    # ==================================================================
    def _sanitize_pii(self, text: str) -> str:
        """脱敏 events/notes 中的 PII（身份证号/手机号/银行账号）

        用户输入"我父亲身份证号是 110101199001011234"会被替换为
        "我父亲身份证号是 [已脱敏:身份证号]"，避免敏感 PII 进入持久化。
        """
        if not text:
            return ""
        sanitized = text
        for _name, pattern, replacement in self.PII_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    # ==================================================================
    # 案例管理
    # ==================================================================
    def create_case(
        self,
        owner_user_id: str,
        decedent_alias: str,
        relationship: str,
    ) -> DecedentRecord:
        """创建案例

        Args:
            owner_user_id: 案例归属用户
            decedent_alias: 用户给的化名（如"我父亲"），不存真实姓名
            relationship: 与逝者关系（配偶/子女/父母/兄弟姐妹/祖父母/孙辈/其他）

        Returns:
            新建的 DecedentRecord
        """
        if relationship not in _VALID_RELATIONSHIPS:
            # 不阻断，仅标准化为"其他"并 warning
            logger.warning(
                "DecedentRegistry: 未知 relationship %r，标准化为 '其他'", relationship
            )
            relationship = "其他"

        # 脱敏化名（防止用户在 alias 中误填身份证号等）
        decedent_alias = self._sanitize_pii(decedent_alias)[:100]

        case_id = f"case-{uuid.uuid4().hex[:12]}"
        now = datetime.utcnow()
        record = DecedentRecord(
            case_id=case_id,
            owner_user_id=owner_user_id,
            decedent_alias=decedent_alias,
            relationship=relationship,
            events=[],
            created_at=now,
            updated_at=now,
            status=STATUS_ACTIVE,
        )
        cases = self._read_cases(owner_user_id)
        cases[case_id] = record.to_dict()
        self._write_cases(owner_user_id, cases)
        return record

    def get_case(
        self, case_id: str, requester_user_id: str
    ) -> DecedentRecord | None:
        """获取案例（仅 owner 可访问）"""
        cases = self._read_cases(requester_user_id)
        entry = cases.get(case_id)
        if not entry:
            return None
        return self._entry_to_record(entry)

    def list_cases(self, owner_user_id: str) -> list[DecedentRecord]:
        """列出我的案例"""
        cases = self._read_cases(owner_user_id)
        return [self._entry_to_record(e) for e in cases.values()]

    def add_event(
        self,
        case_id: str,
        owner_user_id: str,
        event: str,
        agent: str,
        notes: str = "",
    ) -> DecedentRecord | None:
        """添加时间线事件

        Args:
            case_id: 案例 ID
            owner_user_id: 案例归属用户
            event: 事件描述（脱敏后存储）
            agent: 触发 agent 名称（如 death-aftercare / legal-advisor）
            notes: 备注说明（脱敏后存储）
        """
        cases = self._read_cases(owner_user_id)
        entry = cases.get(case_id)
        if not entry:
            return None

        # 脱敏后再持久化（PIPL）
        safe_event = self._sanitize_pii(event)[:500]
        safe_notes = self._sanitize_pii(notes)[:2000]
        safe_agent = (agent or "")[:100]

        event_obj = {
            "event": safe_event,
            "timestamp": datetime.utcnow().isoformat(),
            "agent": safe_agent,
            "notes": safe_notes,
        }
        entry.setdefault("events", []).append(event_obj)
        entry["updated_at"] = datetime.utcnow().isoformat()
        cases[case_id] = entry
        self._write_cases(owner_user_id, cases)
        return self._entry_to_record(entry)

    def update_status(
        self, case_id: str, owner_user_id: str, status: str
    ) -> DecedentRecord | None:
        """更新案例状态"""
        if status not in _VALID_STATUSES:
            raise ValueError(f"无效 status: {status}（允许: {sorted(_VALID_STATUSES)}）")
        cases = self._read_cases(owner_user_id)
        entry = cases.get(case_id)
        if not entry:
            return None
        entry["status"] = status
        entry["updated_at"] = datetime.utcnow().isoformat()
        cases[case_id] = entry
        self._write_cases(owner_user_id, cases)
        return self._entry_to_record(entry)

    def archive_case(self, case_id: str, owner_user_id: str) -> bool:
        """归档案例（用户情绪平复后主动归档）

        归档后案例仍保留在磁盘上，但状态为 archived，不再出现在
        默认 active 列表中（list_cases 默认返回所有状态，UI 可过滤）
        """
        result = self.update_status(case_id, owner_user_id, STATUS_ARCHIVED)
        return result is not None

    def get_timeline(
        self, case_id: str, owner_user_id: str
    ) -> list[dict[str, Any]]:
        """获取时间线（按事件时间排序）"""
        record = self.get_case(case_id, owner_user_id)
        if record is None:
            return []
        events = list(record.events)
        # 按 timestamp 升序
        events.sort(key=lambda e: e.get("timestamp", ""))
        return events

    # ==================================================================
    # 序列化辅助
    # ==================================================================
    @staticmethod
    def _entry_to_record(entry: dict[str, Any]) -> DecedentRecord:
        def _parse_dt(v: Any) -> datetime:
            if not v:
                return datetime.utcnow()
            try:
                return datetime.fromisoformat(v)
            except (TypeError, ValueError):
                return datetime.utcnow()

        return DecedentRecord(
            case_id=entry["case_id"],
            owner_user_id=entry["owner_user_id"],
            decedent_alias=entry.get("decedent_alias", ""),
            relationship=entry.get("relationship", "其他"),
            events=list(entry.get("events", []) or []),
            created_at=_parse_dt(entry.get("created_at")),
            updated_at=_parse_dt(entry.get("updated_at")),
            status=entry.get("status", STATUS_ACTIVE),
        )
