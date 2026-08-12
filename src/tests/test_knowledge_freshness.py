"""测试 deadman.cron.tasks.knowledge_freshness - 知识库时效巡检

覆盖点（14 个）：
  - test_parse_last_updated_standard_format
  - test_parse_last_updated_no_space
  - test_parse_last_updated_list_prefix
  - test_parse_last_updated_single_digit_month_day
  - test_parse_last_updated_missing
  - test_parse_last_updated_invalid_date
  - test_scan_regions_fresh_status
  - test_scan_regions_warning_status
  - test_scan_regions_stale_status
  - test_scan_regions_unknown_when_missing_date
  - test_scan_regions_skips_schema_and_archived
  - test_scan_regions_detects_policy_areas
  - test_propose_refresh_tasks_count_matches_stale
  - test_propose_refresh_tasks_no_scheduler
  - test_propose_refresh_tasks_with_scheduler
  - test_check_official_sources_only_stale
  - test_check_official_sources_extracts_data_points
  - test_run_full_check_end_to_end

设计：
  - 用 tmp_path 隔离测试文件，不污染真实知识库
  - 用固定 reference_date 注入，确保状态判定可重现
  - 不依赖 pytest-asyncio：async 方法用 asyncio.run() 在 sync 测试函数内调用
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from deadman.cron.tasks.knowledge_freshness import (
    DriftItem,
    FreshnessReport,
    KnowledgeFreshnessChecker,
)

# =====================================================================
# 辅助：构造测试 .md 文件
# =====================================================================


def _make_md(
    region: str,
    last_updated: str | None,
    body_extra: str = "",
) -> str:
    """构造一个最小化的地域知识库 .md 内容

    Args:
        region: 地区名
        last_updated: "最后更新"日期字符串；None 表示缺失
        body_extra: 额外正文（用于触发政策领域命中）
    """
    last_updated_line = f"- 最后更新: {last_updated}\n" if last_updated else ""
    return f"""# 中国 - {region} 身后事政策

## 元信息
- 国家: 中华人民共和国
- 地区: {region}
- ISO代码: CN-{region[:2].upper()}
{last_updated_line}- 数据来源:
  - 民政部 https://www.mca.gov.cn
- 数据可信度: 中

## 阶段1：死亡证明

### 签发机构
- 医院

{body_extra}

## 数据来源与免责
- 生成时间: 2026-07-21
"""


def _write_region(
    regions_dir: Path,
    rel_path: str,
    last_updated: str | None = None,
    body_extra: str = "",
) -> Path:
    """在 regions_dir 下写入一个测试 .md 文件

    Args:
        regions_dir: regions 根目录
        rel_path: 相对路径，如 "CN/beijing.md" 或 "US/california.md"
        last_updated: 最后更新日期；None 表示缺失
        body_extra: 额外正文

    Returns:
        写入文件的绝对路径
    """
    md_path = regions_dir / rel_path
    md_path.parent.mkdir(parents=True, exist_ok=True)
    # 从 rel_path 推断 region 名
    region_name = Path(rel_path).stem
    md_path.write_text(
        _make_md(region_name, last_updated, body_extra),
        encoding="utf-8",
    )
    return md_path


# =====================================================================
# 1. _parse_last_updated 单元测试
# =====================================================================


def test_parse_last_updated_standard_format():
    """测试标准格式 '最后更新: 2026-01-01'"""
    text = _make_md("beijing", "2026-01-01")
    parsed = KnowledgeFreshnessChecker._parse_last_updated(text)
    assert parsed == date(2026, 1, 1)


def test_parse_last_updated_no_space():
    """测试无空格格式 '最后更新:2026-01-01'"""
    text = """# 中国 - 测试

## 元信息
- 最后更新:2026-01-15
"""
    parsed = KnowledgeFreshnessChecker._parse_last_updated(text)
    assert parsed == date(2026, 1, 15)


def test_parse_last_updated_list_prefix():
    """测试列表项前缀 '- 最后更新: 2026-01-01'"""
    text = """# 中国 - 测试

