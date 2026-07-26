"""P8.5.5 Multi-timezone management - 多时区处理。

设计:
    - TimezoneManager: 时区管理器
        - detect_timezone(ip) -> str IANA 时区
        - now_in(tz) -> datetime 当前时间
        - convert(dt, from_tz, to_tz) -> datetime 时区转换
        - format(dt, tz, locale) -> str locale 感知格式化
        - business_hours_check(tz, hour_start, hour_end) -> bool
    - 基于 zoneinfo.ZoneInfo(Python 3.9+)
    - 线程安全(只读操作)

IANA 时区数据库:跨平台时区规则,系统应保持 tzdata 更新。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from ..infrastructure.feature_flags import is_enabled
from .locale import Locale

logger = logging.getLogger(__name__)


# IP → IANA 时区 映射(简化版,生产应接 GeoIP)
_IP_TZ_MAP: dict[str, str] = {
    # 中国大陆 → 北京时间
    "cn": "Asia/Shanghai",
    "zh": "Asia/Shanghai",
    # 香港 / 台湾
    "hk": "Asia/Hong_Kong",
    "tw": "Asia/Taipei",
    # 日本 / 韩国
    "jp": "Asia/Tokyo",
    "ja": "Asia/Tokyo",
    "kr": "Asia/Seoul",
    "ko": "Asia/Seoul",
    # 美国(主要时区,真实应细分东西海岸)
    "us": "America/New_York",
    "us_west": "America/Los_Angeles",
    "us_east": "America/New_York",
    # 欧盟主要时区
    "eu": "Europe/Paris",
    "uk": "Europe/London",
    "gb": "Europe/London",
    # 其他
    "sg": "Asia/Singapore",
    "au": "Australia/Sydney",
}

# Locale → IANA 时区(用于无 IP / 无 profile 时兜底)
_LOCALE_TZ_MAP: dict[Locale, str] = {
    Locale.ZH_CN: "Asia/Shanghai",
    Locale.ZH_TW: "Asia/Taipei",
    Locale.JA_JP: "Asia/Tokyo",
    Locale.KO_KR: "Asia/Seoul",
    Locale.EN_US: "America/New_York",
    Locale.EN_GB: "Europe/London",
}

# Locale → 日期时间格式
_DATETIME_FORMATS: dict[Locale, str] = {
    Locale.ZH_CN: "%Y-%m-%d %H:%M:%S %Z",
    Locale.ZH_TW: "%Y-%m-%d %H:%M:%S %Z",
    Locale.JA_JP: "%Y年%m月%d日 %H時%M分%S秒 %Z",
    Locale.KO_KR: "%Y-%m-%d %H:%M:%S %Z",
    Locale.EN_US: "%Y-%m-%d %I:%M:%S %p %Z",
    Locale.EN_GB: "%d/%m/%Y %H:%M:%S %Z",
}


class TimezoneManager:
    """多时区管理器。

    用法:
        tz = TimezoneManager()
        shanghai_now = tz.now_in("Asia/Shanghai")
        tokyo_time = tz.convert(shanghai_now, "Asia/Shanghai", "Asia/Tokyo")
        formatted = tz.format(shanghai_now, "Asia/Shanghai", Locale.ZH_CN)

    特点:
        - 纯本地计算(无外部 API)
        - 基于 zoneinfo(Python 3.9+)
        - feature flag 关闭时所有时间默认 UTC
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ==================================================================
    # 时区检测
    # ==================================================================

    def detect_timezone(self, ip: str) -> str:
        """从 IP 地址检测 IANA 时区。

        Args:
            ip: IPv4 / IPv6 字符串

        Returns:
            IANA 时区字符串(如 "Asia/Shanghai"),失败返回 UTC。
        """
        if not is_enabled("i18n"):
            return "UTC"
        if not ip:
            return "UTC"
        # 私有网段 → Asia/Shanghai(测试默认)
        if ip.startswith(("10.", "172.", "192.168.", "127.")):
            return "Asia/Shanghai"
        # 简化映射,生产应接 GeoIP
        return "Asia/Shanghai"

    def detect_timezone_from_locale(self, locale: Locale | str) -> str:
        """从 locale 推断时区(当 IP 不可用时的兜底)。"""
        if not is_enabled("i18n"):
            return "UTC"
        loc = locale if isinstance(locale, Locale) else Locale.from_string(str(locale))
        return _LOCALE_TZ_MAP.get(loc, "UTC")

    # ==================================================================
    # 时间计算
    # ==================================================================

    def now_in(self, tz: str = "UTC") -> datetime:
        """获取指定时区的当前时间。

        Args:
            tz: IANA 时区字符串(如 "Asia/Shanghai" / "America/New_York")

        Returns:
            timezone-aware datetime
        """
        if not is_enabled("i18n"):
            return datetime.now(timezone.utc)
        zoneinfo_obj = self._get_zoneinfo(tz)
        if zoneinfo_obj is None:
            return datetime.now(timezone.utc)
        return datetime.now(zoneinfo_obj)

    def convert(
        self,
        dt: datetime,
        from_tz: str,
        to_tz: str,
    ) -> datetime:
        """时区转换。

        Args:
            dt: datetime(naive 视为 from_tz,aware 直接用)
            from_tz: 原时区(naive dt 时使用)
            to_tz: 目标时区

        Returns:
            转换后的 datetime(aware)
        """
        if not is_enabled("i18n"):
            # 关闭:统一返回 UTC
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        # naive datetime → 视为 from_tz
        if dt.tzinfo is None:
            from_zone = self._get_zoneinfo(from_tz)
            if from_zone is None:
                from_zone = timezone.utc
            dt = dt.replace(tzinfo=from_zone)

        to_zone = self._get_zoneinfo(to_tz)
        if to_zone is None:
            return dt.astimezone(timezone.utc)
        return dt.astimezone(to_zone)

    # ==================================================================
    # 格式化
    # ==================================================================

    def format(
        self,
        dt: datetime,
        tz: str = "UTC",
        locale: Locale | str = Locale.ZH_CN,
    ) -> str:
        """locale 感知时间格式化。

        Args:
            dt: datetime
            tz: 目标时区(若 dt naive 则视为该时区)
            locale: locale(决定格式风格)

        Returns:
            格式化字符串(如 "2024-01-15 10:30:00 CST")
        """
        if not is_enabled("i18n"):
            tz = "UTC"

        loc = locale if isinstance(locale, Locale) else Locale.from_string(str(locale))
        # 转到目标时区
        if dt.tzinfo is None:
            zone = self._get_zoneinfo(tz)
            if zone is None:
                zone = timezone.utc
            dt = dt.replace(tzinfo=zone)
        else:
            zone = self._get_zoneinfo(tz)
            if zone is not None:
                dt = dt.astimezone(zone)

        fmt = _DATETIME_FORMATS.get(loc, _DATETIME_FORMATS[Locale.ZH_CN])
        try:
            return dt.strftime(fmt)
        except (ValueError, TypeError) as e:
            logger.warning("Time format failed: %s", e)
            return dt.isoformat()

    # ==================================================================
    # 业务时间判断
    # ==================================================================

    def business_hours_check(
        self,
        tz: str = "UTC",
        hour_start: int = 9,
        hour_end: int = 18,
    ) -> bool:
        """判断当前时刻是否在指定时区的业务时间内。

        Args:
            tz: IANA 时区
            hour_start: 业务开始小时(0-23,默认 9)
            hour_end: 业务结束小时(0-23,默认 18,exclusive)

        Returns:
            True 表示当前在业务时间内
        """
        if not is_enabled("i18n"):
            tz = "UTC"
        now = self.now_in(tz)
        return hour_start <= now.hour < hour_end

    # ==================================================================
    # 内部
    # ==================================================================

    def _get_zoneinfo(self, tz: str):
        """获取 ZoneInfo 对象(失败回退 UTC)。"""
        if ZoneInfo is None:
            return timezone.utc
        try:
            return ZoneInfo(tz)
        except (KeyError, ValueError, ImportError) as e:
            logger.warning("Unknown timezone '%s': %s, falling back to UTC", tz, e)
            return timezone.utc


# 全局单例
_timezone_manager_instance: Optional[TimezoneManager] = None
_timezone_manager_lock = threading.Lock()


def get_timezone_manager() -> TimezoneManager:
    """获取全局 TimezoneManager 单例。"""
    global _timezone_manager_instance
    if _timezone_manager_instance is None:
        with _timezone_manager_lock:
            if _timezone_manager_instance is None:
                _timezone_manager_instance = TimezoneManager()
    return _timezone_manager_instance


def reset_timezone_manager() -> None:
    """重置全局单例(测试用)。"""
    global _timezone_manager_instance
    with _timezone_manager_lock:
        _timezone_manager_instance = None
