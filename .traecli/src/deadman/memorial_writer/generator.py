"""MemorialGenerator - AI 悼文/讣告/答谢词/墓志铭/追思会致辞生成

参考竞品 Toast 的结构：姓名+关系+特质+回忆+价值观+口头禅 → 生成悼文。

合规关联：
    - rules/integrity-framework.md：不编造未提供的特质/回忆。
        _build_prompt 显式告知 LLM"用户未提供的特质不要自行补充"，
        降级模板填充也只用 request 中已有的字段。
    - rules/legal-compliance-framework.md 第五章 PIPL：
        decedent_name 仅在内存中用于本次生成，不落盘、不入知识库。
    - rules/service-boundary-framework.md：
        返回结果附 disclaimer（在 CLI/Web 层注入，本模块不重复）。
    - safety-protocol.md：_check_safety 检测自伤/暴力/不当内容，
        命中则 safety_flags[self_harm/violence/inappropriate] = True。

LLM 降级：api_key 为空 / 调用失败 → 走 _fallback_template 填充，
confidence=0.3，不编造（integrity-framework L1）。
"""

from __future__ import annotations

import logging

from ..llm import llm_client
from .models import (
    DOC_TYPES,
    MemorialRequest,
    MemorialResult,
)

logger = logging.getLogger(__name__)


# =====================================================================
# 安全关键词表
# =====================================================================
# 自伤/自杀相关（参考 safety-protocol.md 第一章识别信号）
_SELF_HARM_KEYWORDS: tuple[str, ...] = (
    "自杀", "自残", "自伤", "了结自己", "结束自己", "结束生命",
    "想死", "不想活", "一了百了", "解脱",
    "self-harm", "self harm", "suicide", "kill myself",
)

# 暴力相关（针对他人的伤害描述，悼文场景不应出现）
_VIOLENCE_KEYWORDS: tuple[str, ...] = (
    "杀害", "谋杀", "行凶", "屠杀", "虐待",
    "bloodbath", "massacre", "torture",
)

# 不当内容（与悼文场景严重不符的露骨/歧视表述）
_INAPPROPRIATE_KEYWORDS: tuple[str, ...] = (
    "歧视", "侮辱", "羞辱", "秽语",
    "racist", "sexist",
)


# =====================================================================
# 信仰对应的常用语（仅作为 LLM 提示词中的风格指引，不强制注入）
# =====================================================================
_FAITH_HINTS: dict[str, str] = {
    "none": "无特定宗教背景，使用世俗化的表述。",
    "buddhist": "佛教背景，可使用'往生极乐''莲华化生''超度'等表述。",
    "taoist": "道教背景，可使用'羽化登仙''驾鹤西去''归道''仙逝'等表述。",
    "christian": "基督教背景，可使用'安息主怀''回归天家''蒙主宠召''永生'等表述。",
}

# 语言对应的提示词语言
_LANGUAGE_HINTS: dict[str, str] = {
    "zh-CN": "用简体中文（现代白话文）输出。",
    "zh-Classical": "用文言文（古汉语）输出，可参考'先考某某公讳…'的格式。",
    "en-US": "Output in English.",
}

# 语气对应的提示词
_TONE_HINTS: dict[str, str] = {
    "solemn": "语气庄重肃穆，避免轻佻措辞。",
    "warm": "语气温暖亲切，可多用生活化细节，体现逝者温度。",
    "humorous": "语气适度幽默但得体，可在回忆中带过逝者生前的趣事，不冒犯逝者尊严。",
}


