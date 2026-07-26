"""P8.5.6 High-level Translator orchestrator - 高层 i18n 编排。

集成:
    - LocaleDetector:locale 检测 + 持久化
    - MessageBundle:多语言消息查询
    - CurrencyConverter:货币转换
    - TimezoneManager:时区管理
    - LawAdapter:跨境法律适配

Translator 是单一入口,业务代码只需:
    translator = get_translator()
    msg = translator.t("greeting", user_id="u1", name="Alice")
    money = translator.format_money(100, Currency.USD, user_id="u1")
    dt_str = translator.format_datetime(some_dt, user_id="u1")

用户 locale 缓存:
    - 内存: {user_id: Locale}(线程安全)
    - 持久化: LocaleDetector.persist_preference()

feature flag:`DEADMAN_I18N_ENABLED=0` 关闭时:
    - t() 返回原始 key
    - format_money() 用 currency 默认符号 + rate 1
    - format_datetime() 用 UTC + ZH_CN 格式
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from ..infrastructure.feature_flags import is_enabled
from .currency import Currency, CurrencyConverter, ConvertedAmount, get_currency_converter
from .law_adapter import (
    Jurisdiction,
    LawAdapter,
    ValidationResult,
    get_law_adapter,
)
from .locale import Locale, LocaleDetector, get_locale_detector
from .messages import MessageBundle, get_message_bundle
from .timezone import TimezoneManager, get_timezone_manager

logger = logging.getLogger(__name__)


class Translator:
    """高层 i18n 编排器(单例)。

    用法:
        from deadman.i18n import get_translator, Locale, Currency
        t = get_translator()
        t.set_locale("u1", Locale.EN_US)
        msg = t.t("greeting", "u1", name="Alice")
        # "Hello, Alice! I'm your deadman assistant."

    线程安全: 所有公共方法都加锁。
    """

    def __init__(
        self,
        locale_detector: Optional[LocaleDetector] = None,
        message_bundle: Optional[MessageBundle] = None,
        currency_converter: Optional[CurrencyConverter] = None,
        timezone_manager: Optional[TimezoneManager] = None,
        law_adapter: Optional[LawAdapter] = None,
    ) -> None:
        self._detector = locale_detector or get_locale_detector()
        self._messages = message_bundle or get_message_bundle()
        self._currency = currency_converter or get_currency_converter()
        self._timezone = timezone_manager or get_timezone_manager()
        self._law = law_adapter or get_law_adapter()
        self._lock = threading.RLock()
        # 用户 locale 内存缓存
        self._user_locales: dict[str, Locale] = {}

    # ==================================================================
    # Locale 管理
    # ==================================================================

    def set_locale(self, user_id: str, locale: Locale | str) -> None:
        """显式设置用户 locale(同步持久化)。"""
        loc = locale if isinstance(locale, Locale) else Locale.from_string(str(locale))
        with self._lock:
            self._user_locales[user_id] = loc
        # 持久化(feature flag 关闭时静默失败)
        try:
            self._detector.persist_preference(user_id, loc, source="manual")
        except Exception as e:
            logger.warning("Failed to persist locale for user %s: %s", user_id, e)

    def get_locale(self, user_id: str) -> Locale:
        """获取用户 locale(优先内存缓存,其次持久化偏好,最后默认 ZH_CN)。"""
        if not is_enabled("i18n"):
            return Locale.ZH_CN
        with self._lock:
            if user_id in self._user_locales:
                return self._user_locales[user_id]
        pref = self._detector.detect_from_user_profile(user_id)
        if pref is not None:
            with self._lock:
                self._user_locales[user_id] = pref
            return pref
        return Locale.ZH_CN

    def detect_locale(
        self,
        user_id: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        accept_language: Optional[str] = None,
        ip: Optional[str] = None,
        persist: bool = False,
    ) -> Locale:
        """综合检测用户 locale。

        Args:
            persist: True 时把检测结果持久化(若 user_id 给定)
        """
        if not is_enabled("i18n"):
            return Locale.ZH_CN
        loc = self._detector.detect(
            user_id=user_id,
            headers=headers,
            accept_language=accept_language,
            ip=ip,
        )
        if persist and user_id:
            self.set_locale(user_id, loc)
        return loc

    # ==================================================================
    # 翻译
    # ==================================================================

    def t(self, key: str, user_id: Optional[str] = None, **vars) -> str:
        """翻译消息。

        Args:
            key: 消息 key
            user_id: 用户 ID(用于决定 locale),可选
            **vars: 占位符变量

        Returns:
            翻译后的字符串
        """
        if not is_enabled("i18n"):
            return key  # 关闭:返回原始 key
        loc = self.get_locale(user_id) if user_id else Locale.ZH_CN
        return self._messages.get(key, loc, **vars)

    # ==================================================================
    # 货币
    # ==================================================================

    def format_money(
        self,
        amount: float,
        currency: Currency | str,
        user_id: Optional[str] = None,
        convert_from: Optional[Currency | str] = None,
    ) -> str:
        """格式化货币(可选先转换)。

        Args:
            amount: 金额
            currency: 显示货币(目标)
            user_id: 用户 ID
            convert_from: 若给定,先从该货币转换到 currency 再格式化

        Returns:
            locale 感知货币字符串
        """
        if not is_enabled("i18n"):
            # 关闭:直接显示原始金额 + 默认符号
            c = currency if isinstance(currency, Currency) else Currency.from_string(str(currency))
            return f"{c.symbol}{amount:,.2f}"

        loc = self.get_locale(user_id) if user_id else Locale.ZH_CN
        # 转换
        if convert_from is not None:
            result = self._currency.convert(amount, convert_from, currency)
            amount = result.amount
        return self._currency.format(amount, currency, loc)

    def convert_money(
        self,
        amount: float,
        from_currency: Currency | str,
        to_currency: Currency | str,
    ) -> ConvertedAmount:
        """货币转换(纯计算,不格式化)。"""
        return self._currency.convert(amount, from_currency, to_currency)

    # ==================================================================
    # 时间
    # ==================================================================

    def format_datetime(
        self,
        dt: datetime,
        user_id: Optional[str] = None,
        tz: Optional[str] = None,
    ) -> str:
        """格式化日期时间(按用户 locale / 时区)。

        Args:
            dt: datetime
            user_id: 用户 ID(决定 locale)
            tz: 显式指定时区(优先),否则按 locale 推断
        """
        if not is_enabled("i18n"):
            return self._timezone.format(dt, "UTC", Locale.ZH_CN)

        loc = self.get_locale(user_id) if user_id else Locale.ZH_CN
        if tz is None:
            tz = self._timezone.detect_timezone_from_locale(loc)
        return self._timezone.format(dt, tz, loc)

    def now_for_user(self, user_id: Optional[str] = None, tz: Optional[str] = None) -> datetime:
        """获取用户视角的当前时间。"""
        if not is_enabled("i18n"):
            return self._timezone.now_in("UTC")
        if tz is None:
            loc = self.get_locale(user_id) if user_id else Locale.ZH_CN
            tz = self._timezone.detect_timezone_from_locale(loc)
        return self._timezone.now_in(tz)

    # ==================================================================
    # 管辖区
    # ==================================================================

    def get_jurisdiction(self, user_id: str) -> Jurisdiction:
        """获取用户所在司法管辖区(基于 locale 推断)。"""
        if not is_enabled("i18n"):
            return Jurisdiction.CN_MAINLAND
        loc = self.get_locale(user_id)
        return Jurisdiction.from_locale(loc.value)

    def validate_action(
        self,
        action: str,
        user_id: str,
        data_kind: str = "default",
        target_jurisdiction: Optional[Jurisdiction | str] = None,
    ) -> ValidationResult:
        """校验用户动作是否合规。

        Args:
            action: 动作类型(user_profile_export / data_delete / cross_border_transfer /
                              will_execution / digital_asset_handover / ...)
            user_id: 用户 ID
            data_kind: 数据类型(跨境时使用)
            target_jurisdiction: 目标管辖区(跨境场景)

        Returns:
            ValidationResult
        """
        if not is_enabled("i18n"):
            return ValidationResult(
                allowed=True,
                jurisdiction_from="cn_mainland",
                jurisdiction_to=target_jurisdiction.value if isinstance(target_jurisdiction, Jurisdiction)
                    else (str(target_jurisdiction) if target_jurisdiction else "cn_mainland"),
                data_kind=data_kind,
                legal_basis="i18n_disabled",
                warnings=["i18n disabled, validation skipped"],
            )

        user_j = self.get_jurisdiction(user_id)
        # 跨境动作
        if action == "cross_border_transfer" and target_jurisdiction is not None:
            tj = target_jurisdiction if isinstance(target_jurisdiction, Jurisdiction) else \
                Jurisdiction(str(target_jurisdiction))
            return self._law.validate_cross_border(user_j, tj, data_kind)
        # 同管辖区动作:只检查 consent 列表
        consents = self._law.get_required_consents(user_j, action)
        # 简化:返回 ValidationResult(由上层 ConsentManager 检查具体 consent 状态)
        return ValidationResult(
            allowed=True,  # 此处不阻塞,实际由 ConsentManager.check 决定
            jurisdiction_from=user_j.value,
            jurisdiction_to=user_j.value,
            data_kind=data_kind,
            consents_required=consents,
            legal_basis="local_action",
            warnings=[] if not consents else [
                f"Action '{action}' in {user_j.value} requires consents: {consents}"
            ],
        )

    # ==================================================================
    # 法律查询
    # ==================================================================

    def get_inheritance_law(self, user_id: str) -> dict:
        """获取用户管辖区的继承法。"""
        if not is_enabled("i18n"):
            return {}
        j = self.get_jurisdiction(user_id)
        return self._law.get_inheritance_law(j)

    def get_data_protection_law(self, user_id: str) -> dict:
        """获取用户管辖区的数据保护法。"""
        if not is_enabled("i18n"):
            return {}
        j = self.get_jurisdiction(user_id)
        return self._law.get_data_protection_law(j)

    # ==================================================================
    # 工具:依赖注入(测试用)
    # ==================================================================

    def reset_user_locales(self) -> None:
        """清空内存 locale 缓存(测试用)。"""
        with self._lock:
            self._user_locales.clear()


# 全局单例
_translator_instance: Optional[Translator] = None
_translator_lock = threading.Lock()


def get_translator() -> Translator:
    """获取全局 Translator 单例。"""
    global _translator_instance
    if _translator_instance is None:
        with _translator_lock:
            if _translator_instance is None:
                _translator_instance = Translator()
    return _translator_instance


def reset_translator() -> None:
    """重置全局单例(测试用)。"""
    global _translator_instance
    with _translator_lock:
        _translator_instance = None
