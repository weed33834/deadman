"""Phase 15: 通知信函生成器

LetterGenerator 类：
    - generate(request) -> LetterResult
    - 优先用模板填充（confidence=0.7）
    - 可选调 LLM 优化语气（confidence=0.9），但模板填充的内容不编造
    - _extract_placeholders(template) 提取所有 [xxx] 占位符
    - _mask_pii(text) 脱敏身份证/手机/银行账号

合规关联：
    - rules/integrity-framework.md：LLM 调用只优化语气，不补全未提供的字段
    - rules/service-boundary-framework.md：附 disclaimer，明确"仅为草稿"
    - rules/legal-compliance-framework.md 第五章 PIPL：
        调用方传入的 decedent_id_masked 必须已脱敏；
        生成器对生成的文本二次扫描 PII 并脱敏（防御性）
    - LLM 不可用时降级为纯模板填充，confidence=0.3
"""

from __future__ import annotations

import logging
import re

from .models import (
    DEFAULT_DISCLAIMER,
    LetterRequest,
    LetterResult,
)
from .templates import LETTER_TEMPLATES, LETTER_TYPES

logger = logging.getLogger(__name__)


# ====================================================================
# 占位符正则：匹配 [xxx]，xxx 不含 [ ] 且至少 1 个字符
# ====================================================================
_PLACEHOLDER_RE = re.compile(r"\[([^\[\]]+)\]")


# ====================================================================
# PII 正则（与 doc_extract/extractor.py 一致）
# ====================================================================
# 身份证号 18 位
_ID_CARD_RE = re.compile(r"\b(\d{6})\d{8}(\d{3}[\dXx])\b")
# 手机号 11 位（1 开头）
_PHONE_RE = re.compile(r"\b(1[3-9]\d)\d{4}(\d{4})\b")
# 银行账号 16-19 位连续数字
_BANK_RE = re.compile(r"\b\d{16,19}\b")


