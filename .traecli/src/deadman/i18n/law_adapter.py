"""P8.5.3 Cross-border legal adapter - 跨境法律适配。

法规覆盖:
    - 中国大陆(CN_MAINLAND):民法典继承编 / PIPL / 数据安全法
    - 中国香港(CN_HONGKONG):遗嘱条例 / 个人资料(私隐)条例
    - 美国(US):统一遗嘱法 / 各州差异 / CCPA
    - 欧盟(EU):GDPR / 各国继承法 / eIDAS
    - 日本(JP):民法 / 个保法(APPI)
    - 韩国(KR):民法 / PIPA
    - 英国(UK):遗嘱法 / UK GDPR
    - 其他(OTHER):默认建议

关键差异点(数字遗产 / 遗嘱执行 / 数据删除):
    - 数字遗产:CN 不明 / US 部分州(RUFADAA)/ EU 各异
    - 遗嘱形式:CN 公证 / US 见证 / JP 自书
    - 数据删除:PIPL 7 天 / GDPR 30 天 / CCPA 45 天
    - 跨境同意:CN 必须 / EU 充分性 / US 不强制

⚠️ 警告:本模块内规则为起点参考,非法律意见。实际跨境法律事务
    必须咨询持牌律师。规则可由 JSON 配置覆盖(无外部 API 调用)。

feature flag:`DEADMAN_I18N_ENABLED=0` 关闭时 validate_cross_border 直接通过。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import resolve_data_path

logger = logging.getLogger(__name__)


class Jurisdiction(str, Enum):
    """司法管辖区枚举。"""

    CN_MAINLAND = "cn_mainland"  # 中国大陆
    CN_HONGKONG = "cn_hongkong"  # 中国香港
    US = "us"  # 美国
    EU = "eu"  # 欧盟
    JP = "jp"  # 日本
    KR = "kr"  # 韩国
    UK = "uk"  # 英国
    OTHER = "other"  # 其他 / 未识别

    @classmethod
    def from_locale(cls, locale_str: str) -> "Jurisdiction":
        """根据 locale 字符串(zh-CN / en-US / ...)推断管辖区。"""
        norm = locale_str.replace("_", "-").lower()
        if norm.startswith("zh-cn") or norm == "zh":
            return cls.CN_MAINLAND
        if norm.startswith("zh-tw") or norm.startswith("zh-hk"):
            return cls.CN_HONGKONG
        if norm.startswith("en-us"):
            return cls.US
        if norm.startswith("en-gb"):
            return cls.UK
        if norm.startswith("en"):
            # 其他英文国家默认 US
            return cls.US
        if norm.startswith("ja"):
            return cls.JP
        if norm.startswith("ko"):
            return cls.KR
        # 欧盟主要语言
        if norm.startswith(("de", "fr", "it", "es", "nl", "sv", "da", "fi",
                            "el", "pt", "cs", "pl", "ro", "hu")):
            return cls.EU
        return cls.OTHER


@dataclass
class ValidationResult:
    """跨境合规校验结果。

    Attributes:
        allowed: 是否允许执行
        jurisdiction_from: 用户所在管辖区
        jurisdiction_to: 目标管辖区
        data_kind: 数据类型(user_profile / chat_history / financial / legal_doc / digital_asset)
        consents_required: 需取得的同意列表(若 allowed=False,这些必须先取得)
        legal_basis: 法律依据(如 PIPL 第38条 / GDPR 第44条)
        reason: 失败原因(若 allowed=False)
        warnings: 警告信息(允许但有风险)
    """

    allowed: bool
    jurisdiction_from: str
    jurisdiction_to: str
    data_kind: str
    consents_required: list[str] = field(default_factory=list)
    legal_basis: str = ""
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =====================================================================
# 内置规则库
# 起点 - 真实生产环境应由法务团队审核 + JSON 配置覆盖
# ⚠️ 警告:此处内容仅为框架性参考,不构成法律意见
# =====================================================================

_INHERITANCE_LAWS: dict[Jurisdiction, dict[str, Any]] = {
    Jurisdiction.CN_MAINLAND: {
        "statute": "中华人民共和国民法典 第六编 继承(2021.1.1 施行)",
        "key_rules": [
            "法定继承顺序:第一顺序(配偶 / 子女 / 父母),第二顺序(兄弟姐妹 / 祖父母 / 外祖父母)",
            "遗嘱形式:公证遗嘱 / 自书遗嘱 / 代书遗嘱 / 录音录像遗嘱 / 口头遗嘱(紧急情况)",
            "公证遗嘱不再具有最高效力(民法典取消,多份遗嘱以最后一份为准)",
            "数字遗产:无专门立法,可参照《个人信息保护法》第 49 条处理(死后近亲属可删除)",
            "继承权丧失:故意杀害被继承人 / 遗弃 / 严重虐待 / 伪造 / 篡改 / 销毁遗嘱",
        ],
        "probate_process": [
            "1. 死亡证明(医院 / 公安机关出具)",
            "2. 持遗嘱至公证处办理遗嘱继承公证(或法定继承公证)",
            "3. 凭公证书办理不动产过户 / 银行账户解冻 / 股权变更",
            "4. 数字遗产:联系平台凭继承公证书办理(各平台规则不一)",
        ],
        "time_limits": {
            "accept_or_reject_inheritance": "知道继承后 50 日内表示",
            "statute_of_limitation": "继承权纠纷诉讼时效 3 年(自知道权利受侵害起)",
            "max_statute": "自继承开始超 20 年不得起诉",
        },
    },
    Jurisdiction.CN_HONGKONG: {
        "statute": "遗嘱条例(香港法例第 30 章)/ 无遗嘱者遗产条例(第 73 章)",
        "key_rules": [
            "遗嘱需书面 + 由立遗嘱人在两名见证人面前签署(见证人不得为受益人)",
            "无遗嘱:配偶先获 50 万 + 剩余一半,余下由子女均分",
            "数字遗产:无专门立法,平台 ToS 通常决定访问权",
            "境外遗嘱可在香港申请盖印(sealing)以本地执行",
        ],
        "probate_process": [
            "1. 死亡证明",
            "2. 向高等法院遗产承办处申请授予书(Grant of Representation)",
            "3. 遗嘱执行人凭授予书处理资产",
            "4. 数字遗产:依平台政策,通常需法庭命令",
        ],
        "time_limits": {
            "apply_for_grant": "无明确期限,但建议 12 个月内",
            "statute_of_limitation": "6 年(合约一般时效)",
        },
    },
    Jurisdiction.US: {
        "statute": "统一遗嘱法 (UPC) - 各州有差异;数字遗产:RUFADAA(2015 已在多数州通过)",
        "key_rules": [
            "遗嘱需书面 + 立遗嘱人 + 2 名见证人(部分州允许自书遗嘱 holographic will)",
            "数字遗产:RUFADAA 给予遗嘱执行人访问数字账户的法定权限(2017 后多数州)",
            "州法院 probate 管辖(死者的 domicile state)",
            "配偶强制份额(elective share)通常 1/3",
        ],
        "probate_process": [
            "1. 死亡证明",
            "2. 向死者居住地法院提交遗嘱 / 申请遗产管理人 appointment",
            "3. 通知债权人(公告期 3-6 个月)",
            "4. 清偿债务 + 分配剩余资产",
            "5. 数字遗产:遗嘱执行人凭法庭授权访问账户",
        ],
        "time_limits": {
            "creditor_claim": "通常 3-6 个月(州法定)",
            "statute_of_limitation": "各州不同,通常 1-4 年",
        },
    },
    Jurisdiction.EU: {
        "statute": "欧盟继承条例 EU 650/2012(EU Succession Regulation)+ GDPR",
        "key_rules": [
            "管辖权:死者最后 habitual residence(2015.8.17 起)",
            "适用法律:死者 habitual residence 国法律(可指定选择本国法)",
            "数字遗产:GDPR 不直接覆盖死者,但成员国国内法各异",
            "European Certificate of Succession 跨成员国通用",
        ],
        "probate_process": [
            "1. 死亡证明",
            "2. 申请 European Certificate of Succession(跨国可用)",
            "3. 凭证书在各成员国办理资产过户",
            "4. 数字遗产:依各国国内法,通常需法庭命令",
        ],
        "time_limits": {
            "creditor_claim": "成员国各异,通常 6 个月",
            "statute_of_limitation": "成员国各异,通常 10-30 年",
        },
    },
    Jurisdiction.JP: {
        "statute": "日本民法 第 5 章 相続(2022.4 修订)+ 個人情報保護法(APPI)",
        "key_rules": [
            "法定继承顺序:配偶(始终)+ 子女 → 直系尊属 → 兄弟姐妹",
            "配偶法定相続分:子 1/2 / 直系尊属 2/3 / 兄弟姐妹 3/4",
            "自书証書遺言:全文手写 + 日期 + 签名盖章(无需见证人)",
            "公正証書遺言:公证人在场,需 2 名见证人(无利益冲突)",
            "2022 起新设「配偶者居住権」保障生存配偶居住权",
            "数字遗产:无专门立法,近年案例倾向可继承",
        ],
        "probate_process": [
            "1. 死亡届(7 日内提交)",
            "2. 遺言書検索(公正証書遺言在法务局登记)",
            "3. 家庭裁判所申立遺産分割(继承人协议不成时)",
            "4. 遺産分割協議书作成 + 全员签名盖章",
            "5. 数字遗产:凭遺産分割協議书 + 法庭命令访问",
        ],
        "time_limits": {
            "renunciation": "知道继承起 3 个月内(家庭裁判所申立)",
            "statute_of_limitation": "10 年(相続回复請求権)",
            "tax_filing": "知道继承起 10 个月内申告相続税",
        },
    },
    Jurisdiction.KR: {
        "statute": "韩国民法 第 5 编 相続(2021 修订)+ 个人情報保護法(PIPA)",
        "key_rules": [
            "法定继承顺序:直系卑属 → 直系尊属 → 兄弟姐妹 → 4 寸以内旁系",
            "配偶始终参与(法定相続分:子 1.5 / 直系尊属 0.5 / 兄弟 0.5 加算)",
            "遗嘱形式:自书 / 录音 / 公证 / 秘密证(2 名见证人)",
            "2021 新设「配偶者短期居住権」(1 年内可继续居住)",
            "数字遗产:无专门立法,平台 ToS 主导",
        ],
        "probate_process": [
            "1. 死亡申告(1 个月内)",
            "2. 遗言书検索(公证遗言在公证役場)",
            "3. 相続财产分割协议(全体一致)",
            "4. 不成立时:家庭法院 相続分割審判 申立",
            "5. 数字遗产:凭相続分割协议 + 法庭命令",
        ],
        "time_limits": {
            "renunciation": "知道继承起 3 个月内(家庭法院申立)",
            "statute_of_limitation": "继承权回复请求 3 年 / 财产权一般 10 年",
            "tax_filing": "知道继承起 6 个月内申告相续税",
        },
    },
    Jurisdiction.UK: {
        "statute": "Wills Act 1837 / Administration of Estates Act 1925 / UK GDPR",
        "key_rules": [
            "遗嘱需书面 + 立遗嘱人 + 2 名见证人(见证人不得为受益人或配偶)",
            "无遗嘱:配偶先获 £270,000 + 个人物品 + 剩余一半,余下分给子女",
            "数字遗产:无专门立法,但 2017 起遗嘱可包含数字资产条款",
            "苏格兰法律与英格兰 / 威尔士不同",
        ],
        "probate_process": [
            "1. 死亡证明(register death within 5 days)",
            "2. 申请 Grant of Probate(有遗嘱)/ Letters of Administration(无遗嘱)",
            "3. 遗产税(IHT)申报超过 £325,000 部分 40%",
            "4. 数字遗产:依平台 ToS,通常需 Grant of Probate",
        ],
        "time_limits": {
            "register_death": "5 日内",
            "probate_application": "无明确期限,建议 6 个月内",
            "iht_filing": "死亡后 6 个月内申报,12 个月内缴税",
        },
    },
    Jurisdiction.OTHER: {
        "statute": "未知 / 默认建议:咨询当地持牌律师",
        "key_rules": [
            "无内置规则,需查询当地继承法",
            "数字遗产:多数司法管辖区尚无专门立法",
            "建议:在遗嘱中明确包含数字资产处置条款",
        ],
        "probate_process": [
            "1. 死亡证明",
            "2. 咨询当地律师启动相关程序",
            "3. 数字遗产:依平台 ToS",
        ],
        "time_limits": {
            "statute_of_limitation": "依当地法律",
        },
    },
}


_DATA_PROTECTION_LAWS: dict[Jurisdiction, dict[str, Any]] = {
    Jurisdiction.CN_MAINLAND: {
        "law": "个人信息保护法(PIPL,2021.11.1 施行)+ 数据安全法(DSL,2021.9.1)",
        "key_principles": [
            "同意基础:处理个人信息需明示同意(第 13 条)",
            "跨境传输:第 38-43 条,需单独同意 + 安全评估 / 认证 / 标准合同",
            "数据主体权利:查询 / 复制 / 更正 / 删除 / 撤回同意(第 44-47 条)",
            "死者近亲属权利:第 49 条,死后近亲属可查询 / 复制 / 删除",
            "敏感个人信息:需单独同意(第 29 条)",
            "生成式 AI:需标识 AI 生成内容(2023.7 试行办法)",
        ],
        "right_to_delete_days": 7,
        "cross_border_consent_required": True,
        "data_localization": "关键信息基础设施运营者必须境内存储",
        "regulator": "国家网信办(CAC)",
    },
    Jurisdiction.CN_HONGKONG: {
        "law": "个人资料(私隐)条例(PDPO,1996 施行,2021 修订)+ 2022 修订加强",
        "key_principles": [
            "6 项保障资料原则:目的 / 准确 / 收集 / 使用 / 安全 / 透明",
            "数据主体权利:查询 / 更正 / 反对直接营销",
            "跨境传输:无明确同意要求,但需通知(第 33 条尚未生效)",
            "2022 新增:资料外泄强制通知(合理时间通知私隐专员)",
        ],
        "right_to_delete_days": 30,
        "cross_border_consent_required": False,
        "data_localization": "无强制要求",
        "regulator": "个人资料私隐专员公署(PCPD)",
    },
    Jurisdiction.US: {
        "law": "无统一联邦法 - 加州 CCPA / CPRA(2023), 其他州各有差异",
        "key_principles": [
            "CCPA:加州居民对个人信息有查询 / 删除 / 反对出售 / 不歧视权",
            "CPRA:敏感个人信息(健康 / 财务 / 位置)单独同意",
            "未成年(16 以下):需 opt-in",
            "COPPA:13 岁以下儿童数据需父母同意",
            "数字遗产:RUFADAA 在多数州通过(2015-)",
            "州际差异大,真实业务需逐州评估",
        ],
        "right_to_delete_days": 45,
        "cross_border_consent_required": False,
        "data_localization": "无强制要求",
        "regulator": "FTC(联邦)+ 各州 AG",
    },
    Jurisdiction.EU: {
        "law": "通用数据保护条例(GDPR,2018.5.25 施行)+ ePrivacy Directive",
        "key_principles": [
            "六大合法处理依据:同意 / 合同 / 法定义务 / 重要利益 / 公共任务 / 合法利益",
            "数据主体权利:查询 / 更正 / 删除 / 限制 / 反对 / 可携(第 15-22 条)",
            "跨境传输:第 44-50 条,需充分性认定 / 标准合同条款 / BCR / 特定情形",
            "死者数据:GDPR 不直接覆盖,但成员国国内法可保护",
            "罚款:最高 2000 万欧元 / 全球营业额 4%",
        ],
        "right_to_delete_days": 30,
        "cross_border_consent_required": True,
        "data_localization": "无强制,但跨境需合法依据",
        "regulator": "EDPB(欧盟)+ 各国 DPA",
    },
    Jurisdiction.JP: {
        "law": "個人情報保護法(APPI,2022.4 全面修订生效)",
        "key_principles": [
            "利用目的明示 + 同意(原则上)",
            "数据主体权利:查询 / 更正 / 利用停止(第 33-37 条)",
            "跨境传输:需同意 + 接收方在 PIPA 充分性国家白名单 / 等同保护",
            "敏感信息:人种 / 信仰 / 病歴 / 犯罪経歴 需特别注意",
            "匿名加工情報:加工后无需同意可提供",
        ],
        "right_to_delete_days": 30,
        "cross_border_consent_required": True,
        "data_localization": "无强制要求",
        "regulator": "個人情報保護委員会(PPC)",
    },
    Jurisdiction.KR: {
        "law": "个人情報保護法(PIPA,2011 施行,2023 大幅修订)",
        "key_principles": [
            "收集同意:明示 + 单独同意(敏感信息 / 跨境)",
            "数据主体权利:查询 / 更正 / 删除 / 处理停止",
            "跨境传输:需单独同意 + 接收方所在国保护水平评估",
            "2023 修订:罚款上限提高至全球营业额 3%",
            "敏感信息:居民登记号 / 健康信息 / 生物特征单独同意",
        ],
        "right_to_delete_days": 14,
        "cross_border_consent_required": True,
        "data_localization": "无强制要求",
        "regulator": "个人情報保護委员会(PIPC)",
    },
    Jurisdiction.UK: {
        "law": "UK GDPR + Data Protection Act 2018",
        "key_principles": [
            "脱欧后 UK GDPR 基本沿用欧盟 GDPR",
            "数据主体权利与 GDPR 一致",
            "跨境传输:需充足性认定 / IDTA(国际数据转移协议)",
            "ICO 可对英国境内违规罚款 £17.5M / 4% 全球营业额",
        ],
        "right_to_delete_days": 30,
        "cross_border_consent_required": True,
        "data_localization": "无强制要求",
        "regulator": "Information Commissioner's Office(ICO)",
    },
    Jurisdiction.OTHER: {
        "law": "未知 - 默认按 GDPR 严格标准执行(最保守)",
        "key_principles": [
            "默认要求明示同意",
            "跨境传输默认需同意",
            "建议咨询当地持牌律师",
        ],
        "right_to_delete_days": 30,
        "cross_border_consent_required": True,
        "data_localization": "无强制要求",
        "regulator": "未知 - 默认按 GDPR 标准",
    },
}


# 行动类型(action)对应所需同意(按管辖区)
# action 取值:user_profile_export / data_delete / cross_border_transfer /
#               will_execution / digital_asset_handover / ai_training /
#               marketing / automated_decision
_ACTION_CONSENTS: dict[Jurisdiction, dict[str, list[str]]] = {
    Jurisdiction.CN_MAINLAND: {
        "user_profile_export": ["privacy_policy", "data_export_consent"],
        "data_delete": ["privacy_policy"],  # 第 47 条不需额外同意
        "cross_border_transfer": ["cross_border_consent", "sensitive_data_consent"],
        "will_execution": ["terms_of_service"],
        "digital_asset_handover": ["terms_of_service", "digital_asset_consent"],
        "ai_training": ["ai_training_consent"],
        "marketing": ["marketing_consent"],
        "automated_decision": ["automated_decision_consent"],
    },
    Jurisdiction.CN_HONGKONG: {
        "user_profile_export": ["privacy_policy", "data_access_request"],
        "data_delete": ["privacy_policy"],
        "cross_border_transfer": ["cross_border_notice"],  # PDPO 第 33 条尚未生效
        "will_execution": ["terms_of_service"],
        "digital_asset_handover": ["terms_of_service"],
        "ai_training": ["ai_training_consent"],
        "marketing": ["marketing_consent"],
        "automated_decision": ["automated_decision_consent"],
    },
    Jurisdiction.US: {
        "user_profile_export": ["privacy_policy"],  # CCPA 默认 opt-out
        "data_delete": ["privacy_policy", "ccpa_deletion_request"],
        "cross_border_transfer": [],  # 联邦层无强制
        "will_execution": ["terms_of_service"],
        "digital_asset_handover": ["terms_of_service"],
        "ai_training": ["privacy_policy"],
        "marketing": ["marketing_opt_out"],
        "automated_decision": ["privacy_policy"],
    },
    Jurisdiction.EU: {
        "user_profile_export": ["privacy_policy", "gdpr_subject_access"],
        "data_delete": ["privacy_policy", "gdpr_erasure_request"],
        "cross_border_transfer": ["cross_border_consent", "scc_agreement"],
        "will_execution": ["terms_of_service"],
        "digital_asset_handover": ["terms_of_service", "digital_asset_consent"],
        "ai_training": ["ai_training_consent"],
        "marketing": ["marketing_consent"],
        "automated_decision": ["automated_decision_consent"],
    },
    Jurisdiction.JP: {
        "user_profile_export": ["privacy_policy", "appi_disclosure_request"],
        "data_delete": ["privacy_policy"],
        "cross_border_transfer": ["cross_border_consent"],
        "will_execution": ["terms_of_service"],
        "digital_asset_handover": ["terms_of_service"],
        "ai_training": ["ai_training_consent"],
        "marketing": ["marketing_consent"],
        "automated_decision": ["automated_decision_consent"],
    },
    Jurisdiction.KR: {
        "user_profile_export": ["privacy_policy", "pipa_access_request"],
        "data_delete": ["privacy_policy", "pipa_deletion_request"],
        "cross_border_transfer": ["cross_border_consent", "pipa_transfer_consent"],
        "will_execution": ["terms_of_service"],
        "digital_asset_handover": ["terms_of_service"],
        "ai_training": ["ai_training_consent"],
        "marketing": ["marketing_consent"],
        "automated_decision": ["automated_decision_consent"],
    },
    Jurisdiction.UK: {
        "user_profile_export": ["privacy_policy", "uk_gdpr_subject_access"],
        "data_delete": ["privacy_policy", "uk_gdpr_erasure_request"],
        "cross_border_transfer": ["cross_border_consent", "idta_agreement"],
        "will_execution": ["terms_of_service"],
        "digital_asset_handover": ["terms_of_service", "digital_asset_consent"],
        "ai_training": ["ai_training_consent"],
        "marketing": ["marketing_consent"],
        "automated_decision": ["automated_decision_consent"],
    },
    Jurisdiction.OTHER: {
        "user_profile_export": ["privacy_policy"],
        "data_delete": ["privacy_policy"],
        "cross_border_transfer": ["cross_border_consent"],
        "will_execution": ["terms_of_service"],
        "digital_asset_handover": ["terms_of_service"],
        "ai_training": ["ai_training_consent"],
        "marketing": ["marketing_consent"],
        "automated_decision": ["automated_decision_consent"],
    },
}


# 跨境合规规则:user_jurisdiction → target_jurisdiction → data_kind → 校验规则
# 规则中 allowed=True 表示允许,allowed=False 表示禁止(需取得 consents_required 中同意)
_CROSS_BORDER_RULES: dict[Jurisdiction, dict[Jurisdiction, dict[str, dict[str, Any]]]] = {
    Jurisdiction.CN_MAINLAND: {
        Jurisdiction.US: {
            "default": {"allowed": False, "consents_required": ["cross_border_consent"],
                        "legal_basis": "PIPL 第 38-43 条", "warnings": ["数据出境需安全评估"]},
            "user_profile": {"allowed": False, "consents_required": ["cross_border_consent"],
                              "legal_basis": "PIPL 第 38 条", "warnings": ["个人信息出境需单独同意"]},
            "financial": {"allowed": False, "consents_required": ["cross_border_consent",
                                                                    "sensitive_data_consent"],
                           "legal_basis": "PIPL 第 28-29 条 + 第 38 条",
                           "warnings": ["财务信息为敏感个人信息,需双重同意 + 安全评估"]},
        },
        Jurisdiction.EU: {
            "default": {"allowed": False, "consents_required": ["cross_border_consent"],
                        "legal_basis": "PIPL 第 38-43 条 / GDPR 第 44 条",
                        "warnings": ["中欧双向均需同意"]},
        },
        Jurisdiction.CN_HONGKONG: {
            "default": {"allowed": False, "consents_required": ["cross_border_consent"],
                        "legal_basis": "PIPL 第 38 条", "warnings": ["陆港视为跨境"]},
        },
    },
    Jurisdiction.EU: {
        Jurisdiction.CN_MAINLAND: {
            "default": {"allowed": False, "consents_required": ["scc_agreement"],
                        "legal_basis": "GDPR 第 44-50 条",
                        "warnings": ["中国未通过欧盟充分性认定"]},
        },
        Jurisdiction.US: {
            "default": {"allowed": False, "consents_required": ["scc_agreement"],
                        "legal_basis": "GDPR 第 44-50 条",
                        "warnings": ["美国未通过欧盟充分性认定(隐私盾失效)"]},
        },
    },
    Jurisdiction.US: {
        Jurisdiction.CN_MAINLAND: {
            "default": {"allowed": True, "consents_required": [],
                        "legal_basis": "无联邦强制", "warnings": ["中国接收方需符合 PIPL"]},
        },
        Jurisdiction.EU: {
            "default": {"allowed": True, "consents_required": [],
                        "legal_basis": "无联邦强制", "warnings": ["接收方需符合 GDPR"]},
        },
    },
    Jurisdiction.CN_HONGKONG: {
        Jurisdiction.CN_MAINLAND: {
            "default": {"allowed": True, "consents_required": ["cross_border_notice"],
                        "legal_basis": "PDPO 第 33 条(尚未生效)",
                        "warnings": ["建议通知数据主体"]},
        },
    },
    Jurisdiction.JP: {
        Jurisdiction.CN_MAINLAND: {
            "default": {"allowed": False, "consents_required": ["cross_border_consent"],
                        "legal_basis": "APPI 第 28 条",
                        "warnings": ["中国不在 PPC 充分性白名单"]},
        },
    },
    Jurisdiction.KR: {
        Jurisdiction.CN_MAINLAND: {
            "default": {"allowed": False, "consents_required": ["cross_border_consent",
                                                                  "pipa_transfer_consent"],
                        "legal_basis": "PIPA 第 28 条", "warnings": ["需评估接收方国家保护水平"]},
        },
    },
    Jurisdiction.UK: {
        Jurisdiction.CN_MAINLAND: {
            "default": {"allowed": False, "consents_required": ["idta_agreement"],
                        "legal_basis": "UK GDPR 第 44 条", "warnings": ["需 IDTA 协议"]},
        },
    },
}


class LawAdapter:
    """跨境法律适配器。

    特点:
        - 纯本地规则库(无外部 API)
        - 内置 8 个司法管辖区规则
        - 可通过 load_config() 加载 JSON 配置覆盖
        - 跨境校验返回 ValidationResult(allowed + consents_required + legal_basis + warnings)

    ⚠️ 警告:本类返回内容仅为框架性参考,不构成法律意见。
        真实跨境法律事务必须咨询持牌律师。
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        if config_path is not None and not isinstance(config_path, Path):
            config_path = Path(config_path)
        self.config_path = config_path
        # 深拷贝规则库,允许实例级覆盖
        self._inheritance: dict[Jurisdiction, dict[str, Any]] = {
            j: dict(rules) for j, rules in _INHERITANCE_LAWS.items()
        }
        self._data_protection: dict[Jurisdiction, dict[str, Any]] = {
            j: dict(rules) for j, rules in _DATA_PROTECTION_LAWS.items()
        }
        self._action_consents: dict[Jurisdiction, dict[str, list[str]]] = {
            j: dict(actions) for j, actions in _ACTION_CONSENTS.items()
        }
        self._cross_border: dict[Jurisdiction, dict[Jurisdiction, dict[str, dict[str, Any]]]] = {
            src: {dst: dict(rules) for dst, rules in rules_map.items()}
            for src, rules_map in _CROSS_BORDER_RULES.items()
        }
        self._lock = threading.RLock()
        if config_path is not None and config_path.exists():
            self.load_config(config_path)

    # ==================================================================
    # 继承法
    # ==================================================================

    def get_inheritance_law(self, jurisdiction: Jurisdiction | str) -> dict:
        """获取司法管辖区的继承法规则。

        Returns:
            dict 包含:
                - statute: 法规名称
                - key_rules: 关键规则列表
                - probate_process: 遗产处置流程
                - time_limits: 时效限制
        """
        j = jurisdiction if isinstance(jurisdiction, Jurisdiction) else Jurisdiction(jurisdiction)
        with self._lock:
            return dict(self._inheritance.get(j, self._inheritance[Jurisdiction.OTHER]))

    def get_data_protection_law(self, jurisdiction: Jurisdiction | str) -> dict:
        """获取数据保护法规则。

        Returns:
            dict 包含:
                - law: 法规名称
                - key_principles: 关键原则列表
                - right_to_delete_days: 数据删除时效(天)
                - cross_border_consent_required: 跨境同意是否必需
                - data_localization: 数据本地化要求
                - regulator: 监管机构
        """
        j = jurisdiction if isinstance(jurisdiction, Jurisdiction) else Jurisdiction(jurisdiction)
        with self._lock:
            return dict(self._data_protection.get(j, self._data_protection[Jurisdiction.OTHER]))

    def get_required_consents(
        self,
        jurisdiction: Jurisdiction | str,
        action: str,
    ) -> list[str]:
        """获取执行某动作所需的同意列表。"""
        j = jurisdiction if isinstance(jurisdiction, Jurisdiction) else Jurisdiction(jurisdiction)
        with self._lock:
            actions = self._action_consents.get(j, self._action_consents[Jurisdiction.OTHER])
            return list(actions.get(action, []))

    def validate_cross_border(
        self,
        user_jurisdiction: Jurisdiction | str,
        target_jurisdiction: Jurisdiction | str,
        data_kind: str = "default",
    ) -> ValidationResult:
        """跨境合规校验。

        Args:
            user_jurisdiction: 用户所在管辖区
            target_jurisdiction: 目标管辖区
            data_kind: 数据类型(默认 "default",特殊类型: user_profile / financial /
                       chat_history / legal_doc / digital_asset)

        Returns:
            ValidationResult:
                - allowed=True: 可执行
                - allowed=False: 需先取得 consents_required
        """
        uj = user_jurisdiction if isinstance(user_jurisdiction, Jurisdiction) else \
            Jurisdiction(user_jurisdiction)
        tj = target_jurisdiction if isinstance(target_jurisdiction, Jurisdiction) else \
            Jurisdiction(target_jurisdiction)

        # feature flag 关闭:直接允许(向后兼容)
        if not is_enabled("i18n"):
            return ValidationResult(
                allowed=True,
                jurisdiction_from=uj.value,
                jurisdiction_to=tj.value,
                data_kind=data_kind,
                legal_basis="i18n_disabled",
                warnings=["i18n feature disabled, skipping cross-border validation"],
            )

        # 同管辖区:不需跨境
        if uj == tj:
            return ValidationResult(
                allowed=True,
                jurisdiction_from=uj.value,
                jurisdiction_to=tj.value,
                data_kind=data_kind,
                legal_basis="same_jurisdiction",
            )

        with self._lock:
            src_rules = self._cross_border.get(uj, {})
            target_rules = src_rules.get(tj, {})
            # 优先 data_kind 专门规则,否则 default
            rule = target_rules.get(data_kind) or target_rules.get("default")

        if rule is None:
            # 无内置规则:默认按最保守(需跨境同意)
            dp = self._data_protection.get(uj, self._data_protection[Jurisdiction.OTHER])
            return ValidationResult(
                allowed=False,
                jurisdiction_from=uj.value,
                jurisdiction_to=tj.value,
                data_kind=data_kind,
                consents_required=["cross_border_consent"] if dp.get("cross_border_consent_required") else [],
                legal_basis="default_precautionary",
                warnings=[f"No specific rule for {uj.value}→{tj.value} ({data_kind}); "
                          f"using precautionary default. Consult lawyer."],
            )

        return ValidationResult(
            allowed=rule.get("allowed", False),
            jurisdiction_from=uj.value,
            jurisdiction_to=tj.value,
            data_kind=data_kind,
            consents_required=list(rule.get("consents_required", [])),
            legal_basis=rule.get("legal_basis", ""),
            warnings=list(rule.get("warnings", [])),
        )

    # ==================================================================
    # 配置覆盖(JSON)
    # ==================================================================

    def load_config(self, path: str | Path) -> int:
        """从 JSON 加载配置覆盖规则库。

        JSON 格式:
            {
                "inheritance_law": {"cn_mainland": {...}},
                "data_protection_law": {"cn_mainland": {...}},
                "action_consents": {"cn_mainland": {"action": ["consent"]}},
                "cross_border_rules": {"cn_mainland": {"us": {"default": {...}}}}
            }
        """
        path = Path(path)
        if not path.exists():
            logger.warning("Law config not found: %s", path)
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to load law config %s: %s", path, e)
            return 0

        count = 0
        with self._lock:
            for j_str, rules in (data.get("inheritance_law") or {}).items():
                try:
                    self._inheritance[Jurisdiction(j_str)] = rules
                    count += 1
                except ValueError:
                    logger.warning("Unknown jurisdiction in config: %s", j_str)
            for j_str, rules in (data.get("data_protection_law") or {}).items():
                try:
                    self._data_protection[Jurisdiction(j_str)] = rules
                    count += 1
                except ValueError:
                    continue
            for j_str, actions in (data.get("action_consents") or {}).items():
                try:
                    self._action_consents[Jurisdiction(j_str)] = {
                        action: list(consents) for action, consents in actions.items()
                    }
                    count += 1
                except ValueError:
                    continue
            for src_str, target_map in (data.get("cross_border_rules") or {}).items():
                try:
                    src = Jurisdiction(src_str)
                    for dst_str, rules_map in target_map.items():
                        try:
                            dst = Jurisdiction(dst_str)
                            self._cross_border.setdefault(src, {})[dst] = {
                                kind: dict(rule) for kind, rule in rules_map.items()
                            }
                            count += 1
                        except ValueError:
                            continue
                except ValueError:
                    continue
        logger.info("Loaded %d law config overrides from %s", count, path)
        return count

    def list_jurisdictions(self) -> list[Jurisdiction]:
        """列出已配置的司法管辖区。"""
        return list(Jurisdiction)

    def reload_defaults(self) -> None:
        """重置回内置默认规则(测试用)。"""
        with self._lock:
            self._inheritance = {j: dict(rules) for j, rules in _INHERITANCE_LAWS.items()}
            self._data_protection = {j: dict(rules) for j, rules in _DATA_PROTECTION_LAWS.items()}
            self._action_consents = {j: dict(actions) for j, actions in _ACTION_CONSENTS.items()}
            self._cross_border = {
                src: {dst: dict(rules) for dst, rules in rules_map.items()}
                for src, rules_map in _CROSS_BORDER_RULES.items()
            }


# 全局单例
_law_adapter_instance: Optional[LawAdapter] = None
_law_adapter_lock = threading.Lock()


def get_law_adapter() -> LawAdapter:
    """获取全局 LawAdapter 单例。"""
    global _law_adapter_instance
    if _law_adapter_instance is None:
        with _law_adapter_lock:
            if _law_adapter_instance is None:
                _law_adapter_instance = LawAdapter()
    return _law_adapter_instance


def reset_law_adapter() -> None:
    """重置全局单例(测试用)。"""
    global _law_adapter_instance
    with _law_adapter_lock:
        _law_adapter_instance = None
