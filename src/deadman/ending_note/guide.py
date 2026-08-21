"""AI 引导填写终活笔记 - deadman 差异化

参考日本 エンディングノート 表单（わが家ノート/SouSou/そなえ/遺言ネット），
但用对话引导而非表单填写（deadman 差异化）。

合规关联：
    - safety-protocol.md L0：
        _check_safety_signals 检测自杀风险信号，命中时返回 high severity，
        由调用方（web/CLI）触发 safety-protocol L0 分支（停止流程引导）
    - legal-compliance-framework.md L3 PIPL：
        _mask_pii 在写入笔记字段前对 PII 做掩码，落盘文件不含明文 PII
    - service-boundary-framework.md L3：
        每章引导话术均含"本笔记不是法律文件"边界告知
    - integrity-framework.md L1：
        _check_safety_signals 不编造诊断，仅做关键词检测 + 引导转介专业资源
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from .models import SECTION_KEYS, EndingNote
from .store import EndingNoteStore

logger = logging.getLogger(__name__)


# ====================================================================
# 安全信号关键词表（safety-protocol.md 第一章识别信号）
# ====================================================================
# 命中即返回 high severity，触发 L0 分支
# 注意：本表只做关键词检测，不做诊断（integrity-framework.md 第一章禁止软性造假）
# 关键词来源：safety-protocol.md 第一章"表达自伤/自杀意图"列举的表述
_SUICIDAL_HIGH_KEYWORDS: tuple[str, ...] = (
    "不想活",
    "不想活了",
    "撑不下去",
    "想跟着走",
    "想死",
    "自杀",
    "了结",
    "一了百了",
    "解脱",  # "想解脱" "解脱了" 等告别式表述
    "不想拖累",
    "活着没意思",
    "结束生命",
    "结束自己",
)

# 中度风险关键词：告别式表述 / 安排好后事（指自己）
_SUICIDAL_MEDIUM_KEYWORDS: tuple[str, ...] = (
    "安排好后事",
    "最后的话",
    "告别",
    "没有未来",
    "走不动了",
)


class EndingNoteGuide:
    """AI 引导填写终活笔记 - deadman 差异化

    参考日本 エンディングノート 表单，但用对话引导而非表单填写
    """

    SECTIONS: list[tuple[str, str, str]] = [
        (
            "personal_info",
            "第一章：个人信息",
            (
                "我先了解一下你的基本情况。你愿意告诉我你的姓名（可化名）和出生年份吗？\n"
                "（提示：终活笔记不是法律文件，不替代任何官方身份材料；"
                "你可以用化名，家人能认出即可。）"
            ),
        ),
        (
            "family_relations",
            "第二章：家庭关系",
            (
                "你的家庭成员有哪些？比如配偶、子女、父母、兄弟姐妹。"
                "如果有需要特别告知的事项，也可以一并写下。"
            ),
        ),
        (
            "assets",
            "第三章：资产清单",
            (
                "你的资产有哪些？房产、银行账户、证券、保险、数字资产"
                "（如微信支付宝）等。每项资产希望由谁继承？\n"
                "（提示：本笔记不是遗嘱，资产分配的法律效力需另立公证遗嘱。）"
            ),
        ),
        (
            "funeral_wishes",
            "第四章：葬礼意愿",
            (
                "你对葬礼有什么想法？火葬/土葬/海葬/树葬？仪式偏好？音乐？着装？\n"
                "（这是你的意愿记录，家人会参考，但具体执行可能受当地政策限制。）"
            ),
        ),
        (
            "medical_wishes",
            "第五章：医疗意愿",
            (
                "如果有一天你无法表达，你希望家人怎么决定？是否维持生命支持？"
                "是否愿意捐献器官？\n"
                "（重要：本章节仅作家庭参考，不构成法律效力的医疗预嘱；"
                "如需法律效力，请咨询医生/律师办理正式预立医嘱。）"
            ),
        ),
        (
            "digital_legacy",
            "第六章：数字遗产",
            (
                "你的数字账号怎么处理？微信、支付宝、银行、社交账号？"
                "每个账号希望交给谁？有没有特殊说明？"
            ),
        ),
        (
            "messages",
            "第七章：给家人的留言",
            (
                "有没有想对家人说的话？可以是感谢、道歉、嘱托，或者只是想念。\n"
                "（如果你的话里涉及任何伤害自己的念头，请先告诉我——"
                "你的安全比这份笔记更重要。）"
            ),
        ),
        (
            "emergency_contacts",
            "第八章：重要联系人",
            ("如果家人需要帮助，应该联系谁？律师、公证处、医生、殡仪馆？请提供姓名和联系方式。"),
        ),
        (
            "will_intent",
            "第九章：立遗嘱意向",
            (
                "你已经立了正式遗嘱吗？放在哪里？或者你打算立一份？\n"
                "（重要：本笔记不生成法律效力的遗嘱；"
                "正式遗嘱需咨询律师/公证处办理。）"
            ),
        ),
    ]

    def __init__(self, store: EndingNoteStore | None = None) -> None:
        self.store = store or EndingNoteStore()

    # ------------------------------------------------------------------
    # 引导主流程
    # ------------------------------------------------------------------

    def next_question(self, note: EndingNote) -> tuple[str, str, str]:
        """返回下一章的 (section_key, section_title, question)

        跳过已填写的章节；全部填完返回 ("__done__", "已完成", "你的终活笔记已全部填写完成")

        特殊：若 safety_flags.contains_suicidal_ideation == True，立即返回
        ("__safety__", "安全优先", <转介话术>)，停止流程引导
        （safety-protocol.md 第一章应对步骤 1：停止流程引导）
        """
        # 安全优先：检测到自杀风险信号后停止流程引导
        flags = note.safety_flags or {}
        if flags.get("contains_suicidal_ideation"):
            return (
                "__safety__",
                "安全优先",
                (
                    "我注意到你刚才说的话让我有些担心你的安全。"
                    "在我帮你继续填写笔记之前，"
                    "你能告诉我你现在身边有其他人吗？"
                    "如果你愿意，可以拨打当地心理危机干预热线或急救电话，"
                    "我没办法替你保密这件事，因为你的安全比我守口如瓶更重要。"
                ),
            )

        for section_key, title, question in self.SECTIONS:
            value = getattr(note, section_key, None)
            if not _section_is_filled(value):
                return section_key, title, question

        return (
            "__done__",
            "已完成",
            "你的终活笔记已全部填写完成。需要查看、修改或共享给家人，随时告诉我。",
        )

    def save_answer(self, note: EndingNote, section: str, answer: dict) -> EndingNote:
        """保存用户回答到笔记对应章节

        - 自动 PII 脱敏（调 _mask_pii）
        - 自动检测自杀风险信号（调 _check_safety_signals）
        - 若检测到风险，返回 note 同时设置
          safety_flags.contains_suicidal_ideation=True / needs_professional_review=True

        Args:
            note: 待更新的笔记（原地修改并返回）
            section: 章节 key（必须在 SECTION_KEYS 中）
            answer: 用户回答 dict

        Returns:
            修改后的 note（同一对象）
        """
        if section not in SECTION_KEYS:
            raise ValueError(f"未知章节: {section}，支持的章节: {SECTION_KEYS}")

        # 把整章 answer 序列化为文本，扫描自杀风险信号
        text_for_check = _dict_to_text(answer)
        safety = self._check_safety_signals(text_for_check)

        # 章节级 PII 脱敏
        masked = self._mask_pii(section, answer)

        # 写入字段
        setattr(note, section, masked)
        note.touch()

        # 安全标记（不覆盖既有 True，只升不降）
        flags = note.safety_flags or {
            "contains_suicidal_ideation": False,
            "last_reviewed_at": None,
            "needs_professional_review": False,
        }
        if safety["contains_signal"]:
            flags["contains_suicidal_ideation"] = True
            flags["needs_professional_review"] = True
            flags["last_reviewed_at"] = datetime.now().isoformat()
            logger.warning(
                "EndingNoteGuide.save_answer 检测到自杀风险信号 section=%s severity=%s",
                section,
                safety["severity"],
            )
        else:
            # 没有信号也记录最近一次复核时间
            flags["last_reviewed_at"] = datetime.now().isoformat()
        note.safety_flags = flags

        return note

    # ------------------------------------------------------------------
    # PII 脱敏
    # ------------------------------------------------------------------

    def _mask_pii(self, section: str, data: dict) -> dict:
        """章节级 PII 脱敏

        - personal_info: full_name → f"{name[0]}**", birth_date → "YYYY"
        - family_relations: name → "张**", contact → "138****1234"
        - assets: account → "6222****1234", location → "北京市朝阳区**"
        - emergency_contacts: phone → "138****1234"
        - 其他章节原样返回（messages/funeral_wishes/medical_wishes/
          digital_legacy/will_intent 由用户自行控制是否含 PII；
          若发现明显 PII 字段，按规则补充掩码）

        所有掩码规则：
            - 姓名：首字 + "**"（"张三" → "张**"）
            - 电话/账号：保留前 3 + "****" + 后 4（"13812341234" → "138****1234"）
            - 出生日期：保留年份（"1985-05-03" → "1985"）
            - 地址：保留前 6 字 + "**"（"北京市朝阳区建国路 1 号" → "北京市朝阳区**"）
        """
        if not isinstance(data, dict):
            return data

        if section == "personal_info":
            return _mask_personal_info(data)
        if section == "family_relations":
            return _mask_family_relations(data)
        if section == "assets":
            return _mask_assets(data)
        if section == "emergency_contacts":
            return _mask_emergency_contacts(data)
        if section == "digital_legacy":
            return _mask_digital_legacy(data)
        # 其他章节：递归扫描常见 PII 字段名
        return _mask_generic_dict(data)

    # ------------------------------------------------------------------
    # 安全信号检测
    # ------------------------------------------------------------------

    def _check_safety_signals(self, text: str) -> dict[str, Any]:
        """检测自杀/自伤风险信号

        关键词（safety-protocol.md 第一章识别信号）：
            high:   不想活/不想拖累/自杀/了结/解脱/一了百了/想死/撑不下去
            medium: 安排好后事/最后的话/告别/没有未来/走不动了
            none:   普通文本

        返回 {contains_signal: bool, severity: "low|medium|high|none",
              suggested_action: str}

        触发 safety-protocol.md L0 分支：
            severity=high → 调用方应停止流程引导，转介心理危机资源
            severity=medium → 调用方应温和询问"你现在身边有其他人吗"
        """
        if not text:
            return {
                "contains_signal": False,
                "severity": "none",
                "suggested_action": "无",
            }

        text_lower = text.lower()

        for kw in _SUICIDAL_HIGH_KEYWORDS:
            if kw in text or kw.lower() in text_lower:
                return {
                    "contains_signal": True,
                    "severity": "high",
                    "suggested_action": (
                        "停止流程引导；温和询问用户当前安全状态；"
                        "提示联系当地心理危机干预热线或急救电话；"
                        "不评判、不说教；若用户透露即将自伤，明确说"
                        "'我没办法替你保密这件事'。"
                    ),
                }

        for kw in _SUICIDAL_MEDIUM_KEYWORDS:
            if kw in text or kw.lower() in text_lower:
                return {
                    "contains_signal": True,
                    "severity": "medium",
                    "suggested_action": (
                        "温和询问'你现在身边有其他人吗'；继续观察后续输入是否升级为 high 信号。"
                    ),
                }

        return {
            "contains_signal": False,
            "severity": "none",
            "suggested_action": "无",
        }

    # ------------------------------------------------------------------
    # 完整度
    # ------------------------------------------------------------------

    def completion_rate(self, note: EndingNote) -> dict[str, Any]:
        """计算填写完整度

        返回 {overall: 0.0-1.0, sections: {section: 0.0/1.0}}
        """
        section_scores: dict[str, float] = {}
        filled_count = 0
        for section_key in SECTION_KEYS:
            value = getattr(note, section_key, None)
            score = 1.0 if _section_is_filled(value) else 0.0
            section_scores[section_key] = score
            if score == 1.0:
                filled_count += 1
        overall = filled_count / len(SECTION_KEYS) if SECTION_KEYS else 0.0
        return {"overall": overall, "sections": section_scores}


# ====================================================================
# 辅助：章节填写判定
# ====================================================================


def _section_is_filled(value: Any) -> bool:
    """判定章节是否已填写

    - None / 空 dict / 空 list / 空字符串 → 未填写
    - 非空 dict / 非空 list / 非空字符串 → 已填写
    """
    if value is None:
        return False
    return not (isinstance(value, dict | list | str) and len(value) == 0)


def _dict_to_text(d: Any) -> str:
    """把任意 dict/list/str 拍平为单一字符串，供关键词扫描"""
    if d is None:
        return ""
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        parts: list[str] = []
        for v in d.values():
            parts.append(_dict_to_text(v))
        return " ".join(parts)
    if isinstance(d, list | tuple):
        return " ".join(_dict_to_text(x) for x in d)
    return str(d)


# ====================================================================
# PII 掩码辅助
# ====================================================================


def _mask_name(name: str | None) -> str | None:
    """姓名脱敏：首字 + "**"（"张三" → "张**"；空或单字 → "*"）"""
    if not name or not isinstance(name, str):
        return name
    name = name.strip()
    if not name:
        return name
    if len(name) == 1:
        return "*"
    return f"{name[0]}**"


def _mask_phone(phone: str | None) -> str | None:
    """电话脱敏：保留前 3 + "****" + 后 4（"13812341234" → "138****1234"）

    非标准长度（非 11 位数字）：保留首尾各 2，中间 ****
    """
    if not phone or not isinstance(phone, str):
        return phone
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11:
        return f"{digits[:3]}****{digits[-4:]}"
    if len(digits) > 4:
        return f"{digits[:2]}****{digits[-2:]}"
    if digits:
        return "****"
    # 非电话格式字符串：保留首尾各 1 字符
    s = phone.strip()
    if len(s) > 2:
        return f"{s[0]}****{s[-1]}"
    return "****"


def _mask_account(account: str | None) -> str | None:
    """账号脱敏：保留前 4 + "****" + 后 4（"6222021234567890" → "6222****7890"）

    与 _mask_phone 略有差异：账号通常更长，前 4 位更具识别性
    """
    if not account or not isinstance(account, str):
        return account
    digits = re.sub(r"\D", "", account)
    if len(digits) >= 8:
        return f"{digits[:4]}****{digits[-4:]}"
    if len(digits) > 4:
        return f"{digits[:2]}****{digits[-2:]}"
    if digits:
        return "****"
    s = account.strip()
    if len(s) > 4:
        return f"{s[:2]}****{s[-2:]}"
    return "****"


def _mask_birth_date(birth: str | None) -> str | None:
    """出生日期脱敏：保留年份（"1985-05-03" → "1985"）"""
    if not birth or not isinstance(birth, str):
        return birth
    m = re.match(r"^(\d{4})", birth.strip())
    if m:
        return m.group(1)
    return "****"


def _mask_address(addr: str | None) -> str | None:
    """地址脱敏：保留前 6 字 + "**"（"北京市朝阳区建国路 1 号" → "北京市朝阳区**"）"""
    if not addr or not isinstance(addr, str):
        return addr
    s = addr.strip()
    if len(s) <= 6:
        return s + "**"
    return f"{s[:6]}**"


def _mask_personal_info(data: dict) -> dict:
    """personal_info 章节脱敏"""
    out = dict(data)
    if "full_name" in out:
        out["full_name_masked"] = _mask_name(out.get("full_name"))
        out.pop("full_name", None)
    elif "full_name_masked" in out:
        out["full_name_masked"] = _mask_name(out.get("full_name_masked"))
    if "birth_date" in out:
        out["birth_date_masked"] = _mask_birth_date(out.get("birth_date"))
        out.pop("birth_date", None)
    elif "birth_date_masked" in out:
        out["birth_date_masked"] = _mask_birth_date(out.get("birth_date_masked"))
    return out


def _mask_family_relations(data: dict) -> dict:
    """family_relations 章节脱敏

    输入可能是 {relations: [...]} 或直接 list；本方法接受 dict，
    若发现 list 字段则递归处理
    """
    out = dict(data)
    for key, val in list(out.items()):
        if isinstance(val, list):
            out[key] = [_mask_relation_item(x) for x in val]
        elif isinstance(val, dict):
            out[key] = _mask_relation_item(val)
    return out


def _mask_relation_item(item: Any) -> Any:
    """单条 family_relation 脱敏"""
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if "name" in out:
        out["name_masked"] = _mask_name(out.get("name"))
        out.pop("name", None)
    elif "name_masked" in out:
        out["name_masked"] = _mask_name(out.get("name_masked"))
    if "contact" in out:
        out["contact_masked"] = _mask_phone(out.get("contact"))
        out.pop("contact", None)
    elif "contact_masked" in out:
        out["contact_masked"] = _mask_phone(out.get("contact_masked"))
    if "phone" in out:
        out["contact_masked"] = _mask_phone(out.pop("phone"))
    return out


def _mask_assets(data: dict) -> dict:
    """assets 章节脱敏"""
    out = dict(data)
    for key, val in list(out.items()):
        if isinstance(val, list):
            out[key] = [_mask_asset_item(x) for x in val]
        elif isinstance(val, dict):
            out[key] = _mask_asset_item(val)
    return out


def _mask_asset_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if "account" in out:
        out["account_masked"] = _mask_account(out.pop("account"))
    elif "account_masked" in out:
        out["account_masked"] = _mask_account(out.get("account_masked"))
    if "account_number" in out:
        out["account_masked"] = _mask_account(out.pop("account_number"))
    if "location" in out:
        out["location_masked"] = _mask_address(out.pop("location"))
    elif "location_masked" in out:
        out["location_masked"] = _mask_address(out.get("location_masked"))
    if "description" in out and isinstance(out["description"], str):
        # description 可能含 PII（如"北京市朝阳区 xx 小区 x 号楼"），保守脱敏
        out["description_masked"] = _mask_address(out.pop("description"))
    return out


def _mask_emergency_contacts(data: dict) -> dict:
    """emergency_contacts 章节脱敏"""
    out = dict(data)
    for key, val in list(out.items()):
        if isinstance(val, list):
            out[key] = [_mask_contact_item(x) for x in val]
        elif isinstance(val, dict):
            out[key] = _mask_contact_item(val)
    return out


def _mask_contact_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if "name" in out:
        out["name_masked"] = _mask_name(out.pop("name"))
    elif "name_masked" in out:
        out["name_masked"] = _mask_name(out.get("name_masked"))
    if "phone" in out:
        out["phone_masked"] = _mask_phone(out.pop("phone"))
    elif "phone_masked" in out:
        out["phone_masked"] = _mask_phone(out.get("phone_masked"))
    return out


def _mask_digital_legacy(data: dict) -> dict:
    """digital_legacy 章节脱敏"""
    out = dict(data)
    for key, val in list(out.items()):
        if isinstance(val, list):
            out[key] = [_mask_digital_item(x) for x in val]
        elif isinstance(val, dict):
            out[key] = _mask_digital_item(val)
    return out


def _mask_digital_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if "account" in out:
        out["account_masked"] = _mask_account(out.pop("account"))
    elif "account_masked" in out:
        out["account_masked"] = _mask_account(out.get("account_masked"))
    if "password" in out:
        # 密码永远不应记录在终活笔记里（PIPL 第五章第 3 条：智能体不得要求密码）
        out.pop("password")
        out["_password_removed"] = True
    return out


def _mask_generic_dict(data: dict) -> dict:
    """通用 dict 脱敏：递归扫描常见 PII 字段名（name/phone/account/address/password）"""
    pii_keys = {
        "name": _mask_name,
        "full_name": _mask_name,
        "phone": _mask_phone,
        "contact": _mask_phone,
        "account": _mask_account,
        "account_number": _mask_account,
        "address": _mask_address,
        "location": _mask_address,
        "birth_date": _mask_birth_date,
    }
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k == "password":
            out["_password_removed"] = True
            continue
        if k in pii_keys and isinstance(v, str):
            out[k + "_masked"] = pii_keys[k](v)
        elif isinstance(v, dict):
            out[k] = _mask_generic_dict(v)
        elif isinstance(v, list):
            out[k] = [_mask_generic_dict(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out
