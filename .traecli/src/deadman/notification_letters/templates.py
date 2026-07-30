"""Phase 15: 8 类通知信函模板

每个模板使用 Python str.format 兼容的占位符语法。占位符统一格式：`[占位符名称]`。
注意：占位符两侧用方括号 `[]` 而非 `{}`，避免与 str.format 冲突；
本模块用 string.Template + 自定义替换逻辑处理。

模板结构（每张信函都包含）：
    - 标题
    - 称谓（致 X 机构）
    - 正文（含已填字段 + 占位符）
    - 落款（申请人姓名 + 申请日期占位符）
    - 附件清单

合规关联：
    - rules/integrity-framework.md：
        模板内不编造任何官方电话/地址/网址；
        所有需要办理人填写的具体信息用 [占位符] 表示
    - rules/service-boundary-framework.md：
        模板末尾附"本信函仅为草稿"提示
"""

from __future__ import annotations

from typing import Any

# ====================================================================
# 8 类信函类型常量（与 models.py 保持一致）
# ====================================================================
LETTER_TYPES: list[dict[str, Any]] = [
    {
        "type": "household_cancellation",
        "name": "户口注销通知",
        "recipient_default": "户籍所在地派出所",
        "extra_fields_needed": [
            "household_type",
            "household_address",
        ],
        "description": "致派出所，办理逝者户口注销手续",
    },
    {
        "type": "social_security_benefit",
        "name": "社保丧葬费申领",
        "recipient_default": "参保地社保局",
        "extra_fields_needed": [
            "insurance_location",
            "bank_account_masked",
            "bank_name",
        ],
        "description": "致社保局，申领丧葬费和抚恤金",
    },
    {
        "type": "provident_fund_withdrawal",
        "name": "公积金提取申请",
        "recipient_default": "公积金管理中心",
        "extra_fields_needed": [
            "account_balance",
            "heir_name",
            "heir_id_masked",
        ],
        "description": "致公积金中心，申请提取逝者公积金账户余额",
    },
    {
        "type": "medical_insurance_cancellation",
        "name": "医保账户注销",
        "recipient_default": "参保地医保局",
        "extra_fields_needed": [
            "medical_insurance_card_masked",
        ],
        "description": "致医保局，办理逝者医保账户注销",
    },
    {
        "type": "bank_account_inheritance",
        "name": "银行账户解冻/继承",
        "recipient_default": "开户银行",
        "extra_fields_needed": [
            "bank_name",
            "bank_account_masked",
            "inheritance_method",
        ],
        "description": "致银行，申请逝者账户解冻与继承",
    },
    {
        "type": "property_inheritance_notarization",
        "name": "房产继承公证申请",
        "recipient_default": "公证处",
        "extra_fields_needed": [
            "property_address",
            "heir_name",
            "heir_id_masked",
            "inheritance_method",
        ],
        "description": "致公证处，申请办理房产继承公证",
    },
    {
        "type": "credit_card_cancellation",
        "name": "信用卡销户",
        "recipient_default": "发卡银行",
        "extra_fields_needed": [
            "card_last_four",
        ],
        "description": "致发卡行，办理逝者信用卡销户",
    },
    {
        "type": "internet_account_cancellation",
        "name": "互联网账号注销",
        "recipient_default": "平台运营方",
        "extra_fields_needed": [
            "platform_name",
            "account_name",
        ],
        "description": "致平台方，申请注销逝者互联网账号",
    },
]


# ====================================================================
# 8 个信函模板
# ====================================================================
# 模板变量约定：
#   通用：{decedent_name} {decedent_id_masked} {death_date}
#         {applicant_name} {applicant_relationship} {recipient_org}
#   附加：从 extra_fields 取，模板里用 {xxx} 标记；缺失时转为 [xxx] 占位符
# 注意：模板里手写的 [xxx] 方括号是"用户必须手动填写"的占位符
#       （如 [派出所名称]、[派出所地址] 等办理机构的具体信息）

