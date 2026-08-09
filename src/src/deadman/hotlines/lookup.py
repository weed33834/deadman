"""官方热线查询

数据源：.traecli/knowledge/hotlines/database.json
遵守 compliance-framework.md：不编造电话号码，所有热线必须标 source。

查询逻辑：
- 不指定 province 返回全国
- function: 殡葬服务/政策咨询/法律援助/心理援助/消费者投诉/社保咨询
- 返回 [{phone, note, source, confidence}]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 默认数据库路径（包内自带）
_DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "knowledge"
    / "hotlines"
    / "database.json"
)


class HotlineLookup:
    """官方热线查询"""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path: Path = db_path if db_path is not None else _DEFAULT_DB_PATH
        self._db: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"national": {}, "provincial": {}}
        try:
            return json.loads(self.db_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"national": {}, "provincial": {}}

    @staticmethod
    def _confidence(source: str | None) -> float:
        """根据 source 推断 confidence

        依据 retrieval-guardrails.md：
        - 含"民政"/"政务"/"司法"/"人社"/"市场监管"等官方部门 → 中可信 0.7
        - 其他公开来源 → 中可信 0.6
        - 无 source → 低可信 0.3
        """
        if not source:
            return 0.3
        official_keywords = ("民政", "政务", "国务院", "司法", "人社", "市场监管", "卫健委", "官方")
        if any(k in source for k in official_keywords):
            return 0.7
        return 0.6

    def lookup(
        self,
        province: str | None = None,
        function: str | None = None,
    ) -> list[dict]:
        """查询热线

        - 不指定 province 返回全国热线（national）
        - 指定 province 返回全国 + 该省级热线
        - function 过滤职能（可选）
        返回 [{phone, note, source, confidence, scope, function, province?}]
        """
        results: list[dict] = []
        national = self._db.get("national", {})

        # 全国热线
        for func, entry in national.items():
            if function is not None and func != function:
                continue
            source = entry.get("source", "")
            results.append(
                {
                    "phone": entry["phone"],
                    "note": entry.get("note", ""),
                    "source": source,
                    "confidence": self._confidence(source),
                    "scope": "national",
                    "function": func,
                }
            )

        # 省级热线（指定 province 时）
        if province is not None:
            provincial = self._db.get("provincial", {}).get(province, {})
            for func, entry in provincial.items():
                if function is not None and func != function:
                    continue
                source = entry.get("source", "")
                results.append(
                    {
                        "phone": entry["phone"],
                        "note": entry.get("note", ""),
                        "source": source,
                        "confidence": self._confidence(source),
                        "scope": "provincial",
                        "function": func,
                        "province": province,
                    }
                )

        return results

    def get_national(self, function: str) -> dict | None:
        """查询全国热线（按职能）"""
        entry = self._db.get("national", {}).get(function)
        if entry is None:
            return None
        source = entry.get("source", "")
        return {
            "phone": entry["phone"],
            "note": entry.get("note", ""),
            "source": source,
            "confidence": self._confidence(source),
            "scope": "national",
            "function": function,
        }

    def get_provincial(self, province: str, function: str | None = None) -> list[dict]:
        """查询某省热线（可选职能过滤）"""
        provincial = self._db.get("provincial", {}).get(province, {})
        results: list[dict] = []
        for func, entry in provincial.items():
            if function is not None and func != function:
                continue
            source = entry.get("source", "")
            results.append(
                {
                    "phone": entry["phone"],
                    "note": entry.get("note", ""),
                    "source": source,
                    "confidence": self._confidence(source),
                    "scope": "provincial",
                    "function": func,
                    "province": province,
                }
            )
        return results

    def list_functions(self) -> list[str]:
        """列出所有职能（全国级）"""
        return list(self._db.get("national", {}).keys())

    def list_provinces(self) -> list[str]:
        """列出已收录的省份"""
        return list(self._db.get("provincial", {}).keys())
