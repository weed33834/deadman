"""D15:法规变更通知机制(Regulatory Change Notification Mechanism)。

问题:
    deadman `i18n/law_adapter.py` 是静态规则库,需要手动 JSON reload。
    风险:
        - 法律法规变更(如个税起征点 / 银行账户冻结流程 / 房产继承税率)
        - 用户基于过时知识做决策(可能违法 / 财务损失)
        - 用户不知道规则已变(无主动通知)
        - 多用户 / 多 agent 共享过时知识(错误扩散)

    现有 `KnowledgeFreshnessChecker` 仅检测"文件最后更新时间",
    不检测"内容是否过时"(法规已变但文件未更新)。

缓解:
    - RegulatoryChangeDetector: 检测法规变更(diff / RSS / webhook)
    - ChangeSubscriberRegistry: 用户 / agent 订阅特定法规领域
    - ChangeNotifier: 变更发生时通知所有订阅者(IM / 邮件 / webhook)
    - RegulatorySnapshot: 法规版本快照(支持 diff)
    - 知识库"软失效"标记:变更后旧知识标记为 stale,需用户确认

设计:
    detector = RegulatoryChangeDetector()
    detector.subscribe("inheritance_law", user_id="u1", channels=["im","email"])
    # 周期性(每月)检测
    changes = detector.poll_rss("inheritance_law")
    if changes:
        detector.notify_subscribers("inheritance_law", changes)

集成:
    cron/scheduler.py 每月触发 detector.poll_all()。
    react_loop.py 检索知识时,若标记 stale,提示用户"法规可能已变更"。

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


class ChangeSeverity(str, Enum):
    """法规变更严重度。"""

    INFO = "info"  # 通知性变更(如新增 FAQ)
    MINOR = "minor"  # 次要变更(如金额微调)
    MAJOR = "major"  # 主要变更(如流程修改)
    BREAKING = "breaking"  # 破坏性变更(法律废止 / 重大政策变化)


class NotificationChannel(str, Enum):
    """通知渠道。"""

    IM = "im"  # 即时消息
    EMAIL = "email"
    WEBHOOK = "webhook"
    IN_APP = "in_app"  # 应用内通知


@dataclass
class RegulatoryChange:
    """法规变更记录。"""

    change_id: str
    jurisdiction: str  # CN_MAINLAND / US / EU / ...
    domain: str  # inheritance_law / tax_law / data_protection / ...
    severity: ChangeSeverity
    title: str
    summary: str
    source_url: str = ""
    effective_date: str = ""  # 生效日期(YYYY-MM-DD)
    detected_at: float = field(default_factory=time.time)
    # 影响范围(用户 IDs / agent IDs)
    affected_subscribers: list[str] = field(default_factory=list)
    # 关联旧规则版本(用于 diff)
    old_version_hash: str = ""
    new_version_hash: str = ""
    # 旧 / 新规则内容(可选)
    diff_summary: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class Subscriber:
    """订阅者。"""

    subscriber_id: str  # user_id / agent_name
    domain: str  # 订阅的法规领域
    channels: list[NotificationChannel] = field(default_factory=list)
    min_severity: ChangeSeverity = ChangeSeverity.MINOR
    created_at: float = field(default_factory=time.time)
    # webhook URL(若 channels 含 WEBHOOK)
    webhook_url: str = ""
    # 是否仍活跃(取消订阅后置 False)
    active: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["channels"] = [c.value for c in self.channels]
        d["min_severity"] = self.min_severity.value
        return d


# 严重度排序(INFO < MINOR < MAJOR < BREAKING)
_SEVERITY_ORDER = [
    ChangeSeverity.INFO,
    ChangeSeverity.MINOR,
    ChangeSeverity.MAJOR,
    ChangeSeverity.BREAKING,
]


def severity_at_least(change: ChangeSeverity, threshold: ChangeSeverity) -> bool:
    """变更严重度是否达到阈值。"""
    return _SEVERITY_ORDER.index(change) >= _SEVERITY_ORDER.index(threshold)


class RegulatoryChangeDetector:
    """法规变更检测器。

    用法:
        detector = RegulatoryChangeDetector(store_path=".traecli/data/regulatory_changes.json")

        # 1. 用户订阅
        detector.subscribe(
            subscriber_id="user-123",
            domain="inheritance_law",
            channels=[NotificationChannel.IM, NotificationChannel.EMAIL],
            min_severity=ChangeSeverity.MAJOR,
        )

        # 2. 记录法规快照(首次)
        detector.snapshot_rules("inheritance_law", {"起征点": 60000, "rate": 0.2})

        # 3. 周期检测(规则变更)
        new_rules = {"起征点": 80000, "rate": 0.2}  # 起征点变了
        changes = detector.detect_changes("inheritance_law", new_rules)
        # 若有变更 → 自动通知订阅者

        # 4. RSS / webhook 主动拉取(可选)
        external_changes = detector.poll_rss("inheritance_law")
    """

    def __init__(
        self,
        store_path: str | None = None,
        notifier: Callable[[RegulatoryChange, list[Subscriber]], None] | None = None,
    ) -> None:
        self.store_path = store_path
        self._notifier = notifier or _default_notifier
        self._lock = threading.RLock()
        # 当前规则的快照(domain -> (rules_dict, version_hash))
        self._snapshots: dict[str, tuple[dict, str]] = {}
        # 订阅者(subscriber_id+domain 复合主键)
        self._subscribers: dict[str, Subscriber] = {}
        # 变更历史
        self._changes: list[RegulatoryChange] = []
        # 已发送通知去重(change_id -> True)
        self._notified: set[str] = set()
        if store_path and os.path.exists(store_path):
            self._load()

    # ==================================================================
    # 订阅管理
    # ==================================================================

    def subscribe(
        self,
        subscriber_id: str,
        domain: str,
        channels: list[NotificationChannel] | None = None,
        min_severity: ChangeSeverity = ChangeSeverity.MINOR,
        webhook_url: str = "",
    ) -> Subscriber:
        """订阅法规变更通知。"""
        sub = Subscriber(
            subscriber_id=subscriber_id,
            domain=domain,
            channels=channels or [NotificationChannel.IN_APP],
            min_severity=min_severity,
            webhook_url=webhook_url,
        )
        key = self._sub_key(subscriber_id, domain)
        with self._lock:
            self._subscribers[key] = sub
            self._save()
        logger.info("Subscribed %s to %s (channels=%s)", subscriber_id, domain, channels)
        return sub

    def unsubscribe(self, subscriber_id: str, domain: str) -> bool:
        """取消订阅。"""
        key = self._sub_key(subscriber_id, domain)
        with self._lock:
            sub = self._subscribers.get(key)
            if sub is None:
                return False
            sub.active = False
            self._save()
            return True

    def list_subscribers(self, domain: str | None = None) -> list[Subscriber]:
        with self._lock:
            if domain is None:
                return list(self._subscribers.values())
            return [s for s in self._subscribers.values() if s.domain == domain]

    # ==================================================================
    # 规则快照 + 变更检测
    # ==================================================================

    def snapshot_rules(self, domain: str, rules: dict) -> str:
        """记录规则快照(用于后续 diff)。"""
        rules_str = json.dumps(rules, sort_keys=True, ensure_ascii=False)
        version_hash = hashlib.sha256(rules_str.encode()).hexdigest()[:16]
        with self._lock:
            self._snapshots[domain] = (rules, version_hash)
            self._save()
        logger.info("Snapshotted %s rules (hash=%s)", domain, version_hash)
        return version_hash

    def detect_changes(
        self,
        domain: str,
        new_rules: dict,
        *,
        jurisdiction: str = "CN_MAINLAND",
        source_url: str = "",
    ) -> RegulatoryChange | None:
        """检测规则变更。

        Returns:
            RegulatoryChange(若变更),否则 None
        """
        new_str = json.dumps(new_rules, sort_keys=True, ensure_ascii=False)
        new_hash = hashlib.sha256(new_str.encode()).hexdigest()[:16]

        with self._lock:
            old = self._snapshots.get(domain)
            if old is None:
                # 首次记录,不算变更
                self._snapshots[domain] = (new_rules, new_hash)
                self._save()
                return None
            old_rules, old_hash = old
            if old_hash == new_hash:
                return None  # 无变更

            # 检测变更严重度
            severity, diff = self._compute_severity(old_rules, new_rules)
            change = RegulatoryChange(
                change_id=f"chg-{domain}-{int(time.time())}",
                jurisdiction=jurisdiction,
                domain=domain,
                severity=severity,
                title=f"{domain} rules updated",
                summary=diff,
                source_url=source_url,
                effective_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                old_version_hash=old_hash,
                new_version_hash=new_hash,
                diff_summary=diff,
            )
            self._changes.append(change)
            self._snapshots[domain] = (new_rules, new_hash)

            # 自动通知订阅者
            self._notify_subscribers(change)
            self._save()

        logger.warning(
            "Regulatory change detected: domain=%s, severity=%s, hash=%s->%s",
            domain, severity.value, old_hash, new_hash,
        )
        return change

    # ==================================================================
    # 通知
    # ==================================================================

    def notify_subscribers(
        self,
        change: RegulatoryChange,
    ) -> int:
        """通知订阅者(若未通知过)。"""
        return self._notify_subscribers(change)

    def _notify_subscribers(self, change: RegulatoryChange) -> int:
        """内部通知(去重)。"""
        if not is_enabled("defense"):
            return 0
        if change.change_id in self._notified:
            return 0
        # 找出活跃订阅者 + 严重度达标
        notified_count = 0
        with self._lock:
            for sub in self._subscribers.values():
                if not sub.active:
                    continue
                if sub.domain != change.domain:
                    continue
                if not severity_at_least(change.severity, sub.min_severity):
                    continue
                change.affected_subscribers.append(sub.subscriber_id)
                notified_count += 1

        if notified_count > 0:
            try:
                # 找出真正要通知的订阅者列表
                subs_to_notify = [
                    s for s in self._subscribers.values()
                    if s.active and s.domain == change.domain
                    and severity_at_least(change.severity, s.min_severity)
                ]
                self._notifier(change, subs_to_notify)
                self._notified.add(change.change_id)
                logger.info(
                    "Notified %d subscribers about change %s",
                    notified_count, change.change_id,
                )
            except Exception as e:
                logger.error("Failed to notify subscribers: %s", e)
        return notified_count

    # ==================================================================
    # RSS / Webhook 主动拉取(占位接口)
    # ==================================================================

    def poll_rss(self, domain: str) -> list[RegulatoryChange]:
        """从 RSS 源拉取变更(占位实现,生产对接具体 RSS / API)。"""
        # 占位:生产环境对接政府 RSS / 第三方法规监测服务
        return []

    def poll_webhook(self, domain: str) -> list[RegulatoryChange]:
        """从 webhook 接收变更(占位实现)。"""
        return []

    # ==================================================================
    # 查询 / 审计
    # ==================================================================

    def list_changes(
        self,
        domain: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[RegulatoryChange]:
        with self._lock:
            results = list(self._changes)
        if domain:
            results = [c for c in results if c.domain == domain]
        if since:
            results = [c for c in results if c.detected_at >= since]
        return results[-limit:]

    def get_snapshot(self, domain: str) -> tuple[dict, str] | None:
        with self._lock:
            return self._snapshots.get(domain)

    # ==================================================================
    # 内部
    # ==================================================================

    @staticmethod
    def _sub_key(subscriber_id: str, domain: str) -> str:
        return f"{subscriber_id}:{domain}"

    @staticmethod
    def _compute_severity(old: dict, new: dict) -> tuple[ChangeSeverity, str]:
        """计算变更严重度 + diff 摘要。"""
        diffs = []
        severity = ChangeSeverity.INFO

        all_keys = set(old.keys()) | set(new.keys())
        for k in all_keys:
            old_v = old.get(k)
            new_v = new.get(k)
            if old_v == new_v:
                continue
            if k not in old:
                diffs.append(f"+ {k}: {new_v}")
                if not severity_at_least(severity, ChangeSeverity.MINOR):
                    severity = ChangeSeverity.MINOR
            elif k not in new:
                diffs.append(f"- {k}: {old_v}")
                severity = ChangeSeverity.BREAKING  # 字段移除 = 破坏性
            else:
                diffs.append(f"* {k}: {old_v} -> {new_v}")
                # 数值变化幅度判断
                if isinstance(old_v, (int, float)) and isinstance(new_v, (int, float)):
                    if old_v != 0:
                        change_ratio = abs(new_v - old_v) / abs(old_v)
                        if change_ratio > 0.5:
                            severity = ChangeSeverity.MAJOR
                        elif change_ratio > 0.1 and not severity_at_least(severity, ChangeSeverity.MAJOR):
                            # 仍低于 MAJOR → 提升到 MINOR
                            if not severity_at_least(severity, ChangeSeverity.MINOR):
                                severity = ChangeSeverity.MINOR
                    else:
                        severity = ChangeSeverity.MAJOR
                else:
                    if not severity_at_least(severity, ChangeSeverity.MINOR):
                        severity = ChangeSeverity.MINOR

        diff_summary = "\n".join(diffs) if diffs else "no field changes"
        return severity, diff_summary

    def _save(self) -> None:
        if not self.store_path:
            return
        try:
            os.makedirs(os.path.dirname(self.store_path) or ".", exist_ok=True)
            data = {
                "snapshots": {
                    k: {"rules": v[0], "hash": v[1]}
                    for k, v in self._snapshots.items()
                },
                "subscribers": {
                    k: v.to_dict() for k, v in self._subscribers.items()
                },
                "changes": [c.to_dict() for c in self._changes],
                "notified": list(self._notified),
            }
            tmp = self.store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Failed to save regulatory change store: %s", e)

    def _load(self) -> None:
        try:
            with open(self.store_path, encoding="utf-8") as f:
                data = json.load(f)
            self._snapshots = {
                k: (v["rules"], v["hash"])
                for k, v in data.get("snapshots", {}).items()
            }
            for k, v in data.get("subscribers", {}).items():
                self._subscribers[k] = Subscriber(
                    subscriber_id=v["subscriber_id"],
                    domain=v["domain"],
                    channels=[NotificationChannel(c) for c in v.get("channels", [])],
                    min_severity=ChangeSeverity(v.get("min_severity", "minor")),
                    webhook_url=v.get("webhook_url", ""),
                    created_at=v.get("created_at", time.time()),
                    active=v.get("active", True),
                )
            for c in data.get("changes", []):
                self._changes.append(RegulatoryChange(
                    change_id=c["change_id"],
                    jurisdiction=c["jurisdiction"],
                    domain=c["domain"],
                    severity=ChangeSeverity(c["severity"]),
                    title=c["title"],
                    summary=c["summary"],
                    source_url=c.get("source_url", ""),
                    effective_date=c.get("effective_date", ""),
                    detected_at=c.get("detected_at", time.time()),
                    old_version_hash=c.get("old_version_hash", ""),
                    new_version_hash=c.get("new_version_hash", ""),
                    diff_summary=c.get("diff_summary", ""),
                ))
            self._notified = set(data.get("notified", []))
        except Exception as e:
            logger.error("Failed to load regulatory change store: %s", e)


def _default_notifier(change: RegulatoryChange, subscribers: list[Subscriber]) -> None:
    """默认通知器(仅 log,生产环境注入 IM / 邮件 / webhook 实现)。"""
    logger.info(
        "Regulatory change notification: %s (severity=%s, affected=%d subscribers)",
        change.title, change.severity.value, len(subscribers),
    )


# =====================================================================
# 全局单例
# =====================================================================

_detector_instance: RegulatoryChangeDetector | None = None
_lock = threading.Lock()


def get_regulatory_change_detector(
    store_path: str | None = None,
) -> RegulatoryChangeDetector:
    global _detector_instance
    with _lock:
        if _detector_instance is None:
            path = store_path or os.environ.get(
                "DEADMAN_REGULATORY_STORE",
                ".traecli/data/regulatory_changes.json",
            )
            _detector_instance = RegulatoryChangeDetector(store_path=path)
        return _detector_instance


def reset_regulatory_change_detector() -> None:
    global _detector_instance
    with _lock:
        _detector_instance = None
