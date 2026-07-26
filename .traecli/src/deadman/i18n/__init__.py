"""P8.5 国际化与本地化 - i18n + 跨境法律框架。

模块结构:
    - locale.py: Locale enum + LocaleDetector(三路检测 + 持久化)
    - messages.py: MessageBundle(多语言消息 + str.format)
    - law_adapter.py: Jurisdiction + LawAdapter(跨境法律适配)
    - currency.py: Currency + CurrencyConverter(多货币转换)
    - timezone.py: TimezoneManager(多时区处理)
    - translator.py: Translator 高层编排(单例)

设计:
    - 所有 i18n 内容线程安全 + 原子写持久化(.tmp + os.replace)
    - feature flag DEADMAN_I18N_ENABLED=0 默认关闭
    - 关闭模式:t() 返回原始 key,convert 用 rate 1,timezone 默认 UTC
    - 内置 6 种 locale / 7 种货币 / 8 个司法管辖区规则

⚠️ 警告:LawAdapter 内置规则为框架性参考,非法律意见。
    真实跨境法律事务必须咨询持牌律师。

feature flag:`DEADMAN_I18N_ENABLED=0` 默认关闭。
"""

from __future__ import annotations

from .locale import (
    I18nDisabledError,
    Locale,
    LocaleDetector,
    LocalePreference,
    get_locale_detector,
)
from .messages import MessageBundle, get_message_bundle
from .law_adapter import (
    Jurisdiction,
    LawAdapter,
    ValidationResult,
    get_law_adapter,
)
from .currency import (
    ConvertedAmount,
    Currency,
    CurrencyConverter,
    get_currency_converter,
)
from .timezone import TimezoneManager, get_timezone_manager
from .translator import Translator, get_translator


__all__ = [
    # locale
    "Locale",
    "LocaleDetector",
    "LocalePreference",
    "get_locale_detector",
    # messages
    "MessageBundle",
    "get_message_bundle",
    # law_adapter
    "Jurisdiction",
    "LawAdapter",
    "ValidationResult",
    "get_law_adapter",
    # currency
    "Currency",
    "ConvertedAmount",
    "CurrencyConverter",
    "get_currency_converter",
    # timezone
    "TimezoneManager",
    "get_timezone_manager",
    # translator
    "Translator",
    "get_translator",
    # errors
    "I18nDisabledError",
]
