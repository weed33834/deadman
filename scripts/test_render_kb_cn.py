"""渲染器 kb_cn 的单元测试。

覆盖：
  - split_inherit：金融/不动产/通用三路分流，无交叉重复，金融优先
  - audit：结构 + 数据纪律（未核验电话/URL/门户引用/标题可信度）
  - collect_provinces：无重复 key、数量符合预期
  - render：真实省份渲染产物通过 audit

运行::

    python3 -m pytest scripts/test_render_kb_cn.py -q
"""

from __future__ import annotations

import pytest
import render_kb_cn
from kb_cn_data import Province
from kb_cn_render_lib import split_inherit


def _make_province(**overrides) -> Province:
    """构造一个最小可渲染的 Province 实例。"""
    base = {
        "key": "test",
        "name": "测试省",
        "short": "测试",
        "iso": "CN-TT",
        "portal": "https://www.example.gov.cn",
        "kind": "省",
        "sub_unit": "若干市",
        "one_thing": ["联办说明"],
        "cremation": ["火化政策"],
        "ethnic": [],
        "eco": ["生态葬"],
        "plate": ["车辆"],
        "geo": ["异地"],
        "medical": ["医保"],
        "social": ["社保"],
        "inherit": [],
    }
    base.update(overrides)
    return Province(**base)


# ---------------------------------------------------------------------------
# split_inherit
# ---------------------------------------------------------------------------


def test_split_inherit_three_ways():
    p = _make_province(
        inherit=[
            "银行存款继承须凭公证办理",           # 金融
            "农村宅基地上房屋可以依法继承",       # 不动产
            "侨胞继承另有协助渠道",               # 通用
        ]
    )
    financial, property_, general = split_inherit(p)
    assert financial == ["银行存款继承须凭公证办理"]
    assert property_ == ["农村宅基地上房屋可以依法继承"]
    assert general == ["侨胞继承另有协助渠道"]


def test_split_inherit_no_cross_duplication():
    p = _make_province(
        inherit=[
            "股权继承按公司章程办理",             # 金融
            "林地承包经营权可继承",               # 不动产
            "公房承租权变更需审批",               # 通用
        ]
    )
    financial, property_, general = split_inherit(p)
    all_items = financial + property_ + general
    # 每条只出现一次，不跨阶段重复
    assert len(all_items) == len(p.inherit)
    assert sorted(all_items) == sorted(p.inherit)


def test_split_inherit_financial_priority_over_property():
    # 一条同时命中金融与不动产关键词时，金融优先
    p = _make_province(
        inherit=[
            "银行按揭房产的贷款结清与过户",       # 含「银行」与「房」
        ]
    )
    financial, property_, general = split_inherit(p)
    assert financial and not property_ and not general
    assert "银行按揭房产的贷款结清与过户" in financial


def test_split_inherit_empty():
    p = _make_province(inherit=[])
    financial, property_, general = split_inherit(p)
    assert financial == [] and property_ == [] and general == []


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def test_audit_passes_on_rendered_province():
    # 直接渲染真实数据模块中的省份以保证结构齐全
    real = next(x for x in render_kb_cn.collect_provinces() if x.key == "tianjin")
    text = render_kb_cn.render(real, "2026-08-08")
    assert render_kb_cn.audit(text, real) == []


def test_audit_detects_missing_section():
    p = _make_province()
    real = next(x for x in render_kb_cn.collect_provinces() if x.key == "hebei")
    text = render_kb_cn.render(real, "2026-08-08")
    text = text.replace("## 阶段9：债权债务", "")
    problems = render_kb_cn.audit(text, p)
    assert any("阶段9" in prob for prob in problems)


def test_audit_flags_unverified_phone():
    p = _make_province()
    real = next(x for x in render_kb_cn.collect_provinces() if x.key == "hebei")
    text = render_kb_cn.render(real, "2026-08-08")
    text += "\n本地咨询请拨 010-12345678\n"
    problems = render_kb_cn.audit(text, p)
    assert any("010-12345678" in prob for prob in problems)


def test_audit_flags_unverified_url():
    p = _make_province()
    real = next(x for x in render_kb_cn.collect_provinces() if x.key == "hebei")
    text = render_kb_cn.render(real, "2026-08-08")
    text += "\n详见 https://random-blog.example.com/guide\n"
    problems = render_kb_cn.audit(text, p)
    assert any("random-blog.example.com" in prob for prob in problems)


def test_audit_flags_missing_portal_reference():
    p = _make_province(portal="https://www.tj.gov.cn", name="天津市")
    real = next(x for x in render_kb_cn.collect_provinces() if x.key == "tianjin")
    text = render_kb_cn.render(real, "2026-08-08")
    import re

    text = re.sub(r"https?://[^\s（）()，,、]+", "", text)  # 抹掉全部 URL
    problems = render_kb_cn.audit(text, p)
    assert any("省级门户" in prob for prob in problems)


# ---------------------------------------------------------------------------
# collect_provinces
# ---------------------------------------------------------------------------


def test_collect_provinces_no_duplicate_keys():
    provinces = render_kb_cn.collect_provinces()
    keys = [p.key for p in provinces]
    assert len(keys) == len(set(keys)), f"存在重复 key: {keys}"


def test_collect_provinces_expected_count():
    # 26 个大陆省份 + 4 个直辖市 = 30？ 实际由数据模块决定；仅断言 >0 且为偶数组合
    provinces = render_kb_cn.collect_provinces()
    # 当前数据：26 省（含直辖市重庆/天津已并入省表）
    assert len(provinces) == 26, f"省份数量异常: {len(provinces)}"


def test_collect_provinces_all_render_clean():
    for p in render_kb_cn.collect_provinces():
        text = render_kb_cn.render(p, "2026-08-08")
        problems = render_kb_cn.audit(text, p)
        assert problems == [], f"{p.key} 渲染未通过 audit: {problems}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