## 元信息
- 国家: 中国
- 最后更新: 2026-03-08
- 数据来源: a
"""
    parsed = KnowledgeFreshnessChecker._parse_last_updated(text)
    assert parsed == date(2026, 3, 8)


def test_parse_last_updated_single_digit_month_day():
    """测试单位数月日 '最后更新: 2026-1-1'"""
    text = """## 元信息
- 最后更新: 2026-1-5
"""
    parsed = KnowledgeFreshnessChecker._parse_last_updated(text)
    assert parsed == date(2026, 1, 5)


def test_parse_last_updated_missing():
    """测试缺失'最后更新'字段"""
    text = """# 中国 - 测试

## 元信息
- 国家: 中国
- 数据来源: a
"""
    parsed = KnowledgeFreshnessChecker._parse_last_updated(text)
    assert parsed is None


def test_parse_last_updated_invalid_date():
    """测试非法日期（如 2026-13-40）返回 None"""
    text = """## 元信息
- 最后更新: 2026-13-40
"""
    parsed = KnowledgeFreshnessChecker._parse_last_updated(text)
    assert parsed is None


# =====================================================================
# 2. scan_regions 状态判定测试
# =====================================================================


def test_scan_regions_fresh_status(tmp_path):
    """测试 fresh 状态：30 天内 + 高频政策领域"""
    regions = tmp_path / "regions"
    regions.mkdir()
    today = date(2026, 7, 21)
    fresh_date = (today - timedelta(days=30)).isoformat()
    _write_region(regions, "CN/beijing.md", fresh_date, body_extra="社保 医疗")

    checker = KnowledgeFreshnessChecker(reference_date=today)
    reports = checker.scan_regions(regions)

    assert len(reports) == 1
    r = reports[0]
    assert r.status == "fresh"
    assert r.days_old == 30
    assert r.last_updated == date(2026, 6, 21)
    assert "社保" in r.policy_areas
    assert "医疗" in r.policy_areas


def test_scan_regions_warning_status(tmp_path):
    """测试 warning 状态：100 天 + 高频政策领域（90-180 天之间）"""
    regions = tmp_path / "regions"
    regions.mkdir()
    today = date(2026, 7, 21)
    warning_date = (today - timedelta(days=100)).isoformat()
    _write_region(regions, "CN/shanghai.md", warning_date, body_extra="社保 银行")

    checker = KnowledgeFreshnessChecker(reference_date=today)
    reports = checker.scan_regions(regions)

    assert len(reports) == 1
    r = reports[0]
    assert r.status == "warning"
    assert r.days_old == 100
    assert "社保" in r.policy_areas
    assert "银行" in r.policy_areas


def test_scan_regions_stale_status(tmp_path):
    """测试 stale 状态：200 天（> 180 天）"""
    regions = tmp_path / "regions"
    regions.mkdir()
    today = date(2026, 7, 21)
    stale_date = (today - timedelta(days=200)).isoformat()
    _write_region(regions, "CN/guangdong.md", stale_date, body_extra="医疗")

    checker = KnowledgeFreshnessChecker(reference_date=today)
    reports = checker.scan_regions(regions)

    assert len(reports) == 1
    r = reports[0]
    assert r.status == "stale"
    assert r.days_old == 200


def test_scan_regions_unknown_when_missing_date(tmp_path):
    """测试缺失日期时 status='unknown'"""
    regions = tmp_path / "regions"
    regions.mkdir()
    _write_region(regions, "CN/zhejiang.md", last_updated=None)

    checker = KnowledgeFreshnessChecker(reference_date=date(2026, 7, 21))
    reports = checker.scan_regions(regions)

    assert len(reports) == 1
    r = reports[0]
    assert r.status == "unknown"
    assert r.last_updated is None
    assert r.days_old is None


def test_scan_regions_skips_schema_and_archived(tmp_path):
    """测试跳过 SCHEMA.md 与 _archived/_quarantine 下的文件"""
    regions = tmp_path / "regions"
    regions.mkdir()
    today = date(2026, 7, 21)

    # SCHEMA.md 应被跳过
    (regions / "SCHEMA.md").write_text(
        "# SCHEMA\n## 元信息\n- 最后更新: 2020-01-01\n", encoding="utf-8"
    )
    # 正常文件
    _write_region(regions, "CN/beijing.md", today.isoformat())
    # _archived 下的文件应被跳过
    _write_region(regions, "_archived/CN/old.md", "2020-01-01")
    # _quarantine 下的文件应被跳过
    _write_region(regions, "_quarantine/CN/suspicious.md", "2020-01-01")

    checker = KnowledgeFreshnessChecker(reference_date=today)
    reports = checker.scan_regions(regions)

    assert len(reports) == 1
    assert reports[0].region == "CN/beijing"


def test_scan_regions_detects_policy_areas(tmp_path):
    """测试政策领域检测：税务/社保/银行/医疗等关键词命中"""
    regions = tmp_path / "regions"
    regions.mkdir()
    today = date(2026, 7, 21)
    body = """
