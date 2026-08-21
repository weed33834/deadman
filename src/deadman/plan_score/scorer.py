"""PlanScorer - 身后事规划完整度评分器

计算流程：
    1. 分别加载 5 个维度的实际数据（ending_note / vault / decedent_id /
       deadman_switch / basic_info）
    2. 每维度独立打分（0-100），输出 completed_items / missing_items / suggestions
    3. 加权汇总为 total_score（0-100）
    4. 基于缺失项生成 top-3 跨维度优先建议

权重分配（参考竞品 Trust & Will Plan Strength Score 的偏向核心资产/笔记）：
    - ending_note      35%   （最核心，9 章节覆盖完整身后事意愿）
    - vault            25%   （实际存放数字遗产/遗嘱/保单）
    - decedent_case    15%   （逝者案例流程跟踪）
    - deadman_switch   15%   （失联开关是 deadman 差异化）
    - basic_info       10%   （用户基础信息+留存信号）

合规关联：
    - integrity-framework.md L1：评分基于实际加载到的数据，
      未加载到的字段一律视为"未填写/缺失"，不猜测、不编造
    - service-boundary-framework.md L3：suggestions 仅建议完善信息，
      不表述"评分高=法律效力强"等越界结论
    - safety-protocol.md：评分不读取笔记内容做语义判断，
      避免误判 safety_flags 状态
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..utils.dates import parse_dt
from .models import Category, PlanScore, SubScore

logger = logging.getLogger(__name__)


# 5 维度权重（合计 1.0）
WEIGHTS: dict[Category, float] = {
    Category.ENDING_NOTE: 0.35,
    Category.VAULT: 0.25,
    Category.DECEDENT_CASE: 0.15,
    Category.DEADMAN_SWITCH: 0.15,
    Category.BASIC_INFO: 0.10,
}

# ending_note 9 章节：每章 11 分（9*11=99），加 1 分 will_intent 标记 = 100
# 注：will_intent 既是 9 章之一，又有"已立遗嘱意向"的额外加权（1 分）
_ENDING_NOTE_SECTION_TITLES: dict[str, str] = {
    "personal_info": "第一章：个人信息",
    "family_relations": "第二章：家庭关系",
    "assets": "第三章：资产清单",
    "funeral_wishes": "第四章：葬礼意愿",
    "medical_wishes": "第五章：医疗意愿",
    "digital_legacy": "第六章：数字遗产",
    "messages": "第七章：给家人的留言",
    "emergency_contacts": "第八章：重要联系人",
    "will_intent": "第九章：立遗嘱意向",
}

# 每章节缺失的针对性建议（参考 Trust & Will 的智能建议风格）
_ENDING_NOTE_SECTION_SUGGESTIONS: dict[str, str] = {
    "personal_info": "未填写个人信息章节，建议填写化名、出生年份等基础识别信息",
    "family_relations": "未填写家庭关系章节，建议列出配偶、子女、父母等家庭成员",
    "assets": "未填写资产清单章节，建议列出房产、银行账户、证券、保险等资产",
    "funeral_wishes": "未填写葬礼意愿章节，建议填写火葬/土葬/海葬等仪式偏好",
    "medical_wishes": ("未填写医疗意愿章节，建议填写是否愿意接受姑息治疗、是否捐献器官等"),
    "digital_legacy": "未填写数字遗产章节，建议列出微信、支付宝、银行账号的处理意愿",
    "messages": "未填写给家人的留言章节，建议留下感谢、道歉、嘱托等话语",
    "emergency_contacts": "未填写重要联系人章节，建议列出律师、公证处、医生、殡仪馆等联系方式",
    "will_intent": "未填写立遗嘱意向章节，建议记录是否已立遗嘱、是否打算立遗嘱",
}


class PlanScorer:
    """身后事规划完整度评分器

    使用方式：
        scorer = PlanScorer()
        result = scorer.score(user_id="user-xxx")
        print(result.total_score)

    测试隔离：
        构造时可注入 ending_note_store / vault_store / decedent_registry /
        switch_store / user_store，全部指向 tmp_path 数据目录。
    """

    def __init__(
        self,
        ending_note_store: Any | None = None,
        vault_store: Any | None = None,
        decedent_registry: Any | None = None,
        switch_store: Any | None = None,
        user_store: Any | None = None,
    ) -> None:
        # 懒加载：未注入时按各模块默认路径构造
        self._ending_note_store = ending_note_store
        self._vault_store = vault_store
        self._decedent_registry = decedent_registry
        self._switch_store = switch_store
        self._user_store = user_store

    # ==================================================================
    # 公开入口
    # ==================================================================
    def score(self, user_id: str) -> PlanScore:
        """计算用户完整度评分

        Args:
            user_id: 用户 ID

        Returns:
            PlanScore（包含 5 个 SubScore + 总分 + top-3 建议）
        """
        category_scores: list[SubScore] = [
            self._score_ending_note(user_id),
            self._score_vault(user_id),
            self._score_decedent_case(user_id),
            self._score_deadman_switch(user_id),
            self._score_basic_info(user_id),
        ]
        total = self._weighted_total(category_scores)
        overall = self._generate_suggestions(category_scores)
        return PlanScore(
            user_id=user_id,
            total_score=total,
            category_scores=category_scores,
            overall_suggestions=overall,
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )

    # ==================================================================
    # 加权汇总
    # ==================================================================
    @staticmethod
    def _weighted_total(category_scores: list[SubScore]) -> int:
        """按 WEIGHTS 加权汇总

        缺失维度（不在列表中）按 0 分处理；
        结果四舍五入到整数，clamp 到 [0, 100]。
        """
        score_map: dict[Category, int] = {s.category: s.score for s in category_scores}
        total = 0.0
        for cat, weight in WEIGHTS.items():
            total += score_map.get(cat, 0) * weight
        # 四舍五入 + clamp（round 返回 int，无需 int() 包装）
        result = round(total)
        if result < 0:
            return 0
        if result > 100:
            return 100
        return result

    # ==================================================================
    # 维度 1：终活笔记
    # ==================================================================
    def _score_ending_note(self, user_id: str) -> SubScore:
        """终活笔记评分（9 章节 + will_intent 标记）

        9 章节每章 11 分（9*11=99），加 will_intent 已立遗嘱标记 1 分 = 100

        缺失项：未填写的章节名（中文标题）
        建议：对应章节的针对性建议
        """
        store = self._get_ending_note_store()
        note = store.load(user_id)
        completed: list[str] = []
        missing: list[str] = []
        suggestions: list[str] = []

        # 加载失败/无笔记：9 章全缺，0 分
        if note is None:
            for key, title in _ENDING_NOTE_SECTION_TITLES.items():
                missing.append(f"未填写 {title}")
                suggestions.append(_ENDING_NOTE_SECTION_SUGGESTIONS[key])
            return SubScore(
                category=Category.ENDING_NOTE,
                score=0,
                completed_items=completed,
                missing_items=missing,
                suggestions=suggestions,
            )

        # 调 EndingNoteGuide.completion_rate 获取每章填写状态
        from ..ending_note.guide import EndingNoteGuide

        guide = EndingNoteGuide(store=store)
        rate = guide.completion_rate(note)
        sections = rate.get("sections", {})

        # 9 章节：每章 11 分
        score = 0
        for key, title in _ENDING_NOTE_SECTION_TITLES.items():
            filled = sections.get(key, 0.0) == 1.0
            if filled:
                score += 11
                completed.append(f"{title} 已填写")
            else:
                missing.append(f"未填写 {title}")
                suggestions.append(_ENDING_NOTE_SECTION_SUGGESTIONS[key])

        # will_intent 标记额外 1 分（用户已立正式遗嘱或明确意向）
        will_intent = getattr(note, "will_intent", None)
        if isinstance(will_intent, dict) and (
            will_intent.get("has_formal_will") or will_intent.get("intent_to_create")
        ):
            score += 1
            completed.append("已明确立遗嘱意向")

        return SubScore(
            category=Category.ENDING_NOTE,
            score=score,
            completed_items=completed,
            missing_items=missing,
            suggestions=suggestions,
        )

    # ==================================================================
    # 维度 2：数字遗产保险库
    # ==================================================================
    def _score_vault(self, user_id: str) -> SubScore:
        """保险库评分（4 项指标）

        - 有至少 1 个 password 类条目：30 分
        - 有至少 1 个 document 类条目（遗嘱/保单）：30 分
        - 有至少 1 个 beneficiary_user_ids 指定：20 分
        - 有至少 1 个 delivery_trigger 配置（非 manual）：20 分
        """
        store = self._get_vault_store()
        items = store.list_items(user_id, user_id)
        completed: list[str] = []
        missing: list[str] = []
        suggestions: list[str] = []

        if not items:
            missing.extend(
                [
                    "缺少 password 类条目（账号密码）",
                    "缺少 document 类条目（遗嘱/保单扫描件）",
                    "未指定任何受益人（beneficiary_user_ids）",
                    "未配置投递触发器（delivery_trigger）",
                ]
            )
            suggestions.extend(
                [
                    "建议上传重要账号密码到保险库（加密存储，仅你可见）",
                    "建议上传遗嘱/保单扫描件到保险库，便于身后查找",
                    "建议为至少 1 个条目指定受益人，确保资产有人继承",
                    "建议为重要条目配置投递触发器（on_death / on_date）",
                ]
            )
            return SubScore(
                category=Category.VAULT,
                score=0,
                completed_items=completed,
                missing_items=missing,
                suggestions=suggestions,
            )

        score = 0
        has_password = any(it.get("type") == "password" for it in items)
        has_document = any(it.get("type") == "document" for it in items)
        has_beneficiary = any(it.get("beneficiary_user_ids") for it in items)
        has_trigger = any(
            it.get("delivery_trigger") and it.get("delivery_trigger") != "manual" for it in items
        )

        if has_password:
            score += 30
            completed.append("已存储 password 类条目")
        else:
            missing.append("缺少 password 类条目（账号密码）")
            suggestions.append("建议上传重要账号密码到保险库（加密存储，仅你可见）")

        if has_document:
            score += 30
            completed.append("已存储 document 类条目（遗嘱/保单）")
        else:
            missing.append("缺少 document 类条目（遗嘱/保单扫描件）")
            suggestions.append("建议上传遗嘱/保单扫描件到保险库，便于身后查找")

        if has_beneficiary:
            score += 20
            completed.append("已指定至少 1 个受益人")
        else:
            missing.append("未指定任何受益人（beneficiary_user_ids）")
            suggestions.append("建议为至少 1 个条目指定受益人，确保资产有人继承")

        if has_trigger:
            score += 20
            completed.append("已配置投递触发器（on_death / on_date）")
        else:
            missing.append("未配置投递触发器（delivery_trigger）")
            suggestions.append("建议为重要条目配置投递触发器（on_death / on_date）")

        return SubScore(
            category=Category.VAULT,
            score=score,
            completed_items=completed,
            missing_items=missing,
            suggestions=suggestions,
        )

    # ==================================================================
    # 维度 3：遗码通案例
    # ==================================================================
    def _score_decedent_case(self, user_id: str) -> SubScore:
        """遗码通案例评分

        - 有创建 case：40 分
        - 有至少 1 个 event 时间线：30 分
        - case 已归档（已完成流程）：30 分
        - 无 case 时返回 0 分 + 建议"为逝者创建案例以便跟踪流程"
        """
        reg = self._get_decedent_registry()
        cases = reg.list_cases(user_id)
        completed: list[str] = []
        missing: list[str] = []
        suggestions: list[str] = []

        if not cases:
            missing.extend(
                [
                    "未创建任何逝者案例",
                    "无案例时间线事件",
                    "无已归档案例",
                ]
            )
            suggestions.append(
                "建议为逝者创建案例以便跟踪流程（case-create），"
                "记录死亡证明/户籍注销/资产过户等关键节点"
            )
            return SubScore(
                category=Category.DECEDENT_CASE,
                score=0,
                completed_items=completed,
                missing_items=missing,
                suggestions=suggestions,
            )

        score = 0
        # 有创建 case：40 分
        score += 40
        completed.append(f"已创建 {len(cases)} 个案例")

        # 有至少 1 个 event 时间线：30 分
        has_event = any(len(c.events) > 0 for c in cases)
        if has_event:
            score += 30
            completed.append("案例已有时间线事件")
        else:
            missing.append("案例尚无时间线事件")
            suggestions.append("建议为案例添加时间线事件（case-event-add），记录关键流程节点")

        # case 已归档：30 分
        has_archived = any(getattr(c, "status", "") == "archived" for c in cases)
        if has_archived:
            score += 30
            completed.append("已有案例归档（流程完成）")
        else:
            missing.append("无已归档案例")
            suggestions.append("建议在流程完成后归档案例（case-archive），便于后续查阅与统计")

        return SubScore(
            category=Category.DECEDENT_CASE,
            score=score,
            completed_items=completed,
            missing_items=missing,
            suggestions=suggestions,
        )

    # ==================================================================
    # 维度 4：失联开关
    # ==================================================================
    def _score_deadman_switch(self, user_id: str) -> SubScore:
        """失联开关评分

        - 已初始化：40 分
        - 配置了紧急联系人：30 分
        - 配置了律师：15 分
        - 配置了继承人：15 分
        """
        store = self._get_switch_store()
        record = store.load(user_id)
        completed: list[str] = []
        missing: list[str] = []
        suggestions: list[str] = []

        if record is None:
            missing.extend(
                [
                    "失联开关未初始化",
                    "未配置紧急联系人",
                    "未配置律师",
                    "未配置继承人",
                ]
            )
            suggestions.extend(
                [
                    "建议初始化失联开关（switch-init），定期 check-in 触发身后流程",
                    "建议配置至少 1 名紧急联系人，失联时由其确认",
                    "建议配置律师 user_id，确保法律流程有专业人士介入",
                    "建议配置法定继承人 user_id，确保身后流程可推进",
                ]
            )
            return SubScore(
                category=Category.DEADMAN_SWITCH,
                score=0,
                completed_items=completed,
                missing_items=missing,
                suggestions=suggestions,
            )

        score = 0
        # 已初始化：40 分
        score += 40
        completed.append("失联开关已初始化")

        cfg = record.config

        # 配置了紧急联系人：30 分
        if cfg.emergency_contacts:
            score += 30
            completed.append(f"已配置 {len(cfg.emergency_contacts)} 名紧急联系人")
        else:
            missing.append("未配置紧急联系人")
            suggestions.append("建议配置至少 1 名紧急联系人，失联时由其确认")

        # 配置了律师：15 分
        if cfg.lawyer_user_id:
            score += 15
            completed.append("已配置律师")
        else:
            missing.append("未配置律师")
            suggestions.append("建议配置律师 user_id，确保法律流程有专业人士介入")

        # 配置了继承人：15 分
        if cfg.heir_user_ids:
            score += 15
            completed.append(f"已配置 {len(cfg.heir_user_ids)} 名继承人")
        else:
            missing.append("未配置继承人")
            suggestions.append("建议配置法定继承人 user_id，确保身后流程可推进")

        return SubScore(
            category=Category.DEADMAN_SWITCH,
            score=score,
            completed_items=completed,
            missing_items=missing,
            suggestions=suggestions,
        )

    # ==================================================================
    # 维度 5：用户基础信息
    # ==================================================================
    def _score_basic_info(self, user_id: str) -> SubScore:
        """用户基础信息评分

        - 邮箱已验证：50 分
        - 设置了 display_name：20 分
        - 注册超过 7 天：30 分（用户留存信号）
        """
        store = self._get_user_store()
        user = store.get_user(user_id)
        completed: list[str] = []
        missing: list[str] = []
        suggestions: list[str] = []

        if user is None:
            missing.extend(
                [
                    "用户不存在或未注册",
                    "未设置 display_name",
                    "注册不足 7 天",
                ]
            )
            suggestions.append("请先完成注册并验证邮箱")
            return SubScore(
                category=Category.BASIC_INFO,
                score=0,
                completed_items=completed,
                missing_items=missing,
                suggestions=suggestions,
            )

        score = 0
        # 邮箱已验证：50 分
        # 注：当前 UserStore 不区分 verified 状态，有 email 即视为已验证
        # （注册流程要求邮箱+密码，email 字段存在即视为完成邮箱验证基线）
        email = user.get("email")
        if email:
            score += 50
            completed.append("邮箱已设置")
        else:
            missing.append("邮箱未设置")
            suggestions.append("建议绑定并验证邮箱")

        # 设置了 display_name：20 分
        display_name = user.get("display_name")
        if display_name:
            score += 20
            completed.append(f"已设置 display_name: {display_name}")
        else:
            missing.append("未设置 display_name")
            suggestions.append("建议设置昵称（display_name），便于家人识别")

        # 注册超过 7 天：30 分
        created_at = user.get("created_at")
        if created_at:
            try:
                # ISO 8601 时间戳解析
                created_dt = _parse_iso(created_at)
                if created_dt is not None:
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    if now - created_dt >= timedelta(days=7):
                        score += 30
                        completed.append("注册已超过 7 天")
                    else:
                        missing.append("注册不足 7 天")
                        suggestions.append("建议持续使用 7 天以上以建立留存信号（新用户）")
                else:
                    missing.append("注册时间无法解析")
            except Exception as exc:
                logger.warning("PlanScorer._score_basic_info created_at 解析失败: %s", exc)
                missing.append("注册时间无法解析")
        else:
            missing.append("无注册时间记录")

        return SubScore(
            category=Category.BASIC_INFO,
            score=score,
            completed_items=completed,
            missing_items=missing,
            suggestions=suggestions,
        )

    # ==================================================================
    # 跨维度建议生成
    # ==================================================================
    @staticmethod
    def _generate_suggestions(category_scores: list[SubScore]) -> list[str]:
        """基于缺失项生成 top 3 优先建议

        优先级策略：
            1. 按 category 权重降序遍历（ending_note > vault > ...）
            2. 每个维度内取第 1 条 suggestion
            3. 收集到 3 条后停止
            4. 不足 3 条时返回全部
        """
        # 按权重降序
        ordered = sorted(
            category_scores,
            key=lambda s: WEIGHTS.get(s.category, 0.0),
            reverse=True,
        )
        result: list[str] = []
        for sub in ordered:
            if sub.suggestions:
                result.append(sub.suggestions[0])
            if len(result) >= 3:
                break
        return result

    # ==================================================================
    # 懒加载各 store
    # ==================================================================
    def _get_ending_note_store(self) -> Any:
        if self._ending_note_store is None:
            from ..ending_note.store import EndingNoteStore

            self._ending_note_store = EndingNoteStore()
        return self._ending_note_store

    def _get_vault_store(self) -> Any:
        if self._vault_store is None:
            from ..vault.store import VaultStore

            self._vault_store = VaultStore()
        return self._vault_store

    def _get_decedent_registry(self) -> Any:
        if self._decedent_registry is None:
            from ..decedent_id.registry import DecedentRegistry

            self._decedent_registry = DecedentRegistry()
        return self._decedent_registry

    def _get_switch_store(self) -> Any:
        if self._switch_store is None:
            from ..deadman_switch.store import SwitchStore

            self._switch_store = SwitchStore()
        return self._switch_store

    def _get_user_store(self) -> Any:
        if self._user_store is None:
            from ..auth.store import UserStore

            self._user_store = UserStore()
        return self._user_store


# ======================================================================
# 辅助：ISO 时间戳解析（兼容带/不带时区）
# ======================================================================
def _parse_iso(ts: str) -> datetime | None:
    """解析 ISO 8601 时间戳（统一走 utils.dates，带时区转 UTC naive）。"""
    return parse_dt(ts, to_utc_naive=True)
