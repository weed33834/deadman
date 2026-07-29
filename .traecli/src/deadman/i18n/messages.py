"""P8.5.2 Multi-language message bundle.

设计:
    - MessageBundle: 多语言消息存储(key -> {locale -> value})
    - 支持 YAML / JSON 文件加载
    - str.format 变量替换: get("greeting", locale, name="张三")
    - 兜底: key 在目标 locale 缺失时回退到 ZH_CN(始终保证存在)
    - 线程安全: RLock 保护内部 dict

内置默认消息(zh-CN / en-US / ja-JP / ko-KR / zh-TW / en-GB):
    - greeting: 问候语
    - farewell: 告别语
    - legal_disclaimer: 法律免责声明
    - death_confirmation: 死亡确认提示
    - will_template_intro: 遗嘱模板引言

feature flag:`DEADMAN_I18N_ENABLED=0` 关闭时 get() 返回原始 key(向后兼容)。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

import yaml

from ..infrastructure.feature_flags import is_enabled
from .locale import Locale

logger = logging.getLogger(__name__)


# =====================================================================
# 内置默认消息(全 locale,全 key)
# 通过 _DEFAULT_MESSAGES 注入到每个 MessageBundle 实例
# =====================================================================
_DEFAULT_MESSAGES: dict[str, dict[str, str]] = {
    "greeting": {
        Locale.ZH_CN.value: "您好,{name}!我是您的 deadman 智能助手。",
        Locale.ZH_TW.value: "您好,{name}!我是您的 deadman 智能助手。",
        Locale.EN_US.value: "Hello, {name}! I'm your deadman assistant.",
        Locale.EN_GB.value: "Hello, {name}! I'm your deadman assistant.",
        Locale.JA_JP.value: "こんにちは、{name}さん!deadman アシスタントです。",
        Locale.KO_KR.value: "안녕하세요, {name}님!deadman 어시스턴트입니다.",
    },
    "farewell": {
        Locale.ZH_CN.value: "再见,{name}。祝您一切顺利。",
        Locale.ZH_TW.value: "再見,{name}。祝您一切順利。",
        Locale.EN_US.value: "Goodbye, {name}. All the best.",
        Locale.EN_GB.value: "Goodbye, {name}. All the best.",
        Locale.JA_JP.value: "さようなら、{name}さん。ご健勝をお祈りいたします。",
        Locale.KO_KR.value: "안녕히 가세요, {name}님. 만사가 순조롭기를 바랍니다.",
    },
    "legal_disclaimer": {
        Locale.ZH_CN.value: (
            "⚠️ 本内容由 AI 生成,仅供参考,不构成法律 / 医疗 / 财务建议。"
            "请以当地持牌专业人士意见为准。跨境法律适用以您所在司法管辖区为准。"
        ),
        Locale.ZH_TW.value: (
            "⚠️ 本內容由 AI 生成,僅供參考,不構成法律 / 醫療 / 財務建議。"
            "請以當地持牌專業人士意見為準。跨境法律適用以您所在司法管轄區為準。"
        ),
        Locale.EN_US.value: (
            "⚠️ This content is AI-generated and for reference only. "
            "It does not constitute legal / medical / financial advice. "
            "Cross-border legal applicability follows your jurisdiction."
        ),
        Locale.EN_GB.value: (
            "⚠️ This content is AI-generated and for reference only. "
            "It does not constitute legal / medical / financial advice. "
            "Cross-border legal applicability follows your jurisdiction."
        ),
        Locale.JA_JP.value: (
            "⚠️ このコンテンツは AI 生成であり、参考のみです。"
            "法律 / 医療 / 財務の助言を構成するものではありません。"
            "越境法の適用はお客様の管轄区域に従います。"
        ),
        Locale.KO_KR.value: (
            "⚠️ 이 콘텐츠는 AI가 생성하였으며 참고용입니다. "
            "법률 / 의료 / 재무 자문을 구성하지 않습니다. "
            "국경 간 법률 적용은 귀하의 관할권을 따릅니다."
        ),
    },
    "death_confirmation": {
        Locale.ZH_CN.value: (
            "已确认用户 {name} 离世(时间:{time})。"
            "请根据您所在司法管辖区,联系公证处或律师启动遗产处置流程。"
        ),
        Locale.ZH_TW.value: (
            "已確認用戶 {name} 離世(時間:{time})。"
            "請根據您所在司法管轄區,聯繫公證處或律師啟動遺產處置流程。"
        ),
        Locale.EN_US.value: (
            "User {name} is confirmed deceased (time: {time}). "
            "Please contact a notary or attorney in your jurisdiction to start the probate process."
        ),
        Locale.EN_GB.value: (
            "User {name} is confirmed deceased (time: {time}). "
            "Please contact a notary or solicitor in your jurisdiction to start the probate process."
        ),
        Locale.JA_JP.value: (
            "ユーザー {name} の逝去を確認しました(時間:{time})。"
            "管轄区域に応じて公証役場または弁護士に連絡し、相続手続きを開始してください。"
        ),
        Locale.KO_KR.value: (
            "사용자 {name}님의 사망이 확인되었습니다(시간:{time}). "
            "관할권에 따라 공증사무소 또는 변호사에게 연락하여 상속 절차를 시작하세요."
        ),
    },
    "will_template_intro": {
        Locale.ZH_CN.value: (
            "本遗嘱由 {name} 于 {date} 在 {jurisdiction} 立下。"
            "立遗嘱人具有完全民事行为能力,意思表示真实,无他人胁迫。"
        ),
        Locale.ZH_TW.value: (
            "本遺囑由 {name} 於 {date} 在 {jurisdiction} 立下。"
            "立遺囑人具有完全民事行為能力,意思表示真實,無他人脅迫。"
        ),
        Locale.EN_US.value: (
            "This will is made by {name} on {date} in {jurisdiction}. "
            "The testator has full civil capacity, expresses genuine intent, "
            "and is free from coercion by any third party."
        ),
        Locale.EN_GB.value: (
            "This will is made by {name} on {date} in {jurisdiction}. "
            "The testator has full civil capacity, expresses genuine intent, "
            "and is free from coercion by any third party."
        ),
        Locale.JA_JP.value: (
            "本遺言は {name} により {date} に {jurisdiction} で作成されました。"
            "遺言者は完全な民事行為能力を有し、意思表示は真実であり、第三者の強要はありません。"
        ),
        Locale.KO_KR.value: (
            "본 유언은 {name}에 의해 {date}에 {jurisdiction}에서 작성되었습니다. "
            "유언자는 완전한 민사행위능력을 가지며, 의사표시는 진실하고 타인의 강요가 없습니다."
        ),
    },
}


class MessageBundle:
    """多语言消息存储 + 翻译查询。

    用法:
        bundle = MessageBundle()
        bundle.add(Locale.EN_US, "greeting", "Hello, {name}!")
        msg = bundle.get("greeting", Locale.EN_US, name="Alice")
        # "Hello, Alice!"

    兜底规则:
        1. 在目标 locale 查找 key
        2. 缺失 → 回退到 ZH_CN(始终保证存在)
        3. ZH_CN 也缺失 → 返回原始 key 字符串
    """

    def __init__(self, defaults: Optional[dict[str, dict[str, str]]] = None) -> None:
        self._lock = threading.RLock()
        # 内部结构: {key: {locale_value: message}}
        self._messages: dict[str, dict[str, str]] = {}
        self._init_defaults(defaults or _DEFAULT_MESSAGES)

    def _init_defaults(self, defaults: dict[str, dict[str, str]]) -> None:
        """注入内置默认消息。"""
        for key, locale_map in defaults.items():
            for locale_str, value in locale_map.items():
                self._messages.setdefault(key, {})[locale_str] = value

    # ==================================================================
    # 写入
    # ==================================================================

    def add(self, locale: Locale | str, key: str, value: str) -> None:
        """编程式添加一条消息。

        Args:
            locale: Locale 枚举或字符串(如 "zh-CN")
            key: 消息 key(如 "greeting")
            value: 消息值(支持 {var} 占位符)
        """
        loc_str = locale.value if isinstance(locale, Locale) else str(locale)
        with self._lock:
            self._messages.setdefault(key, {})[loc_str] = value

    def load_file(self, locale: Locale | str, path: str | Path) -> int:
        """从 YAML / JSON 文件加载某 locale 的所有 key。

        文件格式(JSON):
            {
                "greeting": "Hello, {name}!",
                "farewell": "Bye, {name}!"
            }

        文件格式(YAML):
            greeting: "Hello, {name}!"
            farewell: "Bye, {name}!"

        Args:
            locale: Locale 枚举或字符串
            path: 文件路径(.json 或 .yaml/.yml)

        Returns:
            成功加载的 key 数量
        """
        loc_str = locale.value if isinstance(locale, Locale) else str(locale)
        path = Path(path)
        if not path.exists():
            logger.warning("Message file not found: %s", path)
            return 0

        try:
            suffix = path.suffix.lower()
            if suffix == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
            elif suffix in (".yaml", ".yml"):
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            else:
                logger.warning("Unsupported message file type: %s", path)
                return 0
        except (OSError, json.JSONDecodeError, yaml.YAMLError) as e:
            logger.error("Failed to load message file %s: %s", path, e)
            return 0

        if not isinstance(data, dict):
            logger.warning("Message file %s does not contain a dict", path)
            return 0

        count = 0
        with self._lock:
            for key, value in data.items():
                if isinstance(value, str):
                    self._messages.setdefault(str(key), {})[loc_str] = value
                    count += 1
        logger.info("Loaded %d messages for locale %s from %s", count, loc_str, path)
        return count

    # ==================================================================
    # 读取
    # ==================================================================

    def get(self, key: str, locale: Locale | str, **vars: Any) -> str:
        """查询消息,带 str.format 变量替换。

        兜底顺序:
            1. 目标 locale
            2. ZH_CN(始终保证存在)
            3. 返回 key 原文

        Args:
            key: 消息 key
            locale: Locale
            **vars: 占位符变量(如 name="张三")

        Returns:
            渲染后的字符串
        """
        # feature flag 关闭:返回原始 key(向后兼容)
        if not is_enabled("i18n"):
            return key

        loc_str = locale.value if isinstance(locale, Locale) else str(locale)
        with self._lock:
            key_map = self._messages.get(key, {})
            template = key_map.get(loc_str)
            # 回退到 ZH_CN
            if template is None and loc_str != Locale.ZH_CN.value:
                template = key_map.get(Locale.ZH_CN.value)
            # 完全缺失:返回原始 key
            if template is None:
                return key

        # 变量替换(str.format 安全,缺变量保留原占位符)
        if vars:
            try:
                return template.format(**vars)
            except (KeyError, IndexError, ValueError) as e:
                logger.warning("Message format failed for key=%s locale=%s: %s",
                               key, loc_str, e)
                return template
        return template

    def has(self, key: str, locale: Locale | str) -> bool:
        """判断 key 在指定 locale 是否存在(不触发兜底)。"""
        loc_str = locale.value if isinstance(locale, Locale) else str(locale)
        with self._lock:
            key_map = self._messages.get(key)
            if key_map is None:
                return False
            return loc_str in key_map

    def list_keys(self, locale: Locale | str) -> list[str]:
        """列出某 locale 下所有 key。"""
        loc_str = locale.value if isinstance(locale, Locale) else str(locale)
        with self._lock:
            result: list[str] = []
            for key, locale_map in self._messages.items():
                if loc_str in locale_map:
                    result.append(key)
            return sorted(result)

    def list_locales(self, key: str) -> list[str]:
        """列出某 key 已翻译的 locale 列表。"""
        with self._lock:
            key_map = self._messages.get(key, {})
            return sorted(key_map.keys())

    def reload_defaults(self) -> None:
        """重新加载内置默认消息(测试 / 重置用)。"""
        with self._lock:
            self._messages.clear()
            self._init_defaults(_DEFAULT_MESSAGES)


# 全局单例
_message_bundle_instance: Optional[MessageBundle] = None
_message_bundle_lock = threading.Lock()


def get_message_bundle() -> MessageBundle:
    """获取全局 MessageBundle 单例(惰性初始化)。"""
    global _message_bundle_instance
    if _message_bundle_instance is None:
        with _message_bundle_lock:
            if _message_bundle_instance is None:
                _message_bundle_instance = MessageBundle()
    return _message_bundle_instance


def reset_message_bundle() -> None:
    """重置全局单例(测试用)。"""
    global _message_bundle_instance
    with _message_bundle_lock:
        _message_bundle_instance = None
