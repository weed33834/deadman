"""测试 deadman.cron.expr - 5 字段 cron 表达式解析器

覆盖点（7 个）：
  - test_basic_match: "0 9 * * *" 匹配 9:00
  - test_range_match: "0 9-17 * * 1-5" 工作日 9-17 点
  - test_step_match: "*/30 * * * *" 每 30 分钟
  - test_next_fire: 给定时间算下次触发
  - test_min_interval_hours_daily: "0 9 * * *" 间隔 24h
  - test_min_interval_hours_monthly: "0 0 1 * *" 间隔 ≥ 28 天
  - test_invalid_expr: "0 25 * * *"（小时超界）抛 ValueError

不依赖 pytest-asyncio：CronExpr 全同步 API。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from deadman.cron.expr import CronExpr


# =====================================================================
# 基础匹配
# =====================================================================


class TestCronExprBasic:
    """基础匹配测试"""

    def test_basic_match(self):
        """ "0 9 * * *" 匹配每天 9:00，不匹配其他时间"""
        expr = CronExpr("0 9 * * *")
        # 9:00 匹配
        assert expr.matches(datetime(2026, 7, 21, 9, 0)) is True
        # 9:01 不匹配（minute 非 0）
        assert expr.matches(datetime(2026, 7, 21, 9, 1)) is False
        # 8:00 不匹配（hour 非 9）
        assert expr.matches(datetime(2026, 7, 21, 8, 0)) is False
        # 10:00 不匹配
        assert expr.matches(datetime(2026, 7, 21, 10, 0)) is False

    def test_range_match(self):
        """ "0 9-17 * * 1-5" 匹配工作日 9-17 点的整点"""
        expr = CronExpr("0 9-17 * * 1-5")
        # 周一(2026-07-20) 9:00 匹配
        # 2026-07-20 是周一
        assert expr.matches(datetime(2026, 7, 20, 9, 0)) is True
        # 周一 17:00 匹配
        assert expr.matches(datetime(2026, 7, 20, 17, 0)) is True
        # 周一 8:00 不匹配（hour 不在 9-17）
        assert expr.matches(datetime(2026, 7, 20, 8, 0)) is False
        # 周一 18:00 不匹配
        assert expr.matches(datetime(2026, 7, 20, 18, 0)) is False
        # 周日(2026-07-19) 9:00 不匹配（dow 不在 1-5）
        assert expr.matches(datetime(2026, 7, 19, 9, 0)) is False
        # 周六(2026-07-25) 10:00 不匹配
        assert expr.matches(datetime(2026, 7, 25, 10, 0)) is False

    def test_step_match(self):
        """ "*/30 * * * *" 每 30 分钟（0 和 30 分匹配，15 不匹配）"""
        expr = CronExpr("*/30 * * * *")
        assert expr.matches(datetime(2026, 7, 21, 9, 0)) is True
        assert expr.matches(datetime(2026, 7, 21, 9, 30)) is True
        assert expr.matches(datetime(2026, 7, 21, 9, 15)) is False
        assert expr.matches(datetime(2026, 7, 21, 9, 45)) is False


# =====================================================================
# next_fire
# =====================================================================


class TestCronExprNextFire:
    """next_fire 测试"""

    def test_next_fire(self):
        """ "0 9 * * *" 从 2026-07-21 08:00 算下次触发 = 2026-07-21 09:00"""
        expr = CronExpr("0 9 * * *")
        after = datetime(2026, 7, 21, 8, 0)
        nxt = expr.next_fire(after)
        assert nxt == datetime(2026, 7, 21, 9, 0)

        # 从 09:00 算下次 = 2026-07-22 09:00（严格 "之后"）
        nxt2 = expr.next_fire(datetime(2026, 7, 21, 9, 0))
        assert nxt2 == datetime(2026, 7, 22, 9, 0)

        # 从 09:30 算下次 = 2026-07-22 09:00
        nxt3 = expr.next_fire(datetime(2026, 7, 21, 9, 30))
        assert nxt3 == datetime(2026, 7, 22, 9, 0)

    def test_next_fire_monthly(self):
        """ "0 0 1 * *" 从 2026-01-01 00:00 算下次 = 2026-02-01 00:00"""
        expr = CronExpr("0 0 1 * *")
        # 2026-01-01 00:00 已匹配，但 next_fire 是严格"之后"，所以下次是 2 月 1 日
        nxt = expr.next_fire(datetime(2026, 1, 1, 0, 0))
        assert nxt == datetime(2026, 2, 1, 0, 0)

        # 从 2026-01-15 算下次 = 2026-02-01 00:00
        nxt2 = expr.next_fire(datetime(2026, 1, 15, 12, 0))
        assert nxt2 == datetime(2026, 2, 1, 0, 0)


# =====================================================================
# min_interval_hours
# =====================================================================


class TestCronExprMinInterval:
    """min_interval_hours 测试"""

    def test_min_interval_hours_daily(self):
        """ "0 9 * * *" 最小间隔 = 24h（每天同一时刻）"""
        expr = CronExpr("0 9 * * *")
        interval = expr.min_interval_hours()
        assert interval == pytest.approx(24.0, abs=0.01)

    def test_min_interval_hours_monthly(self):
        """ "0 0 1 * *" 最小间隔 ≥ 28 天（2 月只有 28 天，是最短月间间隔）

        2026-02-01 → 2026-03-01 = 28 天 = 672h
        """
        expr = CronExpr("0 0 1 * *")
        interval = expr.min_interval_hours()
        # 至少 28 天（672h），通常正好是 672h（2 月非闰年）
        assert interval >= 28 * 24 - 1  # 容差 1h
        # 不应超过 31 天的最大月间间隔
        assert interval <= 31 * 24 + 1


# =====================================================================
# 非法表达式
# =====================================================================


class TestCronExprInvalid:
    """非法表达式应抛 ValueError"""

    def test_invalid_expr_hour_out_of_range(self):
        """ "0 25 * * *" 小时超界（25 > 23）抛 ValueError"""
        with pytest.raises(ValueError):
            CronExpr("0 25 * * *")

    def test_invalid_expr_minute_out_of_range(self):
        """ "60 * * * *" 分钟超界抛 ValueError"""
        with pytest.raises(ValueError):
            CronExpr("60 * * * *")

    def test_invalid_expr_wrong_field_count(self):
        """非 5 字段抛 ValueError"""
        with pytest.raises(ValueError, match="5"):
            CronExpr("0 9 * *")
        with pytest.raises(ValueError, match="5"):
            CronExpr("0 9 * * * *")

    def test_invalid_expr_empty(self):
        """空表达式抛 ValueError"""
        with pytest.raises(ValueError):
            CronExpr("")
        with pytest.raises(ValueError):
            CronExpr("   ")
