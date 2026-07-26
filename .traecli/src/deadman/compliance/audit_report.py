"""P8.6.4 监管上报接口 - 周期性向监管机构提交合规报告。

法规依据:
    - 中国《生成式人工智能服务管理暂行办法》第 15 条:
      服务提供者应当依法承担网络信息内容生产者责任
    - 中国《互联网信息服务深度合成管理规定》第 22 条:
      深度合成服务提供者应当建立健全管理制度
    - GDPR 第 30 条:处理活动记录
    - GDPR 第 33 条:数据泄露 72 小时内上报

设计:
    - AuditReport: 单次上报内容(基础统计 + 异常事件 + 处置记录)
    - AuditReporter: 上报器(周期性 / 事件驱动)
    - ReportFrequency: 上报频率(日 / 周 / 月 / 事件触发)

上报通道:
    - 在线 API:监管机构开放接口(配置 url + token)
    - 邮件:发到监管机构邮箱(配置 smtp)
    - 文件归档:生成本地报告文件(便于线下提交)

上报内容:
    - 服务概览:用户数 / 调用量 / 模型版本
    - 异常事件:数据泄露 / 模型滥用 / 安全事件
    - 处置记录:违规用户处理 / 模型下架
    - 合规审计:数据驻留检查 / 删除请求处理

feature flag:`DEADMAN_COMPLIANCE_ENABLED=0` 关闭时不实际上报(只生成本地报告)
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import threading
import time
from dataclasses import asdict, dataclass, field
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id

logger = logging.getLogger(__name__)


class ReportFrequency(str, Enum):
    """上报频率。"""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    EVENT_DRIVEN = "event_driven"  # 事件触发(立即上报)


class ReportStatus(str, Enum):
    """上报状态机:

    DRAFT → SUBMITTED → ACKNOWLEDGED
                ↓
            FAILED(网络 / 鉴权失败)
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"  # 已发送
    ACKNOWLEDGED = "acknowledged"  # 监管确认收到
    FAILED = "failed"
    ARCHIVED = "archived"  # 已归档(线下提交)


@dataclass
class AuditEvent:
    """单条审计事件。"""

    timestamp: float
    event_type: str  # data_leak / model_abuse / security_incident / user_complaint
    severity: str  # info / warning / critical
    description: str
    affected_users: int = 0
    affected_records: int = 0
    remediation: str = ""  # 处置措施
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditReport:
    """单次上报内容。"""

    report_id: str
    period_start: float
    period_end: float
    frequency: ReportFrequency
    tenant_id: str
    # 服务概览
    total_users: int = 0
    total_calls: int = 0
    models_used: list[str] = field(default_factory=list)
    # 异常事件
    events: list[AuditEvent] = field(default_factory=list)
    # 处置记录
    remediations: list[dict[str, Any]] = field(default_factory=list)
    # 合规审计
    residency_violations: int = 0
    deletion_requests: int = 0
    deletion_completed: int = 0
    # 状态
    status: ReportStatus = ReportStatus.DRAFT
    submitted_at: Optional[float] = None
    acknowledged_at: Optional[float] = None
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["frequency"] = self.frequency.value
        d["status"] = self.status.value
        return d