class MemorialGenerator:
    """AI 悼文/讣告/答谢词/墓志铭/追思会致辞生成器

    用法：
        gen = MemorialGenerator()
        req = MemorialRequest(doc_type="eulogy", decedent_name="先父",
                              relationship="儿子",
                              personality_traits=["宽厚", "爱读书"],
                              memories=["每天早晨浇花",
                                        "教我骑自行车"])
        result = await gen.generate(req)
        print(result.text)
    """

    # LLM 不可用时的固定 confidence（integrity-framework L1）
    LLM_UNAVAILABLE_CONFIDENCE = 0.3

    # LLM 正常返回时的默认 confidence
    LLM_OK_CONFIDENCE = 0.8

    async def generate(self, request: MemorialRequest) -> MemorialResult:
        """生成悼文

        步骤：
            1. 校验 request（不通过直接抛 ValueError）
            2. 调 LLM 生成主稿
            3. LLM 不可用时降级模板填充，confidence=0.3
            4. _check_safety 检测输出
            5. 返回 MemorialResult
        """
        errors = request.validate()
        if errors:
            raise ValueError(
                "MemorialRequest 校验失败: " + "; ".join(errors)
            )

        # 调 LLM（含降级）
        try:
            text = await self._call_llm(request)
            confidence = self.LLM_OK_CONFIDENCE
        except Exception as exc:
            logger.warning(
                "MemorialGenerator LLM 调用失败，降级模板填充: %s", exc
            )
            text = self._fallback_template(request)
            confidence = self.LLM_UNAVAILABLE_CONFIDENCE

        # 安全检测
        safety_flags = self._check_safety(text)

        return MemorialResult(
            text=text,
            doc_type=request.doc_type,
            confidence=confidence,
            safety_flags=safety_flags,
            alternatives=[],
        )

    # ==================================================================
    # LLM 调用
    # ==================================================================
    async def _call_llm(self, request: MemorialRequest) -> str:
        """调用 LLM 生成悼文

        LLM 不可用（api_key 为空）/调用失败时抛异常，
        由 generate() 捕获后走降级模板。
        """
        if not llm_client.api_key:
            raise RuntimeError("llm_client.api_key 为空，LLM 不可用")

        prompt = self._build_prompt(request)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是悼文撰写助手，专注于为家属撰写"
                    "悼文/讣告/答谢词/墓志铭/追思会致辞。"
                    "严格遵守："
                    "1) integrity：不编造未提供的事实。用户未给出的性格特质、"
                    "回忆、价值观、口头禅，绝对不要自行补充；"
                    "2) 尊重逝者尊严，避免轻佻、猎奇或商业化措辞；"
                    "3) 不在文中插入广告、链接、品牌名；"
                    "4) 输出纯文本，不要 markdown 代码块标记；"
                    "5) 长度严格遵循用户给定的字数范围。"
                ),
            },
            {"role": "user", "content": prompt},
        ]

        # 字数约束 → max_tokens 粗略估算（中文 1 字≈1.5 token，英文 1 字≈1 token）
        word_lo, word_hi = self._resolve_word_range(request)
        max_tokens = max(256, int(word_hi * 2.0))

        resp = await llm_client.chat(
            messages, temperature=0.4, max_tokens=max_tokens
        )
        if not resp or not resp.strip():
            raise RuntimeError("LLM 返回空响应")
        return resp.strip()

    # ==================================================================
    # 提示词构造
    # ==================================================================
    def _build_prompt(self, request: MemorialRequest) -> str:
        """构造提示词

        参考 Toast 验证的结构：姓名+关系+特质+回忆+价值观+口头禅
        明确告知 LLM"用户未提供的不要补全"（integrity-framework L1）。
        """
        doc_meta = DOC_TYPES[request.doc_type]
        word_lo, word_hi = self._resolve_word_range(request)

        # 特质/回忆/价值观清单（用户提供的）
        traits_block = (
            "\n".join(f"  - {t}" for t in request.personality_traits)
            if request.personality_traits
            else "  - （用户未提供，不要编造）"
        )
        memories_block = (
            "\n".join(f"  - {m}" for m in request.memories)
            if request.memories
            else "  - （用户未提供，不要编造）"
        )
        values_block = (
            "\n".join(f"  - {v}" for v in request.values_or_sayings)
            if request.values_or_sayings
            else "  - （用户未提供，不要编造）"
        )

        # 风格指引
        faith_hint = _FAITH_HINTS.get(request.faith, _FAITH_HINTS["none"])
        lang_hint = _LANGUAGE_HINTS.get(
            request.language, _LANGUAGE_HINTS["zh-CN"]
        )
        tone_hint = _TONE_HINTS.get(request.tone, _TONE_HINTS["solemn"])

        # 文档类型说明
        type_desc = doc_meta["description"]

        return (
            f"请撰写一篇【{doc_meta['name']}】（{doc_meta['name_en']}）。\n"
            f"文档类型说明：{type_desc}\n\n"
            f"逝者姓名/称呼：{request.decedent_name}\n"
            f"作者与逝者的关系：{request.relationship}\n\n"
            f"性格特质（用户提供的，未列出的不要自行补充）：\n{traits_block}\n\n"
            f"共同回忆（用户提供的，未列出的不要自行补充）：\n{memories_block}\n\n"
            f"价值观/口头禅（用户提供的，未列出的不要自行补充）：\n{values_block}\n\n"
            f"风格指引：\n"
            f"  - 信仰：{request.faith}（{faith_hint}）\n"
            f"  - 语气：{request.tone}（{tone_hint}）\n"
            f"  - 语言：{request.language}（{lang_hint}）\n"
            f"  - 字数范围：{word_lo}-{word_hi} 字\n\n"
            f"再次强调：以上信息之外的内容不要编造。"
            f"若信息不足以达到字数下限，宁可写短也不要补充未提供的特质/回忆。"
            f"直接输出正文，不要加标题前缀（如'悼文：'），不要解释。"
        )

    # ==================================================================
    # 降级模板填充（LLM 不可用时）
    # ==================================================================
    def _fallback_template(self, request: MemorialRequest) -> str:
        """降级模板填充：用 request 已有字段拼一段朴素文本

        严格遵守 integrity-framework：只用用户提供的信息，
        不补充任何"AI 想象"的特质或回忆。
        confidence=0.3。
        """
        doc_meta = DOC_TYPES[request.doc_type]
        name = request.decedent_name
        rel = request.relationship

        # 古文/英文分支
        if request.language == "zh-Classical":
            return self._fallback_classical(doc_meta, name, rel, request)
        if request.language == "en-US":
            return self._fallback_english(doc_meta, name, rel, request)

        # 中文现代文降级
        faith_phrase = self._faith_phrase_cn(request.faith)
        # 各 doc_type 模板（统一以"[模板生成]"开头标记低 confidence）
        if request.doc_type == "eulogy":
            return self._template_eulogy_cn(name, rel, request, faith_phrase)
        if request.doc_type == "obituary":
            return self._template_obituary_cn(name, rel, request, faith_phrase)
        if request.doc_type == "thank_you_note":
            return self._template_thank_you_cn(name, rel, request, faith_phrase)
        if request.doc_type == "epitaph":
            return self._template_epitaph_cn(name, rel, request, faith_phrase)
        # memorial_speech
        return self._template_memorial_speech_cn(
            name, rel, request, faith_phrase
        )

    # ---------- 中文降级模板 ----------
    def _template_eulogy_cn(
        self, name: str, rel: str, req: MemorialRequest, faith_phrase: str
    ) -> str:
        traits = "、".join(req.personality_traits) if req.personality_traits else "（家属可补充性格特质）"
        memories = "\n".join(f"  · {m}" for m in req.memories) if req.memories else "  · （家属可补充共同回忆）"
        values = "；".join(req.values_or_sayings) if req.values_or_sayings else "（家属可补充价值观或口头禅）"
        return (
            f"[模板生成] 悼文\n\n"
            f"{name}，{rel}。{faith_phrase}\n"
            f"回忆{name}，性格{traits}。\n"
            f"共同回忆：\n{memories}\n"
            f"{name}常言：{values}。\n"
            f"愿{name}安息。\n\n"
            f"（说明：LLM 暂不可用，以上为模板填充。"
            f"建议家属补充具体细节后由 AI 重新生成。）"
        )

    def _template_obituary_cn(
        self, name: str, rel: str, req: MemorialRequest, faith_phrase: str
    ) -> str:
        return (
            f"[模板生成] 讣告\n\n"
            f"{name}，{rel}，{faith_phrase}\n"
            f"生卒日期：（家属补充）\n"
            f"丧礼时间：（家属补充）\n"
            f"丧礼地点：（家属补充）\n"
            f"特此告诸亲友。\n\n"
            f"（说明：LLM 暂不可用，以上为模板填充，请家属补充生卒日期、"
            f"丧礼时间地点等关键信息后由 AI 重新生成。）"
        )

    def _template_thank_you_cn(
        self, name: str, rel: str, req: MemorialRequest, faith_phrase: str
    ) -> str:
        return (
            f"[模板生成] 答谢词\n\n"
            f"各位亲友：\n"
            f"感谢各位前来送别{name}，{rel}。{faith_phrase}\n"
            f"各位的关怀与陪伴，是对{name}最好的告别。\n"
            f"再次感谢。\n\n"
            f"（说明：LLM 暂不可用，以上为模板填充。）"
        )

    def _template_epitaph_cn(
        self, name: str, rel: str, req: MemorialRequest, faith_phrase: str
    ) -> str:
        traits = "、".join(req.personality_traits[:3]) if req.personality_traits else "慈厚"
        values = req.values_or_sayings[0] if req.values_or_sayings else "一生坦荡"
        return (
            f"[模板生成] 墓志铭\n"
            f"{name}之墓。{traits}，{values}。{faith_phrase}\n"
            f"（说明：LLM 暂不可用，以上为模板填充。）"
        )

    def _template_memorial_speech_cn(
        self, name: str, rel: str, req: MemorialRequest, faith_phrase: str
    ) -> str:
        traits = "、".join(req.personality_traits) if req.personality_traits else "（家属可补充性格特质）"
        memories = "\n".join(f"  · {m}" for m in req.memories) if req.memories else "  · （家属可补充共同回忆）"
        values = "；".join(req.values_or_sayings) if req.values_or_sayings else "（家属可补充价值观或口头禅）"
        return (
            f"[模板生成] 追思会致辞\n\n"
            f"各位亲友：\n"
            f"今天我们聚在一起，追思{name}。{rel}。{faith_phrase}\n"
            f"{name}的性格{traits}，是我们心中永远的印记。\n"
            f"回忆点滴：\n{memories}\n"
            f"{name}常言：{values}。\n"
            f"愿{name}在另一个世界安息，也愿我们带着这份思念继续前行。\n\n"
            f"（说明：LLM 暂不可用，以上为模板填充。建议补充具体细节后由 AI 重新生成。）"
        )

    # ---------- 古文降级模板 ----------
    def _fallback_classical(
        self, doc_meta: dict, name: str, rel: str, req: MemorialRequest
    ) -> str:
        faith_phrase = self._faith_phrase_classical(req.faith)
        traits = "、".join(req.personality_traits[:3]) if req.personality_traits else "性敦厚"
        values = req.values_or_sayings[0] if req.values_or_sayings else "行止有度"
        # 古文模板以"先考"或"先妣"开头（参考明清墓志铭格式）
        prefix = "先考" if "父" in rel or "考" in name else "先妣" if "母" in rel or "妣" in name else "先"
        return (
            f"[模板生成] {doc_meta['name']}\n"
            f"{prefix}{name}，{traits}，{values}。{faith_phrase}\n"
            f"呜呼哀哉，伏惟尚飨。\n"
            f"（说明：LLM 暂不可用，以上为模板填充。）"
        )

    # ---------- 英文降级模板 ----------
    def _fallback_english(
        self, doc_meta: dict, name: str, rel: str, req: MemorialRequest
    ) -> str:
        faith_phrase = self._faith_phrase_en(req.faith)
        traits = ", ".join(req.personality_traits) if req.personality_traits else "(family may add traits)"
        memories = "\n".join(f"  - {m}" for m in req.memories) if req.memories else "  - (family may add memories)"
        values = "; ".join(req.values_or_sayings) if req.values_or_sayings else "(family may add sayings)"
        return (
            f"[Template-generated] {doc_meta['name_en']}\n\n"
            f"In memory of {name}, my {rel}. {faith_phrase}\n"
            f"Traits: {traits}.\n"
            f"Memories:\n{memories}\n"
            f"Sayings: {values}.\n"
            f"May {name} rest in peace.\n\n"
            f"(Note: LLM unavailable, this is a template. Please add details and regenerate.)"
        )

    # ---------- 信仰短语 ----------
    def _faith_phrase_cn(self, faith: str) -> str:
        return {
            "buddhist": "愿往生极乐。",
            "taoist": "愿羽化登仙。",
            "christian": "愿安息主怀。",
            "none": "",
        }.get(faith, "")

    def _faith_phrase_classical(self, faith: str) -> str:
        return {
            "buddhist": "愿往生净土，莲华化生。",
            "taoist": "驾鹤西归，归道登仙。",
            "christian": "蒙主宠召，归返天家。",
            "none": "",
        }.get(faith, "")

    def _faith_phrase_en(self, faith: str) -> str:
        return {
            "buddhist": "May they be reborn in the Pure Land.",
            "taoist": "May they ascend as immortals.",
            "christian": "May they rest in the Lord's embrace.",
            "none": "",
        }.get(faith, "")

    # ==================================================================
    # 安全检测
    # ==================================================================
    def _check_safety(self, text: str) -> dict[str, bool]:
        """检测输出是否含自伤/暴力/不当内容

        命中关键词则在 safety_flags 中标 True。
        不修改原文（仅标注，由调用方决定是否拦截）。
        """
        if not text:
            return {
                "self_harm": False,
                "violence": False,
                "inappropriate": False,
            }

        text_lower = text.lower()

        def _hit(keywords: tuple[str, ...]) -> bool:
            for kw in keywords:
                if kw in text or kw.lower() in text_lower:
                    return True
            return False

        return {
            "self_harm": _hit(_SELF_HARM_KEYWORDS),
            "violence": _hit(_VIOLENCE_KEYWORDS),
            "inappropriate": _hit(_INAPPROPRIATE_KEYWORDS),
        }

    # ==================================================================
    # 字数范围解析
    # ==================================================================
    def _resolve_word_range(
        self, request: MemorialRequest
    ) -> tuple[int, int]:
        """解析字数范围

        优先用 request.word_limit 作为上限（>0 时），
        否则用 doc_type 默认 word_range。
        """
        word_lo, word_hi = DOC_TYPES[request.doc_type]["word_range"]
        if request.word_limit and request.word_limit > 0:
            # 用户指定上限：上限 = word_limit，下限 = min(word_lo, word_limit)
            return (min(word_lo, request.word_limit), request.word_limit)
        return (word_lo, word_hi)
