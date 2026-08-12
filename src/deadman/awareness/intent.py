"""思维意识识别 - 用户意图分类

能力栈中的「思维意识识别」层：理解用户输入的意图（想写遗嘱 / 办手续 /
寻求陪伴 / 管理数字遗产 / 设死人开关 / 写纪念文 / 查知识），为上层路由到
正确的智能体与工具提供依据。

设计要点：
    - 关键词打分为主（确定性、可测试、零依赖、可离线）
    - 可选 LLM 增强：当关键词置信度低时，用 LLM 做意图判定
    - 严守数据纪律：不存储、不推断用户隐私；只做路由用的最短意图标签
    - 与 grief.detect_crisis 互补：本模块管「意图」，grief 管「安全状态」
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class IntentType(str, Enum):
    WILL = "will"  # 遗嘱 / 终活笔记 / 财产分配
    FUNERAL = "funeral"  # 死亡证明 / 销户 / 丧葬费 / 办事流程
    GRIEF = "grief"  # 哀伤陪伴 / 倾诉
    DIGITAL_LEGACY = "digital_legacy"  # 数字遗产 / 账号 / 密码
    DEAD_SWITCH = "dead_switch"  # 死人开关 / 定时投递
    MEMORIAL = "memorial"  # 悼文 / 讣告 / 墓志铭
    KNOWLEDGE = "knowledge"  # 法条 / 政策查询
    GENERAL = "general"  # 兜底


# 每个意图的关键词（按中文常见表达），命中越多置信度越高
_INTENT_KEYWORDS: dict[IntentType, tuple[str, ...]] = {
    IntentType.WILL: (
        "遗嘱",
        "遗书",
        "终活笔记",
        "身后事安排",
        "财产分配",
        "分配财产",
        "立遗嘱",
        "公证遗嘱",
        "自书遗嘱",
        "遗赠",
        "继承安排",
    ),
    IntentType.FUNERAL: (
        "死亡证明",
        "户口注销",
        "销户",
        "丧葬",
        "火化",
        "火葬",
        "殡仪",
        "丧葬费",
        "丧葬补助金",
        "社保丧葬",
        "公积金提取",
        "殡葬",
        "办手续",
        "去世手续",
        "死后怎么办",
        "遗体",
    ),
    IntentType.GRIEF: (
        "好难过",
        "想他",
        "想她",
        "舍不得",
        "思念",
        "崩溃",
        "走不出来",
        "心里空",
        "陪我",
        "倾诉",
        "太想",
        "怀念",
        "难过",
    ),
    IntentType.DIGITAL_LEGACY: (
        "数字遗产",
        "账号",
        "密码",
        "加密货币",
        "虚拟资产",
        "微信账号",
        "支付宝",
        "游戏账号",
        "数字资产",
        "云盘",
        "会员账号",
        "社交账号",
    ),
    IntentType.DEAD_SWITCH: (
        "死人开关",
        "定时",
        "如果我走了",
        "万一我不在",
        "自动发送",
        "定时发送",
        "遗言自动",
        "不在了就",
        "身后自动",
    ),
    IntentType.MEMORIAL: (
        "悼文",
        "讣告",
        "墓志铭",
        "答谢词",
        "追思",
        "纪念文",
        "缅怀",
        "追悼词",
        "生平",
        "纪念文章",
    ),
    IntentType.KNOWLEDGE: (
        "法律",
        "规定",
        "政策",
        "继承法",
        "民法典",
        "可以吗",
        "是否合法",
        "怎么算",
        "有权",
        "法条",
        "条例",
        "规定是",
        "咨询",
    ),
}


@dataclass
class IntentResult:
    intent: IntentType
    confidence: float  # 0~1，关键词命中比例
    scores: dict[str, int]  # 各意图命中分（调试用）

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 3),
            "scores": {k: v for k, v in self.scores.items() if v > 0},
        }


def classify_intent_keyword(text: str) -> IntentResult:
    """关键词打分分类（确定性、可测试）。"""
    t = (text or "").lower()
    scores: dict[str, int] = {}
    for intent, kws in _INTENT_KEYWORDS.items():
        cnt = sum(1 for kw in kws if kw in t)
        if cnt:
            scores[intent.value] = cnt

    if not scores:
        return IntentResult(IntentType.GENERAL, 0.0, scores)

    best = max(scores, key=lambda k: scores[k])
    best_cnt = scores[best]
    total_kw = len(_INTENT_KEYWORDS[IntentType(best)])
    confidence = min(1.0, best_cnt / max(1, total_kw))
    return IntentResult(IntentType(best), confidence, scores)


async def classify_intent(text: str, llm=None) -> IntentResult:
    """意图分类：关键词优先；低置信度时可选 LLM 判定。"""
    kw = classify_intent_keyword(text)
    if kw.intent != IntentType.GENERAL and kw.confidence >= 0.15:
        return kw

    # 兜底：用 LLM 做一次轻量判定（失败则回退关键词）
    if llm is not None:
        try:
            labels = "/".join(i.value for i in IntentType)
            prompt = (
                f"你是意图分类器。把用户的话分到以下之一（只回标签，不要解释）：{labels}\n"
                f"用户：{text}"
            )
            raw = await llm.chat([{"role": "user", "content": prompt}], max_tokens=20)
            label = raw.strip().lower().split()[0].strip(" .,:/")
            for i in IntentType:
                if i.value == label:
                    return IntentResult(i, 0.7, {i.value: 1})
        except Exception:
            pass
    return kw