LETTER_TEMPLATES: dict[str, str] = {
    # ----------------------------------------------------------------
    # 1. 户口注销通知
    # ----------------------------------------------------------------
    "household_cancellation": """\
【户口注销申请】

致：{recipient_org}
[派出所全称]
[派出所详细地址]

申请人：{applicant_name}（与逝者关系：{applicant_relationship}）
申请人身份证号：[申请人身份证号]
联系电话：[申请人联系电话]

逝者信息：
  姓名：{decedent_name}
  身份证号：{decedent_id_masked}
  死亡日期：{death_date}
  户口性质：{household_type}
  户籍地址：{household_address}

申请事项：
  根据《中华人民共和国户口登记条例》第八条之规定，公民死亡后须由户主、
  亲属或利害关系人向户口登记机关申报死亡登记，注销户口。现申请办理上述
  逝者的户口注销手续。

附件清单：
  1. 死亡医学证明（原件 + 复印件）
  2. 逝者居民身份证（原件）
  3. 逝者户口簿（原件）
  4. 申请人身份证（原件 + 复印件）
  5. 申请人与逝者关系证明（如户口簿、结婚证、出生证等）
  6. [其他办理机构要求补充的材料]

申请人（签字）：{applicant_name}
申请日期：[申请日期]

---
本信函仅为草稿，具体格式请以办理机构要求为准。
""",
    # ----------------------------------------------------------------
    # 2. 社保丧葬费申领
    # ----------------------------------------------------------------
    "social_security_benefit": """\
【丧葬费、抚恤金申领申请】

致：{recipient_org}
[社保局全称]
[社保局详细地址]

申请人：{applicant_name}（与逝者关系：{applicant_relationship}）
申请人身份证号：[申请人身份证号]
联系电话：[申请人联系电话]

逝者信息：
  姓名：{decedent_name}
  身份证号：{decedent_id_masked}
  死亡日期：{death_date}
  参保地：{insurance_location}

申领事项：
  根据《中华人民共和国社会保险法》第十七条规定，参加基本养老保险的个人，
  因病或者非因工死亡的，其遗属可以领取丧葬补助金和抚恤金。现申请领取上述
  逝者的丧葬补助金、抚恤金，并指定以下银行账户为收款账户：
    开户行：{bank_name}
    账户号：{bank_account_masked}
    账户户名：{applicant_name}

附件清单：
  1. 死亡医学证明（原件 + 复印件）
  2. 逝者身份证、户口簿（注销页或注销证明）
  3. 申请人身份证（原件 + 复印件）
  4. 申请人与逝者关系证明
  5. 指定收款银行账户存折/卡复印件
  6. [其他办理机构要求补充的材料]

申请人（签字）：{applicant_name}
申请日期：[申请日期]

---
本信函仅为草稿，具体格式请以办理机构要求为准。
""",
    # ----------------------------------------------------------------
    # 3. 公积金提取申请
    # ----------------------------------------------------------------
    "provident_fund_withdrawal": """\
【住房公积金提取申请】

致：{recipient_org}
[公积金管理中心全称]
[公积金中心详细地址]

申请人（继承人）：{applicant_name}
申请人身份证号：[申请人身份证号]
与逝者关系：{applicant_relationship}
联系电话：[申请人联系电话]

逝者信息：
  姓名：{decedent_name}
  身份证号：{decedent_id_masked}
  死亡日期：{death_date}

继承情况：
  继承人姓名：{heir_name}
  继承人身份证号：{heir_id_masked}
  账户余额（截至申请日）：{account_balance}
  继承方式：{inheritance_method}

申请事项：
  根据《住房公积金管理条例》第二十四条之规定，职工死亡或者被宣告死亡的，
  其法定继承人或受遗赠人可以提取逝者住房公积金账户内的存储余额。现申请
  提取上述逝者住房公积金账户余额，并指定收款账户：
    开户行：[收款账户开户行]
    账户号：[收款账户号]
    账户户名：{heir_name}

附件清单：
  1. 死亡医学证明（原件 + 复印件）
  2. 继承权公证书或遗嘱（原件 + 复印件）
  3. 逝者身份证、户口簿注销证明
  4. 继承人身份证（原件 + 复印件）
  5. 收款银行账户存折/卡复印件
  6. [其他办理机构要求补充的材料]

申请人（签字）：{applicant_name}
申请日期：[申请日期]

---
本信函仅为草稿，具体格式请以办理机构要求为准。
""",
    # ----------------------------------------------------------------
    # 4. 医保账户注销
    # ----------------------------------------------------------------
    "medical_insurance_cancellation": """\
【医疗保险账户注销申请】

致：{recipient_org}
[医保局全称]
[医保局详细地址]

申请人：{applicant_name}（与逝者关系：{applicant_relationship}）
申请人身份证号：[申请人身份证号]
联系电话：[申请人联系电话]

逝者信息：
  姓名：{decedent_name}
  身份证号：{decedent_id_masked}
  医保卡号：{medical_insurance_card_masked}
  死亡日期：{death_date}

申请事项：
  根据《中华人民共和国社会保险法》及相关规定，参保人员死亡后其医疗保险
  关系终止，个人账户余额按规定处理。现申请办理上述逝者医保账户注销手续，
  并按规定办理个人账户余额退付/继承。

  个人账户余额退付指定收款账户：
    开户行：[收款账户开户行]
    账户号：[收款账户号]
    账户户名：{applicant_name}

附件清单：
  1. 死亡医学证明（原件 + 复印件）
  2. 逝者医保卡/社保卡（原件）
  3. 逝者身份证、户口簿注销证明
  4. 申请人身份证（原件 + 复印件）
  5. 申请人与逝者关系证明
  6. 收款银行账户存折/卡复印件
  7. [其他办理机构要求补充的材料]

申请人（签字）：{applicant_name}
申请日期：[申请日期]

---
本信函仅为草稿，具体格式请以办理机构要求为准。
""",
    # ----------------------------------------------------------------
    # 5. 银行账户解冻/继承
    # ----------------------------------------------------------------
    "bank_account_inheritance": """\
【银行账户解冻与继承申请】

致：{recipient_org}
[开户银行全称]
[开户行营业网点详细地址]

申请人：{applicant_name}（与逝者关系：{applicant_relationship}）
申请人身份证号：[申请人身份证号]
联系电话：[申请人联系电话]

逝者信息：
  姓名：{decedent_name}
  身份证号：{decedent_id_masked}
  死亡日期：{death_date}

账户信息：
  开户行：{bank_name}
  账号：{bank_account_masked}
  继承方式：{inheritance_method}

申请事项：
  根据《中华人民共和国民法典》继承编及《中国人民银行关于执行<储蓄管理
  条例>的若干规定》，存款人死亡后，其合法继承人可凭有效证明向银行办理
  账户解冻与存款继承。现申请办理上述逝者银行账户的解冻与继承手续。

  如账户余额超过 [银行规定免公证起付金额]，已附继承权公证书；
  如未超过，按照小额存款继承简化手续办理。

附件清单：
  1. 死亡医学证明（原件 + 复印件）
  2. 逝者身份证（原件）
  3. 申请人身份证（原件 + 复印件）
  4. 申请人与逝者关系证明
  5. 继承权公证书或遗嘱（如适用）
  6. 逝者银行存折/卡（原件）
  7. [其他办理机构要求补充的材料]

申请人（签字）：{applicant_name}
申请日期：[申请日期]

---
本信函仅为草稿，具体格式请以办理机构要求为准。
""",
    # ----------------------------------------------------------------
    # 6. 房产继承公证申请
    # ----------------------------------------------------------------
    "property_inheritance_notarization": """\
【房产继承公证申请】

致：{recipient_org}
[公证处全称]
[公证处详细地址]

申请人（继承人）：{applicant_name}
申请人身份证号：[申请人身份证号]
与逝者关系：{applicant_relationship}
联系电话：[申请人联系电话]

逝者（原产权人）信息：
  姓名：{decedent_name}
  身份证号：{decedent_id_masked}
  死亡日期：{death_date}

继承标的：
  房产地址：{property_address}
  房产证号：[房产证号/不动产权证书号]

继承情况：
  继承人姓名：{heir_name}
  继承人身份证号：{heir_id_masked}
  继承方式：{inheritance_method}

申请事项：
  根据《中华人民共和国民法典》继承编、《中华人民共和国公证法》第十一条
  之规定，现申请办理上述房产继承公证，由继承人 {heir_name} 依法继承逝者
  {decedent_name} 名下上述房产。

附件清单：
  1. 死亡医学证明（原件 + 复印件）
  2. 逝者身份证、户口簿注销证明
  3. 房产证/不动产权证书（原件 + 复印件）
  4. 全体法定继承人身份证、户口簿（原件 + 复印件）
  5. 申请人与逝者关系证明
  6. 被继承人生前所立遗嘱（如适用）
  7. 放弃继承权声明书（其他法定继承人放弃时提供，需公证）
  8. [其他办理机构要求补充的材料]

申请人（签字）：{applicant_name}
申请日期：[申请日期]

---
本信函仅为草稿，具体格式请以办理机构要求为准。
""",
    # ----------------------------------------------------------------
    # 7. 信用卡销户
    # ----------------------------------------------------------------
    "credit_card_cancellation": """\
【信用卡销户申请】

致：{recipient_org}
[发卡银行全称]
[发卡行信用卡中心详细地址]

申请人：{applicant_name}（与逝者关系：{applicant_relationship}）
申请人身份证号：[申请人身份证号]
联系电话：[申请人联系电话]

逝者信息：
  姓名：{decedent_name}
  身份证号：{decedent_id_masked}
  死亡日期：{death_date}

信用卡信息：
  发卡行：[发卡银行全称]
  卡号（后四位）：{card_last_four}

申请事项：
  根据《商业银行信用卡业务监督管理办法》及发卡行相关规定，持卡人死亡后
  其信用卡账户应办理销户手续。现申请办理上述逝者名下信用卡（卡号后四位
  {card_last_four}）的销户手续。

  欠款情况说明：[如有欠款，请说明还款安排；如无欠款，请填"无欠款"]

附件清单：
  1. 死亡医学证明（原件 + 复印件）
  2. 逝者身份证（原件）
  3. 信用卡原件（如可提供）
  4. 申请人身份证（原件 + 复印件）
  5. 申请人与逝者关系证明
  6. [其他办理机构要求补充的材料]

申请人（签字）：{applicant_name}
申请日期：[申请日期]

---
本信函仅为草稿，具体格式请以办理机构要求为准。
""",
    # ----------------------------------------------------------------
    # 8. 互联网账号注销
    # ----------------------------------------------------------------
    "internet_account_cancellation": """\
【互联网账号注销申请】

致：{recipient_org}
[平台运营方全称]
[平台运营方注册地址/客户服务部门]

申请人：{applicant_name}（与逝者关系：{applicant_relationship}）
申请人身份证号：[申请人身份证号]
联系电话：[申请人联系电话]

逝者信息：
  姓名：{decedent_name}
  身份证号：{decedent_id_masked}
  死亡日期：{death_date}

账号信息：
  平台名称：{platform_name}
  账号名称/ID：{account_name}
  注销原因：{cancellation_reason}

申请事项：
  根据《中华人民共和国网络安全法》《中华人民共和国个人信息保护法》
  第四十九条之规定，自然人死亡的，其近亲属为了自身的合法、正当利益，
  可以对死者的相关个人信息行使本章规定的查阅、复制、更正、删除等权利。
  现申请注销逝者在上述平台开设的账号，并删除其个人信息。

附件清单：
  1. 死亡医学证明（复印件）
  2. 逝者身份证（复印件）
  3. 申请人身份证（复印件）
  4. 申请人与逝者关系证明（如户口簿、结婚证、出生证等）
  5. [其他平台要求补充的材料]

申请人（签字）：{applicant_name}
申请日期：[申请日期]

---
本信函仅为草稿，具体格式请以办理机构要求为准。
""",
}


# ====================================================================
# 辅助：每种类型对应的 extra_fields 字段名清单（用于 CLI/Web 列出）
# ====================================================================
def get_required_extra_fields(letter_type: str) -> list[str]:
    """返回指定信函类型所需的 extra_fields 字段名清单"""
    for item in LETTER_TYPES:
        if item["type"] == letter_type:
            return list(item.get("extra_fields_needed", []))
    return []


def get_letter_type_meta(letter_type: str) -> dict[str, Any] | None:
    """返回指定信函类型的元信息 dict（或 None）"""
    for item in LETTER_TYPES:
        if item["type"] == letter_type:
            return dict(item)
    return None
