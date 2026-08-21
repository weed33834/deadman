"""数据分析工具测试。"""

from __future__ import annotations

from deadman.research.data_analysis import analyze, describe


def test_describe_numeric_and_categorical():
    rows = [
        {"region": "北京", "cost": 100},
        {"region": "上海", "cost": 200},
        {"region": "北京", "cost": 300},
        {"region": "广州"},  # 缺 cost
    ]
    stats = describe(rows)
    assert stats["rows"] == 4
    cost = stats["columns"]["cost"]
    assert cost["type"] == "numeric"
    assert cost["mean"] == 200.0
    assert cost["median"] == 200.0
    assert cost["missing"] == 1
    region = stats["columns"]["region"]
    assert region["type"] == "categorical"
    assert region["unique"] == 3


def test_describe_empty():
    assert describe([])["rows"] == 0


def test_analyze_ok():
    out = analyze([{"a": 1}, {"a": 2}], question="统计")
    assert out["ok"] is True
    assert out["question"] == "统计"
