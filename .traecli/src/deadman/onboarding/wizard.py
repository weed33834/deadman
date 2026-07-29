"""Onboarding 引导逻辑 - Phase 16C

OnboardingWizard 提供 5 步引导：
1. relationship：与逝者的关系
2. location：所在地点
3. death_date：逝者去世日期
4. current_stage：当前办理进度
5. consent：免责声明同意

每步返回 {step, key, question, type, options/placeholder}
validate_answer 返回 (ok, error_msg)
save_profile 将收集到的 answers 转为 OnboardingProfile 并持久化
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .models import OnboardingProfile
from .store import OnboardingStore


# 省份列表（用于 location 下拉）
_PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古",
    "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "广西", "海南",
    "重庆", "四川", "贵州", "云南", "西藏",
    "陕西", "甘肃", "青海", "宁夏", "新疆",
    "香港", "澳门", "台湾",
    "海外",
]

# 办理阶段选项（参考 skills/death-aftercare-guide 划分）
_STAGE_OPTIONS = [
    "尚未开始",
    "死亡证明",
    "遗体处理",
    "户口注销",
    "数字账户",
    "金融资产",
    "不动产",
    "遗产继承",
    "社保抚恤",
    "债务处理",
    "已完成",
]


class OnboardingWizard:
    """5 步引导向导

    使用方式：
        wiz = OnboardingWizard()
        step = wiz.get_step(0)  # 第一步问题
        ok, err = wiz.validate_answer(0, "亲属")
        if ok:
            answers["relationship"] = "亲属"
        ...
        profile = wiz.save_profile(user_id, answers)
    """

    STEPS = ["relationship", "location", "death_date", "current_stage", "consent"]
    TOTAL_STEPS = 5

    def __init__(self, store: OnboardingStore | None = None) -> None:
        self._store = store

    # ============================================================
    # 步骤定义
    # ============================================================

    def get_step(self, step_index: int) -> dict[str, Any]:
        """返回第 N 步的问题与选项

        每步返回字段：
        - step：步骤序号（0-based）
        - key：步骤键（relationship / location / ...）
        - question：问题文本
        - type：select / multiselect / date / checkbox
        - options：选项列表（仅 select / multiselect 用）
        - placeholder：占位提示
        - required：是否必填
        """
        if step_index < 0 or step_index >= self.TOTAL_STEPS:
            raise ValueError(
                f"step_index 超出范围 [0, {self.TOTAL_STEPS - 1}]，收到: {step_index}"
            )

        key = self.STEPS[step_index]
        if key == "relationship":
            return {
                "step": step_index,
                "key": "relationship",
                "question": "您与逝者的关系是？",
                "type": "select",
                "options": ["亲属", "朋友", "本人", "其他"],
                "placeholder": "请选择关系",
                "required": True,
            }
        if key == "location":
            return {
                "step": step_index,
                "key": "location",
                "question": "您所在的省份/地区？",
                "type": "select",
                "options": _PROVINCES,
                "placeholder": "请选择省份",
                "required": True,
            }
        if key == "death_date":
            return {
                "step": step_index,
                "key": "death_date",
                "question": "逝者去世日期？（如选择「本人」可跳过）",
                "type": "date",
                "placeholder": "YYYY-MM-DD",
                "required": False,
                "skippable_when": "relationship=本人",
            }
        if key == "current_stage":
            return {
                "step": step_index,
                "key": "current_stage",
                "question": "您目前已办理到哪些阶段？（可多选）",
                "type": "multiselect",
                "options": _STAGE_OPTIONS,
                "placeholder": "可跳过",
                "required": False,
            }
        if key == "consent":
            return {
                "step": step_index,
                "key": "consent",
                "question": "我已阅读并同意《用户协议》和《隐私政策》",
                "type": "checkbox",
                "links": ["/terms", "/privacy"],
                "required": True,
            }
        # 不会到达
        raise ValueError(f"未知 step key: {key}")

    # ============================================================
    # 校验
    # ============================================================

    def validate_answer(self, step_index: int, answer: Any) -> tuple[bool, str]:
        """校验答案

        返回 (ok, error_msg)。ok=True 时 error_msg 为空字符串。
        """
        if step_index < 0 or step_index >= self.TOTAL_STEPS:
            return False, f"step_index 超出范围 [0, {self.TOTAL_STEPS - 1}]"

        key = self.STEPS[step_index]
        if key == "relationship":
            return self._validate_relationship(answer)
        if key == "location":
            return self._validate_location(answer)
        if key == "death_date":
            return self._validate_death_date(answer)
        if key == "current_stage":
            return self._validate_current_stage(answer)
        if key == "consent":
            return self._validate_consent(answer)
        return False, f"未知 step key: {key}"

    def _validate_relationship(self, answer: Any) -> tuple[bool, str]:
        if not isinstance(answer, str) or not answer.strip():
            return False, "请选择关系"
        if answer not in {"亲属", "朋友", "本人", "其他"}:
            return False, "关系必须是 亲属 / 朋友 / 本人 / 其他 之一"
        return True, ""

    def _validate_location(self, answer: Any) -> tuple[bool, str]:
        if not isinstance(answer, str) or not answer.strip():
            return False, "请选择省份"
        if answer not in _PROVINCES:
            return False, "省份不在支持列表内"
        return True, ""

    def _validate_death_date(self, answer: Any) -> tuple[bool, str]:
        # 可空（本人场景）
        if answer is None or answer == "":
            return True, ""
        if not isinstance(answer, str):
            return False, "death_date 必须是字符串（YYYY-MM-DD）"
        try:
            d = datetime.fromisoformat(answer).date()
        except (TypeError, ValueError):
            return False, "日期格式不正确，应为 YYYY-MM-DD"
        today = date.today()
        if d > today:
            return False, "去世日期不能晚于今天"
        if d.year < 1900:
            return False, "去世日期不早于 1900 年"
        return True, ""

    def _validate_current_stage(self, answer: Any) -> tuple[bool, str]:
        # 可空
        if answer is None or answer == "" or answer == []:
            return True, ""
        if not isinstance(answer, list):
            return False, "current_stage 必须是 list[str]"
        for s in answer:
            if not isinstance(s, str):
                return False, "current_stage 内每个元素必须是字符串"
            if s not in _STAGE_OPTIONS:
                return False, f"阶段不在支持列表内: {s}"
        return True, ""

    def _validate_consent(self, answer: Any) -> tuple[bool, str]:
        if not isinstance(answer, bool):
            return False, "consent 必须是布尔值"
        if answer is not True:
            return False, "请勾选同意《用户协议》与《隐私政策》后才能继续"
        return True, ""

    # ============================================================
    # 持久化
    # ============================================================

    def save_profile(self, user_id: str, answers: dict[str, Any]) -> OnboardingProfile:
        """根据收集到的 answers 构造 OnboardingProfile 并保存

        校验：必填字段必须存在且通过 validate_answer
        """
        if not user_id or not isinstance(user_id, str):
            raise ValueError("user_id 不能为空")
        if not isinstance(answers, dict):
            raise ValueError("answers 必须是 dict")

        # 校验所有必填字段
        for i, key in enumerate(self.STEPS):
            step_def = self.get_step(i)
            value = answers.get(key)
            if step_def.get("required") and (value is None or value == "" or value == []):
                raise ValueError(f"必填字段缺失: {key}")
            ok, err = self.validate_answer(i, value)
            if not ok:
                raise ValueError(f"字段 {key} 校验失败: {err}")

        # 构造 profile
        death_date = answers.get("death_date") or None
        current_stage = list(answers.get("current_stage") or [])

        profile = OnboardingProfile(
            user_id=user_id,
            relationship=answers["relationship"],
            location=answers["location"],
            death_date=death_date,
            current_stage=current_stage,
            consent_disclaimer=bool(answers.get("consent", False)),
        )

        if self._store is not None:
            self._store.save(profile)
        return profile

    # ============================================================
    # 转换为 ConversationState.user_profile
    # ============================================================

    @staticmethod
    def to_user_profile(profile: OnboardingProfile) -> dict[str, Any]:
        """把 OnboardingProfile 转为 ConversationState.user_profile 字典

        输出语义与 orchestration/state.py 的 user_profile 字段兼容
        （该字段语义为「地点/关系/时间/情形/遗嘱/家庭/财产」）。
        """
        return {
            "relationship": profile.relationship,
            "location": profile.location,
            "death_date": profile.death_date,
            "current_stage": list(profile.current_stage),
            "consent_disclaimer": bool(profile.consent_disclaimer),
            # 标记来源（便于后续节点识别这是 onboarding 画像）
            "source": "onboarding_wizard",
        }
