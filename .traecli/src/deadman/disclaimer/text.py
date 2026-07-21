"""免责告知文本构建器 - 遵守 compliance / service-boundary / transparency / legal-compliance 规则

依据：
- compliance-framework.md（L3 合规边界：四项禁止——代办/代查/法律意见/替代官方）
- service-boundary-framework.md（L3 服务边界：超出范围转介路径）
- legal-compliance-framework.md（L3 法律合规：免责声明统一版 + 告知时机）
- transparency-framework.md（L5 透明度告知：AI 身份 + 能力边界 + 数据使用 + 不确定性）

提供 4 类告知文本：
1. 平台身份告知（开场）
2. 法律意见免责（涉及法律问题时）
3. 代办边界免责（用户要求代办时）
4. 数据准确性免责（涉及具体电话/费用/时限时）
"""

from __future__ import annotations


class DisclaimerBuilder:
    """免责告知文本构建器

    所有文本严格遵守：
    - 不冒充官方机构
    - 不暗示持有任何执照
    - 不夸大能力
    - 关键信息提示以官方确认为准
    """

    # === 1. 平台身份告知（compliance: 不替代官方；transparency: AI 身份告知）===
    PLATFORM_IDENTITY = (
        "本平台是 deadman，AI 身后事信息引导工具。"
        "本平台不销售任何殡葬产品、不与殡仪馆分成、不推荐具体商家。"
    )

    # === 2. 法律意见免责（compliance: 禁止出具法律意见；legal-compliance: 免责声明）===
    LEGAL_DISCLAIMER = (
        "本平台不提供法律意见，不替代律师、公证处、法律援助中心。"
        "涉及继承、遗嘱、财产分配等法律问题，请咨询持牌律师或当地公证处。"
    )

    # === 3. 代办边界免责（compliance: 禁止代办；service-boundary: 转介路径）===
    NO_AGENT_DISCLAIMER = (
        "本平台不代办任何手续。死亡证明请到医院/派出所开具，"
        "火化请直接联系殡仪馆，户口注销请到户籍地派出所。"
    )

    # === 4. 数据准确性免责（retrieval-guardrails: 中可信加注；transparency: 不确定性告知）===
    DATA_ACCURACY_DISCLAIMER = (
        "本平台提供的电话/费用/时限信息基于公开资料整理，可能已变更。"
        "办理前请拨打官方热线核实。"
    )

    # === 场景 -> 简短提醒映射 ===
    _SCENARIO_MAP = {
        "identity": PLATFORM_IDENTITY,
        "legal": LEGAL_DISCLAIMER,
        "agent": NO_AGENT_DISCLAIMER,
        "data": DATA_ACCURACY_DISCLAIMER,
    }

    @classmethod
    def full_opening(cls) -> str:
        """开场完整告知（首次会话用）

        依据 legal-compliance-framework.md 第六节"告知时机"：
        - 首次交互：AI 身份告知 + 免责声明简要版 + 数据使用简要说明
        本方法合并 4 类告知，作为首次会话开场。
        """
        return "\n\n".join([
            cls.PLATFORM_IDENTITY,
            cls.LEGAL_DISCLAIMER,
            cls.NO_AGENT_DISCLAIMER,
            cls.DATA_ACCURACY_DISCLAIMER,
        ])

    @classmethod
    def short_reminder(cls, scenario: str) -> str:
        """场景化简短提醒

        依据 legal-compliance-framework.md 第六节"告知时机"：
        - 关键决策节点：重申免责声明核心
        - 涉及敏感信息时：触发隐私政策要点提示

        scenario 取值：
        - identity: 用户误以为真人/官方时，重申平台身份
        - legal: 涉及继承/遗嘱/财产分配等法律问题时
        - agent: 用户要求代办/代签/代提交时
        - data: 涉及具体电话/费用/时限等可能过时数据时
        """
        if scenario not in cls._SCENARIO_MAP:
            raise ValueError(
                f"未知 scenario: {scenario}，可选值: {sorted(cls._SCENARIO_MAP.keys())}"
            )
        return cls._SCENARIO_MAP[scenario]

    @classmethod
    def for_web_footer(cls) -> str:
        """Web 页面底部固定告知

        依据 transparency-framework.md 第五节"AI 生成内容标注"：
        - 整理性内容应标注"由 AI 整理自公开渠道"
        - 不冒充官方

        简洁版（< 200 字），用于 Web 页面底部固定展示。
        """
        return (
            "本平台是 AI 身后事信息引导工具，不代办任何手续、不出法律意见、不替代官方渠道。"
            "电话/费用/时限等信息整理自公开资料，可能已变更，办理前请拨打 12345 或当地官方热线核实。"
            "涉及法律/财务/医疗决策，请咨询持牌专业人士。"
        )
