"""D16:多 provider 风格归一化(Multi-Provider Style Normalization)。

问题:
    deadman 调用多个 LLM provider(OpenAI / Anthropic / Zhipu / 本地),
    不同 provider 对相同 prompt 的响应风格差异大:
        - OpenAI:简洁 / 工程化 / 列表多
        - Anthropic:Claude 风格 / 注重语气 / 拒绝性强
        - Zhipu(GLM):中文为主 / 偏正式
        - 本地 Llama / Qwen:风格随机 / 可能重复 / 不稳定

    Fallback 切换 provider 时,用户感知"人格分裂":
        - 同一会话先 Claude 后 GPT → 风格突变
        - ReAct 循环中模型变更 → 输出格式不一致 → 工具解析失败

    其他差异:
        - 温度参数语义不同(OpenAI 0=确定,Anthropic 不支持 0)
        - max_tokens 含义不同(Anthropic 含 reasoning tokens)
        - 响应长度偏好不同(GPT 倾向长,Claude 倾向适中)
        - 拒绝 / 安全护栏触发阈值不同
        - Tool call 格式不同

缓解:
    - StyleProfile:定义人格(语气 / 长度 / 格式 / 安全偏好)
    - ProviderStyleAdapter:每个 provider 的输入预处理 + 输出后处理
    - StyleNormalizer:统一接口,内部按 provider 选择 adapter
    - 风格漂移检测:跨调用风格一致性度量

设计:
    normalizer = StyleNormalizer()
    # 1. 输入预处理(根据目标 provider 调整 prompt)
    adjusted_prompt = normalizer.adjust_prompt(prompt, target_provider="anthropic")
    # 2. 输出后处理(归一化到统一风格)
    normalized_output = normalizer.normalize_response(
        response="...", source_provider="openai", target_profile=profile
    )

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


class Provider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    ZHIPU = "zhipu"
    OLLAMA = "ollama"
    CUSTOM = "custom"


class ToneStyle(str, Enum):
    """语气风格。"""

    FORMAL = "formal"  # 正式
    WARM = "warm"  # 温暖(deadman 主风格)
    NEUTRAL = "neutral"  # 中立
    TECHNICAL = "technical"  # 技术性


@dataclass
class StyleProfile:
    """风格档案(deadman 主人格)。"""

    tone: ToneStyle = ToneStyle.WARM
    max_response_length: int = 800  # 字符
    prefer_lists: bool = False  # 是否偏好列表
    prefer_emojis: bool = False
    # 拒绝时是否给替代建议
    suggest_alternatives: bool = True
    # 中文为主(若用户中文输入)
    prefer_chinese: bool = True
    # 工具调用格式标准化
    standardize_tool_calls: bool = True


# 各 provider 默认风格特征(用于归一化时识别)
_PROVIDER_DEFAULTS = {
    Provider.OPENAI: {
        "avg_length": 1200,
        "list_preference": 0.7,  # 70% 输出含列表
        "emoji_frequency": 0.05,
        "tone": ToneStyle.TECHNICAL,
    },
    Provider.ANTHROPIC: {
        "avg_length": 900,
        "list_preference": 0.4,
        "emoji_frequency": 0.02,
        "tone": ToneStyle.WARM,
    },
    Provider.ZHIPU: {
        "avg_length": 1000,
        "list_preference": 0.5,
        "emoji_frequency": 0.01,
        "tone": ToneStyle.FORMAL,
    },
    Provider.OLLAMA: {
        "avg_length": 1500,  # 本地模型常冗长
        "list_preference": 0.3,
        "emoji_frequency": 0.0,
        "tone": ToneStyle.NEUTRAL,
    },
    Provider.CUSTOM: {
        "avg_length": 1000,
        "list_preference": 0.4,
        "emoji_frequency": 0.02,
        "tone": ToneStyle.NEUTRAL,
    },
}

# 默认 profile(用于未注册的 provider)
_DEFAULT_PROVIDER_PROFILE = _PROVIDER_DEFAULTS[Provider.CUSTOM]


@dataclass
class StyleDriftReport:
    """风格漂移报告。"""

    source_provider: Provider
    target_provider: Provider
    length_delta: int = 0
    tone_changed: bool = False
    format_changed: bool = False
    drift_score: float = 0.0  # 0-1,1=完全不一致


class ProviderStyleAdapter:
    """单个 provider 的风格适配器。

    职责:
        - adjust_prompt: 输入预处理(根据 provider 特性调整 prompt)
        - normalize_response: 输出后处理(归一化到统一风格)
    """

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.defaults = _PROVIDER_DEFAULTS.get(provider, _DEFAULT_PROVIDER_PROFILE)

    def adjust_prompt(
        self,
        prompt: str,
        target_profile: StyleProfile,
    ) -> str:
        """根据 provider 调整 prompt。

        - Anthropic:增加语气提示(温暖)
        - OpenAI:增加长度限制提示
        - 本地:增加格式约束
        """
        if not prompt:
            return prompt

        adjusted = prompt
        if self.provider == Provider.ANTHROPIC:
            # Claude 注重语气:加入温暖提示
            if target_profile.tone == ToneStyle.WARM and "温暖" not in adjusted:
                adjusted = f"{adjusted}\n\n请以温暖、同理心的语气回复。"
        elif self.provider == Provider.OPENAI:
            # OpenAI 倾向冗长:加入长度约束
            if target_profile.max_response_length < 1000:
                adjusted = (
                    f"{adjusted}\n\n"
                    f"请控制在 {target_profile.max_response_length} 字符以内,"
                    f"重点突出,避免冗长。"
                )
        elif self.provider == Provider.OLLAMA:
            # 本地模型:增加格式约束
            if target_profile.prefer_lists:
                adjusted = (
                    f"{adjusted}\n\n"
                    f"请使用清晰的列表格式输出,避免重复啰嗦。"
                )

        return adjusted

    def normalize_response(
        self,
        response: str,
        target_profile: StyleProfile,
    ) -> str:
        """将 provider 输出归一化到目标风格。

        - 截断过长输出
        - 移除 / 添加 emoji(按 profile)
        - 移除冗余"以下是...""希望..."开头
        - 统一列表格式
        """
        if not response:
            return response

        normalized = response

        # 1. 移除冗余开头
        normalized = self._strip_redundant_opening(normalized)

        # 2. 长度截断
        if len(normalized) > target_profile.max_response_length * 1.2:
            # 截断到目标长度 + 省略号
            cut = target_profile.max_response_length
            # 找最近的句子边界
            for sep in ["。", "!", "?", "\n"]:
                last_sep = normalized.rfind(sep, 0, cut)
                if last_sep > cut * 0.8:
                    cut = last_sep + 1
                    break
            normalized = normalized[:cut].rstrip() + "……"

        # 3. Emoji 处理
        if not target_profile.prefer_emojis:
            # 移除 emoji(简化版:移除常见 emoji 范围)
            normalized = self._strip_emojis(normalized)

        # 4. 列表格式统一(若偏好列表但当前是段落)
        if target_profile.prefer_lists and "\n-" not in normalized and "\n*" not in normalized:
            # 检查是否含"1. 2. 3."样式
            if not re.search(r"^\s*\d+\.\s", normalized, re.MULTILINE):
                # 段落含分号 / 顿号 → 转 list
                if "；" in normalized or ";" in normalized:
                    parts = re.split(r"[；;]", normalized)
                    parts = [p.strip() for p in parts if p.strip()]
                    if len(parts) >= 2:
                        normalized = "\n".join(f"- {p}" for p in parts)

        # 5. 中文偏好(若原文中文)
        if target_profile.prefer_chinese and self._is_mostly_chinese(normalized):
            # 简单替换常见英文术语 → 中文(防止 provider 用英文术语)
            replacements = {
                "Note:": "注意:",
                "Warning:": "警告:",
                "Summary:": "总结:",
                "Conclusion:": "结论:",
            }
            for en, zh in replacements.items():
                normalized = normalized.replace(en, zh)

        return normalized.strip()

    def detect_drift(
        self,
        prev_response: str,
        curr_response: str,
    ) -> StyleDriftReport:
        """检测风格漂移(用于审计 / 告警)。"""
        report = StyleDriftReport(
            source_provider=self.provider,
            target_provider=self.provider,
        )
        prev_len = len(prev_response)
        curr_len = len(curr_response)
        report.length_delta = curr_len - prev_len
        # 长度变化 > 50% → 漂移
        if prev_len > 0:
            ratio = abs(curr_len - prev_len) / prev_len
            if ratio > 0.5:
                report.drift_score += 0.3
        # 格式变化(列表 ↔ 段落)
        prev_has_list = bool(re.search(r"^\s*[-*]\s|^\s*\d+\.\s", prev_response, re.MULTILINE))
        curr_has_list = bool(re.search(r"^\s*[-*]\s|^\s*\d+\.\s", curr_response, re.MULTILINE))
        if prev_has_list != curr_has_list:
            report.format_changed = True
            report.drift_score += 0.4
        # 语气变化(简化:含"抱歉"等道歉词变化)
        prev_apology = "抱歉" in prev_response or "对不起" in prev_response
        curr_apology = "抱歉" in curr_response or "对不起" in curr_response
        if prev_apology != curr_apology:
            report.tone_changed = True
            report.drift_score += 0.3

        report.drift_score = min(1.0, report.drift_score)
        return report

    # ==================================================================
    # 内部
    # ==================================================================

    @staticmethod
    def _strip_redundant_opening(text: str) -> str:
        """移除冗余开头。"""
        patterns = [
            r"^(好的|好的,|好的。|没问题|当然可以|当然|以下是)[,，。:\s]*",
            r"^(希望这能帮到你|希望对你有帮助)[。.\s]*",
            r"^(Let me|I'll|Sure|Of course)[,.:\s]*",
        ]
        result = text
        for p in patterns:
            result = re.sub(p, "", result, count=1, flags=re.IGNORECASE)
        return result.lstrip()

    @staticmethod
    def _strip_emojis(text: str) -> str:
        """移除 emoji(简化版)。"""
        # 常见 emoji 范围:U+1F600-U+1F64F, U+1F300-U+1F5FF, U+2600-U+26FF
        emoji_pattern = re.compile(
            "[\U0001F600-\U0001F64F"
            "\U0001F300-\U0001F5FF"
            "\U00002600-\U000026FF"
            "\U00002700-\U000027BF"
            "]+",
            flags=re.UNICODE,
        )
        return emoji_pattern.sub("", text)

    @staticmethod
    def _is_mostly_chinese(text: str) -> bool:
        """判断是否以中文为主。"""
        if not text:
            return False
        chinese_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        alpha_count = sum(1 for c in text if c.isalpha() and ord(c) < 128)
        if chinese_count + alpha_count == 0:
            return False
        return chinese_count > alpha_count


class StyleNormalizer:
    """风格归一化器(统一入口)。

    用法:
        normalizer = StyleNormalizer(profile=StyleProfile(tone=ToneStyle.WARM))
        # 输入预处理(切换 provider 时调整 prompt)
        prompt = normalizer.adjust_prompt(prompt, target_provider=Provider.ANTHROPIC)
        # 调用 LLM
        response = llm.chat(prompt, provider="anthropic")
        # 输出归一化
        normalized = normalizer.normalize_response(response, source_provider=Provider.ANTHROPIC)
    """

    def __init__(
        self,
        profile: Optional[StyleProfile] = None,
    ) -> None:
        self.profile = profile or StyleProfile()
        self._lock = threading.RLock()
        self._adapters: dict[Provider, ProviderStyleAdapter] = {}
        # 漂移历史(用于审计)
        self._drift_history: list[StyleDriftReport] = []

    def get_adapter(self, provider: Provider) -> ProviderStyleAdapter:
        with self._lock:
            if provider not in self._adapters:
                self._adapters[provider] = ProviderStyleAdapter(provider)
            return self._adapters[provider]

    def adjust_prompt(
        self,
        prompt: str,
        target_provider: Provider,
    ) -> str:
        if not is_enabled("defense"):
            return prompt
        adapter = self.get_adapter(target_provider)
        return adapter.adjust_prompt(prompt, self.profile)

    def normalize_response(
        self,
        response: str,
        source_provider: Provider,
        prev_response: Optional[str] = None,
    ) -> str:
        if not is_enabled("defense"):
            return response
        adapter = self.get_adapter(source_provider)
        normalized = adapter.normalize_response(response, self.profile)

        # 漂移检测(若有上一条响应)
        if prev_response:
            drift = adapter.detect_drift(prev_response, response)
            if drift.drift_score > 0.5:
                logger.warning(
                    "Style drift detected: %s (score=%.2f, format_changed=%s)",
                    source_provider.value, drift.drift_score, drift.format_changed,
                )
            with self._lock:
                self._drift_history.append(drift)
                if len(self._drift_history) > 1000:
                    self._drift_history = self._drift_history[-500:]
        return normalized

    def get_drift_history(self, limit: int = 50) -> list[StyleDriftReport]:
        with self._lock:
            return list(self._drift_history[-limit:])


# =====================================================================
# 全局单例
# =====================================================================

_normalizer: Optional[StyleNormalizer] = None
_lock = threading.Lock()


def get_style_normalizer() -> StyleNormalizer:
    global _normalizer
    with _lock:
        if _normalizer is None:
            _normalizer = StyleNormalizer()
        return _normalizer


def reset_style_normalizer() -> None:
    global _normalizer
    with _lock:
        _normalizer = None