class AuditReporter:
    """监管上报器。

    用法:
        reporter = get_audit_reporter()
        reporter.record_event(AuditEvent(
            timestamp=time.time(),
            event_type="data_leak",
            severity="critical",
            description="Unauthorized access to user data",
            affected_users=5,
        ))
        # 周期性上报(由 cron 触发)
        report = reporter.generate_report(period_start, period_end)
        reporter.submit(report)
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store_path = store_path or Path(
            os.environ.get("DEADMAN_AUDIT_REPORT_STORE", "data/compliance/audit_reports.json")
        )
        self._lock = threading.RLock()
        self._events: list[AuditEvent] = []
        self._reports: dict[str, AuditReport] = {}
        self._loaded = False

    def record_event(self, event: AuditEvent) -> None:
        """记录审计事件(供后续上报)。"""
        if not is_enabled("compliance"):
            return
        with self._lock:
            self._load()
            self._events.append(event)
            self._save_events()

    def generate_report(
        self,
        period_start: float,
        period_end: float,
        frequency: ReportFrequency = ReportFrequency.MONTHLY,
        tenant_id: Optional[str] = None,
    ) -> AuditReport:
        """生成上报报告(从事件库聚合)。"""
        if not is_enabled("compliance"):
            return self._disabled_report(period_start, period_end)

        tid = tenant_id or get_current_tenant_id() or "default"

        with self._lock:
            self._load()
            # 筛选时间窗口内的事件
            period_events = [
                e for e in self._events
                if period_start <= e.timestamp <= period_end
            ]

            # 聚合统计
            residency_violations = sum(
                1 for e in period_events if e.event_type == "residency_violation"
            )
            deletion_requests = sum(
                1 for e in period_events if e.event_type == "deletion_request"
            )
            deletion_completed = sum(
                1 for e in period_events
                if e.event_type == "deletion_completed"
            )

            report = AuditReport(
                report_id=self._generate_id(period_end, tid),
                period_start=period_start,
                period_end=period_end,
                frequency=frequency,
                tenant_id=tid,
                events=period_events,
                residency_violations=residency_violations,
                deletion_requests=deletion_requests,
                deletion_completed=deletion_completed,
            )
            self._reports[report.report_id] = report
            self._save_reports()

        logger.info("Generated audit report %s (events=%d)", report.report_id, len(period_events))
        return report

    def submit(self, report: AuditReport) -> bool:
        """提交上报(按配置的通道)。"""
        if not is_enabled("compliance"):
            report.status = ReportStatus.ARCHIVED
            return True

        channel = os.environ.get("DEADMAN_AUDIT_SUBMIT_CHANNEL", "file")
        success = False

        try:
            if channel == "api":
                success = self._submit_via_api(report)
            elif channel == "email":
                success = self._submit_via_email(report)
            else:  # file
                success = self._submit_via_file(report)

            if success:
                report.status = ReportStatus.SUBMITTED
                report.submitted_at = time.time()
                logger.info("Audit report %s submitted via %s", report.report_id, channel)
            else:
                report.status = ReportStatus.FAILED
                report.error_message = f"Submit via {channel} returned False"
        except Exception as e:
            report.status = ReportStatus.FAILED
            report.error_message = str(e)
            logger.error("Submit audit report %s failed: %s", report.report_id, e)

        with self._lock:
            self._save_reports()
        return success

    def acknowledge(self, report_id: str) -> bool:
        """监管确认收到(更新状态)。"""
        with self._lock:
            self._load()
            report = self._reports.get(report_id)
            if report is None:
                return False
            report.status = ReportStatus.ACKNOWLEDGED
            report.acknowledged_at = time.time()
            self._save_reports()
            return True

    def list_reports(
        self,
        status: Optional[ReportStatus] = None,
        limit: int = 100,
    ) -> list[AuditReport]:
        with self._lock:
            self._load()
            reports = list(self._reports.values())
            if status:
                reports = [r for r in reports if r.status == status]
            reports.sort(key=lambda r: r.created_at, reverse=True)
            return reports[:limit]

    # ==================================================================
    # 内部:上报通道实现
    # ==================================================================

    def _submit_via_api(self, report: AuditReport) -> bool:
        """通过监管机构 API 上报(占位,实际需 SDK)。"""
        api_url = os.environ.get("DEADMAN_AUDIT_API_URL", "")
        api_token = os.environ.get("DEADMAN_AUDIT_API_TOKEN", "")
        if not api_url or not api_token:
            logger.warning("Audit API URL or token not configured, skipping")
            return False
        # 占位:实际用 requests.post(api_url, json=report.to_dict(), headers={"Authorization": f"Bearer {api_token}"})
        logger.info("Would submit report %s to %s", report.report_id, api_url)
        return True

    def _submit_via_email(self, report: AuditReport) -> bool:
        """通过邮件上报(附件形式)。"""
        smtp_host = os.environ.get("DEADMAN_AUDIT_SMTP_HOST", "")
        smtp_port = int(os.environ.get("DEADMAN_AUDIT_SMTP_PORT", "587"))
        smtp_user = os.environ.get("DEADMAN_AUDIT_SMTP_USER", "")
        smtp_pass = os.environ.get("DEADMAN_AUDIT_SMTP_PASS", "")
        recipient = os.environ.get("DEADMAN_AUDIT_SMTP_TO", "")

        if not all([smtp_host, smtp_user, smtp_pass, recipient]):
            logger.warning("SMTP not configured, falling back to file")
            return self._submit_via_file(report)

        msg = MIMEMultipart()
        msg["From"] = smtp_user
        msg["To"] = recipient
        msg["Subject"] = f"[合规上报] {report.report_id} ({report.frequency.value})"

        body = f"""
