"""P8.5 国际化 + 跨境法律框架测试。

覆盖:
    - Locale 检测(request / user_profile / ip)+ 持久化
    - MessageBundle add / get / load / has / fallback
    - LawAdapter 每个管辖区 + 跨境校验
    - Currency convert + format + update rates + persistence
    - Timezone detect / now / convert / format / business hours
    - Translator end-to-end
    - Disabled state(raw key / UTC / rate 1)
    - Locale persistence
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture(autouse=True)
def enable_i18n(monkeypatch, tmp_path):
    """每个测试启用 i18n(默认关闭)+ 用 tmp_path 避免污染。"""
    monkeypatch.setenv("DEADMAN_I18N_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_MULTI_TENANT_ENABLED", "0")
    # 重置 feature flag 缓存
    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    # 重置全局单例
    import deadman.i18n as i18n_pkg
    i18n_pkg._translator_instance = None
    i18n_pkg.locale._locale_detector_instance = None
    i18n_pkg.messages._message_bundle_instance = None
    i18n_pkg.currency._currency_converter_instance = None
    i18n_pkg.timezone._timezone_manager_instance = None
    i18n_pkg.law_adapter._law_adapter_instance = None
    # 让所有测试默认指向 tmp_path
    monkeypatch.setenv("DEADMAN_TENANTS_ROOT", str(tmp_path / "tenants"))
    yield
    # 测试后关闭避免污染
    monkeypatch.setenv("DEADMAN_I18N_ENABLED", "0")
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    i18n_pkg._translator_instance = None
    i18n_pkg.locale._locale_detector_instance = None
    i18n_pkg.messages._message_bundle_instance = None
    i18n_pkg.currency._currency_converter_instance = None
    i18n_pkg.timezone._timezone_manager_instance = None
    i18n_pkg.law_adapter._law_adapter_instance = None


@pytest.fixture
def disable_i18n(monkeypatch):
    """显式关闭 i18n 的测试用。"""
    monkeypatch.setenv("DEADMAN_I18N_ENABLED", "0")
    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    import deadman.i18n as i18n_pkg
    i18n_pkg._translator_instance = None
    i18n_pkg.locale._locale_detector_instance = None
    i18n_pkg.messages._message_bundle_instance = None
    i18n_pkg.currency._currency_converter_instance = None
    i18n_pkg.timezone._timezone_manager_instance = None
    i18n_pkg.law_adapter._law_adapter_instance = None
    yield


# =====================================================================
# 1. Locale
# =====================================================================

class TestLocale:
    def test_locale_enum_values(self):
        from deadman.i18n import Locale
        assert Locale.ZH_CN.value == "zh-CN"
        assert Locale.ZH_TW.value == "zh-TW"
        assert Locale.EN_US.value == "en-US"
        assert Locale.EN_GB.value == "en-GB"
        assert Locale.JA_JP.value == "ja-JP"
        assert Locale.KO_KR.value == "ko-KR"

    def test_locale_from_string_exact(self):
        from deadman.i18n import Locale
        assert Locale.from_string("zh-CN") == Locale.ZH_CN
        assert Locale.from_string("en-US") == Locale.EN_US
        assert Locale.from_string("ja-JP") == Locale.JA_JP

    def test_locale_from_string_loose(self):
        from deadman.i18n import Locale
        # 下划线 / 大小写 / 简写
        assert Locale.from_string("zh_cn") == Locale.ZH_CN
        assert Locale.from_string("EN-us") == Locale.EN_US
        assert Locale.from_string("zh") == Locale.ZH_CN
        assert Locale.from_string("en") == Locale.EN_US
        assert Locale.from_string("ja") == Locale.JA_JP
        assert Locale.from_string("ko") == Locale.KO_KR

    def test_locale_from_string_unknown_fallback(self):
        from deadman.i18n import Locale
        assert Locale.from_string("xx-XX") == Locale.ZH_CN
        assert Locale.from_string("") == Locale.ZH_CN
        assert Locale.from_string(None) == Locale.ZH_CN

    def test_locale_properties(self):
        from deadman.i18n import Locale
        assert Locale.ZH_CN.language == "zh"
        assert Locale.ZH_CN.region == "CN"
        assert Locale.EN_US.language == "en"
        assert Locale.EN_US.region == "US"


# =====================================================================
# 2. LocaleDetector
# =====================================================================

class TestLocaleDetector:
    def test_detect_from_request_accept_language(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        loc = det.detect_from_request(accept_language="en-US,en;q=0.9,zh-CN;q=0.8")
        assert loc == Locale.EN_US

    def test_detect_from_request_headers_dict(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        # 大小写不敏感
        loc = det.detect_from_request(headers={"ACCEPT-LANGUAGE": "ja-JP"})
        assert loc == Locale.JA_JP

    def test_detect_from_request_q_weighted(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        # en q=0.1 应排到 zh q=0.9 后面
        loc = det.detect_from_request(accept_language="en;q=0.1,zh-CN;q=0.9")
        assert loc == Locale.ZH_CN

    def test_detect_from_request_empty_falls_back(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        assert det.detect_from_request(accept_language=None, headers=None) == Locale.ZH_CN

    def test_detect_from_request_malformed(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        # 异常输入不抛异常,返回 ZH_CN
        assert det.detect_from_request(accept_language=";;;invalid;;;") == Locale.ZH_CN

    def test_detect_from_user_profile_no_pref(self, tmp_path):
        from deadman.i18n import LocaleDetector
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        assert det.detect_from_user_profile("u1") is None

    def test_persist_preference_and_load(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        ok = det.persist_preference("u1", Locale.EN_US)
        assert ok is True
        # 新建实例加载持久化数据
        det2 = LocaleDetector(store_path=tmp_path / "prefs.json")
        loc = det2.detect_from_user_profile("u1")
        assert loc == Locale.EN_US

    def test_persist_preference_string_locale(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        det.persist_preference("u2", "ja-JP")
        det2 = LocaleDetector(store_path=tmp_path / "prefs.json")
        assert det2.detect_from_user_profile("u2") == Locale.JA_JP

    def test_clear_preference(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        det.persist_preference("u3", Locale.JA_JP)
        assert det.clear_preference("u3") is True
        assert det.detect_from_user_profile("u3") is None

    def test_detect_from_ip_private(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        # 私有网段 → ZH_CN(测试默认)
        assert det.detect_from_ip("127.0.0.1") == Locale.ZH_CN
        assert det.detect_from_ip("192.168.1.1") == Locale.ZH_CN
        assert det.detect_from_ip("10.0.0.1") == Locale.ZH_CN

    def test_detect_from_ip_cn_prefix(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        # 中国大陆常见网段
        assert det.detect_from_ip("114.114.114.114") == Locale.ZH_CN
        assert det.detect_from_ip("223.5.5.5") == Locale.ZH_CN

    def test_detect_from_ip_us_prefix(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        # 美国常见网段
        assert det.detect_from_ip("8.8.8.8") == Locale.EN_US

    def test_detect_from_ip_empty(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        assert det.detect_from_ip("") == Locale.ZH_CN

    def test_detect_comprehensive_priority(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        # 1. 持久化偏好优先
        det.persist_preference("u1", Locale.KO_KR)
        loc = det.detect(user_id="u1", accept_language="en-US", ip="8.8.8.8")
        assert loc == Locale.KO_KR

    def test_detect_no_pref_falls_to_request(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        loc = det.detect(user_id="u2", accept_language="en-US")
        assert loc == Locale.EN_US


# =====================================================================
# 3. MessageBundle
# =====================================================================

class TestMessageBundle:
    def test_default_messages_loaded(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        # 内置 5 个 key 都应有
        assert bundle.has("greeting", Locale.ZH_CN)
        assert bundle.has("farewell", Locale.ZH_CN)
        assert bundle.has("legal_disclaimer", Locale.ZH_CN)
        assert bundle.has("death_confirmation", Locale.ZH_CN)
        assert bundle.has("will_template_intro", Locale.ZH_CN)

    def test_get_with_vars(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        msg = bundle.get("greeting", Locale.ZH_CN, name="张三")
        assert "张三" in msg
        assert "您好" in msg

    def test_get_en_us(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        msg = bundle.get("greeting", Locale.EN_US, name="Alice")
        assert "Alice" in msg
        assert "Hello" in msg

    def test_get_fallback_to_zh_cn(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        # 删除 EN_US 的翻译,触发兜底
        bundle._messages["greeting"].pop(Locale.EN_US.value, None)
        msg = bundle.get("greeting", Locale.EN_US, name="X")
        # 应回退到 zh-CN
        assert "您好" in msg

    def test_get_missing_key_returns_key(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        assert bundle.get("nonexistent_key_xyz", Locale.ZH_CN) == "nonexistent_key_xyz"

    def test_has_returns_false_for_missing(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        assert bundle.has("nonexistent", Locale.ZH_CN) is False
        assert bundle.has("greeting", Locale.JA_JP) is True

    def test_list_keys(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        keys = bundle.list_keys(Locale.ZH_CN)
        assert "greeting" in keys
        assert "farewell" in keys
        assert "legal_disclaimer" in keys

    def test_add_programmatic(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        bundle.add(Locale.EN_US, "custom.key", "Hello {name}!")
        assert bundle.has("custom.key", Locale.EN_US)
        assert bundle.get("custom.key", Locale.EN_US, name="Bob") == "Hello Bob!"

    def test_add_string_locale(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        bundle.add("ja-JP", "custom.ja", "こんにちは")
        assert bundle.get("custom.ja", Locale.JA_JP) == "こんにちは"

    def test_load_file_json(self, tmp_path):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        path = tmp_path / "msgs.json"
        path.write_text(json.dumps({
            "custom.from.file": "File loaded {name}",
            "another.key": "another",
        }), encoding="utf-8")
        count = bundle.load_file(Locale.EN_US, path)
        assert count == 2
        assert bundle.get("custom.from.file", Locale.EN_US, name="X") == "File loaded X"

    def test_load_file_yaml(self, tmp_path):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        path = tmp_path / "msgs.yaml"
        path.write_text("custom.yaml.key: 'YAML loaded {name}'", encoding="utf-8")
        count = bundle.load_file(Locale.JA_JP, path)
        assert count == 1
        assert bundle.get("custom.yaml.key", Locale.JA_JP, name="X") == "YAML loaded X"

    def test_load_file_not_found(self, tmp_path):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        count = bundle.load_file(Locale.EN_US, tmp_path / "nonexistent.json")
        assert count == 0

    def test_format_missing_var_does_not_crash(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        # 缺少 name 变量应保留模板不抛异常
        msg = bundle.get("greeting", Locale.ZH_CN)
        assert isinstance(msg, str)

    def test_list_locales_for_key(self):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        locs = bundle.list_locales("greeting")
        # 内置应有 6 个 locale
        assert Locale.ZH_CN.value in locs
        assert Locale.EN_US.value in locs
        assert Locale.JA_JP.value in locs


# =====================================================================
# 4. LawAdapter
# =====================================================================

class TestLawAdapter:
    def test_inheritance_law_cn_mainland(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        law = la.get_inheritance_law(Jurisdiction.CN_MAINLAND)
        assert "statute" in law
        assert "民法典" in law["statute"]
        assert isinstance(law["key_rules"], list)
        assert len(law["key_rules"]) > 0
        assert "probate_process" in law
        assert "time_limits" in law

    def test_inheritance_law_each_jurisdiction(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        for j in Jurisdiction:
            law = la.get_inheritance_law(j)
            assert "statute" in law, f"missing statute for {j}"
            assert "key_rules" in law

    def test_data_protection_law_pipl(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        dp = la.get_data_protection_law(Jurisdiction.CN_MAINLAND)
        assert "PIPL" in dp["law"]
        assert dp["cross_border_consent_required"] is True
        assert dp["right_to_delete_days"] == 7

    def test_data_protection_law_gdpr(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        dp = la.get_data_protection_law(Jurisdiction.EU)
        assert "GDPR" in dp["law"]
        assert dp["cross_border_consent_required"] is True

    def test_data_protection_law_ccpa(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        dp = la.get_data_protection_law(Jurisdiction.US)
        assert "CCPA" in dp["law"]
        assert dp["cross_border_consent_required"] is False
        assert dp["right_to_delete_days"] == 45

    def test_data_protection_each_jurisdiction(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        for j in Jurisdiction:
            dp = la.get_data_protection_law(j)
            assert "law" in dp
            assert "right_to_delete_days" in dp
            assert "regulator" in dp

    def test_get_required_consents_cn_cross_border(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        consents = la.get_required_consents(Jurisdiction.CN_MAINLAND, "cross_border_transfer")
        assert "cross_border_consent" in consents
        assert "sensitive_data_consent" in consents

    def test_get_required_consents_eu_deletion(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        consents = la.get_required_consents(Jurisdiction.EU, "data_delete")
        assert "gdpr_erasure_request" in consents

    def test_get_required_consents_unknown_action(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        # 未知 action 应返回空列表
        assert la.get_required_consents(Jurisdiction.CN_MAINLAND, "unknown_action") == []

    def test_validate_cross_border_same_jurisdiction(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        result = la.validate_cross_border(Jurisdiction.CN_MAINLAND, Jurisdiction.CN_MAINLAND)
        assert result.allowed is True
        assert result.legal_basis == "same_jurisdiction"

    def test_validate_cross_border_cn_to_us_blocked(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        result = la.validate_cross_border(
            Jurisdiction.CN_MAINLAND, Jurisdiction.US, "user_profile"
        )
        assert result.allowed is False
        assert "cross_border_consent" in result.consents_required
        assert "PIPL" in result.legal_basis

    def test_validate_cross_border_us_to_cn_allowed(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        result = la.validate_cross_border(Jurisdiction.US, Jurisdiction.CN_MAINLAND)
        # 美国无联邦强制跨境同意
        assert result.allowed is True

    def test_validate_cross_border_eu_to_cn_blocked(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        result = la.validate_cross_border(Jurisdiction.EU, Jurisdiction.CN_MAINLAND)
        assert result.allowed is False
        assert "scc_agreement" in result.consents_required

    def test_validate_cross_border_financial_sensitive(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        result = la.validate_cross_border(
            Jurisdiction.CN_MAINLAND, Jurisdiction.US, "financial"
        )
        assert result.allowed is False
        # financial 应触发 sensitive_data_consent
        assert "sensitive_data_consent" in result.consents_required

    def test_validate_cross_border_no_rule_default_precaution(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        # KR -> EU 没有专门规则,应走 precautionary default
        result = la.validate_cross_border(Jurisdiction.KR, Jurisdiction.EU)
        # KR 跨境同意默认必需
        assert result.allowed is False
        assert "cross_border_consent" in result.consents_required

    def test_jurisdiction_from_locale(self):
        from deadman.i18n import Jurisdiction
        assert Jurisdiction.from_locale("zh-CN") == Jurisdiction.CN_MAINLAND
        assert Jurisdiction.from_locale("zh-TW") == Jurisdiction.CN_HONGKONG
        assert Jurisdiction.from_locale("en-US") == Jurisdiction.US
        assert Jurisdiction.from_locale("en-GB") == Jurisdiction.UK
        assert Jurisdiction.from_locale("ja-JP") == Jurisdiction.JP
        assert Jurisdiction.from_locale("ko-KR") == Jurisdiction.KR
        assert Jurisdiction.from_locale("de-DE") == Jurisdiction.EU
        assert Jurisdiction.from_locale("xx-XX") == Jurisdiction.OTHER

    def test_load_config_json_override(self, tmp_path):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        config = {
            "inheritance_law": {
                "cn_mainland": {
                    "statute": "TEST_OVERRIDE_LAW",
                    "key_rules": ["test rule"],
                    "probate_process": ["test"],
                    "time_limits": {"x": "1d"},
                }
            }
        }
        path = tmp_path / "law.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        count = la.load_config(path)
        assert count >= 1
        law = la.get_inheritance_law(Jurisdiction.CN_MAINLAND)
        assert law["statute"] == "TEST_OVERRIDE_LAW"

    def test_list_jurisdictions(self):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        js = la.list_jurisdictions()
        assert Jurisdiction.CN_MAINLAND in js
        assert len(js) == len(list(Jurisdiction))


# =====================================================================
# 5. CurrencyConverter
# =====================================================================

class TestCurrencyConverter:
    def test_currency_enum_values(self):
        from deadman.i18n import Currency
        assert Currency.CNY.value == "CNY"
        assert Currency.USD.value == "USD"
        assert Currency.EUR.value == "EUR"
        assert Currency.JPY.value == "JPY"
        assert Currency.KRW.value == "KRW"
        assert Currency.GBP.value == "GBP"
        assert Currency.HKD.value == "HKD"

    def test_currency_symbol(self):
        from deadman.i18n import Currency
        assert Currency.CNY.symbol == "¥"
        assert Currency.USD.symbol == "$"
        assert Currency.EUR.symbol == "€"
        assert Currency.JPY.symbol == "¥"
        assert Currency.KRW.symbol == "₩"
        assert Currency.GBP.symbol == "£"

    def test_currency_zero_decimal(self):
        from deadman.i18n import Currency
        assert Currency.JPY.is_zero_decimal is True
        assert Currency.KRW.is_zero_decimal is True
        assert Currency.CNY.is_zero_decimal is False

    def test_get_rate_same_currency(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        assert cc.get_rate(Currency.USD, Currency.USD) == 1.0

    def test_get_rate_usd_to_cny(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        rate = cc.get_rate(Currency.USD, Currency.CNY)
        # 1 USD = 7.20 CNY(默认值)
        assert rate == pytest.approx(7.20, rel=0.01)

    def test_convert_usd_to_cny(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        result = cc.convert(100, Currency.USD, Currency.CNY)
        assert result.amount == pytest.approx(720.0, rel=0.01)
        assert result.from_currency == "USD"
        assert result.to_currency == "CNY"
        assert result.original_amount == 100

    def test_convert_cny_to_usd(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        result = cc.convert(720, Currency.CNY, Currency.USD)
        assert result.amount == pytest.approx(100.0, rel=0.01)

    def test_convert_jpy_zero_decimal(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        # 100 JPY ≈ 4.8 CNY
        result = cc.convert(100, Currency.JPY, Currency.CNY)
        assert result.amount == pytest.approx(4.8, rel=0.05)

    def test_update_rates(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        count = cc.update_rates({"USD": 7.50, "EUR": 8.10})
        assert count == 2
        assert cc.get_rate(Currency.USD, Currency.CNY) == pytest.approx(7.50)

    def test_update_rates_invalid_skipped(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        # 0 或负数应跳过
        count = cc.update_rates({"USD": 0, "EUR": -1, "JPY": 0.05})
        assert count == 1
        assert cc.get_rate(Currency.JPY, Currency.CNY) == pytest.approx(0.05)

    def test_rates_persisted_to_file(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        path = tmp_path / "rates.json"
        cc = CurrencyConverter(store_path=path)
        cc.update_rates({"USD": 7.55})
        # 新实例加载持久化数据
        cc2 = CurrencyConverter(store_path=path)
        assert cc2.get_rate(Currency.USD, Currency.CNY) == pytest.approx(7.55)

    def test_format_cny_zh_cn(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency, Locale
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        formatted = cc.format(1234.5, Currency.CNY, Locale.ZH_CN)
        assert "¥" in formatted
        assert "1,234.50" in formatted

    def test_format_jpy_no_decimal(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency, Locale
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        formatted = cc.format(1500, Currency.JPY, Locale.JA_JP)
        # JPY 通常无小数
        assert "1,500" in formatted
        assert "1,500.00" not in formatted

    def test_format_no_symbol(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency, Locale
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        formatted = cc.format(100, Currency.USD, Locale.EN_US, include_symbol=False)
        assert "$" not in formatted
        assert "100" in formatted

    def test_get_all_rates(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        all_rates = cc.get_all_rates()
        assert "USD" in all_rates
        assert "CNY" in all_rates
        assert all_rates["CNY"] == 1.0

    def test_set_rate(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        assert cc.set_rate(Currency.EUR, 8.20) is True
        assert cc.get_rate(Currency.EUR, Currency.CNY) == pytest.approx(8.20)

    def test_currency_from_string_alias(self):
        from deadman.i18n import Currency
        assert Currency.from_string("$") == Currency.USD
        assert Currency.from_string("€") == Currency.EUR
        assert Currency.from_string("£") == Currency.GBP
        assert Currency.from_string("RMB") == Currency.CNY
        assert Currency.from_string("unknown") == Currency.USD


# =====================================================================
# 6. TimezoneManager
# =====================================================================

class TestTimezoneManager:
    def test_detect_timezone_private_ip(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        assert tm.detect_timezone("127.0.0.1") == "Asia/Shanghai"
        assert tm.detect_timezone("192.168.1.1") == "Asia/Shanghai"

    def test_detect_timezone_empty(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        assert tm.detect_timezone("") == "UTC"

    def test_detect_timezone_from_locale(self):
        from deadman.i18n import TimezoneManager, Locale
        tm = TimezoneManager()
        assert tm.detect_timezone_from_locale(Locale.ZH_CN) == "Asia/Shanghai"
        assert tm.detect_timezone_from_locale(Locale.JA_JP) == "Asia/Tokyo"
        assert tm.detect_timezone_from_locale(Locale.KO_KR) == "Asia/Seoul"
        assert tm.detect_timezone_from_locale(Locale.EN_US) == "America/New_York"
        assert tm.detect_timezone_from_locale(Locale.EN_GB) == "Europe/London"

    def test_now_in_shanghai(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        now = tm.now_in("Asia/Shanghai")
        # 应为 aware datetime
        assert now.tzinfo is not None
        # 北京时间是 UTC+8
        assert now.utcoffset().total_seconds() == 8 * 3600

    def test_now_in_utc(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        now = tm.now_in("UTC")
        assert now.utcoffset().total_seconds() == 0

    def test_convert_shanghai_to_tokyo(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        # naive dt 视为 from_tz
        dt = datetime(2024, 1, 15, 12, 0, 0)
        converted = tm.convert(dt, "Asia/Shanghai", "Asia/Tokyo")
        # 北京 12:00 = 东京 13:00
        assert converted.hour == 13

    def test_convert_aware_dt(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        # aware dt 直接用其 tzinfo
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        converted = tm.convert(dt, "Asia/Shanghai", "UTC")
        # 北京 12:00 = UTC 04:00
        assert converted.hour == 4

    def test_convert_unknown_tz_falls_back_utc(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        dt = datetime(2024, 1, 15, 12, 0, 0)
        # 未知时区不抛异常,回退 UTC
        result = tm.convert(dt, "Invalid/Zone", "UTC")
        assert result.tzinfo is not None

    def test_format_zh_cn(self):
        from deadman.i18n import TimezoneManager, Locale
        tm = TimezoneManager()
        dt = datetime(2024, 1, 15, 12, 30, 45, tzinfo=timezone(timedelta(hours=8)))
        formatted = tm.format(dt, "Asia/Shanghai", Locale.ZH_CN)
        assert "2024-01-15" in formatted
        assert "12:30:45" in formatted

    def test_format_ja_jp(self):
        from deadman.i18n import TimezoneManager, Locale
        tm = TimezoneManager()
        dt = datetime(2024, 1, 15, 12, 30, 45, tzinfo=timezone(timedelta(hours=9)))
        formatted = tm.format(dt, "Asia/Tokyo", Locale.JA_JP)
        assert "2024年01月15日" in formatted
        assert "12時30分45秒" in formatted

    def test_format_naive_dt(self):
        from deadman.i18n import TimezoneManager, Locale
        tm = TimezoneManager()
        dt = datetime(2024, 1, 15, 12, 0, 0)
        # naive dt 应自动加上 tz
        formatted = tm.format(dt, "Asia/Shanghai", Locale.ZH_CN)
        assert "2024-01-15" in formatted

    def test_business_hours_check_in_range(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        # 取当前北京时间,若在 0-23 之间,业务时间必包含某个区间
        now = tm.now_in("Asia/Shanghai")
        # 用 0-24 总能命中
        result = tm.business_hours_check("Asia/Shanghai", 0, 24)
        assert result is True

    def test_business_hours_check_outside_range(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        now = tm.now_in("Asia/Shanghai")
        # hour_start > 当前 hour + 1 必不在范围内
        start = (now.hour + 1) % 24
        end = (start + 1) % 24
        if end > start:
            result = tm.business_hours_check("Asia/Shanghai", start, end)
            assert result is False

    def test_business_hours_default_range(self):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        # 默认 9-18,可能 True 或 False,只验证不抛异常
        result = tm.business_hours_check("Asia/Shanghai")
        assert isinstance(result, bool)


# =====================================================================
# 7. Translator (end-to-end)
# =====================================================================

class TestTranslator:
    def test_set_and_get_locale(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.EN_US)
        assert t.get_locale("u1") == Locale.EN_US

    def test_translate_with_user_locale(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.EN_US)
        msg = t.t("greeting", "u1", name="Alice")
        assert "Hello" in msg
        assert "Alice" in msg

    def test_translate_default_locale(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        # 未设置 locale → 默认 ZH_CN
        msg = t.t("greeting", "u_unknown", name="张三")
        assert "您好" in msg

    def test_format_money_no_conversion(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter, Currency,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.ZH_CN)
        formatted = t.format_money(100, Currency.CNY, "u1")
        assert "¥" in formatted
        assert "100" in formatted

    def test_format_money_with_conversion(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter, Currency,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.ZH_CN)
        # 100 USD → CNY 显示
        formatted = t.format_money(100, Currency.CNY, "u1", convert_from=Currency.USD)
        assert "¥" in formatted
        # 100 USD ≈ 720 CNY
        assert "720" in formatted

    def test_format_datetime_user_locale(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.ZH_CN)
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        formatted = t.format_datetime(dt, "u1", tz="Asia/Shanghai")
        assert "2024-01-15" in formatted

    def test_format_datetime_default_tz(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.JA_JP)
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        # 不传 tz,应按 locale 推断(JA_JP → Asia/Tokyo)
        formatted = t.format_datetime(dt, "u1")
        assert "2024" in formatted

    def test_get_jurisdiction_from_locale(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter, Jurisdiction,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.EN_US)
        assert t.get_jurisdiction("u1") == Jurisdiction.US
        t.set_locale("u2", Locale.JA_JP)
        assert t.get_jurisdiction("u2") == Jurisdiction.JP

    def test_validate_action_local(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.ZH_CN)
        result = t.validate_action("data_delete", "u1")
        assert result.allowed is True
        # 应有 consent 列表
        assert "privacy_policy" in result.consents_required

    def test_validate_action_cross_border(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter, Jurisdiction,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.ZH_CN)
        result = t.validate_action(
            "cross_border_transfer", "u1",
            data_kind="user_profile",
            target_jurisdiction=Jurisdiction.US,
        )
        assert result.allowed is False
        assert "cross_border_consent" in result.consents_required

    def test_now_for_user(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.ZH_CN)
        now = t.now_for_user("u1")
        assert now.tzinfo is not None

    def test_get_inheritance_law_for_user(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.ZH_CN)
        law = t.get_inheritance_law("u1")
        assert "民法典" in law["statute"]

    def test_get_data_protection_law_for_user(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        t.set_locale("u1", Locale.EN_US)
        dp = t.get_data_protection_law("u1")
        assert "CCPA" in dp["law"]

    def test_detect_locale_persist(self, tmp_path):
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        loc = t.detect_locale(
            user_id="u1", accept_language="en-US", persist=True,
        )
        assert loc == Locale.EN_US
        # 重置内存缓存验证持久化
        t.reset_user_locales()
        assert t.get_locale("u1") == Locale.EN_US


# =====================================================================
# 8. Disabled state behavior
# =====================================================================

class TestDisabledState:
    def test_disabled_t_returns_raw_key(self, disable_i18n):
        from deadman.i18n import Translator, LocaleDetector, MessageBundle, CurrencyConverter, TimezoneManager, LawAdapter
        t = Translator(
            locale_detector=LocaleDetector(),
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        # i18n 关闭:t() 返回原始 key
        assert t.t("greeting", "u1", name="X") == "greeting"

    def test_disabled_get_locale_zh_cn(self, disable_i18n):
        from deadman.i18n import Translator, LocaleDetector, MessageBundle, CurrencyConverter, TimezoneManager, LawAdapter, Locale
        t = Translator(
            locale_detector=LocaleDetector(),
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        # i18n 关闭:get_locale 永远返回 ZH_CN
        assert t.get_locale("u1") == Locale.ZH_CN

    def test_disabled_currency_rate_1(self, disable_i18n):
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter()
        # i18n 关闭:get_rate 返回 1.0
        assert cc.get_rate(Currency.USD, Currency.CNY) == 1.0
        # convert 也用 rate 1
        result = cc.convert(100, Currency.USD, Currency.CNY)
        assert result.amount == 100.0
        assert result.rate == 1.0

    def test_disabled_timezone_utc(self, disable_i18n):
        from deadman.i18n import TimezoneManager
        tm = TimezoneManager()
        # i18n 关闭:detect_timezone 返回 UTC
        assert tm.detect_timezone("8.8.8.8") == "UTC"
        assert tm.detect_timezone_from_locale("zh-CN") == "UTC"
        # now_in 返回 UTC
        now = tm.now_in("Asia/Shanghai")
        assert now.utcoffset().total_seconds() == 0

    def test_disabled_format_datetime_uses_utc(self, disable_i18n):
        from deadman.i18n import Translator, LocaleDetector, MessageBundle, CurrencyConverter, TimezoneManager, LawAdapter
        t = Translator(
            locale_detector=LocaleDetector(),
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        formatted = t.format_datetime(dt, "u1")
        # 即使传 Asia/Shanghai,关闭模式应用 UTC
        assert "2024-01-15" in formatted

    def test_disabled_validate_action_allowed(self, disable_i18n):
        from deadman.i18n import Translator, LocaleDetector, MessageBundle, CurrencyConverter, TimezoneManager, LawAdapter, Jurisdiction
        t = Translator(
            locale_detector=LocaleDetector(),
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )
        result = t.validate_action("cross_border_transfer", "u1", target_jurisdiction=Jurisdiction.US)
        # 关闭模式:校验跳过,直接 allowed
        assert result.allowed is True

    def test_disabled_message_bundle_returns_raw_key(self, disable_i18n):
        from deadman.i18n import MessageBundle, Locale
        bundle = MessageBundle()
        # i18n 关闭:get 返回原始 key(不查 messages)
        assert bundle.get("greeting", Locale.EN_US, name="X") == "greeting"

    def test_disabled_law_validate_passes(self, disable_i18n):
        from deadman.i18n import LawAdapter, Jurisdiction
        la = LawAdapter()
        result = la.validate_cross_border(Jurisdiction.CN_MAINLAND, Jurisdiction.US, "user_profile")
        # 关闭:直接通过
        assert result.allowed is True
        assert "i18n_disabled" in result.legal_basis

    def test_disabled_locale_detector_returns_zh_cn(self, disable_i18n, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        # 关闭:所有 detect 返回 ZH_CN
        assert det.detect_from_request(accept_language="en-US") == Locale.ZH_CN
        assert det.detect_from_ip("8.8.8.8") == Locale.ZH_CN
        assert det.detect_from_user_profile("u1") is None  # None 而非 ZH_CN
        # persist_preference 失败
        assert det.persist_preference("u1", Locale.EN_US) is False


# =====================================================================
# 9. Locale persistence (atomic write)
# =====================================================================

class TestLocalePersistence:
    def test_persist_atomic_write(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        path = tmp_path / "prefs.json"
        det = LocaleDetector(store_path=path)
        det.persist_preference("u1", Locale.JA_JP)
        # 文件应已写入
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "prefs" in data
        assert data["prefs"]["u1"]["locale"] == "ja-JP"

    def test_persist_overwrite(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        det.persist_preference("u1", Locale.EN_US)
        det.persist_preference("u1", Locale.JA_JP)  # 覆盖
        det2 = LocaleDetector(store_path=tmp_path / "prefs.json")
        assert det2.detect_from_user_profile("u1") == Locale.JA_JP

    def test_persist_multiple_users(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        det.persist_preference("u1", Locale.EN_US)
        det.persist_preference("u2", Locale.JA_JP)
        det.persist_preference("u3", Locale.KO_KR)
        det2 = LocaleDetector(store_path=tmp_path / "prefs.json")
        assert det2.detect_from_user_profile("u1") == Locale.EN_US
        assert det2.detect_from_user_profile("u2") == Locale.JA_JP
        assert det2.detect_from_user_profile("u3") == Locale.KO_KR

    def test_persist_no_tmp_file_left(self, tmp_path):
        from deadman.i18n import LocaleDetector, Locale
        path = tmp_path / "prefs.json"
        det = LocaleDetector(store_path=path)
        det.persist_preference("u1", Locale.EN_US)
        # 不应遗留 .tmp 文件
        assert not (tmp_path / "prefs.json.tmp").exists()

    def test_currency_rates_atomic_write(self, tmp_path):
        from deadman.i18n import CurrencyConverter, Currency
        path = tmp_path / "rates.json"
        cc = CurrencyConverter(store_path=path)
        cc.update_rates({"USD": 7.55})
        # 文件已写入
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "rates" in data
        assert data["rates"]["USD"] == 7.55
        # 不应遗留 .tmp
        assert not (tmp_path / "rates.json.tmp").exists()


# =====================================================================
# 10. Thread safety (sanity)
# =====================================================================

class TestThreadSafety:
    def test_concurrent_locale_set(self, tmp_path):
        import threading
        from deadman.i18n import (
            Translator, Locale, LocaleDetector, MessageBundle,
            CurrencyConverter, TimezoneManager, LawAdapter,
        )
        det = LocaleDetector(store_path=tmp_path / "prefs.json")
        t = Translator(
            locale_detector=det,
            message_bundle=MessageBundle(),
            currency_converter=CurrencyConverter(store_path=tmp_path / "rates.json"),
            timezone_manager=TimezoneManager(),
            law_adapter=LawAdapter(),
        )

        errors = []

        def worker(uid, locale):
            try:
                for _ in range(50):
                    t.set_locale(uid, locale)
                    t.get_locale(uid)
                    t.t("greeting", uid, name=uid)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            uid = f"u{i}"
            locale = [Locale.ZH_CN, Locale.EN_US, Locale.JA_JP, Locale.KO_KR, Locale.EN_GB][i]
            threads.append(threading.Thread(target=worker, args=(uid, locale)))
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == [], f"Concurrent errors: {errors}"

    def test_concurrent_currency_update(self, tmp_path):
        import threading
        from deadman.i18n import CurrencyConverter, Currency
        cc = CurrencyConverter(store_path=tmp_path / "rates.json")
        errors = []

        def worker():
            try:
                for i in range(20):
                    cc.update_rates({"USD": 7.0 + i * 0.01})
                    cc.convert(100, Currency.USD, Currency.CNY)
                    cc.get_all_rates()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == [], f"Concurrent errors: {errors}"


# =====================================================================
# 11. Module exports
# =====================================================================

class TestModuleExports:
    def test_all_exports_present(self):
        import deadman.i18n as i18n_pkg
        expected = [
            "Locale", "LocaleDetector", "LocalePreference",
            "MessageBundle",
            "Jurisdiction", "LawAdapter", "ValidationResult",
            "Currency", "ConvertedAmount", "CurrencyConverter",
            "TimezoneManager",
            "Translator", "get_translator",
            "I18nDisabledError",
        ]
        for name in expected:
            assert hasattr(i18n_pkg, name), f"Missing export: {name}"

    def test_i18n_disabled_error_is_exception(self):
        from deadman.i18n import I18nDisabledError
        assert issubclass(I18nDisabledError, Exception)
        # 可抛 / 可捕获
        with pytest.raises(I18nDisabledError):
            raise I18nDisabledError("test")

    def test_validation_result_dataclass(self):
        from deadman.i18n import ValidationResult
        r = ValidationResult(
            allowed=True,
            jurisdiction_from="cn_mainland",
            jurisdiction_to="us",
            data_kind="user_profile",
        )
        assert r.allowed is True
        assert r.jurisdiction_from == "cn_mainland"
        assert r.consents_required == []
        # to_dict 可序列化
        d = r.to_dict()
        assert d["allowed"] is True

    def test_converted_amount_dataclass(self):
        from deadman.i18n import ConvertedAmount
        c = ConvertedAmount(
            amount=720.0,
            from_currency="USD",
            to_currency="CNY",
            rate=7.2,
            original_amount=100,
        )
        assert c.amount == 720.0
        d = c.to_dict()
        assert d["to_currency"] == "CNY"
