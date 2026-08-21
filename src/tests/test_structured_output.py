"""结构化输出工具测试。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from deadman.utils.structured_output import parse_json, validate


class _Item(BaseModel):
    name: str
    score: float = Field(ge=0, le=1)


def test_parse_json_plain():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_code_block():
    assert parse_json('以下是结果：\n```json\n{"name": "x", "score": 0.9}\n```\n完') == {
        "name": "x",
        "score": 0.9,
    }


def test_parse_json_with_prefix_suffix():
    assert parse_json('说明：{"name": "x", "score": 0.8} 结束') == {
        "name": "x",
        "score": 0.8,
    }


def test_parse_json_invalid():
    assert parse_json("不是 json") is None


def test_validate_ok_and_error():
    obj, errs = validate(_Item, {"name": "a", "score": 0.5})
    assert obj is not None and errs == []
    obj2, errs2 = validate(_Item, '{"name": "b", "score": 1.5}')
    assert obj2 is None and errs2  # score 越界 → 校验失败
