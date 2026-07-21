"""5 字段 cron 表达式解析器 - 轻量自实现，不引入 croniter 依赖

借鉴 Hermes cron/jobs.py 中 parse_schedule 的 cron 分支思路，但简化为：
仅支持标准 5 字段 cron 表达式（min hour dom mon dow），不支持 @daily 等别名
（避免歧义），不支持 L/W/# 高级语法（身后事场景无需）。

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
  - dow:     0-7   (0 和 7 都是周日；与 cron 标准一致)

时间锚点：所有 datetime 视作 naive（本地时区）。deadman 是轻量部署，
用户时区由调用方在传入 datetime 时决定；本模块不做时区转换。
"""

from __future__ import annotations

from datetime import datetime, timedelta


# 各字段允许的取值范围 (min, max)
_FIELD_RANGES = (
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 7),    # day of week (0 和 7 都是周日)
)

_FIELD_NAMES = ("minute", "hour", "dom", "month", "dow")


class CronExpr:
    """5 字段 cron 表达式解析器

    构造时即校验表达式合法性，非法抛 ValueError。
    解析结果以 set 形式保存各字段的可触发值，matches/next_fire 基于集合判断。

    Note: dow 字段中 0 和 7 都视为周日，构造时归一化为 0。
    """

    def __init__(self, expr: str):
        if not isinstance(expr, str):
            raise ValueError(f"cron 表达式必须是字符串: {expr!r}")
        text = expr.strip()
        if not text:
            raise ValueError("cron 表达式不能为空")

        parts = text.split()
        if len(parts) != 5:
            raise ValueError(
                f"cron 表达式必须为 5 个字段 (min hour dom mon dow): {expr!r}"
            )

        # 逐字段解析 + 校验范围
        parsed: list[set[int]] = []
        for i, (raw, (lo, hi)) in enumerate(zip(parts, _FIELD_RANGES)):
            try:
                values = _parse_field(raw, lo, hi)
            except ValueError as e:
                # 把字段名带进错误信息，便于排错
                raise ValueError(
                    f"cron 字段 {_FIELD_NAMES[i]} 非法 ({raw!r}): {e}"
                ) from e
            parsed.append(values)

        self._minutes, self._hours, self._doms, self._months, self._dows = parsed
        # dow: 7 → 0 归一化
        if 7 in self._dows:
            self._dows = (self._dows | {0}) - {7}

        self._expr = text

    @property
    def expr(self) -> str:
        """原始表达式字符串"""
        return self._expr

    # ============================================================
    # 公共 API
    # ============================================================

    def matches(self, dt: datetime) -> bool:
        """判断给定 datetime 是否匹配本 cron 表达式

        匹配规则：5 个字段全部命中即匹配。秒/微秒不参与判断。
        """
        cron_dow = self._python_weekday_to_cron_dow(dt.weekday())
        return (
            dt.minute in self._minutes
            and dt.hour in self._hours
            and dt.day in self._doms
            and dt.month in self._months
            and cron_dow in self._dows
        )

    def next_fire(self, after: datetime) -> datetime:
        """计算严格在 after 之后的下一次触发时间

        实现思路：从 (after 截断到分钟 + 1 分钟) 起逐分钟扫描，第一个匹配即返回。
        为防止病态表达式导致死循环，扫描上限 366 天。

        Returns:
            下一次触发的 datetime（naive，秒/微秒为 0）

        Raises:
            ValueError: 366 天内无匹配（极端表达式如 "0 0 30 2 *" 永不触发）
        """
        # 截断到分钟，从下一分钟开始
        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        deadline = after + timedelta(days=366)

        while candidate <= deadline:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)

        raise ValueError(
            f"cron 表达式 {self._expr!r} 在 366 天内无触发时间（可能永不触发）"
        )

    def min_interval_hours(self) -> float:
        """估算最小触发间隔（小时）

        实现：从一个固定基准点（2026-01-01 00:00）开始扫描，取前 13 次触发，
        计算相邻两次间隔的最小值。13 次足以覆盖月度 cron 一整年的所有月间间隔
        （含 28 天的 2 月），足以反映表达式最密集的触发节奏。

        Returns:
            最小间隔小时数。若表达式在扫描窗口内触发不足 2 次，返回 inf
            （表示"无足够样本"，调用方按 ≥ 24h 校验时会被允许通过，
             但此类表达式实际也无法触发，无安全风险）
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

        gaps = [
            (fires[i + 1] - fires[i]).total_seconds() / 3600.0
            for i in range(len(fires) - 1)
        ]
        return min(gaps)

    # ============================================================
    # 内部辅助
    # ============================================================

    @staticmethod
    def _python_weekday_to_cron_dow(py_weekday: int) -> int:
        """Python weekday (0=Mon..6=Sun) → cron dow (0=Sun..6=Sat)

        Python: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
        Cron:   Sun=0, Mon=1, Tue=2, Wed=3, Thu=4, Fri=5, Sat=6
        """
        return (py_weekday + 1) % 7

    def __repr__(self) -> str:
        return f"CronExpr({self._expr!r})"


# ============================================================
# 字段解析（模块级函数，便于单测）
# ============================================================


def _parse_field(field: str, min_val: int, max_val: int) -> set[int]:
    """解析单个 cron 字段为取值集合

    支持：*, 5, 1-5, 1,3,5, */5, 1-10/2, 5/2
    """
    result: set[int] = set()
    # 列表按逗号分割，每段独立解析后合并
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"空列表元素: {field!r}")
        result |= _parse_part(part, min_val, max_val)
    return result


def _parse_part(part: str, min_val: int, max_val: int) -> set[int]:
    """解析单个 cron 字段段（不含逗号）"""
    if "/" in part:
        range_str, step_str = part.split("/", 1)
        if not step_str:
            raise ValueError(f"步长为空: {part!r}")
        try:
            step = int(step_str)
        except ValueError as e:
            raise ValueError(f"步长非整数: {step_str!r}") from e
        if step <= 0:
            raise ValueError(f"步长必须为正整数: {step!r}")

        start, end = _resolve_range(range_str, min_val, max_val, part)
        return set(range(start, end + 1, step))

    if part == "*":
        return set(range(min_val, max_val + 1))

    if "-" in part:
        start, end = _resolve_range(part, min_val, max_val, part)
        return set(range(start, end + 1))

    # 纯数字
    try:
        v = int(part)
    except ValueError as e:
        raise ValueError(f"非整数: {part!r}") from e
    _check_bounds(v, min_val, max_val, part)
    return {v}


def _resolve_range(
    range_str: str, min_val: int, max_val: int, original: str
) -> tuple[int, int]:
    """解析 "lo-hi" 或 "*" 或单数字（用作步长起点）为 (start, end) 闭区间

    - "*" → (min_val, max_val)
    - "lo-hi" → (lo, hi)，要求 min_val <= lo <= hi <= max_val
    - "N"（单数字）→ (N, max_val)（cron 语义：N/step 等价于 N-max_val/step）
    """
    if range_str == "*":
        return min_val, max_val

    if "-" in range_str:
        lo_str, hi_str = range_str.split("-", 1)
        try:
            lo = int(lo_str)
            hi = int(hi_str)
        except ValueError as e:
            raise ValueError(f"范围非整数: {range_str!r}") from e
        _check_bounds(lo, min_val, max_val, original)
        _check_bounds(hi, min_val, max_val, original)
        if lo > hi:
            raise ValueError(f"范围下界大于上界: {range_str!r}")
        return lo, hi

    # 单数字（用于步长表达式如 5/2 → 5..max）
    try:
        v = int(range_str)
    except ValueError as e:
        raise ValueError(f"非整数: {range_str!r}") from e
    _check_bounds(v, min_val, max_val, original)
    return v, max_val


def _check_bounds(v: int, min_val: int, max_val: int, original: str) -> None:
    """校验单值是否在字段取值范围内"""
    if v < min_val or v > max_val:
        raise ValueError(
            f"值 {v} 超出允许范围 [{min_val}, {max_val}]: {original!r}"
        )
