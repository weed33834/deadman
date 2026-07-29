"""P8.5.1 Locale detection and switching.

设计:
    - Locale: 支持的 6 种 locale(zh-CN / zh-TW / en-US / ja-JP / ko-KR / en-GB)
    - LocaleDetector: 三路检测(request header / user profile / IP 地理位置)
    - 持久化: 用户偏好保存到 data/i18n/locale_prefs.json(原子写)
    - multi_tenant.resolve_data_path 用于解析租户隔离路径

检测优先级(从高到低):
    1. 用户显式设置的偏好(persist_preference)
    2. 请求头 Accept-Language
    3. 用户 profile 中存储的偏好
    4. IP 地理位置(时区数据库)
    5. 默认 zh-CN(产品主语言)

feature flag:`DEADMAN_I18N_ENABLED=0` 关闭时所有 detect 返回 ZH_CN。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import resolve_data_path

logger = logging.getLogger(__name__)


class Locale(str, Enum):
    """支持的 locale 列表。

    值为 BCP 47 语言标签(如 zh-CN)。
    """

    ZH_CN = "zh-CN"  # 简体中文(中国大陆)— 默认/兜底语言
    ZH_TW = "zh-TW"  # 繁体中文(台湾)
    EN_US = "en-US"  # English (United States)
    EN_GB = "en-GB"  # English (United Kingdom)
    JA_JP = "ja-JP"  # 日本語
    KO_KR = "ko-KR"  # 한국어

    @classmethod
    def from_string(cls, value: str) -> "Locale":
        """宽松解析 locale 字符串,匹配失败回退到 ZH_CN。

        支持:
            - "zh-CN" / "zh-cn" / "zh_CN" / "zh" / "zh-Hans-CN"
            - "en" / "en-US" / "en_US"
        """
        if not value:
            return cls.ZH_CN
        normalized = value.strip().replace("_", "-").lower()
        # 精确匹配
        for loc in cls:
            if loc.value.lower() == normalized:
                return loc
        # 前缀匹配(只看主语言)
        prefix = normalized.split("-")[0]
        prefix_map = {
            "zh": cls.ZH_CN,
            "en": cls.EN_US,
            "ja": cls.JA_JP,
            "ko": cls.KO_KR,
        }
        if prefix in prefix_map:
            return prefix_map[prefix]
        return cls.ZH_CN

    @property
    def language(self) -> str:
        """主语言代码(如 zh / en / ja / ko)。"""
        return self.value.split("-")[0]

    @property
    def region(self) -> str:
        """区域代码(如 CN / TW / US / JP / KR / GB)。"""
        parts = self.value.split("-")
        return parts[1] if len(parts) > 1 else ""


@dataclass
class LocalePreference:
    """用户 locale 偏好记录。"""

    user_id: str
    locale: str  # Locale.value
    source: str = "manual"  # manual / request / profile / ip
    updated_at: float = field(default_factory=time.time)


class I18nDisabledError(Exception):
    """i18n feature flag 关闭时调用 i18n 高级功能抛出。

    关闭模式下:
        - detect_* 仍可工作但返回 ZH_CN(向后兼容)
        - 高级操作(显式切换 locale / 加载消息文件等)抛此异常
    """


class LocaleDetector:
    """Locale 检测器。

    持久化: data/i18n/locale_prefs.json(按 tenant 隔离)
    线程安全: RLock + 原子写(.tmp + os.replace)
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        if store_path is None:
            store_path = resolve_data_path("i18n/locale_prefs.json")
        self.store_path = Path(store_path) if not isinstance(store_path, Path) else store_path
        self._lock = threading.RLock()
        self._prefs: dict[str, LocalePreference] = {}
        self._loaded = False

    # ==================================================================
    # 三路检测
    # ==================================================================

    def detect_from_request(
        self,
        headers: Optional[dict[str, str]] = None,
        accept_language: Optional[str] = None,
    ) -> Locale:
        """从 HTTP 请求头检测 locale。

        Args:
            headers: HTTP headers 字典(从中读取 Accept-Language)
            accept_language: 直接传入 Accept-Language 字符串(优先)

        Returns:
            Locale(失败回退 ZH_CN)
        """
        if not is_enabled("i18n"):
            return Locale.ZH_CN

        # accept_language 优先,其次从 headers 拿
        if accept_language is None and headers:
            # 大小写不敏感查找
            for k, v in headers.items():
                if k.lower() == "accept-language":
                    accept_language = v
                    break

        if not accept_language:
            return Locale.ZH_CN

        # 解析 Accept-Language: "zh-CN,zh;q=0.9,en;q=0.8"
        try:
            parts = [p.strip() for p in accept_language.split(",")]
            # 按 q 值排序
            weighted: list[tuple[float, str]] = []
            for part in parts:
                if ";q=" in part:
                    lang, q_str = part.split(";q=", 1)
                    weighted.append((float(q_str), lang.strip()))
                else:
                    weighted.append((1.0, part))
            weighted.sort(key=lambda x: -x[0])
            for _, lang in weighted:
                if lang == "*":
                    continue
                return Locale.from_string(lang)
        except (ValueError, IndexError) as e:
            logger.warning("Failed to parse Accept-Language '%s': %s", accept_language, e)

        return Locale.ZH_CN

    def detect_from_user_profile(self, user_id: str) -> Optional[Locale]:
        """从用户持久化偏好检测 locale。

        Returns:
            Locale(若用户从未设置则 None)
        """
        if not is_enabled("i18n"):
            return None
        with self._lock:
            self._load()
            pref = self._prefs.get(user_id)
            if pref is None:
                return None
            try:
                return Locale(pref.locale)
            except ValueError:
                return Locale.from_string(pref.locale)

    def detect_from_ip(self, ip: str) -> Locale:
        """从 IP 地址地理定位检测 locale(基于时区数据库)。

        生产环境应接 GeoIP 数据库(MaxMind / ip2region)。
        本实现:
            - 内置简单 IPv4 段映射(私有网段 → ZH_CN)
            - 测试场景默认返回 ZH_CN

        Args:
            ip: IPv4 / IPv6 字符串
        """
        if not is_enabled("i18n"):
            return Locale.ZH_CN

        if not ip:
            return Locale.ZH_CN

        # 简单规则:私有网段 + 中国大陆常见网段 → ZH_CN
        # 真实场景需接入 GeoIP 库
        try:
            if ip.startswith(("10.", "172.16.", "172.17.", "172.18.", "172.19.",
                              "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                              "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                              "172.30.", "172.31.", "192.168.", "127.")):
                return Locale.ZH_CN
            # 简单启发:以 1. / 14. / 27. / 36. / 39. / 42. / 49. / 58. / 59. / 60. /
            # 61. / 101. / 106. / 110. / 111. / 112. / 113. / 114. / 115. / 116. /
            # 117. / 118. / 119. / 120. / 121. / 122. / 123. / 124. / 125. / 175. /
            # 180. / 182. / 183. / 202. / 210. / 211. / 218. / 219. / 220. / 221. /
            # 222. / 223. 开头的 IPv4 大概率属于中国大陆
            cn_prefixes = ("1.", "14.", "27.", "36.", "39.", "42.", "49.", "58.",
                           "59.", "60.", "61.", "101.", "106.", "110.", "111.",
                           "112.", "113.", "114.", "115.", "116.", "117.", "118.",
                           "119.", "120.", "121.", "122.", "123.", "124.", "125.",
                           "175.", "180.", "182.", "183.", "202.", "210.", "211.",
                           "218.", "219.", "220.", "221.", "222.", "223.")
            if ip.startswith(cn_prefixes):
                return Locale.ZH_CN
            # 美国 / 欧洲常见网段(粗略)
            if ip.startswith(("4.", "8.", "12.", "23.", "24.", "34.", "35.", "50.",
                              "52.", "63.", "64.", "65.", "66.", "67.", "68.", "71.",
                              "72.", "73.", "74.", "75.", "76.", "77.", "78.", "79.",
                              "80.", "81.", "82.", "83.", "84.", "85.", "86.", "87.",
                              "88.", "89.", "91.", "92.", "93.", "94.", "95.", "96.",
                              "97.", "98.", "99.", "100.", "104.", "107.", "108.",
                              "128.", "129.", "130.", "131.", "132.", "134.", "135.",
                              "136.", "137.", "138.", "139.", "140.", "143.", "144.",
                              "146.", "147.", "148.", "149.", "152.", "155.", "156.",
                              "157.", "158.", "159.", "160.", "161.", "162.", "164.",
                              "165.", "166.", "167.", "168.", "169.", "170.", "173.",
                              "174.", "184.", "189.", "192.", "193.", "194.", "195.",
                              "198.", "199.", "204.", "205.", "206.", "207.", "208.",
                              "209.", "213.", "216.")):
                return Locale.EN_US
        except Exception as e:
            logger.warning("IP locale detect failed for '%s': %s", ip, e)

        # 默认 fallback
        return Locale.ZH_CN

    # ==================================================================
    # 持久化偏好
    # ==================================================================

    def persist_preference(
        self,
        user_id: str,
        locale: Locale | str,
        source: str = "manual",
    ) -> bool:
        """保存用户 locale 偏好。

        Args:
            user_id: 用户 ID
            locale: Locale 枚举或字符串
            source: 偏好来源(manual / request / profile / ip)

        Returns:
            True 表示成功持久化
        """
        if not is_enabled("i18n"):
            # 关闭时记录到内存但不持久化
            return False

        loc = locale if isinstance(locale, Locale) else Locale.from_string(str(locale))
        pref = LocalePreference(
            user_id=user_id,
            locale=loc.value,
            source=source,
            updated_at=time.time(),
        )
        with self._lock:
            self._load()
            self._prefs[user_id] = pref
            self._save()
        logger.info("Persisted locale pref for user %s: %s (source=%s)",
                    user_id, loc.value, source)
        return True

    def clear_preference(self, user_id: str) -> bool:
        """清除用户偏好(回到自动检测)。"""
        with self._lock:
            self._load()
            if user_id in self._prefs:
                del self._prefs[user_id]
                self._save()
                return True
            return False

    def list_preferences(self) -> dict[str, LocalePreference]:
        """列出所有偏好(管理/审计用)。"""
        with self._lock:
            self._load()
            return dict(self._prefs)

    # ==================================================================
    # 综合 detect 入口
    # ==================================================================

    def detect(
        self,
        user_id: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        accept_language: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> Locale:
        """综合检测:用户偏好 > Accept-Language > IP > 默认 ZH_CN。"""
        if not is_enabled("i18n"):
            return Locale.ZH_CN

        # 1. 用户显式偏好
        if user_id:
            pref = self.detect_from_user_profile(user_id)
            if pref is not None:
                return pref
        # 2. 请求头
        if headers or accept_language:
            loc = self.detect_from_request(headers, accept_language)
            if loc != Locale.ZH_CN:
                return loc
        # 3. IP
        if ip:
            return self.detect_from_ip(ip)
        # 4. 默认
        return Locale.ZH_CN

    # ==================================================================
    # 内部:文件 IO(原子写)
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for uid, pdata in data.get("prefs", {}).items():
                    self._prefs[uid] = LocalePreference(
                        user_id=uid,
                        locale=pdata["locale"],
                        source=pdata.get("source", "manual"),
                        updated_at=pdata.get("updated_at", 0.0),
                    )
        except (OSError, json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("Locale prefs load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "prefs": {uid: asdict(p) for uid, p in self._prefs.items()},
            }
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.store_path)
        except OSError as e:
            logger.error("Locale prefs save failed: %s", e)
            raise


# 全局单例
_locale_detector_instance: Optional[LocaleDetector] = None
_locale_detector_lock = threading.Lock()


def get_locale_detector() -> LocaleDetector:
    """获取全局 LocaleDetector 单例。"""
    global _locale_detector_instance
    if _locale_detector_instance is None:
        with _locale_detector_lock:
            if _locale_detector_instance is None:
                _locale_detector_instance = LocaleDetector()
    return _locale_detector_instance


def reset_locale_detector() -> None:
    """重置全局单例(测试用)。"""
    global _locale_detector_instance
    with _locale_detector_lock:
        _locale_detector_instance = None