## 阶段8：社保与福利结算
- 银行账户查询
- 公积金提取
- 医保结算
- 医疗保险
- 遗产税
- 契税
- 不动产过户
- 车辆继承
- 保险理赔
"""
    _write_region(regions, "CN/jiangsu.md", today.isoformat(), body_extra=body)

    checker = KnowledgeFreshnessChecker(reference_date=today)
    reports = checker.scan_regions(regions)

    assert len(reports) == 1
    r = reports[0]
    # 应命中所有出现的关键词
    for area in [
        "社保",
        "银行",
        "医疗",
        "医保",
        "公积金",
        "不动产",
        "车辆",
        "保险",
    ]:
        assert area in r.policy_areas


# =====================================================================
# 3. propose_refresh_tasks 测试
# =====================================================================


def test_propose_refresh_tasks_count_matches_stale(tmp_path):
    """测试 propose_refresh_tasks 生成数量 = stale 文件数量"""
    regions = tmp_path / "regions"
    regions.mkdir()
    today = date(2026, 7, 21)

    # 3 个 stale 文件 + 1 个 fresh + 1 个 warning
    stale_date = (today - timedelta(days=200)).isoformat()
    fresh_date = today.isoformat()
    warning_date = (today - timedelta(days=100)).isoformat()

    _write_region(regions, "CN/beijing.md", stale_date, body_extra="医疗")
    _write_region(regions, "CN/shanghai.md", stale_date, body_extra="医疗")
    _write_region(regions, "CN/guangdong.md", stale_date, body_extra="医疗")
    _write_region(regions, "CN/zhejiang.md", fresh_date, body_extra="医疗")
    _write_region(regions, "CN/jiangsu.md", warning_date, body_extra="医疗")

    checker = KnowledgeFreshnessChecker(reference_date=today)
    reports = checker.scan_regions(regions)
    proposals = checker.propose_refresh_tasks(reports)

    # 仅 3 个 stale 文件应生成建议
    assert len(proposals) == 3
    for p in proposals:
        assert p["proposed"] is False  # 未注入 scheduler
        assert p["job_id"] is None
        assert "未注入 scheduler" in p["message"]
        assert p["days_old"] >= 180


def test_propose_refresh_tasks_no_scheduler(tmp_path):
    """测试未注入 scheduler 时仅返回建议列表"""
    regions = tmp_path / "regions"
    regions.mkdir()
    today = date(2026, 7, 21)
    stale_date = (today - timedelta(days=200)).isoformat()
    _write_region(regions, "CN/beijing.md", stale_date, body_extra="医疗")

    checker = KnowledgeFreshnessChecker(reference_date=today)
    reports = checker.scan_regions(regions)
    proposals = checker.propose_refresh_tasks(reports)

    assert len(proposals) == 1
    p = proposals[0]
    assert p["proposed"] is False
    assert p["job_id"] is None
    assert p["region"] == "CN/beijing"


def test_propose_refresh_tasks_with_scheduler(tmp_path):
    """测试注入 scheduler 时调用 propose_job 并填入 job_id"""
    regions = tmp_path / "regions"
    regions.mkdir()
    today = date(2026, 7, 21)
    stale_date = (today - timedelta(days=200)).isoformat()
    _write_region(regions, "CN/beijing.md", stale_date, body_extra="医疗")

    # mock scheduler：propose_job 是 async 方法
    mock_scheduler = MagicMock()
    mock_scheduler.propose_job = AsyncMock(
        return_value={
            "job_id": "abc123",
            "needs_confirmation": True,
            "message": "已记录提议",
        }
    )

    checker = KnowledgeFreshnessChecker(reference_date=today, scheduler=mock_scheduler)
    reports = checker.scan_regions(regions)
    proposals = checker.propose_refresh_tasks(reports)

    assert len(proposals) == 1
    p = proposals[0]
    assert p["proposed"] is True
    assert p["job_id"] == "abc123"
    mock_scheduler.propose_job.assert_called_once()
    # 验证 propose_job 接收的参数
    call_args = mock_scheduler.propose_job.call_args
    assert call_args.kwargs["schedule"] == "0 9 1 * *"
    assert "CN/beijing" in call_args.kwargs["content"]


# =====================================================================
# 4. check_official_sources 测试
# =====================================================================


def test_check_official_sources_only_stale(tmp_path):
    """测试 check_official_sources 仅对 stale 文件生效"""
    md_path = tmp_path / "fresh.md"
    md_path.write_text(
        _make_md("test", "2026-07-21", body_extra="社保 100 元"),
        encoding="utf-8",
    )
    report = FreshnessReport(
        file_path=md_path,
        region="CN/test",
        last_updated=date(2026, 7, 21),
        days_old=0,
        status="fresh",
        policy_areas=["社保"],
    )

    checker = KnowledgeFreshnessChecker()
    drifts = checker.check_official_sources(report)
    assert drifts == []  # 非 stale 不处理


def test_check_official_sources_extracts_data_points(tmp_path):
    """测试 check_official_sources 能提取含金额/时限/电话/法条号的行"""
    md_path = tmp_path / "stale.md"
    md_path.write_text(
        _make_md(
            "test",
            "2025-01-01",
            body_extra=(
                "## 阶段8：社保\n"
                "- 丧葬费约 5000 元\n"
                "- 户籍注销时限 30 天内\n"
                "- 政务咨询 12345\n"
                "- 依据《民法典》第 1127 条\n"
                "- 这是一行普通文字，不应被提取\n"
            ),
        ),
        encoding="utf-8",
    )
    report = FreshnessReport(
        file_path=md_path,
        region="CN/test",
        last_updated=date(2025, 1, 1),
        days_old=200,
        status="stale",
        policy_areas=["社保"],
    )

    checker = KnowledgeFreshnessChecker()
    drifts = checker.check_official_sources(report)

    # 应至少提取 4 条（含金额/时限/电话/法条号的社保相关行）
    assert len(drifts) >= 4
    for d in drifts:
        assert d.area == "社保"
        assert d.current_text  # 非空
        assert d.suggested_text == ""  # 本期留空
        assert d.source_url == ""
        assert d.confidence == "unknown"
        assert d.file_path == md_path


# =====================================================================
# 5. 综合测试
# =====================================================================


def test_run_full_check_end_to_end(tmp_path):
    """测试 run_full_check 端到端：scan + check + propose"""
    regions = tmp_path / "regions"
    regions.mkdir()
    today = date(2026, 7, 21)

    # 1 个 stale + 1 个 fresh
    stale_date = (today - timedelta(days=200)).isoformat()
    fresh_date = today.isoformat()
    _write_region(
        regions,
        "CN/beijing.md",
        stale_date,
        body_extra="社保 丧葬费约 5000 元 时限 30 天内",
    )
    _write_region(regions, "CN/shanghai.md", fresh_date, body_extra="医疗")

    checker = KnowledgeFreshnessChecker(reference_date=today)
    reports, drifts, proposals = checker.run_full_check(regions)

    # 2 个报告
    assert len(reports) == 2
    statuses = {r.region: r.status for r in reports}
    assert statuses["CN/beijing"] == "stale"
    assert statuses["CN/shanghai"] == "fresh"

    # drifts 来自 stale 文件
    assert len(drifts) >= 1
    for d in drifts:
        assert "beijing" in str(d.file_path)

    # proposals 仅 1 个（stale 文件）
    assert len(proposals) == 1
    assert proposals[0]["region"] == "CN/beijing"


def test_compute_status_boundary_conditions():
    """测试状态判定的边界条件"""
    checker = KnowledgeFreshnessChecker(reference_date=date(2026, 7, 21))

    # 0 天 + 高频领域 → fresh
    assert checker._compute_status(0, ["社保"]) == "fresh"
    # 89 天 + 高频领域 → fresh（< warning_days）
    assert checker._compute_status(89, ["社保"]) == "fresh"
    # 90 天 + 高频领域 → warning（== warning_days）
    assert checker._compute_status(90, ["社保"]) == "warning"
    # 90 天 + 无高频领域 → fresh（非高频不触发 warning）
    assert checker._compute_status(90, []) == "fresh"
    # 179 天 + 高频领域 → warning（< stale_days）
    assert checker._compute_status(179, ["社保"]) == "warning"
    # 180 天 → stale（无论是否高频）
    assert checker._compute_status(180, []) == "stale"
    assert checker._compute_status(180, ["社保"]) == "stale"
    # 365 天 → stale
    assert checker._compute_status(365, []) == "stale"


def test_constructor_validation():
    """测试构造器参数校验"""
    # stale_days 必须为正
    with pytest.raises(ValueError):
        KnowledgeFreshnessChecker(stale_days=0)
    with pytest.raises(ValueError):
        KnowledgeFreshnessChecker(stale_days=-1)
    # warning_days 必须为正
    with pytest.raises(ValueError):
        KnowledgeFreshnessChecker(warning_days=0)
    # warning_days 不能大于 stale_days
    with pytest.raises(ValueError):
        KnowledgeFreshnessChecker(stale_days=90, warning_days=180)


def test_scan_regions_nonexistent_dir(tmp_path):
    """测试扫描不存在的目录返回空列表"""
    regions = tmp_path / "nonexistent"
    checker = KnowledgeFreshnessChecker()
    reports = checker.scan_regions(regions)
    assert reports == []


def test_scan_regions_not_a_directory(tmp_path):
    """测试传入文件而非目录返回空列表"""
    file_path = tmp_path / "not_a_dir.md"
    file_path.write_text("# test", encoding="utf-8")
    checker = KnowledgeFreshnessChecker()
    reports = checker.scan_regions(file_path)
    assert reports == []


def test_to_dict_serialization():
    """测试 FreshnessReport 与 DriftItem 的 to_dict 序列化"""
    report = FreshnessReport(
        file_path=Path("/tmp/test.md"),
        region="CN/test",
        last_updated=date(2026, 7, 21),
        days_old=0,
        status="fresh",
        policy_areas=["社保", "医疗"],
    )
    d = report.to_dict()
    assert d["region"] == "CN/test"
    assert d["last_updated"] == "2026-07-21"
    assert d["status"] == "fresh"
    assert d["file_path"] == "/tmp/test.md"
    assert d["policy_areas"] == ["社保", "医疗"]

    drift = DriftItem(
        file_path=Path("/tmp/test.md"),
        area="社保",
        current_text="丧葬费约 5000 元",
        suggested_text="丧葬费约 6000 元",
        source_url="https://example.gov.cn",
        confidence="medium",
    )
    dd = drift.to_dict()
    assert dd["area"] == "社保"
    assert dd["current_text"] == "丧葬费约 5000 元"
    assert dd["suggested_text"] == "丧葬费约 6000 元"
    assert dd["source_url"] == "https://example.gov.cn"
    assert dd["confidence"] == "medium"