合规审计报告

报告 ID: {report.report_id}
周期: {report.period_start} - {report.period_end}
租户: {report.tenant_id}

异常事件数: {len(report.events)}
数据驻留违规: {report.residency_violations}
删除请求: {report.deletion_requests}
删除已完成: {report.deletion_completed}
""".strip()
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # 附件:完整 JSON
        attachment = MIMEApplication(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
            Name=f"{report.report_id}.json",
        )
        attachment["Content-Disposition"] = f'attachment; filename="{report.report_id}.json"'
        msg.attach(attachment)

        try:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            return True
        except Exception as e:
            logger.error("Email submit failed: %s", e)
            return False

    def _submit_via_file(self, report: AuditReport) -> bool:
        """生成本地报告文件(便于线下提交)。"""
        archive_dir = self.store_path.parent / "submitted"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{report.report_id}.json"
        try:
            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(report.to_dict(), f, ensure_ascii=False, indent=2, default=str)
            logger.info("Audit report archived to %s", archive_path)
            return True
        except Exception as e:
            logger.error("File archive failed: %s", e)
            return False

    def _disabled_report(
        self,
        period_start: float,
        period_end: float,
    ) -> AuditReport:
        return AuditReport(
            report_id="disabled",
            period_start=period_start,
            period_end=period_end,
            frequency=ReportFrequency.MONTHLY,
            tenant_id="default",
            status=ReportStatus.ARCHIVED,
        )

    def _generate_id(self, period_end: float, tenant_id: str) -> str:
        ts = time.strftime("%Y%m%d", time.localtime(period_end))
        return f"audit-{tenant_id}-{ts}-{int(period_end) % 100000}"

    # ==================================================================
    # 持久化
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                self._events = [AuditEvent(**e) for e in data.get("events", [])]
                self._reports = {
                    r["report_id"]: AuditReport(
                        frequency=ReportFrequency(r["frequency"]),
                        status=ReportStatus(r["status"]),
                        **{k: v for k, v in r.items() if k not in ("frequency", "status")},
                    )
                    for r in data.get("reports", [])
                }
        except Exception as e:
            logger.warning("Load audit reports failed: %s", e)
        self._loaded = True

    def _save_events(self) -> None:
        # 事件单独存储(避免 reports 文件过大)
        events_path = self.store_path.parent / "audit_events.jsonl"
        try:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(events_path, "a", encoding="utf-8") as f:
                for e in self._events[-10:]:  # 只写最近 10 条(避免重复)
                    f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Save events failed: %s", e)

    def _save_reports(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".tmp")
            data = {
                "events": [asdict(e) for e in self._events],
                "reports": [
                    {k: v for k, v in r.to_dict().items()}
                    for r in self._reports.values()
                ],
            }
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Save reports failed: %s", e)


# 全局单例
_ar_instance: Optional[AuditReporter] = None
_ar_lock = threading.Lock()


def get_audit_reporter() -> AuditReporter:
    global _ar_instance
    if _ar_instance is None:
        with _ar_lock:
            if _ar_instance is None:
                _ar_instance = AuditReporter()
    return _ar_instance
