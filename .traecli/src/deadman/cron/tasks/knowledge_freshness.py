"""知识库时效巡检 - Phase 16A

扫描 .traecli/knowledge/regions/ 下的地域知识库 markdown 文件，
解析元信息中"最后更新"日期，按 retrieval-guardrails.md 第二节规则判定时效：
  - 超 180 天未更新 → stale（必须触发更新或明确告知用户"此信息可能已过时"）
  - 超 90 天未更新（针对税务/社保/银行/医疗等政策变更高发领域）→ warning
  - 其余 → fresh

并提供：
  - check_official_sources: 对 stale 文件提取关键政策点，输出待审核列表
    （实际调用 LLM/WebSearch 的对比逻辑后续接入；先实现"读取文件→提取关键政策点"骨架）
  - propose_refresh_tasks: 为每个 stale 文件生成 cron 任务建议，
    通过 deadman.cron.scheduler 的 propose 机制提交

设计原则：
  - 不引入新依赖（仅用 stdlib + 项目已有模块）
  - 用 stdlib re 解析 markdown 元信息
  - 不修改知识库文件本身（只读 + 生成报告/任务建议）
  - 与 integrity-framework.md / retrieval-guardrails.md 协同：
    本模块负责"识别 + 提议"，不替代用户决策
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# retrieval-guardrails.md §二：超 6 个月（约 180 天）视为过期；
# 政策变更高发领域建议 3 个月（约 90 天）复核一次
STALE_DAYS = 180
WARNING_DAYS = 90

# 政策变更高发领域关键词（出现在文件路径或文件名中即视为 warning 适用）
# 与 SCHEMA.md 各阶段标题对应
HIGH_FREQ_POLICY_AREAS = [
    "税务",
    "社保",
    "银行",
    "医疗",
    "金融",
    "不动产",
    "车辆",
    "公积金",
    "医保",
    "保险",
    "继承",
    "债权债务",
    "遗产税",
    "契税",
]


# ============================================================
# 数据模型
# ============================================================


@dataclass
class FreshnessReport:
    """单文件时效报告

    Attributes:
        file_path: .md 文件绝对路径
        region: 地区标识（如 "CN/beijing"、"US/california"）
        last_updated: 文件元信息中的"最后更新"日期；缺失为 None
        days_old: 距今天数；last_updated 为 None 时为 None
        status: fresh / warning / stale / unknown
        policy_areas: 命中的高频政策领域列表（用于触发 warning 判定）
    """

    file_path: Path
    region: str
    last_updated: date | None = None
    days_old: int | None = None
    status: str = "unknown"
    policy_areas: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """序列化为可 JSON 化的 dict"""
        return {
            "file_path": str(self.file_path),
            "region": self.region,
            "last_updated": (self.last_updated.isoformat() if self.last_updated else None),
            "days_old": self.days_old,
            "status": self.status,
            "policy_areas": list(self.policy_areas),
        }


@dataclass
class DriftItem:
    """单条政策漂移待审核项

    表示"知识库当前文本" vs "建议核对/替换文本"的差异候选。
    本期仅生成骨架数据（current_text 来自文件，suggested_text/source_url 留空），
    实际对比逻辑由后续接入的 LLM/WebSearch 调用填充。

    Attributes:
        file_path: 来源文件绝对路径
        area: 政策领域（如"社保"、"医疗"）
        current_text: 知识库中当前的文本片段
        suggested_text: 建议替换文本（本期为空，待 LLM/WebSearch 填充）
        source_url: 建议来源 URL（本期为空）
        confidence: 置信度 high/medium/low/unknown（本期默认 unknown）
    """

    file_path: Path
    area: str
    current_text: str
    suggested_text: str = ""
    source_url: str = ""
    confidence: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "file_path": str(self.file_path),
            "area": self.area,
            "current_text": self.current_text,
            "suggested_text": self.suggested_text,
            "source_url": self.source_url,
            "confidence": self.confidence,
        }


# ============================================================
# KnowledgeFreshnessChecker
# ============================================================


class KnowledgeFreshnessChecker:
    """知识库时效巡检器

    用法：
        checker = KnowledgeFreshnessChecker()
        reports = checker.scan_regions(Path(".traecli/knowledge/regions"))
        stale_reports = [r for r in reports if r.status == "stale"]
        for r in stale_reports:
            drifts = checker.check_official_sources(r)
            # drifts 中包含建议人工审核的关键政策点
        proposals = checker.propose_refresh_tasks(stale_reports)
        # proposals 为 cron 任务建议列表，可人工确认后提交

    设计：
        - 无状态（每次 scan 重新读取文件）
        - 不修改知识库文件
        - 不调用外部 LLM/WebSearch（本期）；接入点已留好
    """

    def __init__(
        self,
        stale_days: int = STALE_DAYS,
        warning_days: int = WARNING_DAYS,
        reference_date: date | None = None,
        scheduler=None,
    ):
        """构造巡检器

        Args:
            stale_days: 超过此天数标记为 stale（默认 180）
            warning_days: 超过此天数（且 < stale_days）标记为 warning（默认 90）
            reference_date: 参考日期，默认今天；测试时可注入固定日期
            scheduler: 可选 CronScheduler 实例；若提供，
                propose_refresh_tasks 会调用 scheduler.propose_job 提交任务建议
        """
        if stale_days <= 0 or warning_days <= 0:
            raise ValueError("stale_days / warning_days 必须为正整数")
        if warning_days > stale_days:
            raise ValueError("warning_days 不能大于 stale_days")
        self.stale_days = stale_days
        self.warning_days = warning_days
        self.reference_date = reference_date or date.today()
        self.scheduler = scheduler

    # ============================================================
    # scan_regions: 扫描所有 .md 文件，解析元信息，判定状态
    # ============================================================

    def scan_regions(self, regions_dir: Path) -> list[FreshnessReport]:
        """扫描 regions_dir 下所有 .md 文件，生成 FreshnessReport 列表

        - 递归扫描子目录（如 CN/、US/）
        - 跳过 SCHEMA.md（schema 文件不计入时效扫描）
        - 解析元信息区块中"最后更新: YYYY-MM-DD"格式的日期
        - 缺失日期的文件标记为 status="unknown"
        - policy_areas 根据文件内容关键词命中

        Args:
            regions_dir: regions 目录路径（通常 .traecli/knowledge/regions）

        Returns:
            FreshnessReport 列表，按文件路径排序
        """
        regions_dir = Path(regions_dir)
        if not regions_dir.exists():
            logger.warning("regions_dir 不存在: %s", regions_dir)
            return []
        if not regions_dir.is_dir():
            logger.warning("regions_dir 不是目录: %s", regions_dir)
            return []

        reports: list[FreshnessReport] = []
        for md_path in sorted(regions_dir.rglob("*.md")):
            # 跳过 SCHEMA.md（schema 文件不计入时效扫描）
            if md_path.name == "SCHEMA.md":
                continue
            # 跳过 _archived/ 和 _quarantine/ 下的文件（按 retrieval-guardrails §二）
            if "_archived" in md_path.parts or "_quarantine" in md_path.parts:
                continue
            try:
                report = self._scan_file(md_path, regions_dir)
            except Exception as e:
                logger.warning("扫描文件失败 %s: %s（跳过）", md_path, e)
                continue
            reports.append(report)
        return reports

    def _scan_file(self, md_path: Path, regions_dir: Path) -> FreshnessReport:
        """扫描单个 .md 文件并生成报告"""
        text = md_path.read_text(encoding="utf-8")
        last_updated = self._parse_last_updated(text)
        policy_areas = self._detect_policy_areas(text)

        # 计算 region 标识：相对 regions_dir 的父路径（如 "CN/beijing"）
        try:
            rel = md_path.relative_to(regions_dir)
            # 去掉 .md 后缀，用 / 连接
            region = str(rel.with_suffix("")).replace("\\", "/")
        except ValueError:
            region = md_path.stem

        # 计算天数与状态
        if last_updated is None:
            days_old = None
            status = "unknown"
        else:
            days_old = (self.reference_date - last_updated).days
            status = self._compute_status(days_old, policy_areas)

        return FreshnessReport(
            file_path=md_path,
            region=region,
            last_updated=last_updated,
            days_old=days_old,
            status=status,
            policy_areas=policy_areas,
        )

    @staticmethod
    def _parse_last_updated(text: str) -> date | None:
        """从 markdown 元信息中解析"最后更新"日期

        支持以下格式：
            - 最后更新: 2026-01-01
            - 最后更新: 2026-1-1（单位数月日）
            - 最后更新:2026-01-01（无空格）
            - - 最后更新: 2026-01-01（列表项）

        匹配元信息区块（文件顶部 ## 元信息 之下，或行首出现"最后更新"）。
        缺失或格式错误返回 None。
        """
        # 匹配 "最后更新: YYYY-MM-DD"，允许前后空白与可选列表符
        pattern = re.compile(r"最后更新\s*[:：]\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
        m = pattern.search(text)
        if not m:
            return None
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            # 日期非法（如 2026-13-40）
            return None

    @staticmethod
    def _detect_policy_areas(text: str) -> list[str]:
        """检测文本中命中的高频政策领域

        返回去重后的领域列表（保持 HIGH_FREQ_POLICY_AREAS 顺序）。
        """
        hit: list[str] = []
        for area in HIGH_FREQ_POLICY_AREAS:
            if area in text:
                hit.append(area)
        return hit

    def _compute_status(self, days_old: int, policy_areas: list[str]) -> str:
        """根据天数与政策领域判定状态

        - >= stale_days → stale（无论是否高频领域）
        - >= warning_days（且 < stale_days）→ warning
            - 高频领域（policy_areas 非空）在 90 天就 warning
            - 非高频领域在 90-180 天之间仍为 fresh（按 retrieval-guardrails §二
              "超 6 个月未更新才提示过期"）
            - 但若文件无任何高频领域且未达 stale 阈值，标 fresh
        - 其余 → fresh
        """
        if days_old >= self.stale_days:
            return "stale"
        if days_old >= self.warning_days and policy_areas:
            return "warning"
        return "fresh"

    # ============================================================
    # check_official_sources: 对 stale 文件提取关键政策点
    # ============================================================

    def check_official_sources(self, report: FreshnessReport) -> list[DriftItem]:
        """对 stale 文件提取关键政策点，输出待审核列表

        本期实现骨架：
          1. 读取文件内容
          2. 按高频政策领域关键词切片，提取含金额/时限/电话/法条号的句子
          3. 输出 DriftItem 列表（suggested_text/source_url 留空，confidence="unknown"）

        实际对比官方源的逻辑（调用 LLM/WebSearch）后续接入。
        建议接入点：在生成 DriftItem 后，对每条调用 web_search
        搜索 "[政策点] [地区] 官方 最新"，由 LLM 比对填充 suggested_text/source_url。

        Args:
            report: 待检查的 FreshnessReport（status 应为 stale）

        Returns:
            DriftItem 列表；若文件不存在或无关键政策点，返回空列表
        """
        if report.status != "stale":
            logger.info(
                "check_official_sources 仅对 stale 文件生效，%s 当前为 %s",
                report.file_path,
                report.status,
            )
            return []

        if not report.file_path.exists():
            logger.warning("文件不存在: %s", report.file_path)
            return []

        text = report.file_path.read_text(encoding="utf-8")
        drifts: list[DriftItem] = []

        # 按行扫描，提取含具体数据点的句子（金额/时限/电话/法条号/百分号）
        # 这些是 retrieval-guardrails.md §四要求附来源的关键信息
        amount_re = re.compile(r"\d+\s*[元万亿%]|约\s*\d|人民币")
        time_re = re.compile(r"\d+\s*[天月年]|工作日|日内")
        phone_re = re.compile(r"\d{3}[-\s]?\d{3,4}[-\s]?\d{4}|12\d{3}|95\d{3}")
        law_re = re.compile(r"《[^》]+》|第\s?\d+\s?条|法[律例]")
        heading_re = re.compile(r"^#+\s*(.+)$")

        # 跟踪当前章节标题，用于归属行到政策领域
        # 例如 "## 阶段8：社保" 下的所有数据点行都归属"社保"
        current_section: str = ""

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # 跟踪章节标题
            h_match = heading_re.match(stripped)
            if h_match:
                current_section = h_match.group(1)
                continue

            # 是否含具体数据点
            if not (
                amount_re.search(stripped)
                or time_re.search(stripped)
                or phone_re.search(stripped)
                or law_re.search(stripped)
            ):
                continue

            # 找该行所属政策领域：
            # 1. 先看本行是否含高频领域关键词
            # 2. 再看当前章节标题是否含高频领域关键词
            matched_area: str | None = None
            for area in report.policy_areas:
                if area in stripped:
                    matched_area = area
                    break
            if matched_area is None:
                for area in report.policy_areas:
                    if area in current_section:
                        matched_area = area
                        break

            if matched_area is None:
                continue

            drifts.append(
                DriftItem(
                    file_path=report.file_path,
                    area=matched_area,
                    current_text=stripped,
                    suggested_text="",  # 待 LLM/WebSearch 填充
                    source_url="",  # 待 LLM/WebSearch 填充
                    confidence="unknown",
                )
            )

        logger.info(
            "check_official_sources: %s 提取 %d 条待审核项",
            report.file_path,
            len(drifts),
        )
        return drifts

    # ============================================================
    # propose_refresh_tasks: 生成 cron 任务建议
    # ============================================================

    def propose_refresh_tasks(
        self,
        reports: list[FreshnessReport],
        user_id: str = "system",
        schedule: str = "0 9 1 * *",  # 每月 1 日 9:00
    ) -> list[dict]:
        """为 stale 文件生成 cron 任务建议

        每个 stale 文件一个任务建议，通过 scheduler.propose_job 提交
        （若构造时注入了 scheduler）；未注入 scheduler 时仅返回建议列表。

        与 notification-guardrails.md §三 协同：
          - 任务 pending_confirmation=True，需用户在下一轮确认后才激活
          - schedule 默认每月一次（满足"最小间隔 24h"约束）
          - 任务内容仅含"待刷新文件路径 + 提示"，不含逝者隐私信息

        Args:
            reports: scan_regions 返回的报告列表（仅 stale 的会被处理）
            user_id: 任务归属用户，默认 "system"
            schedule: cron 表达式，默认每月 1 日 9:00

        Returns:
            任务建议 dict 列表，每项含：
              - file_path, region, last_updated, days_old
              - proposed: 是否已通过 scheduler 提交（True/False）
              - job_id: 若已提交，scheduler 返回的 job_id；否则 None
              - message: 提示信息
        """
        stale_reports = [r for r in reports if r.status == "stale"]
        proposals: list[dict] = []

        for r in stale_reports:
            content = (
                f"[知识库时效巡检] 文件 {r.region} 已 {r.days_old} 天未更新"
                f"（最后更新: {r.last_updated.isoformat() if r.last_updated else '未知'}）。"
                f"建议人工核实最新政策并刷新文件。"
            )
            proposal: dict = {
                "file_path": str(r.file_path),
                "region": r.region,
                "last_updated": (r.last_updated.isoformat() if r.last_updated else None),
                "days_old": r.days_old,
                "proposed": False,
                "job_id": None,
                "message": "未注入 scheduler，仅生成建议",
            }

            if self.scheduler is not None:
                # 通过 scheduler 的 propose 机制提交（双重确认：用户需 confirm）
                try:
                    import asyncio

                    result = asyncio.run(
                        self.scheduler.propose_job(
                            user_id=user_id,
                            schedule=schedule,
                            content=content,
                        )
                    )
                    proposal["proposed"] = True
                    proposal["job_id"] = result.get("job_id")
                    proposal["message"] = result.get("message", "已通过 scheduler.propose_job 提交")
                except Exception as e:
                    logger.warning(
                        "scheduler.propose_job 失败 %s: %s（仅生成建议）",
                        r.file_path,
                        e,
                    )
                    proposal["message"] = f"scheduler 提交失败: {e}"

            proposals.append(proposal)

        logger.info(
            "propose_refresh_tasks: 生成 %d 条任务建议（其中 %d 已提交 scheduler）",
            len(proposals),
            sum(1 for p in proposals if p["proposed"]),
        )
        return proposals

    # ============================================================
    # 综合入口
    # ============================================================

    def run_full_check(
        self, regions_dir: Path
    ) -> tuple[list[FreshnessReport], list[DriftItem], list[dict]]:
        """一键运行完整巡检流程

        1. scan_regions 扫描所有文件
        2. 对每个 stale 文件调用 check_official_sources 提取待审核项
        3. 调用 propose_refresh_tasks 生成刷新任务建议

        Returns:
            (reports, drift_items, proposals)
        """
        reports = self.scan_regions(regions_dir)
        all_drifts: list[DriftItem] = []
        for r in reports:
            if r.status == "stale":
                all_drifts.extend(self.check_official_sources(r))
        proposals = self.propose_refresh_tasks(reports)
        return reports, all_drifts, proposals
