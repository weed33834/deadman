"""P8.6.6 用户同意管理 - 明示同意 + 撤回(PIPL / GDPR 强制要求)。

法规依据:
    - PIPL 第 13-16 条:处理个人信息需"单独同意"
    - PIPL 第 16 条:用户有权撤回同意(撤回不影响已处理活动)
    - GDPR 第 6 条:合法处理依据(同意是其一)
    - GDPR 第 7 条:同意可随时撤回
    - 中国《生成式 AI 管理办法》第 10 条:
      服务提供者应当取得用户同意(明示,默认拒绝)

设计:
    - ConsentType: 同意类型(服务条款 / 隐私 / 跨境传输 / 敏感数据 / AI 训练 / 营销)
    - ConsentStatus: 状态机(GRANTED → WITHDRAWN,可重新授予)
    - ConsentManager: 同意管理(记录 / 查询 / 撤回 / 审计)

同意书版本控制:
    - 每次更新 terms 版本号 → 已有用户需重新同意
    - 版本不一致时拒绝服务(强制重新同意)

撤回影响:
    - 撤回后停止相关处理
    - 已处理数据按 retention policy 保留 / 删除
    - 撤回记录永久保留(法规要求,审计用)

feature flag:`DEADMAN_COMPLIANCE_ENABLED=0` 关闭时默认所有 consent = granted(透传)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id

logger = logging.getLogger(__name__)


class ConsentType(str, Enum):
    """同意类型。"""

    TERMS_OF_SERVICE = "terms_of_service"  # 服务条款
    PRIVACY_POLICY = "privacy_policy"  # 隐私政策
    CROSS_BORDER = "cross_border"  # 跨境传输
    SENSITIVE_DATA = "sensitive_data"  # 敏感数据处理(健康 / 财务 / 法律)
    AI_TRAINING = "ai_training"  # 数据用于 AI 训练
    MARKETING = "marketing"  # 营销推送
    THIRD_PARTY_SHARE = "third_party_share"  # 第三方共享
    AUTOMATED_DECISION = "automated_decision"  # 自动化决策


class ConsentStatus(str, Enum):
    """同意状态机:

    PENDING → GRANTED → WITHDRAWN
                  ↓
              EXPIRED(同意书版本更新)
                  ↓
              PENDING(需重新同意)
    """

    PENDING = "pending"  # 待用户决定
    GRANTED = "granted"  # 已同意
    WITHDRAWN = "withdrawn"  # 已撤回
    EXPIRED = "expired"  # 已过期(条款版本更新)


# 默认同意书版本(每次更新条款时递增)
DEFAULT_CONSENT_VERSIONS: dict[ConsentType, str] = {
    ConsentType.TERMS_OF_SERVICE: "2024.01.0",
    ConsentType.PRIVACY_POLICY: "2024.01.0",
    ConsentType.CROSS_BORDER: "2024.01.0",
    ConsentType.SENSITIVE_DATA: "2024.01.0",
    ConsentType.AI_TRAINING: "2024.01.0",
    ConsentType.MARKETING: "2024.01.0",
    ConsentType.THIRD_PARTY_SHARE: "2024.01.0",
    ConsentType.AUTOMATED_DECISION: "2024.01.0",
}


@dataclass
class ConsentRecord:
    """单条同意记录(不可变,append-only)。"""

    record_id: str
    user_id: str
    tenant_id: str
    consent_type: ConsentType
    status: ConsentStatus
    version: str  # 同意书版本
    granted_at: float | None = None
    withdrawn_at: float | None = None
    expires_at: float | None = None  # 同意有效期(可选)
    # 来源信息(审计)
    source: str = "web"  # web / api / cli / import
    ip_address: str = ""
    user_agent: str = ""
    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def is_valid(self, now: float | None = None) -> bool:
        """是否有效(已授予 + 未过期 + 未撤回)。"""
        now = now or time.time()
        if self.status != ConsentStatus.GRANTED:
            return False
        return not (self.expires_at and now > self.expires_at)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["consent_type"] = self.consent_type.value
        d["status"] = self.status.value
        return d


class ConsentManager:
    """用户同意管理器。

    用法:
        cm = get_consent_manager()
        # 检查用户是否已同意
        if not cm.check(user_id, ConsentType.TERMS_OF_SERVICE):
            # 引导用户同意
            cm.grant(user_id, ConsentType.TERMS_OF_SERVICE, source="web")
        # 撤回
        cm.withdraw(user_id, ConsentType.MARKETING)
    """

    def __init__(
        self,
        store_path: Path | None = None,
        consent_versions: dict[ConsentType, str] | None = None,
    ) -> None:
        self.store_path = store_path or Path(
            os.environ.get("DEADMAN_CONSENT_STORE", "data/compliance/consents.json")
        )
        self.consent_versions = consent_versions or dict(DEFAULT_CONSENT_VERSIONS)
        self._lock = threading.RLock()
        # {user_id: {consent_type: [ConsentRecord]}}  append-only 历史记录
        self._records: dict[str, dict[ConsentType, list[ConsentRecord]]] = {}
        self._loaded = False

    def check(
        self,
        user_id: str,
        consent_type: ConsentType,
        tenant_id: str | None = None,
    ) -> bool:
        """检查用户是否已同意(且版本一致)。"""
        if not is_enabled("compliance"):
            return True  # 关闭:默认所有 consent granted(透传)

        with self._lock:
            self._load()
            records = self._records.get(user_id, {}).get(consent_type, [])
            if not records:
                return False
            latest = records[-1]
            # 版本不一致 → 视为未同意(需重新同意)
            if latest.version != self.consent_versions.get(consent_type):
                return False
            return latest.is_valid()

    def grant(
        self,
        user_id: str,
        consent_type: ConsentType,
        source: str = "web",
        tenant_id: str | None = None,
        ip_address: str = "",
        user_agent: str = "",
        expires_in_days: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConsentRecord:
        """用户授予同意。"""
        if not is_enabled("compliance"):
            return self._disabled_record(user_id, consent_type, ConsentStatus.GRANTED, tenant_id)

        tid = tenant_id or get_current_tenant_id() or "default"
        version = self.consent_versions.get(consent_type, "1.0.0")
        now = time.time()
        expires_at = now + expires_in_days * 86400 if expires_in_days else None

        record = ConsentRecord(
            record_id=self._generate_id(user_id, consent_type, now),
            user_id=user_id,
            tenant_id=tid,
            consent_type=consent_type,
            status=ConsentStatus.GRANTED,
            version=version,
            granted_at=now,
            expires_at=expires_at,
            source=source,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata=metadata or {},
        )
        with self._lock:
            self._load()
            self._records.setdefault(user_id, {}).setdefault(consent_type, []).append(record)
            self._save()
        logger.info("User %s granted %s (v=%s)", user_id, consent_type.value, version)
        return record

    def withdraw(
        self,
        user_id: str,
        consent_type: ConsentType,
        source: str = "web",
        tenant_id: str | None = None,
        reason: str = "",
    ) -> ConsentRecord | None:
        """用户撤回同意。"""
        if not is_enabled("compliance"):
            return self._disabled_record(user_id, consent_type, ConsentStatus.WITHDRAWN, tenant_id)

        with self._lock:
            self._load()
            records = self._records.get(user_id, {}).get(consent_type, [])
            if not records:
                return None
            latest = records[-1]
            if latest.status != ConsentStatus.GRANTED:
                return None

            now = time.time()
            record = ConsentRecord(
                record_id=self._generate_id(user_id, consent_type, now),
                user_id=user_id,
                tenant_id=latest.tenant_id,
                consent_type=consent_type,
                status=ConsentStatus.WITHDRAWN,
                version=latest.version,
                withdrawn_at=now,
                source=source,
                metadata={"reason": reason, "withdrawn_from_version": latest.version},
            )
            self._records[user_id][consent_type].append(record)
            self._save()
        logger.info("User %s withdrew %s (reason=%s)", user_id, consent_type.value, reason)
        return record

    def get_history(
        self,
        user_id: str,
        consent_type: ConsentType | None = None,
    ) -> list[ConsentRecord]:
        """查询同意历史(审计用)。"""
        with self._lock:
            self._load()
            user_records = self._records.get(user_id, {})
            if consent_type:
                return list(user_records.get(consent_type, []))
            result: list[ConsentRecord] = []
            for records in user_records.values():
                result.extend(records)
            return sorted(result, key=lambda r: r.created_at)

    def list_user_consents(self, user_id: str) -> dict[str, ConsentStatus]:
        """列出用户所有同意状态(看板用)。"""
        with self._lock:
            self._load()
            user_records = self._records.get(user_id, {})
            result: dict[str, ConsentStatus] = {}
            for ctype, records in user_records.items():
                if records:
                    result[ctype.value] = records[-1].status
            return result

    def update_consent_version(
        self,
        consent_type: ConsentType,
        new_version: str,
    ) -> int:
        """更新同意书版本(条款变更时调用)。

        Returns:
            受影响的用户数(已同意旧版本的用户,需重新同意)
        """
        if not is_enabled("compliance"):
            return 0

        with self._lock:
            old_version = self.consent_versions.get(consent_type)
            self.consent_versions[consent_type] = new_version
            # 标记所有已同意旧版本的用户为 EXPIRED
            affected = 0
            for user_id, type_records in self._records.items():
                records = type_records.get(consent_type, [])
                if records and records[-1].status == ConsentStatus.GRANTED:
                    if records[-1].version == old_version:
                        now = time.time()
                        expired_record = ConsentRecord(
                            record_id=self._generate_id(user_id, consent_type, now),
                            user_id=user_id,
                            tenant_id=records[-1].tenant_id,
                            consent_type=consent_type,
                            status=ConsentStatus.EXPIRED,
                            version=old_version,
                            metadata={"reason": "version_update", "new_version": new_version},
                        )
                        records.append(expired_record)
                        affected += 1
            self._save()
        logger.info(
            "Consent version updated: %s %s→%s (affected=%d users)",
            consent_type.value,
            old_version,
            new_version,
            affected,
        )
        return affected

    def export_for_audit(
        self,
        user_id: str | None = None,
        consent_type: ConsentType | None = None,
    ) -> list[dict[str, Any]]:
        """导出同意记录(审计 / 监管上报用)。"""
        with self._lock:
            self._load()
            result: list[dict[str, Any]] = []
            for uid, type_records in self._records.items():
                if user_id and uid != user_id:
                    continue
                for ctype, records in type_records.items():
                    if consent_type and ctype != consent_type:
                        continue
                    for r in records:
                        result.append(r.to_dict())
            return result

    # ==================================================================
    # 内部
    # ==================================================================

    def _disabled_record(
        self,
        user_id: str,
        consent_type: ConsentType,
        status: ConsentStatus,
        tenant_id: str | None,
    ) -> ConsentRecord:
        return ConsentRecord(
            record_id="disabled",
            user_id=user_id,
            tenant_id=tenant_id or "default",
            consent_type=consent_type,
            status=status,
            version="disabled",
        )

    def _generate_id(self, user_id: str, consent_type: ConsentType, timestamp: float) -> str:
        return f"consent-{user_id}-{consent_type.value}-{int(timestamp)}"

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for uid_str, type_records in data.get("records", {}).items():
                    self._records[uid_str] = {}
                    for ctype_str, records_list in type_records.items():
                        ctype = ConsentType(ctype_str)
                        self._records[uid_str][ctype] = [
                            ConsentRecord(
                                record_id=r["record_id"],
                                user_id=r["user_id"],
                                tenant_id=r.get("tenant_id", "default"),
                                consent_type=ctype,
                                status=ConsentStatus(r["status"]),
                                version=r["version"],
                                granted_at=r.get("granted_at"),
                                withdrawn_at=r.get("withdrawn_at"),
                                expires_at=r.get("expires_at"),
                                source=r.get("source", ""),
                                ip_address=r.get("ip_address", ""),
                                user_agent=r.get("user_agent", ""),
                                metadata=r.get("metadata", {}),
                                created_at=r.get("created_at", time.time()),
                            )
                            for r in records_list
                        ]
                # 加载 consent_versions(若文件中有)
                stored_versions = data.get("versions", {})
                for ctype_str, version in stored_versions.items():
                    self.consent_versions[ConsentType(ctype_str)] = version
        except Exception as e:
            logger.warning("Load consents failed: %s", e)
            return
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".tmp")
            data = {
                "versions": {ctype.value: v for ctype, v in self.consent_versions.items()},
                "records": {
                    uid: {
                        ctype.value: [r.to_dict() for r in records]
                        for ctype, records in type_records.items()
                    }
                    for uid, type_records in self._records.items()
                },
            }
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Save consents failed: %s", e)


# 全局单例
_cm_instance: ConsentManager | None = None
_cm_lock = threading.Lock()


def get_consent_manager() -> ConsentManager:
    global _cm_instance
    if _cm_instance is None:
        with _cm_lock:
            if _cm_instance is None:
                _cm_instance = ConsentManager()
    return _cm_instance
