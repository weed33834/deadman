"""5 字段 cron 表达式解析器 - 基于 croniter 库。

支持格式：
  - 数字：5
  - 范围：1-5
  - 列表：1,3,5
  - 步长：*/5, 1-10/2, 5/2
  - 通配：*

字段范围：
  - minute:  0-59
  - hour:    0-23
  - dom:     1-31  (day of month)
  - month:   1-12
  - dow:     0-7   (0 和 7 都是周日)

时间锚点：所有 datetime 视作 naive（本地时区）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from croniter import croniter


class CronExpr:
    """5 字段 cron 表达式解析器（基于 croniter）。

    构造时即校验表达式合法性，非法抛 ValueError。
    """

    def __init__(self, expr: str):
        if not isinstance(expr, str):
            raise ValueError(f"cron 表达式必须是字符串: {expr!r}")
        text = expr.strip()
        if not text:
            raise ValueError("cron 表达式不能为空")

        parts = text.split()
        if len(parts) != 5:
            raise ValueError(f"cron 表达式必须为 5 个字段 (min hour dom mon dow): {expr!r}")

        # croniter 校验合法性
        if not croniter.is_valid(text):
            raise ValueError(f"cron 表达式非法: {expr!r}")

        self._expr = text
        self._cron = croniter(text)

    @property
    def expr(self) -> str:
        """原始表达式字符串。"""
        return self._expr

    def matches(self, dt: datetime) -> bool:
        """判断给定 datetime 是否匹配本 cron 表达式。

        通过检查 dt 前一刻的 next_fire 是否等于 dt（截断到分钟）来判断。
        """
        dt_truncated = dt.replace(second=0, microsecond=0)
        # 从 dt 前一分钟开始找下一个触发时间
        base = dt_truncated - timedelta(minutes=1)
        cron = croniter(self._expr, base)
        next_fire = cron.get_next(datetime)
        return next_fire.replace(second=0, microsecond=0) == dt_truncated

    def next_fire(self, after: datetime) -> datetime:
        """计算严格在 after 之后的下一次触发时间。

        Returns:
            下一次触发的 datetime（naive，秒/微秒为 0）

        Raises:
            ValueError: 无法计算下一次触发时间
        """
        try:
            cron = croniter(self._expr, after)
            result = cron.get_next(datetime)
            return result.replace(second=0, microsecond=0)
        except (ValueError, KeyError) as e:
            raise ValueError(f"cron 表达式 {self._expr!r} 无法计算下次触发时间: {e}") from e

    def min_interval_hours(self) -> float:
        """估算最小触发间隔（小时）。

        从基准点扫描前 13 次触发，取相邻间隔最小值。
        """
        base = datetime(2026, 1, 1, 0, 0, 0)
        fires: list[datetime] = []
        current = base
        for _ in range(13):
            try:
                nxt = self.next_fire(current)
            except ValueError:
                break
            fires.append(nxt)
            current = nxt

        if len(fires) < 2:
            return float("inf")

        gaps = [(fires[i + 1] - fires[i]).total_seconds() / 3600.0 for i in range(len(fires) - 1)]
        return min(gaps)

    def __repr__(self) -> str:
        return f"CronExpr({self._expr!r})"
