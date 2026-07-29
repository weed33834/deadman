"""终活笔记数据模型 - 参考日本 エンディングノート 设计，加入 deadman 安全约束

重要：终活笔记不是法律文件，不替代遗嘱/信托/医疗预嘱。
（service-boundary-framework.md 第三章边界模糊场景：医疗意愿章节仅作家庭参考）

数据模型设计原则：
    - 所有 PII 字段在落盘前由 EndingNoteStore 加密 + 由 EndingNoteGuide._mask_pii 脱敏
    - safety_flags 由 EndingNoteGuide._check_safety_signals 自动写入
    - delivery_triggers 不在 deadman 内自动执行，仅记录意图
      （notification-guardrails.md：默认静默，主动推送是特权而非默认）

9 章结构参考日本終活应用通用模板（わが家ノート/SouSou/そなえ/遺言ネット）：
    1. personal_info        个人信息
    2. family_relations     家庭关系
    3. assets               资产清单
    4. funeral_wishes       葬礼意愿
    5. medical_wishes       医疗意愿（非法律文件，仅家庭参考）
    6. digital_legacy       数字遗产
    7. messages             给家人的留言
    8. emergency_contacts   重要联系人
    9. will_intent          立遗嘱意向（仅记录意愿，不生成法律文件）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any


@dataclass
class EndingNote:
    """终活笔记 - 参考日本 エンディングノート 设计，但加入 deadman 安全约束

    重要：终活笔记不是法律文件，不替代遗嘱/信托/医疗预嘱
    （service-boundary-framework.md 第三章·补：医疗意愿章节仅作家庭参考，
     法律效力的医疗预嘱需另行公证，本字段不构成法律意见）
    """

    note_id: str
    user_id: str
    created_at: datetime
    updated_at: datetime

    # 第一章 个人信息
    # {full_name_masked, birth_date_masked, nationality, occupation, religion}
    personal_info: dict | None = None

    # 第二章 家庭关系
    # [{relation: "配偶", name_masked, contact_masked, notes}]
    family_relations: list[dict] | None = None

    # 第三章 资产清单
    # [{type: "房产|银行账户|证券|保险|数字资产", description_masked, location_masked, beneficiary}]
    assets: list[dict] | None = None

    # 第四章 葬礼意愿
    # {type: "土葬|火葬|海葬|树葬|其他", location_preference, ceremony_preference,
    #  music, dress_code, participants}
    funeral_wishes: dict | None = None

    # 第五章 医疗意愿（非法律文件，仅家庭参考）
    # {life_sustaining: bool, hospice_preference, organ_donation: bool, primary_contact}
    medical_wishes: dict | None = None

    # 第六章 数字遗产
    # [{platform: "微信|支付宝|银行|社交", account_masked, instruction, beneficiary}]
    digital_legacy: list[dict] | None = None

    # 第七章 给家人的留言
    # [{recipient: "配偶|子女|父母|朋友", content, delivery_timing: "立即|去世后|指定日期"}]
    messages: list[dict] | None = None

    # 第八章 重要联系人
    # [{role: "律师|公证处|医生|殡仪馆", name_masked, phone_masked, notes}]
    emergency_contacts: list[dict] | None = None

    # 第九章 立遗嘱意向（仅记录意愿，不生成法律文件）
    # {has_formal_will: bool, location: "律师处|公证处|家中|银行保管箱", intent_to_create: bool}
    will_intent: dict | None = None

    # 共享设置
    # list of user_id（家庭共享）
    shared_with: list[str] | None = None

    # [{type: "death_confirmation|date|manual", date: "2027-01-01"|"", recipient: "user_id"}]
    delivery_triggers: list[dict] | None = None

    # 安全标记
    # {contains_suicidal_ideation: bool, last_reviewed_at, needs_professional_review: bool}
    safety_flags: dict | None = None

    @classmethod
    def new(cls, user_id: str) -> "EndingNote":
        """创建一份空白终活笔记

        生成新 note_id + 时间戳，所有章节为 None，safety_flags 初始化为安全默认。
        """
        now = datetime.now()
        return cls(
            note_id=str(uuid.uuid4()),
            user_id=user_id,
            created_at=now,
            updated_at=now,
            safety_flags={
                "contains_suicidal_ideation": False,
                "last_reviewed_at": None,
                "needs_professional_review": False,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的 dict（datetime 转 ISO 字符串）"""
        data = asdict(self)
        # datetime → ISO 字符串
        for key in ("created_at", "updated_at"):
            v = data.get(key)
            if isinstance(v, datetime):
                data[key] = v.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EndingNote":
        """从 dict 反序列化（容错：缺失字段走默认；时间戳字符串回 datetime）"""
        created = data.get("created_at")
        updated = data.get("updated_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except ValueError:
                created = datetime.now()
        if isinstance(updated, str):
            try:
                updated = datetime.fromisoformat(updated)
            except ValueError:
                updated = datetime.now()
        if not isinstance(created, datetime):
            created = datetime.now()
        if not isinstance(updated, datetime):
            updated = datetime.now()
        return cls(
            note_id=str(data.get("note_id") or uuid.uuid4()),
            user_id=str(data.get("user_id") or ""),
            created_at=created,
            updated_at=updated,
            personal_info=data.get("personal_info"),
            family_relations=data.get("family_relations"),
            assets=data.get("assets"),
            funeral_wishes=data.get("funeral_wishes"),
            medical_wishes=data.get("medical_wishes"),
            digital_legacy=data.get("digital_legacy"),
            messages=data.get("messages"),
            emergency_contacts=data.get("emergency_contacts"),
            will_intent=data.get("will_intent"),
            shared_with=data.get("shared_with"),
            delivery_triggers=data.get("delivery_triggers"),
            safety_flags=data.get("safety_flags"),
        )

    def touch(self) -> None:
        """更新 updated_at 时间戳"""
        self.updated_at = datetime.now()


# 9 章节 key 列表（与 EndingNoteGuide.SECTIONS 对齐）
SECTION_KEYS: tuple[str, ...] = (
    "personal_info",
    "family_relations",
    "assets",
    "funeral_wishes",
    "medical_wishes",
    "digital_legacy",
    "messages",
    "emergency_contacts",
    "will_intent",
)