# ====================================================================
# LetterGenerator
# ====================================================================
class LetterGenerator:
    """通知信函生成器

    生成流程：
        1. 取模板（LETTER_TEMPLATES[letter_type]）
        2. 把 LetterRequest + extra_fields 中提供的字段填入 {xxx} 占位
           （缺失的 {xxx} 转为 [xxx] 占位符留给用户填）
        3. 提取所有 [xxx] 占位符放入 placeholders 列表
        4. 对生成文本做 PII 二次脱敏（防御性）
        5. 可选：调 LLM 优化语气（confidence 0.7 → 0.9）
           LLM 不可用：confidence=0.3

    重要：LLM 仅优化语气（更正式/更通顺），不补全字段、不编造事实。
    """

    # LLM 不可用时的固定 confidence
    LLM_UNAVAILABLE_CONFIDENCE = 0.3
    # 纯模板填充 confidence
    TEMPLATE_ONLY_CONFIDENCE = 0.7
    # 模板 + LLM 优化 confidence
    LLM_OPTIMIZED_CONFIDENCE = 0.9

    def __init__(self, use_llm: bool = False) -> None:
        """初始化生成器

        Args:
            use_llm: 是否启用 LLM 语气优化。默认 False（纯模板）。
                     即使 True，若 llm_client 不可用也会降级。
        """
        self.use_llm = use_llm

    # ==================================================================
    # 主入口
    # ==================================================================
    def generate(self, request: LetterRequest) -> LetterResult:
        """生成通知信函

        Args:
            request: LetterRequest，letter_type 必须是 8 类之一

        Returns:
            LetterResult
        """
        if request.letter_type not in LETTER_TEMPLATES:
            raise ValueError(
                f"未知信函类型: {request.letter_type}，"
                f"支持类型: {list(LETTER_TEMPLATES.keys())}"
            )

        template = LETTER_TEMPLATES[request.letter_type]

        # 1. 收集所有可填字段（通用 + extra_fields）
        fill_values = self._collect_fill_values(request)

        # 2. 填充模板（缺失字段自动转为 [xxx] 占位符）
        filled_text = self._fill_template(template, fill_values)

        # 3. 提取 [xxx] 占位符（用于 placeholders 列表）
        placeholders = self._extract_placeholders(filled_text)

        # 4. PII 二次脱敏（防御性）
        masked_text = self._mask_pii(filled_text)

        # 5. 决定 confidence
        if not self.use_llm:
            confidence = self.TEMPLATE_ONLY_CONFIDENCE
            final_text = masked_text
        else:
            optimized, ok = self._optimize_with_llm(
                masked_text, request.letter_type
            )
            if ok:
                confidence = self.LLM_OPTIMIZED_CONFIDENCE
                final_text = self._mask_pii(optimized)
            else:
                # LLM 不可用 → 降级 confidence=0.3
                confidence = self.LLM_UNAVAILABLE_CONFIDENCE
                final_text = masked_text

        return LetterResult(
            text=final_text,
            letter_type=request.letter_type,
            confidence=confidence,
            placeholders=placeholders,
            disclaimer=DEFAULT_DISCLAIMER,
        )

    # ==================================================================
    # 字段收集
    # ==================================================================
    @staticmethod
    def _collect_fill_values(request: LetterRequest) -> dict[str, str]:
        """收集所有可填字段

        来源：
            - LetterRequest 的通用字段
            - request.extra_fields 的类型特定字段
        所有值统一转为字符串（None/空 → 空字符串，由 _fill_template 处理为占位符）
        """
        values: dict[str, str] = {
            "decedent_name": str(request.decedent_name or ""),
            "decedent_id_masked": str(request.decedent_id_masked or ""),
            "death_date": str(request.death_date or ""),
            "applicant_name": str(request.applicant_name or ""),
            "applicant_relationship": str(request.applicant_relationship or ""),
            "recipient_org": str(request.recipient_org or ""),
        }
        # extra_fields 全部转字符串
        for k, v in (request.extra_fields or {}).items():
            values[str(k)] = "" if v is None else str(v)
        return values

    # ==================================================================
    # 模板填充
    # ==================================================================
    @staticmethod
    def _fill_template(template: str, values: dict[str, str]) -> str:
        """填充模板

        规则：
            - 模板中 {xxx} 形式的占位符被替换为 values[xxx]（如有）
            - 若 values 中没有 xxx 或值为空字符串 → 替换为 [xxx] 占位符
              （让用户后续手动填写）
            - 模板中原本就有的 [xxx] 方括号占位符原样保留

        实现用 re.sub + 自定义 replacer，避免 str.format 抛 KeyError。
        """
        def _replacer(m: re.Match) -> str:
            key = m.group(1)
            val = values.get(key, "")
            if val:
                return val
            # 缺失值 → 转为 [xxx] 占位符
            return f"[{key}]"

        # 匹配 {xxx}，xxx 不含 { } 且至少 1 个字符
        return re.sub(r"\{([^\{\}]+)\}", _replacer, template)

    # ==================================================================
    # 占位符提取
    # ==================================================================
    @staticmethod
    def _extract_placeholders(text: str) -> list[str]:
        """提取所有 [xxx] 占位符（去重保序）

        Args:
            text: 已填充的模板文本

        Returns:
            占位符列表，如 ["[申请人身份证号]", "[申请日期]"]
            顺序按文本中出现顺序，去重
        """
        seen: set[str] = set()
        result: list[str] = []
        for m in _PLACEHOLDER_RE.finditer(text):
            placeholder = f"[{m.group(1)}]"
            if placeholder not in seen:
                seen.add(placeholder)
                result.append(placeholder)
        return result

    # ==================================================================
    # PII 脱敏（防御性）
    # ==================================================================
    @staticmethod
    def _mask_pii(text: str) -> str:
        """PII 二次脱敏

        规则：
            - 身份证号 18 位 → 前 6 + ******** + 后 4
            - 手机号 11 位 → 前 3 + **** + 后 4
            - 银行账号 16-19 位 → 前 4 + **** + 后 4

        注：调用方传入的 decedent_id_masked 已脱敏，
            本方法对生成文本做兜底扫描，防止 LLM 输出意外 PII。
        """
        if not text:
            return ""

        # 身份证号
        text = _ID_CARD_RE.sub(
            lambda m: f"{m.group(1)}********{m.group(2)}", text
        )
        # 手机号
        text = _PHONE_RE.sub(
            lambda m: f"{m.group(1)}****{m.group(2)}", text
        )
        # 银行账号
        def _mask_bank(m: re.Match) -> str:
            digits = m.group(0)
            return f"{digits[:4]}{'*' * (len(digits) - 8)}{digits[-4:]}"

        text = _BANK_RE.sub(_mask_bank, text)
        return text

    # ==================================================================
    # LLM 语气优化（可选）
    # ==================================================================
    def _optimize_with_llm(
        self, text: str, letter_type: str
    ) -> tuple[str, bool]:
        """调 LLM 优化信函语气

        约束（integrity-framework.md）：
            - LLM 只优化语气、用词、行文通顺度
            - 不编造事实，不补全未提供的字段
            - 占位符 [xxx] 必须原样保留
            - 不添加电话/地址等具体信息

        Args:
            text: 已填充并脱敏的信函文本
            letter_type: 信函类型

        Returns:
            (optimized_text, success)
            success=False 表示 LLM 不可用或调用失败，调用方应降级
        """
        try:
            from ..llm import llm_client
        except Exception as exc:
            logger.warning(
                "LetterGenerator: 无法导入 llm_client: %s", exc
            )
            return text, False

        if not getattr(llm_client, "api_key", ""):
            return text, False

        prompt = self._build_llm_prompt(text, letter_type)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是中文公文写作助手，专注于优化通知信函的语气和格式。"
                    "严格遵守："
                    "1) 不编造任何事实，不补全未提供的字段；"
                    "2) 所有 [xxx] 方括号占位符必须原样保留，由用户手动填写；"
                    "3) 不添加电话号码、地址、网址等具体联系信息；"
                    "4) 只调整措辞、行文流畅度、公文格式；"
                    "5) 输出完整信函文本，不带任何解释或前后缀。"
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            import asyncio

            resp = asyncio.run(
                llm_client.chat(messages, temperature=0.2, max_tokens=2048)
            )
        except Exception as exc:
            logger.warning(
                "LetterGenerator: LLM 调用失败: %s", exc
            )
            return text, False

        if not resp or not isinstance(resp, str) or not resp.strip():
            return text, False

        # 验证：LLM 输出不能比原文短太多（防止被截断）
        if len(resp.strip()) < len(text) * 0.5:
            logger.warning(
                "LetterGenerator: LLM 响应过短，疑似截断，降级使用原模板"
            )
            return text, False

        # 验证：占位符不能丢失（integrity-framework：不补全未提供字段）
        original_placeholders = set(_PLACEHOLDER_RE.findall(text))
        new_placeholders = set(_PLACEHOLDER_RE.findall(resp))
        missing = original_placeholders - new_placeholders
        if missing:
            logger.warning(
                "LetterGenerator: LLM 输出丢失占位符 %s，降级使用原模板",
                missing,
            )
            return text, False

        return resp.strip(), True

    @staticmethod
    def _build_llm_prompt(text: str, letter_type: str) -> str:
        """构造 LLM 提示词"""
        type_meta = next(
            (t for t in LETTER_TYPES if t["type"] == letter_type), None
        )
        type_name = type_meta["name"] if type_meta else letter_type
        return (
            f"以下是【{type_name}】信函草稿，已用模板填充关键字段。\n"
            f"请优化其语气和行文，使其更符合中国公文/正式信函的写法习惯，"
            f"但不要补全任何 [xxx] 占位符所代表的字段（这些字段需要用户手动填写）。\n\n"
            f"--- 信函草稿 ---\n{text}\n--- 信函草稿结束 ---\n\n"
            f"请直接输出优化后的完整信函文本，不要加任何解释。"
        )
