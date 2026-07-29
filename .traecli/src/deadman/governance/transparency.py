"""P8.22 AI 透明度报告 - 周期性发布 AI 决策透明度报告。

借鉴 OpenAI Transparency Report 和 Google AI Principles Transparency Report,
定期 (monthly / quarterly / annual) 发布统计报告,公开 AI 决策次数 / 偏见事件 /
用户反馈 / 模型使用 / 数据请求等指标。

模块结构:
    - TransparencyReport: 单次报告 (dataclass)
    - TransparencyReporter: 报告器 (生成 / 添加章节 / 导出 / 列表)

设计:
    - 与 compliance.audit_report 集成 (lazy import,可选),拉取决策 / 删除请求统计
    - 支持 json / markdown / html 三种导出格式
    - sections: dict of section name → content,允许任意附加章节
    - 持久化到 data/governance/transparency_reports.json (按租户隔离)
    - 原子写 + 线程安全

feature flag:`DEADMAN_GOVERNANCE_ENABLED=0` 关闭时操作静默 no-op。
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
from ..infrastructure.multi_tenant import get_current_tenant_id, resolve_data_path

logger = logging.getLogger(__name__)


class ReportPeriod(str, Enum):
    """报告周期。"""

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    ADHOC = "adhoc"  # 临时报告


@dataclass
class TransparencyReport:
    """单次透明度报告。

    Attributes:
        report_id: 报告唯一 ID
        period_start: 报告周期开始 (epoch)
        period_end: 报告周期结束 (epoch)
        generated_at: 生成时间戳
        period: 周期类型 (monthly / quarterly / annual / adhoc)
        tenant_id: 租户 ID
        total_decisions: 决策总数 (含人工)
        ai_decisions_count: AI 决策数
        human_review_count: 人工复核数
        bias_incidents_count: 偏见事件数
        user_feedback_summary: 用户反馈摘要 (dict of category → count)
        model_usage_stats: 模型使用统计 (dict of model_id → call_count)
        data_requests_count: 用户数据请求 (导出) 数
        deletion_requests_count: 删除请求数
        sections: 附加章节 (dict of section name → content string)
    """

    report_id: str
    period_start: float
    period_end: float
    generated_at: float = field(default_factory=time.time)
    period: ReportPeriod = ReportPeriod.MONTHLY
    tenant_id: str = "default"
    total_decisions: int = 0
    ai_decisions_count: int = 0
    human_review_count: int = 0
    bias_incidents_count: int = 0
    user_feedback_summary: dict[str, int] = field(default_factory=dict)
    model_usage_stats: dict[str, int] = field(default_factory=dict)
    data_requests_count: int = 0
    deletion_requests_count: int = 0
    sections: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["period"] = self.period.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransparencyReport:
        period_val = data.get("period", "monthly")
        try:
            period = ReportPeriod(period_val)
        except ValueError:
            period = ReportPeriod.MONTHLY
        return cls(
            report_id=data["report_id"],
            period_start=float(data["period_start"]),
            period_end=float(data["period_end"]),
            generated_at=float(data.get("generated_at", time.time())),
            period=period,
            tenant_id=data.get("tenant_id", "default"),
            total_decisions=int(data.get("total_decisions", 0)),
            ai_decisions_count=int(data.get("ai_decisions_count", 0)),
            human_review_count=int(data.get("human_review_count", 0)),
            bias_incidents_count=int(data.get("bias_incidents_count", 0)),
            user_feedback_summary=dict(data.get("user_feedback_summary", {})),
            model_usage_stats=dict(data.get("model_usage_stats", {})),
            data_requests_count=int(data.get("data_requests_count", 0)),
            deletion_requests_count=int(data.get("deletion_requests_count", 0)),
            sections=dict(data.get("sections", {})),
        )


class TransparencyReporter:
    """透明度报告生成器。

    用法:
        reporter = get_transparency_reporter()
        report = reporter.generate_report(period_start, period_end)
        reporter.add_section(report.report_id, "executive_summary", "...")
        md = reporter.export(report.report_id, format="markdown")
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or resolve_data_path(
            "governance/transparency_reports.json"
        )
        self._lock = threading.RLock()
        self._cache: dict[str, TransparencyReport] = {}
        self._loaded = False

    def generate_report(
        self,
        period_start: float,
        period_end: float,
        period: ReportPeriod = ReportPeriod.MONTHLY,
        tenant_id: str | None = None,
    ) -> TransparencyReport:
        """生成报告 (聚合周期内统计)。"""
        if not is_enabled("governance"):
            logger.debug("Governance disabled, return stub report")
            return TransparencyReport(
                report_id="disabled",
                period_start=period_start,
                period_end=period_end,
                period=period,
                tenant_id=tenant_id or get_current_tenant_id() or "default",
            )

        tid = tenant_id or get_current_tenant_id() or "default"
        report_id = self._generate_id(period_end, tid, period)

        # 拉取 compliance.audit_report 统计 (lazy import,可选)
        audit_stats = self._fetch_audit_stats(period_start, period_end)

        report = TransparencyReport(
            report_id=report_id,
            period_start=period_start,
            period_end=period_end,
            period=period,
            tenant_id=tid,
            total_decisions=audit_stats.get("total_decisions", 0),
            ai_decisions_count=audit_stats.get("ai_decisions_count", 0),
            human_review_count=audit_stats.get("human_review_count", 0),
            bias_incidents_count=audit_stats.get("bias_incidents_count", 0),
            deletion_requests_count=audit_stats.get("deletion_requests", 0),
            data_requests_count=audit_stats.get("data_requests", 0),
            user_feedback_summary=audit_stats.get("user_feedback_summary", {}),
            model_usage_stats=audit_stats.get("model_usage_stats", {}),
        )

        with self._lock:
            self._load()
            self._cache[report.report_id] = report
            self._save()

        logger.info("Transparency report generated: %s", report.report_id)
        return report

    def add_section(self, report_id: str, name: str, content: str) -> TransparencyReport | None:
        """向报告添加附加章节。"""
        with self._lock:
            self._load()
            report = self._cache.get(report_id)
            if report is None:
                return None
            report.sections[name] = content
            self._save()
            return report

    def get(self, report_id: str) -> TransparencyReport | None:
        """按 ID 获取报告。"""
        with self._lock:
            self._load()
            return self._cache.get(report_id)

    def list_reports(
        self,
        period: ReportPeriod | None = None,
    ) -> list[TransparencyReport]:
        """列出报告 (按生成时间倒序)。"""
        with self._lock:
            self._load()
            reports = list(self._cache.values())
            if period:
                reports = [r for r in reports if r.period == period]
            reports.sort(key=lambda r: r.generated_at, reverse=True)
            return reports

    def export(self, report_id: str, format: str = "json") -> bytes:
        """导出报告为指定格式 (json / markdown / html)。

        Args:
            report_id: 报告 ID
            format: "json" / "markdown" / "html"

        Returns:
            bytes (UTF-8 编码)

        Raises:
            KeyError: 报告不存在
            ValueError: 不支持的 format
        """
        with self._lock:
            self._load()
            report = self._cache.get(report_id)
            if report is None:
                raise KeyError(f"Transparency report not found: {report_id}")

        fmt = format.lower()
        if fmt == "json":
            return json.dumps(
                report.to_dict(), ensure_ascii=False, indent=2, default=str
            ).encode("utf-8")
        elif fmt == "markdown":
            return self._render_markdown(report).encode("utf-8")
        elif fmt == "html":
            return self._render_html(report).encode("utf-8")
        else:
            raise ValueError(f"Unsupported export format: {format}")

    # ==================================================================
    # 渲染
    # ==================================================================

    def _render_markdown(self, report: TransparencyReport) -> str:
        lines = [
            f"# AI 透明度报告 {report.report_id}",
            "",
            f"- **周期**: {report.period.value}",
            f"- **时间范围**: {report.period_start} - {report.period_end}",
            f"- **租户**: {report.tenant_id}",
            f"- **生成时间**: {report.generated_at}",
            "",
            "## 关键指标",
            "",
            f"- 决策总数: {report.total_decisions}",
            f"- AI 决策数: {report.ai_decisions_count}",
            f"- 人工复核数: {report.human_review_count}",
            f"- 偏见事件数: {report.bias_incidents_count}",
            f"- 数据导出请求: {report.data_requests_count}",
            f"- 删除请求: {report.deletion_requests_count}",
            "",
        ]
        if report.model_usage_stats:
            lines.append("## 模型使用统计")
            lines.append("")
            for mid, count in report.model_usage_stats.items():
                lines.append(f"- {mid}: {count}")
            lines.append("")
        if report.user_feedback_summary:
            lines.append("## 用户反馈")
            lines.append("")
            for cat, count in report.user_feedback_summary.items():
                lines.append(f"- {cat}: {count}")
            lines.append("")
        if report.sections:
            lines.append("## 附加章节")
            lines.append("")
            for name, content in report.sections.items():
                lines.append(f"### {name}")
                lines.append("")
                lines.append(content)
                lines.append("")
        return "\n".join(lines)

    def _render_html(self, report: TransparencyReport) -> str:
        md = self._render_markdown(report)
        # 极简 markdown → html 转换 (h1/h2/h3/list)
        html_parts = ["<!DOCTYPE html>", "<html><head><meta charset='utf-8'>",
                      "<title>AI 透明度报告</title></head><body>"]
        for line in md.split("\n"):
            stripped = line.strip()
            if stripped.startswith("# "):
                html_parts.append(f"<h1>{stripped[2:]}</h1>")
            elif stripped.startswith("## "):
                html_parts.append(f"<h2>{stripped[3:]}</h2>")
            elif stripped.startswith("### "):
                html_parts.append(f"<h3>{stripped[4:]}</h3>")
            elif stripped.startswith("- "):
                html_parts.append(f"<li>{stripped[2:]}</li>")
            elif stripped:
                html_parts.append(f"<p>{stripped}</p>")
        html_parts.append("</body></html>")
        return "\n".join(html_parts)

    # ==================================================================
    # 集成 compliance.audit_report (lazy import,可选)
    # ==================================================================

    def _fetch_audit_stats(
        self,
        period_start: float,
        period_end: float,
    ) -> dict[str, Any]:
        """从 compliance.audit_report 拉取统计 (可选集成)。

        若 compliance 未启用 / 导入失败,返回空统计。
        """
        stats: dict[str, Any] = {}
        try:
            from ..compliance.audit_report import get_audit_reporter  # lazy
            reporter = get_audit_reporter()
            reports = reporter.list_reports(limit=1000)
            period_reports = [
                r for r in reports
                if period_start <= r.period_start <= period_end
            ]
            stats["total_decisions"] = sum(r.total_calls for r in period_reports)
            stats["deletion_requests"] = sum(r.deletion_requests for r in period_reports)
            stats["data_requests"] = sum(r.deletion_completed for r in period_reports)
            # 偏见事件计数 (event_type="bias_incident")
            stats["bias_incidents_count"] = sum(
                1 for r in period_reports
                for e in r.events if e.event_type == "bias_incident"
            )
        except Exception as e:
            logger.debug("Fetch audit stats failed (optional): %s", e)
        return stats

    # ==================================================================
    # 内部
    # ==================================================================

    def _generate_id(
        self,
        period_end: float,
        tenant_id: str,
        period: ReportPeriod,
    ) -> str:
        ts = time.strftime("%Y%m%d", time.localtime(period_end))
        return f"transparency-{tenant_id}-{period.value}-{ts}-{int(period_end) % 100000}"

    # ==================================================================
    # 持久化
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for rid, rdata in data.get("reports", {}).items():
                    self._cache[rid] = TransparencyReport.from_dict(rdata)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Load transparency reports failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "reports": {rid: r.to_dict() for rid, r in self._cache.items()},
            }
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, self.store_path)
        except OSError as e:
            logger.error("Save transparency reports failed: %s", e)


# 全局单例
_tr_instance: TransparencyReporter | None = None
_tr_lock = threading.Lock()


def get_transparency_reporter() -> TransparencyReporter:
    global _tr_instance
    if _tr_instance is None:
        with _tr_lock:
            if _tr_instance is None:
                _tr_instance = TransparencyReporter()
    return _tr_instance
