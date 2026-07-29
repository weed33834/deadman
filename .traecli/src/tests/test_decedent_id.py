"""测试 deadman.decedent_id.registry - 遗码通

覆盖：
    - 创建案例
    - 仅 owner 可访问
    - 列出我的案例
    - 添加事件
    - 归档
    - 获取时间线
    - 不存敏感 PII（身份证号/手机号/银行账号）

测试隔离：每个测试用 tmp_path 独立目录。
"""

from __future__ import annotations

from pathlib import Path


from deadman.decedent_id.registry import (
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    DecedentRecord,
    DecedentRegistry,
)


# =====================================================================
# 辅助：构造独立 registry
# =====================================================================
def _make_registry(tmp_path: Path) -> DecedentRegistry:
    return DecedentRegistry(data_dir=tmp_path / "cases")


# =====================================================================
# 1. 创建案例
# =====================================================================
def test_create_case(tmp_path: Path):
    reg = _make_registry(tmp_path)
    record = reg.create_case(
        owner_user_id="u-owner",
        decedent_alias="我父亲",
        relationship="父母",
    )
    assert isinstance(record, DecedentRecord)
    assert record.case_id.startswith("case-")
    assert record.owner_user_id == "u-owner"
    assert record.decedent_alias == "我父亲"
    assert record.relationship == "父母"
    assert record.status == STATUS_ACTIVE
    assert record.events == []
    # 持久化到磁盘
    cases = reg._read_cases("u-owner")
    assert record.case_id in cases


# =====================================================================
# 2. 仅 owner 可访问
# =====================================================================
def test_get_case_owner_only(tmp_path: Path):
    reg = _make_registry(tmp_path)
    record = reg.create_case("u-owner", "我父亲", "父母")
    # owner 能拿到
    fetched = reg.get_case(record.case_id, "u-owner")
    assert fetched is not None
    assert fetched.case_id == record.case_id
    # 其他人拿不到
    assert reg.get_case(record.case_id, "u-stranger") is None


# =====================================================================
# 3. 列出我的案例
# =====================================================================
def test_list_cases(tmp_path: Path):
    reg = _make_registry(tmp_path)
    reg.create_case("u-owner", "我父亲", "父母")
    reg.create_case("u-owner", "我母亲", "父母")
    reg.create_case("u-other", "他人案例", "其他")
    cases = reg.list_cases("u-owner")
    assert len(cases) == 2
    aliases = {c.decedent_alias for c in cases}
    assert aliases == {"我父亲", "我母亲"}


# =====================================================================
# 4. 添加事件
# =====================================================================
def test_add_event(tmp_path: Path):
    reg = _make_registry(tmp_path)
    record = reg.create_case("u-owner", "我父亲", "父母")
    updated = reg.add_event(
        case_id=record.case_id,
        owner_user_id="u-owner",
        event="完成死亡证明办理",
        agent="death-aftercare",
        notes="在朝阳区公安分局办理",
    )
    assert updated is not None
    assert len(updated.events) == 1
    e = updated.events[0]
    assert e["event"] == "完成死亡证明办理"
    assert e["agent"] == "death-aftercare"
    assert e["notes"] == "在朝阳区公安分局办理"
    assert "timestamp" in e
    # 不存在的 case_id 应返回 None
    assert reg.add_event("case-nonexistent", "u-owner", "x", "y") is None


# =====================================================================
# 5. 归档
# =====================================================================
def test_archive_case(tmp_path: Path):
    reg = _make_registry(tmp_path)
    record = reg.create_case("u-owner", "我父亲", "父母")
    assert reg.archive_case(record.case_id, "u-owner") is True
    fetched = reg.get_case(record.case_id, "u-owner")
    assert fetched is not None
    assert fetched.status == STATUS_ARCHIVED
    # 归档不存在的 case_id 应返回 False
    assert reg.archive_case("case-nonexistent", "u-owner") is False


# =====================================================================
# 6. 获取时间线
# =====================================================================
def test_get_timeline(tmp_path: Path):
    reg = _make_registry(tmp_path)
    record = reg.create_case("u-owner", "我父亲", "父母")
    reg.add_event(record.case_id, "u-owner", "事件A", "agent-1")
    reg.add_event(record.case_id, "u-owner", "事件B", "agent-2")
    reg.add_event(record.case_id, "u-owner", "事件C", "agent-3")
    timeline = reg.get_timeline(record.case_id, "u-owner")
    assert len(timeline) == 3
    # 按时间升序
    timestamps = [e["timestamp"] for e in timeline]
    assert timestamps == sorted(timestamps)
    events = [e["event"] for e in timeline]
    assert events == ["事件A", "事件B", "事件C"]


# =====================================================================
# 7. 不存敏感 PII（身份证号/手机号/银行账号）
# =====================================================================
def test_no_pii_stored(tmp_path: Path):
    reg = _make_registry(tmp_path)
    # 用户在 alias 中误填身份证号
    record = reg.create_case(
        owner_user_id="u-owner",
        decedent_alias="我父亲 身份证 110101199001011234",
        relationship="父母",
    )
    # 持久化后的 alias 不应含完整身份证号
    cases = reg._read_cases("u-owner")
    entry = cases[record.case_id]
    assert "110101199001011234" not in entry["decedent_alias"]
    assert "已脱敏:身份证号" in entry["decedent_alias"]

    # 同样测试 events/notes
    record2 = reg.create_case("u-owner", "测试", "其他")
    reg.add_event(
        case_id=record2.case_id,
        owner_user_id="u-owner",
        event="办手续，电话 13812345678",
        agent="death-aftercare",
        notes="银行账号 6222021234567890123",
    )
    cases = reg._read_cases("u-owner")
    entry2 = cases[record2.case_id]
    last_event = entry2["events"][-1]
    assert "13812345678" not in last_event["event"]
    assert "已脱敏:手机号" in last_event["event"]
    assert "6222021234567890123" not in last_event["notes"]
    assert "已脱敏:银行账号" in last_event["notes"]
