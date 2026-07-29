"""P5.4 外部内容沙箱 - 网页/文件/工具结果注入 prompt 前的统一清洗

外部内容（web_search 结果 / 文件读取 / 工具输出）在注入 LLM prompt 前，
统一过 ContentSandbox.sanitize，做三件事：
1. PII 脱敏（复用 memory.manager.sanitize_before_store 的字段级掩码）
2. Prompt injection 痕迹检测（复用 orchestration.nodes.INJECTION_PATTERNS）
3. 长度限制（> max_length 截断，默认 5000 字符）

核心组件：
- SandboxResult: 沙箱清洗结果（sanitized_content/pii_detected/
                  injection_detected/truncated/warnings）
- ContentSandbox: 清洗器

Feature flag: DEADMAN_CONTENT_SANDBOX_ENABLED=0 默认关闭
- 关闭时 sanitize 直接返回原内容（wrapped in SandboxResult，所有检测标志 False），
  调用方走旧路径，行为完全不变
- 开启时执行 PII 脱敏 + 注入检测 + 长度截断

降级路径全覆盖：
1. feature flag 关闭 → 返回原内容，所有标志 False
2. PII 脱敏失败 → 原内容透传，warnings 记录
3. 注入检测失败 → 原内容透传，warnings 记录
4. INJECTION_PATTERNS 复用 orchestration.nodes 的定义（不重复定义）

设计要点：
- 复用现有 sanitize_before_store（dict 级 PII 脱敏）和 INJECTION_PATTERNS
- 不引入新依赖，仅用 stdlib + 已有模块
- SandboxResult.warnings 收集所有降级/告警信息，便于诊断
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
CONTENT_SANDBOX_ENABLED: bool = os.environ.get(
    "DEADMAN_CONTENT_SANDBOX_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 默认最大长度（5000 字符）
DEFAULT_MAX_LENGTH = 5000


# =====================================================================
# 数据模型
# =====================================================================


@dataclass
class SandboxResult:
    """沙箱清洗结果

    Attributes:
        sanitized_content: 清洗后的内容（脱敏 + 截断后）
        pii_detected: 是否检测到 PII（True 表示已脱敏）
        injection_detected: 是否检测到 prompt injection 痕迹
        truncated: 是否被截断（原内容超过 max_length）
        warnings: 告警信息列表（降级/异常记录）
    """

    sanitized_content: str = ""
    pii_detected: bool = False
    injection_detected: bool = False
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


# =====================================================================
# ContentSandbox
# =====================================================================


class ContentSandbox:
    """外部内容沙箱 - PII 脱敏 + 注入检测 + 长度限制

    所有操作在 CONTENT_SANDBOX_ENABLED=False 时返回原内容（不脱敏/不检测/不截断）。
    """

    def __init__(self, max_length: int = DEFAULT_MAX_LENGTH):
        """Args:
            max_length: 最大长度（字符数），超过则截断
        """
        self.max_length = max_length

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def sanitize(self, content: str, max_length: int | None = None) -> SandboxResult:
        """清洗外部内容

        Args:
            content: 原始外部内容
            max_length: 覆盖默认最大长度；None 用构造时的 max_length

        Returns:
            SandboxResult；feature flag 关闭时返回原内容（所有标志 False）

        降级路径：
        1. CONTENT_SANDBOX_ENABLED=False → 返回原内容，所有标志 False
        2. PII 脱敏失败 → 原内容透传，warnings 记录
        3. 注入检测失败 → 原内容透传，warnings 记录
        """
        if not isinstance(content, str):
            content = str(content) if content is not None else ""

        # feature flag 关闭：原内容透传，所有标志 False
        if not CONTENT_SANDBOX_ENABLED:
            return SandboxResult(
                sanitized_content=content,
                pii_detected=False,
                injection_detected=False,
                truncated=False,
                warnings=[],
            )

        warnings: list[str] = []
        pii_detected = False
        injection_detected = False
        truncated = False
        sanitized = content

        # 1. PII 脱敏
        try:
            sanitized, pii_detected = self._sanitize_pii(sanitized)
        except Exception as e:
            warnings.append(f"PII 脱敏失败，原内容透传: {e}")
            logger.warning("content sandbox PII 脱敏失败: %s", e)

        # 2. Prompt injection 检测
        try:
            injection_detected = self._detect_injection(sanitized)
        except Exception as e:
            warnings.append(f"注入检测失败: {e}")
            logger.warning("content sandbox 注入检测失败: %s", e)

        # 3. 长度限制
        effective_max = max_length if max_length is not None else self.max_length
        if effective_max > 0 and len(sanitized) > effective_max:
            sanitized = sanitized[:effective_max]
            truncated = True

        if pii_detected:
            warnings.append("检测到 PII，已脱敏")
        if injection_detected:
            warnings.append("检测到 prompt injection 痕迹，建议结合 GUID 沙箱使用")

        return SandboxResult(
            sanitized_content=sanitized,
            pii_detected=pii_detected,
            injection_detected=injection_detected,
            truncated=truncated,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _sanitize_pii(self, content: str) -> tuple[str, bool]:
        """PII 脱敏

        复用 memory.manager.sanitize_before_store 做 dict 级脱敏，
        但外部内容通常是纯文本，因此对纯文本做正则脱敏：
        - 手机号 1[3-9]xxxxxxxxx → 1xx****xxxx
        - 身份证 r'\\d{17}[\\dXx]' → 前 6 + **** + 后 4
        - 银行卡 r'\\d{16,19}' → 前 4 + **** + 后 4
        - 邮箱 r'\\S+@\\S+' → 前 2 + ****@域名

        Args:
            content: 原始内容

        Returns:
            (脱敏后内容, 是否检测到 PII)
        """
        pii_found = False
        sanitized = content

        # 手机号：1[3-9]xxxxxxxxx
        phone_pattern = re.compile(r"1[3-9]\d{9}")
        if phone_pattern.search(sanitized):
            pii_found = True
            sanitized = phone_pattern.sub(
                lambda m: m.group()[:3] + "****" + m.group()[-4:],
                sanitized,
            )

        # 身份证：\d{17}[\dXx]（18 位）
        id_pattern = re.compile(r"\d{17}[\dXx]")
        if id_pattern.search(sanitized):
            pii_found = True
            sanitized = id_pattern.sub(
                lambda m: m.group()[:6] + "********" + m.group()[-4:],
                sanitized,
            )

        # 银行卡：\d{16,19}
        card_pattern = re.compile(r"\d{16,19}")
        if card_pattern.search(sanitized):
            pii_found = True
            sanitized = card_pattern.sub(
                lambda m: m.group()[:4] + "****" + m.group()[-4:],
                sanitized,
            )

        # 邮箱
        email_pattern = re.compile(r"(\S{1,2})\S*@(\S+)")
        if email_pattern.search(sanitized) and "@" in sanitized:
            pii_found = True
            sanitized = email_pattern.sub(
                lambda m: m.group(1) + "****@" + m.group(2),
                sanitized,
            )

        return sanitized, pii_found

    def _detect_injection(self, content: str) -> bool:
        """检测 prompt injection 痕迹

        复用 orchestration.nodes.INJECTION_PATTERNS（不重复定义）。

        Args:
            content: 待检测内容

        Returns:
            True 表示检测到注入痕迹
        """
        import re

        try:
            from ..orchestration.nodes import INJECTION_PATTERNS
        except ImportError as e:
            logger.warning("无法导入 INJECTION_PATTERNS，跳过注入检测: %s", e)
            return False

        return any(re.search(pattern, content, re.IGNORECASE) for pattern in INJECTION_PATTERNS)


# =====================================================================
# 全局单例（延迟初始化）
# =====================================================================

_sandbox_instance: ContentSandbox | None = None


def get_content_sandbox() -> ContentSandbox:
    """获取全局 ContentSandbox 单例"""
    global _sandbox_instance
    if _sandbox_instance is None:
        _sandbox_instance = ContentSandbox()
    return _sandbox_instance


def reset_content_sandbox() -> None:
    """重置全局单例（主要用于测试）"""
    global _sandbox_instance
    _sandbox_instance = None
