"""日期/时间解析统一实现。

原先 ``_parse_dt`` / ``_parse_iso`` 在 db.repositories / vault / deadman_switch /
cron / doc_extract / decedent_id / plan_score 等 7 处重复实现，且行为略有漂移
（个别转 UTC naive，其余保留 tzinfo）。这里收敛为单一实现：

- ``parse_dt(v, to_utc_naive=False)``：解析 ISO 字符串 → datetime；失败/空返回 None。
  ``to_utc_naive=True`` 时带时区输入转 UTC 后去 tzinfo（plan_score 场景）。
- ``to_iso(dt)``：datetime → ISO 字符串（无 tzinfo 时按本地/UTC 语义，随调用方）。

规则变更只改本文件，全项目生效，避免行为不一致。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["parse_dt", "to_iso"]


def parse_dt(v: Any, to_utc_naive: bool = False) -> datetime | None:
    """解析 ISO 格式日期/时间。

    Args:
        v: datetime / ISO 字符串 / None / 空串。
        to_utc_naive: 为 True 时，带时区输入转 UTC 后去掉 tzinfo（统一为 naive UTC）。
    """
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v))
        except (ValueError, TypeError):
            return None
    if to_utc_naive and dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def to_iso(dt: datetime) -> str:
    """datetime → ISO 字符串。"""
    return dt.isoformat()
