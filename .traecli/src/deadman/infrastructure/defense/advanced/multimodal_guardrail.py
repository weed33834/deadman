"""D12:多模态流水线护栏(Multimodal Pipeline Guardrails)。

问题:
    deadman `multimodal/pipeline.py` 现有护栏只覆盖 OCR 输出:
        - PII redaction:仅 `config.pii_redact_ocr=True` 时
        - BudgetCoordinator:post-hoc allocate/release
    漏洞:
        - ASR 转写后:语音含 PII(身份证号念出 / 电话号码)未脱敏
        - TTS 输入:含 PII 的文本被合成成音频,无法事后脱敏
        - Vision 描述:LLM 看图像识别出人脸 / 车牌 / 身份证 → PII 泄漏
        - ImageGen prompt:用户 prompt 含 PII → 生成含 PII 的图像
        - 内容安全:无 toxicity / CSAM / deepfake 检测
        - 输入大小:无音频 / 图像大小上限(DoS / 成本失控)
        - Prompt 注入:Vision / ImageGen 的 prompt 可被注入
        - Budget:post-hoc,无法 pre-check 拒绝超 budget 的请求

缓解:
    - MultimodalGuardrail:统一护栏,覆盖所有模态
    - Pre-check:输入大小 / budget / PII / 内容安全 / prompt 注入
    - Post-process:输出 PII 脱敏 / 内容安全二次检测
    - Audit:每个护栏的检测结果记录(命中数 / 类型)

设计:
    guard = MultimodalGuardrail()
    # 1. 输入预检
    pre = guard.pre_check(
        capability="ocr",
        input_data=image_bytes,
        user_id="u1",
        budget_remaining=0.05,
    )
    if pre.blocked:
        return pre.block_reason
    # 2. 执行 multimodal 调用
    output = pipeline.ocr(...)
    # 3. 输出脱敏
    cleaned = guard.post_process("ocr", output, user_id="u1")
    return cleaned

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


class GuardrailAction(str, Enum):
    """护栏动作。"""

    ALLOW = "allow"  # 放行
    BLOCK = "block"  # 拒绝
    REDACT = "redact"  # 脱敏后放行
    DEGRADE = "degrade"  # 降级处理(如降低分辨率)


@dataclass
class GuardrailDecision:
    """护栏决策。"""

    action: GuardrailAction
    reason: str = ""
    # 输入侧
    input_size_bytes: int = 0
    input_too_large: bool = False
    pii_in_input: bool = False
    pii_in_input_count: int = 0
    prompt_injection_detected: bool = False
    budget_insufficient: bool = False
    # 输出侧(仅 post_process 时填)
    pii_in_output: bool = False
    pii_in_output_count: int = 0
    content_safety_violation: bool = False
    safety_categories: list[str] = field(default_factory=list)


# 模态默认输入大小上限(bytes)
DEFAULT_SIZE_LIMITS = {
    "ocr": 20 * 1024 * 1024,  # 20 MB 图像
    "asr": 25 * 1024 * 1024,  # 25 MB 音频
    "tts": 10_000,  # 10K 字符
    "vision": 20 * 1024 * 1024,  # 20 MB 图像
    "image_gen": 5_000,  # 5K 字符 prompt
}

# Prompt 注入信号
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(previous|above|all)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(previous|prior)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+\w+", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"<\s*\|.*?\|\s*>", re.IGNORECASE),  # 特殊 token
    re.compile(r"\[INST\]|<\|im_start\|>|<\|endoftext\|>", re.IGNORECASE),
]

# 内容安全信号(简化版;production 接入 OpenAI moderation / Perspective API)
_TOXICITY_PATTERNS = [
    re.compile(r"\b(?:暴|杀|血|色情|自杀|自残)\b"),
    re.compile(r"\b(?:rape|kill|suicide|self-?harm)\b", re.IGNORECASE),
]
_CSAM_PATTERNS = [
    re.compile(r"未成年.*(?:裸|性)", re.IGNORECASE),
    re.compile(r"\bminor\s+(?:nude|sexual)", re.IGNORECASE),
]

# 简化 PII 正则
_PII_PATTERNS = {
    "china_id_card": re.compile(r"\b\d{17}[\dXx]\b"),
    "china_phone": re.compile(r"\b1[3-9]\d{9}\b"),
    "email": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "bank_card": re.compile(r"\b\d{16,19}\b"),
}


class MultimodalGuardrail:
    """多模态流水线护栏。

    用法:
        guard = MultimodalGuardrail()
        # 1. 输入预检
        pre = guard.pre_check(
            capability="ocr",
            input_data=image_bytes,
            budget_remaining=0.05,
        )
        if pre.action == GuardrailAction.BLOCK:
            raise ValueError(pre.reason)
        # 2. 执行调用
        output = pipeline.ocr(image_bytes)
        # 3. 输出脱敏
        cleaned, decision = guard.post_process("ocr", output)
        return cleaned
    """

    def __init__(
        self,
        size_limits: dict[str, int] | None = None,
        enable_content_safety: bool = True,
        enable_prompt_injection: bool = True,
    ) -> None:
        self.size_limits = {**DEFAULT_SIZE_LIMITS, **(size_limits or {})}
        self.enable_content_safety = enable_content_safety
        self.enable_prompt_injection = enable_prompt_injection
        self._lock = threading.RLock()
        # 统计
        self._stats = {
            "pre_checks": 0,
            "blocked": 0,
            "redacted": 0,
            "post_processed": 0,
        }

    def pre_check(
        self,
        capability: str,
        input_data: Any,
        *,
        prompt: str | None = None,
        budget_remaining: float | None = None,
        estimated_cost: float | None = None,
        user_id: str = "",
    ) -> GuardrailDecision:
        """输入预检。

        Args:
            capability: ocr / asr / tts / vision / image_gen
            input_data: 输入数据(bytes / str)
            prompt: 文本 prompt(用于 vision / image_gen)
            budget_remaining: 剩余 budget
            estimated_cost: 估算成本
            user_id: 用户 ID(用于审计)

        Returns:
            GuardrailDecision: 决策
        """
        decision = GuardrailDecision(action=GuardrailAction.ALLOW)
        if not is_enabled("defense"):
            return decision

        with self._lock:
            self._stats["pre_checks"] += 1

        # 1. 大小检查
        size = self._measure_size(input_data)
        decision.input_size_bytes = size
        limit = self.size_limits.get(capability, 10 * 1024 * 1024)
        if size > limit:
            decision.input_too_large = True
            decision.action = GuardrailAction.BLOCK
            decision.reason = f"input too large: {size} > {limit} (capability={capability})"
            with self._lock:
                self._stats["blocked"] += 1
            return decision

        # 2. Prompt 注入检测(仅文本输入)
        if prompt and self.enable_prompt_injection:
            for pattern in _INJECTION_PATTERNS:
                if pattern.search(prompt):
                    decision.prompt_injection_detected = True
                    decision.action = GuardrailAction.BLOCK
                    decision.reason = f"prompt injection detected: {pattern.pattern}"
                    with self._lock:
                        self._stats["blocked"] += 1
                    return decision

        # 3. PII 检测(仅文本 / 字符串输入)
        text_input = None
        if isinstance(input_data, str):
            text_input = input_data
        elif prompt:
            text_input = prompt
        if text_input:
            pii_count = self._count_pii(text_input)
            if pii_count > 0:
                decision.pii_in_input = True
                decision.pii_in_input_count = pii_count
                # OCR / ASR 输入含 PII 不可逆(图像 / 音频无法简单脱敏)
                if capability in ("ocr", "asr"):
                    decision.action = GuardrailAction.BLOCK
                    decision.reason = (
                        f"PII in {capability} input cannot be redacted (count={pii_count})"
                    )
                    with self._lock:
                        self._stats["blocked"] += 1
                    return decision
                # TTS / Vision / ImageGen:输入文本可脱敏
                decision.action = GuardrailAction.REDACT
                decision.reason = f"PII in input will be redacted (count={pii_count})"

        # 4. Budget 检查
        if budget_remaining is not None and estimated_cost is not None:
            if estimated_cost > budget_remaining:
                decision.budget_insufficient = True
                decision.action = GuardrailAction.BLOCK
                decision.reason = (
                    f"budget insufficient: cost={estimated_cost} > remaining={budget_remaining}"
                )
                with self._lock:
                    self._stats["blocked"] += 1
                return decision

        return decision

    def post_process(
        self,
        capability: str,
        output: Any,
        *,
        user_id: str = "",
    ) -> tuple[Any, GuardrailDecision]:
        """输出后处理(PII 脱敏 + 内容安全检测)。

        Args:
            capability: ocr / asr / tts / vision / image_gen
            output: 输出数据(文本 / bytes / dict)
            user_id: 用户 ID

        Returns:
            (cleaned_output, decision)
        """
        decision = GuardrailDecision(action=GuardrailAction.ALLOW)
        if not is_enabled("defense"):
            return output, decision

        with self._lock:
            self._stats["post_processed"] += 1

        # 1. PII 脱敏(仅文本输出)
        if isinstance(output, str):
            cleaned, pii_count = self._redact_pii(output)
            if pii_count > 0:
                decision.pii_in_output = True
                decision.pii_in_output_count = pii_count
                with self._lock:
                    self._stats["redacted"] += 1
            # 2. 内容安全检测
            if self.enable_content_safety:
                violation, categories = self._check_content_safety(cleaned)
                if violation:
                    decision.content_safety_violation = True
                    decision.safety_categories = categories
                    decision.action = GuardrailAction.BLOCK
                    decision.reason = f"content safety violation: {categories}"
                    return "[BLOCKED: content safety violation]", decision
            return cleaned, decision
        elif isinstance(output, dict):
            # 递归处理 dict
            cleaned_dict = {}
            for k, v in output.items():
                cleaned_v, sub_decision = self.post_process(capability, v, user_id=user_id)
                if sub_decision.pii_in_output:
                    decision.pii_in_output = True
                    decision.pii_in_output_count += sub_decision.pii_in_output_count
                if sub_decision.content_safety_violation:
                    decision.content_safety_violation = True
                    decision.safety_categories.extend(sub_decision.safety_categories)
                cleaned_dict[k] = cleaned_v
            return cleaned_dict, decision
        elif isinstance(output, list):
            cleaned_list = []
            for item in output:
                cleaned_item, sub_decision = self.post_process(capability, item, user_id=user_id)
                cleaned_list.append(cleaned_item)
                if sub_decision.pii_in_output:
                    decision.pii_in_output = True
                    decision.pii_in_output_count += sub_decision.pii_in_output_count
            return cleaned_list, decision
        # bytes / int / float → 原样返回
        return output, decision

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for k in self._stats:
                self._stats[k] = 0

    # ==================================================================
    # 内部
    # ==================================================================

    @staticmethod
    def _measure_size(input_data: Any) -> int:
        if input_data is None:
            return 0
        if isinstance(input_data, (bytes, bytearray)):
            return len(input_data)
        if isinstance(input_data, str):
            return len(input_data.encode("utf-8"))
        if isinstance(input_data, (list, tuple)):
            return sum(MultimodalGuardrail._measure_size(x) for x in input_data)
        if isinstance(input_data, dict):
            return sum(MultimodalGuardrail._measure_size(v) for v in input_data.values())
        # 其他对象 → 用 repr 长度估算
        return len(repr(input_data))

    @staticmethod
    def _count_pii(text: str) -> int:
        count = 0
        for pattern in _PII_PATTERNS.values():
            count += len(pattern.findall(text))
        return count

    @staticmethod
    def _redact_pii(text: str) -> tuple[str, int]:
        redacted = text
        count = 0
        for pii_type, pattern in _PII_PATTERNS.items():
            matches = pattern.findall(redacted)
            if matches:
                count += len(matches)
                redacted = pattern.sub(f"[REDACTED-PII:{pii_type}]", redacted)
        return redacted, count

    @staticmethod
    def _check_content_safety(text: str) -> tuple[bool, list[str]]:
        violations = []
        for pattern in _CSAM_PATTERNS:
            if pattern.search(text):
                violations.append("csam")
                return True, violations
        for pattern in _TOXICITY_PATTERNS:
            if pattern.search(text):
                violations.append("toxicity")
        return len(violations) > 0, violations


# =====================================================================
# 全局单例
# =====================================================================

_guard_instance: MultimodalGuardrail | None = None
_lock = threading.Lock()


def get_multimodal_guardrail() -> MultimodalGuardrail:
    global _guard_instance
    with _lock:
        if _guard_instance is None:
            _guard_instance = MultimodalGuardrail()
        return _guard_instance


def reset_multimodal_guardrail() -> None:
    global _guard_instance
    with _lock:
        _guard_instance = None
