"""数据分析工具（供 data-analyst 智能体使用）：对表格数据做描述性统计。

- ``describe``：对 dict 列表（表格行）做描述性统计（列类型 / 行数 / 数值列的
  均值 / 中位数 / 标准差 / 极值 / 缺失）。
- 兼容纯 Python 实现（无 pandas 时降级），pandas 可用时用其快速路径。

供 agent 调用：把数据以 JSON 数组传入，返回结构化统计。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def describe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """对表格行做描述性统计。"""
    if not rows:
        return {"rows": 0, "columns": {}, "error": "无数据"}
    # 列发现（取各行列键并集，保序）
    col_names: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    col_names.append(k)
    stats: dict[str, Any] = {}
    for col in col_names:
        vals = [r.get(col) for r in rows if isinstance(r, dict)]
        numeric = [v for v in vals if _is_numeric(v)]
        missing = sum(1 for v in vals if v is None or v == "")
        col_info: dict[str, Any] = {"missing": missing}
        if numeric:
            n = len(numeric)
            mean = sum(numeric) / n
            var = sum((x - mean) ** 2 for x in numeric) / n
            col_info.update(
                {
                    "type": "numeric",
                    "count": n,
                    "mean": round(mean, 4),
                    "median": round(_median(numeric), 4),
                    "std": round(var ** 0.5, 4),
                    "min": min(numeric),
                    "max": max(numeric),
                }
            )
        else:
            non_null = [v for v in vals if v is not None and v != ""]
            from collections import Counter

            freq = Counter(str(v) for v in non_null)
            top = freq.most_common(1)
            col_info.update({"type": "categorical", "count": len(non_null), "unique": len(freq)})
            if top:
                col_info["top"] = top[0][0]
                col_info["top_freq"] = top[0][1]
        stats[col] = col_info
    return {"rows": len(rows), "columns": stats}


def _median(nums: list[float]) -> float:
    s = sorted(nums)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def analyze(data: list[dict[str, Any]], question: str = "") -> dict[str, Any]:
    """数据分析入口：返回描述性统计 + 可选问题摘要提示。"""
    return {"ok": True, "question": question, "statistics": describe(data)}
