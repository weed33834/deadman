"""P8.5.4 Multi-currency conversion - 多货币转换。

设计:
    - Currency: 7 种货币(CNY / USD / EUR / JPY / KRW / GBP / HKD)
    - CurrencyConverter: 货币转换器
        - convert(amount, from, to) -> ConvertedAmount
        - get_rate(from, to) -> float
        - update_rates(dict) 手动更新(无实时 API)
        - format(amount, currency, locale) -> str locale 感知格式化
    - 默认汇率内置硬编码(近似值,清晰标记)
    - 原子持久化到 data/i18n/rates.json

⚠️ 警告:默认汇率为内置近似值,非实时报价。生产环境应接入权威外汇源
    (如 ECB / 中国外汇交易中心 / 央行中间价),并设置更新频率。

feature flag:`DEADMAN_I18N_ENABLED=0` 关闭时 convert 乘以 1,format 不做转换。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import resolve_data_path
from .locale import Locale

logger = logging.getLogger(__name__)


class Currency(str, Enum):
    """支持的货币列表。

    ISO 4217 货币代码。
    """

    CNY = "CNY"  # 人民币
    USD = "USD"  # 美元
    EUR = "EUR"  # 欧元
    JPY = "JPY"  # 日元
    KRW = "KRW"  # 韩元
    GBP = "GBP"  # 英镑
    HKD = "HKD"  # 港币

    @classmethod
    def from_string(cls, value: str) -> "Currency":
        """宽松解析货币代码,匹配失败回退到 USD。"""
        if not value:
            return cls.USD
        norm = value.strip().upper()
        try:
            return cls(norm)
        except ValueError:
            # 常见别名(¥ / $ / € 等)
            alias_map = {
                "￥": cls.CNY, "¥": cls.CNY, "RMB": cls.CNY,
                "$": cls.USD, "US$": cls.USD,
                "€": cls.EUR,
                "JP¥": cls.JPY, "円": cls.JPY,  # 円 日元
                "₩": cls.KRW, "원": cls.KRW,
                "£": cls.GBP,
                "HK$": cls.HKD,
            }
            if norm in alias_map:
                return alias_map[norm]
            return cls.USD

    @property
    def symbol(self) -> str:
        """货币符号(用于 format)。"""
        return {
            Currency.CNY: "¥",
            Currency.USD: "$",
            Currency.EUR: "€",
            Currency.JPY: "¥",
            Currency.KRW: "₩",
            Currency.GBP: "£",
            Currency.HKD: "HK$",
        }[self]

    @property
    def is_zero_decimal(self) -> bool:
        """是否为零小数位货币(JPY / KRW 通常无小数)。"""
        return self in (Currency.JPY, Currency.KRW)

    @property
    def default_locale(self) -> Locale:
        """该货币的默认 locale(用于本地化格式)。"""
        return {
            Currency.CNY: Locale.ZH_CN,
            Currency.USD: Locale.EN_US,
            Currency.EUR: Locale.EN_US,  # 欧盟使用多语,默认 en-US
            Currency.JPY: Locale.JA_JP,
            Currency.KRW: Locale.KO_KR,
            Currency.GBP: Locale.EN_GB,
            Currency.HKD: Locale.ZH_TW,
        }[self]


# =====================================================================
# 内置默认汇率(以 1 unit = N CNY 报价)
# ⚠️ 警告:此为 2024-2025 大致中间价,非实时数据。
#    生产部署必须接入权威外汇源并定期刷新。
# =====================================================================
# 单位: 1 单位该货币 = X 单位 CNY
_DEFAULT_RATES_TO_CNY: dict[Currency, float] = {
    Currency.CNY: 1.0,         # 基准
    Currency.USD: 7.20,       # 美元 → 人民币
    Currency.EUR: 7.85,       # 欧元 → 人民币
    Currency.JPY: 0.048,      # 日元 → 人民币(100 JPY ≈ 4.8 CNY)
    Currency.KRW: 0.0054,     # 韩元 → 人民币(100 KRW ≈ 0.54 CNY)
    Currency.GBP: 9.15,       # 英镑 → 人民币
    Currency.HKD: 0.92,       # 港币 → 人民币
}


@dataclass
class ConvertedAmount:
    """转换结果。

    Attributes:
        amount: 转换后金额
        from_currency: 原始货币
        to_currency: 目标货币
        rate: 使用的汇率(1 原始货币 = rate 目标货币)
        rate_timestamp: 汇率更新时间戳
        original_amount: 原始金额
    """

    amount: float
    from_currency: str
    to_currency: str
    rate: float
    rate_timestamp: float = 0.0
    original_amount: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.to_currency} (rate={self.rate:.6f})"


class CurrencyConverter:
    """多货币转换器。

    用法:
        cc = CurrencyConverter()
        result = cc.convert(100, Currency.USD, Currency.CNY)
        # result.amount = 720.0
        formatted = cc.format(720.0, Currency.CNY, Locale.ZH_CN)
        # "¥720.00"

    特点:
        - 无实时 API,默认硬编码汇率
        - update_rates(dict) 手动刷新
        - 原子持久化到 data/i18n/rates.json
        - feature flag 关闭时 convert 返回 amount * 1(format 仅符号)
    """

    def __init__(
        self,
        store_path: Optional[Path] = None,
        initial_rates: Optional[dict[Currency, float]] = None,
    ) -> None:
        if store_path is None:
            store_path = resolve_data_path("i18n/rates.json")
        self.store_path = Path(store_path) if not isinstance(store_path, Path) else store_path
        self._lock = threading.RLock()
        # rates[to_cny]: {Currency: rate_to_cny}
        self._rates: dict[Currency, float] = dict(initial_rates or _DEFAULT_RATES_TO_CNY)
        self._rate_timestamp: float = time.time()
        self._loaded = False

    # ==================================================================
    # 汇率查询
    # ==================================================================

    def get_rate(self, from_currency: Currency | str, to_currency: Currency | str) -> float:
        """获取 from → to 的汇率(1 unit from = N unit to)。

        feature flag 关闭时返回 1.0(无转换)。
        """
        if not is_enabled("i18n"):
            return 1.0

        fc = from_currency if isinstance(from_currency, Currency) else \
            Currency.from_string(str(from_currency))
        tc = to_currency if isinstance(to_currency, Currency) else \
            Currency.from_string(str(to_currency))

        if fc == tc:
            return 1.0

        with self._lock:
            self._load()
            from_to_cny = self._rates.get(fc)
            to_to_cny = self._rates.get(tc)
            if from_to_cny is None or to_to_cny is None or to_to_cny == 0:
                logger.warning("Missing rate for %s or %s", fc.value, tc.value)
                return 1.0
            return from_to_cny / to_to_cny

    # ==================================================================
    # 转换
    # ==================================================================

    def convert(
        self,
        amount: float,
        from_currency: Currency | str,
        to_currency: Currency | str,
    ) -> ConvertedAmount:
        """金额转换。

        Args:
            amount: 原始金额
            from_currency: 原始货币
            to_currency: 目标货币

        Returns:
            ConvertedAmount(amount, from, to, rate, ...)
        """
        fc = from_currency if isinstance(from_currency, Currency) else \
            Currency.from_string(str(from_currency))
        tc = to_currency if isinstance(to_currency, Currency) else \
            Currency.from_string(str(to_currency))

        if not is_enabled("i18n"):
            # 关闭:直接返回原值(汇率 1)
            return ConvertedAmount(
                amount=float(amount),
                from_currency=fc.value,
                to_currency=tc.value,
                rate=1.0,
                rate_timestamp=time.time(),
                original_amount=float(amount),
            )

        rate = self.get_rate(fc, tc)
        converted = float(amount) * rate
        with self._lock:
            ts = self._rate_timestamp
        return ConvertedAmount(
            amount=converted,
            from_currency=fc.value,
            to_currency=tc.value,
            rate=rate,
            rate_timestamp=ts,
            original_amount=float(amount),
        )

    # ==================================================================
    # 汇率更新
    # ==================================================================

    def update_rates(self, rates_dict: dict[str, float]) -> int:
        """手动更新汇率字典。

        Args:
            rates_dict: {currency_code: rate_to_cny}
                        例:{"USD": 7.25, "EUR": 7.90}

        Returns:
            成功更新的货币数
        """
        with self._lock:
            self._load()
            count = 0
            for code, rate in rates_dict.items():
                try:
                    currency = Currency.from_string(str(code))
                    if rate <= 0:
                        logger.warning("Invalid rate for %s: %s, skipping", code, rate)
                        continue
                    self._rates[currency] = float(rate)
                    count += 1
                except (ValueError, TypeError) as e:
                    logger.warning("Failed to update rate %s=%s: %s", code, rate, e)
            if count > 0:
                self._rate_timestamp = time.time()
                self._save()
            logger.info("Updated %d currency rates", count)
            return count

    def set_rate(self, currency: Currency | str, rate_to_cny: float) -> bool:
        """设置单个货币对 CNY 的汇率。"""
        c = currency if isinstance(currency, Currency) else Currency.from_string(str(currency))
        if rate_to_cny <= 0:
            return False
        with self._lock:
            self._load()
            self._rates[c] = float(rate_to_cny)
            self._rate_timestamp = time.time()
            self._save()
        return True

    def get_all_rates(self) -> dict[str, float]:
        """获取全部汇率(currency_code -> rate_to_cny)。"""
        with self._lock:
            self._load()
            return {c.value: r for c, r in self._rates.items()}

    # ==================================================================
    # 实时汇率获取（在线 API）
    # ==================================================================

    # 免费 API 端点（无需 API key）：
    # 1. exchangerate.host（ primary）- 返回 { "rates": { "CNY": 7.2, ... } }（基准 USD）
    # 2. open.er-api.com（fallback） - 返回 { "rates": { "CNY": 7.2, ... } }（基准 USD）
    _ONLINE_API_URLS: list[str] = [
        "https://api.exchangerate.host/latest?base=USD",
        "https://open.er-api.com/v6/latest/USD",
    ]

    async def fetch_rates_online(self) -> dict[str, float] | None:
        """从免费在线 API 获取实时汇率（基准 USD → 各货币）。

        尝试多个 API 端点，第一个成功即返回。
        失败返回 None（不抛异常，调用方回退到内置汇率）。

        Returns:
            {currency_code: rate_to_cny} 字典；失败返回 None
        """
        try:
            import httpx
        except ImportError:
            logger.warning("httpx 未安装，无法获取在线汇率")
            return None

        for api_url in self._ONLINE_API_URLS:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(api_url)
                    resp.raise_for_status()
                    data = resp.json()

                # 两个 API 都返回 { "rates": { "CNY": 7.2, "EUR": 0.93, ... } }
                # 基准都是 USD，即 1 USD = X target_currency
                usd_rates = data.get("rates", {})
                if not usd_rates:
                    continue

                # 转换为 rate_to_cny 格式（1 unit currency = N CNY）
                # usd_to_cny = usd_rates["CNY"]
                # rate_to_cny[currency] = usd_rates[currency]  (if currency == CNY: 1.0)
                # 但我们的存储格式是 1 unit X = N CNY
                # 1 USD = usd_to_cny CNY → 1 CNY = 1/usd_to_cny USD
                # 1 EUR = usd_rates["EUR"] USD → 1 EUR = usd_rates["EUR"] * usd_to_cny CNY
                usd_to_cny = float(usd_rates.get("CNY", 0))
                if usd_to_cny <= 0:
                    continue

                rates_to_cny: dict[str, float] = {"CNY": 1.0}
                for code, usd_rate in usd_rates.items():
                    try:
                        currency = Currency.from_string(str(code))
                        if currency == Currency.CNY:
                            continue
                        # 1 unit currency = usd_rate USD = usd_rate * usd_to_cny CNY
                        rates_to_cny[currency.value] = float(usd_rate) * usd_to_cny
                    except (ValueError, TypeError):
                        continue

                logger.info("在线汇率获取成功（source=%s），共 %d 种货币", api_url, len(rates_to_cny))
                return rates_to_cny

            except Exception as e:
                logger.debug("在线汇率 API %s 获取失败: %s", api_url, e)
                continue

        logger.warning("所有在线汇率 API 均不可用，回退到内置汇率")
        return None

    async def refresh_rates(self) -> int:
        """从在线 API 刷新汇率（失败时保留内置汇率）。

        Returns:
            成功更新的货币数；在线获取失败返回 0
        """
        online_rates = await self.fetch_rates_online()
        if online_rates is None or not online_rates:
            return 0
        return self.update_rates(online_rates)

    # ==================================================================
    # 本地化格式化
    # ==================================================================

    def format(
        self,
        amount: float,
        currency: Currency | str,
        locale: Locale | str,
        include_symbol: bool = True,
        decimals: Optional[int] = None,
    ) -> str:
        """locale 感知货币格式化。

        Args:
            amount: 金额
            currency: 货币
            locale: Locale
            include_symbol: 是否包含货币符号
            decimals: 小数位(默认按 currency.is_zero_decimal)

        Returns:
            格式化字符串(如 "¥720.00" / "$1,234.50" / "¥1,234")
        """
        c = currency if isinstance(currency, Currency) else Currency.from_string(str(currency))
        loc = locale if isinstance(locale, Locale) else Locale.from_string(str(locale))

        # 小数位
        if decimals is None:
            decimals = 0 if c.is_zero_decimal else 2

        # 千分位:大多数 locale 用 ,;zh-CN/ja-JP/ko-KR 也用 ,
        formatted = f"{amount:,.{decimals}f}"

        if not include_symbol:
            return formatted

        # 符号位置:大多数西式在前面;CNY / JPY 在前面(¥/$ /€/£/₩)
        # zh-CN 通常用 "¥720.00";en-US 用 "$1,234.50"
        if loc in (Locale.ZH_CN, Locale.ZH_TW, Locale.JA_JP, Locale.KO_KR):
            return f"{c.symbol}{formatted}"
        return f"{c.symbol}{formatted}"

    # ==================================================================
    # 内部:文件 IO(原子写)
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for code, rate in data.get("rates", {}).items():
                    try:
                        currency = Currency.from_string(str(code))
                        self._rates[currency] = float(rate)
                    except (ValueError, TypeError) as e:
                        logger.warning("Skip rate %s=%s: %s", code, rate, e)
                self._rate_timestamp = float(data.get("updated_at", time.time()))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("Rates load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": self._rate_timestamp,
                "base": "CNY",
                "rates": {c.value: r for c, r in self._rates.items()},
            }
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.store_path)
        except OSError as e:
            logger.error("Rates save failed: %s", e)
            raise


# 全局单例
_currency_converter_instance: Optional[CurrencyConverter] = None
_currency_converter_lock = threading.Lock()


def get_currency_converter() -> CurrencyConverter:
    """获取全局 CurrencyConverter 单例。"""
    global _currency_converter_instance
    if _currency_converter_instance is None:
        with _currency_converter_lock:
            if _currency_converter_instance is None:
                _currency_converter_instance = CurrencyConverter()
    return _currency_converter_instance


def reset_currency_converter() -> None:
    """重置全局单例(测试用)。"""
    global _currency_converter_instance
    with _currency_converter_lock:
        _currency_converter_instance = None
