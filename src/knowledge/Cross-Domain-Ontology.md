# 跨域本体设计

> 本文件定义身后事 + 医疗导航 + 法律 + 财务 + 跨境 + 政策研究 6 个领域的统一本体（Ontology）。借鉴 Schema.org、Wikidata、DBpedia、FOAF、Schema.org Health、Dublin Core、OpenKG-CN、CN-DBpedia。
>
> **目的**：给 LightRAG 知识图谱、Graphiti 时态记忆、MCP query_knowledge 工具、6 个并列智能体提供统一的实体/关系/属性词汇表，消除"death-aftercare 说'死亡证明'、legal-advisor 说'死亡证书'、medical-guide 说'死亡医学证明书'"这类同义词混乱。

## 为什么需要跨域本体

### 当前痛点

```
death-aftercare:    "死亡证明" → stage-1-death-certificate.md
legal-advisor:      "死亡证书" → 继承法相关
medical-guide:      "死亡医学证明书" → 医疗纠纷
cross-border:       "Death Certificate" → 加州政策
financial-analyst:  "死亡证明" → 保险理赔

→ 5 个智能体用 5 种说法指同一个东西
→ LightRAG 知识图谱里会有 4 个孤立节点
→ query_knowledge 检索召回率下降
→ Graphiti 时态查询无法跨智能体关联
```

### 本体补强

统一本体提供：
1. **规范实体类型**：所有智能体共用同一套实体类型
2. **同义词归一**：死亡证明 = 死亡证书 = Death Certificate（同一实体，不同 label）
3. **跨域关系**：死亡证明 → requires → 户口注销（跨 death-aftercare 与 legal-advisor 域）
4. **属性约束**：每个实体类型的必填/可选属性
5. **多语言标签**：中英日三语 label，支持 cross-border-specialist

## 本体分层结构

```
┌─────────────────────────────────────────────────┐
│  顶层本体（Top-Level Ontology）                  │
│  Thing → Entity / Event / Relation / Property   │
├─────────────────────────────────────────────────┤
│  跨域共享层（Shared Layer）                       │
│  Person / Organization / Document / Location /  │
│  Time / Money / Role                            │
├─────────────────────────────────────────────────┤
│  领域层（Domain Layer）                          │
│  ┌──────────┬──────────┬──────────┬──────────┐  │
│  │ 身后事域  │ 医疗域   │ 法律域   │ 财务域   │  │
│  ├──────────┼──────────┼──────────┼──────────┤  │
│  │ 跨境域    │ 政策域   │          │          │  │
│  └──────────┴──────────┴──────────┴──────────┘  │
├─────────────────────────────────────────────────┤
│  实例层（Instance Layer）                        │
│  CN/US/JP 地域实例 + 具体法规/机构/流程实例      │
└─────────────────────────────────────────────────┘
```

## 顶层本体

```python
# ontology/top_level.py（伪代码 - OWL/Turtle 风格）

# 顶层类层级
class Thing:
    """所有实体的基类"""

    pass


class Entity(Thing):
    """实体 - 有持久存在的对象"""

    properties: list[str]  # 属性
    labels: dict[str, str]  # 多语言标签 {zh: "...", en: "...", ja: "..."}


class Event(Thing):
    """事件 - 有时间发生的动作"""

    start_time: datetime
    end_time: Optional[datetime]
    location: Optional[Location]
    participants: list[Entity]


class Relation(Thing):
    """关系 - 实体之间的连接"""

    source: Entity
    target: Entity
    relation_type: str
    valid_time: TimeRange  # 与 Graphiti 集成


class Property(Thing):
    """属性 - 实体的特征"""

    property_type: str  # datatype property
    value: Any
    unit: Optional[str]
    confidence: float
    source: Source
```

## 跨域共享层

### 1. Person（人）

```python
class Person(Entity):
    """自然人 - 所有域的核心主体"""

    labels = {"zh": "人", "en": "Person", "ja": "人物"}

    # 必填属性
    identifier: str  # 身份证号/护照号（脱敏存储）
    name: str
    date_of_birth: Optional[Date]
    date_of_death: Optional[Date]
    nationality: list[str]
    domicile: Optional[Location]

    # 可选属性
    marital_status: Optional[str]  # married/single/divorced/widowed
    occupation: Optional[str]
    languages: list[str]

    # 关系
    spouse: Optional["Person"]
    parents: list["Person"]
    children: list["Person"]
    siblings: list["Person"]
    legal_guardian: Optional["Person"]

    # 角色标签（决定哪个智能体负责）
    roles: list[str]  # deceased / bereaved / heir / creditor / debtor /
    # patient / guardian / agent / minor / elderly
```

### 2. Organization（组织/机构）

```python
class Organization(Entity):
    """组织机构"""

    labels = {"zh": "机构", "en": "Organization", "ja": "機関"}

    # 必填
    name: str
    org_type: str  # government / hospital / bank / insurance /
    # notary / court / funeral_home / embassy / consulate
    jurisdiction: Location

    # 可选
    address: Optional[str]
    phone: Optional[str]  # 标注来源
    website: Optional[str]
    service_hours: Optional[str]
    authority_level: str  # national / provincial / municipal / district
```

### 3. Document（文件）

```python
class Document(Entity):
    """文件/证件"""

    labels = {"zh": "文件", "en": "Document", "ja": "書類"}

    # 必填
    doc_type: str  # 见下方文档类型表
    issuing_authority: Organization
    jurisdiction: Location

    # 可选
    doc_number: Optional[str]  # 编号（脱敏）
    issue_date: Optional[Date]
    expiry_date: Optional[Date]
    validity: str  # valid / expired / revoked / pending
    required_for: list[str]  # 该文档可用于哪些流程

    # 多语言别名（关键！消除同义词混乱）
    aliases: dict[str, list[str]] = {
        "death_certificate": {
            "zh": ["死亡证明", "死亡证书", "死亡医学证明书"],
            "en": ["Death Certificate", "Certificate of Death"],
            "ja": ["死亡診断書", "死体検案書"],
        }
    }
```

### 4. Location（地点）

```python
class Location(Entity):
    """地点 - 支持多级管辖"""

    labels = {"zh": "地点", "en": "Location", "ja": "場所"}

    # 层级
    country: str  # ISO 3166-1 alpha-2: CN/US/JP/AU/...
    region: Optional[str]  # 州/省
    city: Optional[str]
    district: Optional[str]

    # 法律体系
    legal_system: str  # civil_law / common_law / mixed / religious
    language: list[str]

    # 跨境相关
    is_cross_border_relevant: bool
    apostille_country: Optional[str]  # 海牙认证成员国
```

### 5. Time（时间）

```python
class TimeRange:
    """时间区间 - 与 Graphiti bi-temporal model 集成"""

    valid_from: datetime  # 事实有效时间开始
    valid_to: Optional[datetime]  # 事实有效时间结束（None=至今有效）
    transaction_time: datetime  # 记录时间


class TimeLimit(Entity):
    """时限"""

    labels = {"zh": "时限", "en": "Time Limit", "ja": "期限"}

    duration_days: Optional[int]
    duration_months: Optional[int]
    duration_years: Optional[int]
    starts_from: str  # death_date / issue_date / discovery_date / ...
    is_hard_deadline: bool  # 硬性截止 vs 建议时限
    consequence_if_missed: Optional[str]
```

### 6. Money（金额）

```python
class Money(Entity):
    """金额"""

    amount: float
    currency: str  # CNY/USD/JPY/...
    amount_in_cny: Optional[float]  # 跨境时换算
    exchange_rate_date: Optional[Date]
    is_estimate: bool  # 估算值 vs 精确值
    source: Optional[str]
```

### 7. Role（角色）

```python
class Role(Entity):
    """角色 - 人在特定场景中的身份"""

    labels = {"zh": "角色", "en": "Role", "ja": "役割"}

    role_type: str
    # deceased: 逝者
    # bereaved: 遗属
    # heir: 继承人
    # creditor: 债权人
    # debtor: 债务人
    # patient: 患者
    # guardian: 监护人
    # agent: 代理人/协助者
    # minor: 未成年人
    # elderly: 老人
    # disabled: 残疾人
    # consular: 领事
    # lawyer: 律师
    # notary: 公证员

    held_by: Person
    valid_in_context: str  # 继承/医疗/跨境/...
```

## 领域层 - 身后事域

```python
class DeathAftercareDomain:
    """身后事域本体"""

    # === 实体类型 ===
    entities = {
        "DeathEvent": {
            "labels": {"zh": "死亡事件", "en": "Death Event"},
            "properties": {
                "date": Date,
                "location": Location,
                "cause": Optional[str],  # 病死/意外/...
                "manner": Optional[str],  # 自然/非自然
                "was_expected": bool,
            },
            "relations": ["involves_deceased", "occurred_at", "triggered_by"]
        },

        "DeathCertificate": {
            "aliases": ["死亡证明", "死亡证书", "死亡医学证明书",
                       "Death Certificate", "死亡診断書"],
            "properties": {
                "doc_type": "death_certificate",
                "issuing_authority": Organization,  # 医院/派出所/卫健委
                "requires_autopsy": bool,
                "copies_needed": int,
                "who_can_apply": list[Role],  # 第一顺序: 配偶/父母/子女
            },
            "relations": ["issued_by", "required_for", "valid_for_days"]
        },

        "BodyHandlingProcedure": {
            "labels": {"zh": "遗体处理流程", "en": "Body Handling"},
            "properties": {
                "stage": str,  # 停放/运输/冷藏/火化/土葬/海葬
                "location": Organization,  # 殡仪馆
                "duration_limit": TimeLimit,
                "requires_permit": list[Document],
            }
        },

        "HouseholdCancellation": {
            "aliases": ["户口注销", "户籍注销", "Hukou Cancellation"],
            "properties": {
                "authority": Organization,  # 派出所
                "required_documents": list[Document],
                "time_limit": TimeLimit,
            }
        },

        "DigitalAccount": {
            "labels": {"zh": "数字账号", "en": "Digital Account"},
            "properties": {
                "platform": str,  # 微信/支付宝/Apple/Google/...
                "account_type": str,  # social/payment/cloud/game/...
                "has_monetary_value": bool,
                "inheritance_policy": Optional[str],  # 平台的继承政策
                "succession_process": Optional[str],
            }
        },

        "EstateAsset": {
            "aliases": ["遗产", "Estate", "遺産"],
            "properties": {
                "asset_type": str,  # real_estate/bank_account/stock/vehicle/...
                "value": Money,
                "location": Location,
                "ownership_form": str,  # sole/joint/community/...
                "is_insured": bool,
            }
        },

        "FuneralService": {
            "labels": {"zh": "殡葬服务", "en": "Funeral Service"},
            "properties": {
                "service_provider": Organization,
                "service_type": str,  # 基本殡葬/选择性殡葬
                "price": Money,
                "government_subsidy": Optional[Money],
            }
        },

        "AftercareStage": {
            """9 阶段流程 - 与 skills/death-aftercare-guide/ 对齐"""
            labels: {"zh": "阶段", "en": "Stage"}
            stage_number: int  # 1-9
            stage_name: str
            required_documents: list[Document]
            responsible_authorities: list[Organization]
            time_limit: Optional[TimeLimit]
            can_transfer_to_legal: bool
            can_transfer_to_financial: bool
        }
    }

    # === 关系类型 ===
    relations = {
        "triggers": {"domain": "DeathEvent", "range": "AftercareStage"},
        "requires_document": {"domain": "AftercareStage", "range": "Document"},
        "issued_by": {"domain": "Document", "range": "Organization"},
        "processed_at": {"domain": "Procedure", "range": "Organization"},
        "inherits_asset": {"domain": "Person", "range": "EstateAsset"},
        "owns_account": {"domain": "Person", "range": "DigitalAccount"},
        "cancels_account": {"domain": "Document", "range": "DigitalAccount"},
    }
```

## 领域层 - 医疗域

```python
class MedicalDomain:
    """医疗导航域本体 - 与 medical-guide 对齐"""

    entities = {
        "MedicalEncounter": {
            "labels": {"zh": "就诊", "en": "Medical Encounter"},
            "properties": {
                "patient": Person,
                "hospital": Organization,
                "department": str,
                "encounter_date": Date,
                "diagnosis": Optional[str],
                "is_emergency": bool,
            }
        },

        "MedicalInsurance": {
            "aliases": ["医保", "医疗保险", "Health Insurance", "健康保険"],
            "properties": {
                "insurance_type": str,  # 城镇职工/城镇居民/新农合/商保/...
                "coverage_scope": list[str],
                "deductible": Money,
                "reimbursement_rate": float,
                "cross_region_allowed": bool,
            }
        },

        "MedicalRecord": {
            "aliases": ["病历", "Medical Record", "カルテ"],
            "properties": {
                "record_type": str,  # 门诊/住院/急诊
                "hospital": Organization,
                "patient": Person,
                "retention_period": TimeLimit,
                "can_be_inherited": bool,  # 患者死亡后家属能否调取
            }
        },

        "SpecialDisease": {
            """门诊特殊病种"""
            labels: {"zh": "门诊特殊病种", "en": "Special Outpatient Disease"}
            disease_name: str
            insurance_coverage: MedicalInsurance
            application_process: str
        },

        "CriticalIllnessInsurance": {
            "labels": {"zh": "大病保险", "en": "Critical Illness Insurance"},
            threshold: Money
            coverage_after_threshold: float
        },

        "CommercialInsurance": {
            "aliases": ["商保", "Commercial Insurance"],
            policy_number: str
            insured: Person
            beneficiary: Person
            coverage: list[str]
            claim_process: str
            claim_time_limit: TimeLimit
        },

        "MedicalDispute": {
            "aliases": ["医疗纠纷", "Medical Dispute"],
            dispute_type: str  # 责任/技术/服务
            involved_hospital: Organization
            resolution_pathway: str  # 协商/调解/诉讼/鉴定
        }
    }

    relations = {
        "treated_at": {"domain": "Person", "range": "Organization"},
        "covered_by": {"domain": "Person", "range": "MedicalInsurance"},
        "filed_claim": {"domain": "Person", "range": "CommercialInsurance"},
        "diagnosed_with": {"domain": "Person", "range": "SpecialDisease"},
    }
```

## 领域层 - 法律域

```python
class LegalDomain:
    """法律域本体 - 与 legal-advisor 对齐"""

    entities = {
        "InheritanceDispute": {
            "labels": {"zh": "继承争议", "en": "Inheritance Dispute"},
            "properties": {
                "dispute_type": str,  # 法定继承/遗嘱继承/遗赠/代位继承/...
                "estate": EstateAsset,
                "heirs": list[Person],
                "has_will": bool,
                "will_validity": Optional[str],  # valid/disputed/invalid
                "risk_level": str,  # R0-R3
            },
        },
        "Will": {
            "aliases": ["遗嘱", "Will", "遺言"],
            "properties": {
                "will_type": str,  # 自书/代书/打印/录音录像/口头/公证
                "testator": Person,
                "beneficiaries": list[Person],
                "witnesses": list[Person],
                "notarized": bool,
                "validity_requirements": list[str],
            },
        },
        "LegalProcedure": {
            "labels": {"zh": "法律程序", "en": "Legal Procedure"},
            "properties": {
                "procedure_type": str,  # 公证/诉讼/调解/仲裁
                "court": Optional[Organization],
                "statute_of_limitations": TimeLimit,
                "filing_fee": Money,
            },
        },
        "Statute": {
            "aliases": ["法条", "法规", "Statute", "Law"],
            "properties": {
                "law_name": str,  # 继承法/民法典/...
                "article": str,  # 第X条
                "jurisdiction": Location,
                "effective_date": Date,
                "superseded_by": Optional["Statute"],  # 与 Graphiti 集成
                "source_text": str,  # 原文
            },
        },
        "DebtObligation": {
            "aliases": ["债务", "Debt", "債務"],
            "properties": {
                "creditor": Person,
                "debtor": Person,  # 逝者
                "amount": Money,
                "debt_type": str,  # mortgage/credit_card/loan/...
                "secured": bool,
                "inherited_by_estate": bool,  # 是否由遗产清偿
            },
        },
    }

    relations = {
        "governed_by": {"domain": "InheritanceDispute", "range": "Statute"},
        "filed_at": {"domain": "LegalProcedure", "range": "Organization"},
        "settled_by": {"domain": "InheritanceDispute", "range": "LegalProcedure"},
        "owes_to": {"domain": "Person", "range": "Person"},
        "supersedes": {"domain": "Statute", "range": "Statute"},
    }
```

## 领域层 - 财务域

```python
class FinancialDomain:
    """财务域本体 - 与 financial-analyst 对齐"""

    entities = {
        "BankAccount": {
            "labels": {"zh": "银行账户", "en": "Bank Account"},
            "properties": {
                "bank": Organization,
                "account_number": str,  # 脱敏
                "account_type": str,  # checking/savings/time_deposit
                "balance": Money,
                "joint_owners": list[Person],
                "inheritance_process": str,
            },
        },
        "RealEstate": {
            "aliases": ["房产", "不动产", "Real Estate", "不動産"],
            "properties": {
                "address": Location,
                "property_type": str,  # residential/commercial/land
                "ownership_form": str,  # sole/joint_tenancy/tenancy_in_common/community
                "market_value": Money,
                "mortgage": Optional[DebtObligation],
                "title_deed": Document,
            },
        },
        "StockPortfolio": {
            "labels": {"zh": "股票", "en": "Stock"},
            "properties": {
                "broker": Organization,
                "account_number": str,
                "holdings": list[dict],  # [{symbol, shares, value}]
                "total_value": Money,
            },
        },
        "TaxObligation": {
            "aliases": ["税务", "Tax", "税務"],
            "properties": {
                "tax_type": str,  # inheritance_tax/estate_tax/income_tax/property_tax
                "jurisdiction": Location,
                "tax_rate": float,
                "exemption_threshold": Money,
                "filing_deadline": TimeLimit,
                "filing_authority": Organization,
            },
        },
        "InsurancePolicy": {
            "labels": {"zh": "保险", "en": "Insurance Policy"},
            "properties": {
                "insurer": Organization,
                "policy_type": str,  # life/accident/health/property
                "insured": Person,
                "beneficiary": Person,
                "sum_assured": Money,
                "claim_time_limit": TimeLimit,
            },
        },
    }

    relations = {
        "owned_by": {"domain": "BankAccount", "range": "Person"},
        "secured_by": {"domain": "DebtObligation", "range": "RealEstate"},
        "subject_to": {"domain": "EstateAsset", "range": "TaxObligation"},
        "pays_out_to": {"domain": "InsurancePolicy", "range": "Person"},
    }
```

## 领域层 - 跨境域

```python
class CrossBorderDomain:
    """跨境域本体 - 与 cross-border-specialist 对齐"""

    entities = {
        "CrossBorderDeath": {
            "labels": {"zh": "跨境死亡", "en": "Cross-Border Death"},
            "properties": {
                "death_location": Location,  # 死亡地
                "deceased_nationality": str,
                "deceased_domicile": Location,  # 常住地
                "intended_burial_location": Location,  # 拟安葬地
                "involves_consular": bool,
            },
        },
        "BodyRepatriation": {
            "aliases": ["遗体运输", "遗体回国", "Body Repatriation"],
            "properties": {
                "origin": Location,
                "destination": Location,
                "transport_method": str,  # air/land/sea
                "required_documents": list[Document],
                "estimated_cost": Money,
                "duration": TimeLimit,
            },
        },
        "ConsularAuthentication": {
            "aliases": ["领事认证", "海牙认证", "Apostille", "Consular Authentication"],
            "properties": {
                "auth_type": str,  # apostille / consular / diplomatic
                "issuing_authority": Organization,  # 使领馆
                "target_country": Location,
                "documents_to_authenticate": list[Document],
                "processing_time": TimeLimit,
                "fee": Money,
            },
        },
        "LegalConflict": {
            "labels": {"zh": "法律冲突", "en": "Legal Conflict"},
            "properties": {
                "conflict_type": str,  # inheritance_jurisdiction / property_law / ...
                "jurisdictions_involved": list[Location],
                "applicable_law": Optional[Statute],  # 冲突法指向
                "forum_options": list[Organization],  # 可起诉地
            },
        },
        "ForeignDeathCertificate": {
            "aliases": ["国外死亡证明", "Foreign Death Certificate"],
            "properties": {
                "issuing_country": Location,
                "needs_translation": bool,
                "needs_notarization": bool,
                "needs_apostille": bool,
                "chinese_equivalent": Document,
            },
        },
    }

    relations = {
        "requires_authentication": {
            "domain": "ForeignDeathCertificate",
            "range": "ConsularAuthentication",
        },
        "governed_by_conflict_rules": {"domain": "CrossBorderDeath", "range": "LegalConflict"},
        "transported_via": {"domain": "CrossBorderDeath", "range": "BodyRepatriation"},
    }
```

## 领域层 - 政策域

```python
class PolicyDomain:
    """政策域本体 - 与 policy-researcher 对齐"""

    entities = {
        "Policy": {
            "aliases": ["政策", "Policy", "政策"],
            "properties": {
                "policy_name": str,
                "policy_type": str,  # regulation/law/notice/guideline
                "issuing_authority": Organization,
                "jurisdiction": Location,
                "effective_date": Date,
                "expiry_date": Optional[Date],
                "superseded_by": Optional["Policy"],
                "source_url": str,
                "source_verified": bool,  # 源验证
                "verification_date": Date,
            },
        },
        "PolicyChange": {
            "labels": {"zh": "政策变更", "en": "Policy Change"},
            "properties": {
                "old_policy": Policy,
                "new_policy": Policy,
                "change_date": Date,
                "change_type": str,  # amendment/repeal/replacement/new
                "affected_procedures": list[str],
            },
        },
        "OfficialSource": {
            "labels": {"zh": "官方来源", "en": "Official Source"},
            "properties": {
                "source_type": str,  # government_website/official_gazette/...
                "url": str,
                "last_verified": Date,
                "trust_level": str,  # high/medium/low
            },
        },
    }

    relations = {
        "issued_by": {"domain": "Policy", "range": "Organization"},
        "verified_by": {"domain": "Policy", "range": "OfficialSource"},
        "supersedes": {"domain": "Policy", "range": "Policy"},
        "affects": {"domain": "Policy", "range": "Procedure"},
    }
```

## 跨域关系总表

```python
# ontology/cross_domain_relations.py

CROSS_DOMAIN_RELATIONS = {
    # 死亡事件 → 各域
    "triggers_inheritance": {"domain": "DeathEvent", "range": "InheritanceDispute"},
    "triggers_insurance_claim": {"domain": "DeathEvent", "range": "InsurancePolicy"},
    "triggers_tax_filing": {"domain": "DeathEvent", "range": "TaxObligation"},
    "triggers_account_cancellation": {"domain": "DeathEvent", "range": "DigitalAccount"},
    # 文档 → 多域
    "required_for_inheritance": {"domain": "DeathCertificate", "range": "LegalProcedure"},
    "required_for_insurance": {"domain": "DeathCertificate", "range": "InsurancePolicy"},
    "required_for_repatriation": {"domain": "DeathCertificate", "range": "BodyRepatriation"},
    "required_for_tax": {"domain": "DeathCertificate", "range": "TaxObligation"},
    # 跨境 → 多域
    "needs_legal_review": {"domain": "CrossBorderDeath", "range": "InheritanceDispute"},
    "needs_financial_review": {"domain": "CrossBorderDeath", "range": "EstateAsset"},
    # 政策 → 多域
    "governs_procedure": {"domain": "Policy", "range": "AftercareStage"},
    "governs_inheritance": {"domain": "Policy", "range": "InheritanceDispute"},
    "governs_medical": {"domain": "Policy", "range": "MedicalEncounter"},
    # 医疗 → 法律（医疗纠纷转介）
    "may_lead_to_dispute": {"domain": "MedicalEncounter", "range": "MedicalDispute"},
    # 人 → 多域
    "has_role_in": {"domain": "Person", "range": "Event"},
}
```

## 与 LightRAG 的集成

```python
# ontology/lightrag_integration.py

# 把本体映射为 LightRAG 实体类型
ENTITY_TYPE_MAPPING = {
    # 共享层
    "Person": "Person",
    "Organization": "Organization",
    "Document": "Document",
    "Location": "Location",
    "TimeLimit": "TimeLimit",
    "Money": "Money",
    "Role": "Role",
    # 身后事域
    "DeathEvent": "DeathEvent",
    "DeathCertificate": "DeathCertificate",
    "BodyHandlingProcedure": "Procedure",
    "HouseholdCancellation": "Procedure",
    "DigitalAccount": "DigitalAccount",
    "EstateAsset": "EstateAsset",
    "FuneralService": "Service",
    "AftercareStage": "Stage",
    # 医疗域
    "MedicalEncounter": "Encounter",
    "MedicalInsurance": "Insurance",
    "MedicalRecord": "Document",
    "MedicalDispute": "Dispute",
    # 法律域
    "InheritanceDispute": "Dispute",
    "Will": "Document",
    "LegalProcedure": "Procedure",
    "Statute": "Regulation",
    "DebtObligation": "Obligation",
    # 财务域
    "BankAccount": "Account",
    "RealEstate": "Asset",
    "StockPortfolio": "Asset",
    "TaxObligation": "Obligation",
    "InsurancePolicy": "Policy",
    # 跨境域
    "CrossBorderDeath": "Event",
    "BodyRepatriation": "Procedure",
    "ConsularAuthentication": "Procedure",
    "LegalConflict": "Dispute",
    "ForeignDeathCertificate": "Document",
    # 政策域
    "Policy": "Regulation",
    "PolicyChange": "Event",
    "OfficialSource": "Source",
}


# 把本体关系映射为 LightRAG 关系类型
RELATION_TYPE_MAPPING = {
    "requires_document": "requires",
    "issued_by": "issued_by",
    "processed_at": "processed_at",
    "triggers": "triggers",
    "owns_account": "owns",
    "inherits_asset": "inherits",
    "cancels_account": "cancels",
    "covered_by": "covered_by",
    "treated_at": "treated_at",
    "governed_by": "governed_by",
    "filed_at": "filed_at",
    "owes_to": "owes_to",
    "supersedes": "supersedes",
    "owned_by": "owned_by",
    "subject_to": "subject_to",
    "requires_authentication": "requires",
    "needs_legal_review": "depends_on",
    "governs_procedure": "restricted_by",
    "has_role_in": "eligibility",
}


def extract_entities_with_ontology(text, jurisdiction):
    """
    用本体指导实体提取。
    比 LightRAG 默认的 LLM 提取更精准。
    """
    prompt = f"""
    从以下文本提取实体和关系，严格使用本体词汇表。

    本体实体类型：{list(ENTITY_TYPE_MAPPING.keys())}
    本体关系类型：{list(RELATION_TYPE_MAPPING.keys())}

    管辖地：{jurisdiction}

    文本：{text}

    输出 JSON：
    {{
      "entities": [
        {{"type": "DeathCertificate", "name": "...", "properties": {{...}}}}
      ],
      "relations": [
        {{"source": "...", "type": "issued_by", "target": "...", "source_text": "原文片段"}}
      ]
    }}

    重要：每条关系必须记录 source_text（原文片段），用于诚信校验。
    """
    return call_llm(prompt)
```

## 与 Graphiti 的集成

```python
# ontology/graphiti_integration.py

# 本体类型 → Graphiti 时态对象类型
TEMPORAL_OBJECT_MAPPING = {
    "Policy": "PolicyFact",  # 政策有时效
    "Statute": "PolicyFact",  # 法条有时效
    "TimeLimit": "PolicyFact",  # 时限随政策变
    "Procedure": "PolicyFact",  # 流程随政策变
    "InsurancePolicy": "PolicyFact",
    "TaxObligation": "PolicyFact",
    # 以下作为 UserProgressEvent 记录
    "DeathEvent": "UserProgressEvent",
    "InheritanceDispute": "UserProgressEvent",
    "MedicalEncounter": "UserProgressEvent",
    "CrossBorderDeath": "UserProgressEvent",
    # 以下作为 KnowledgeVersion 记录
    "Document": "KnowledgeVersion",
    "Organization": "KnowledgeVersion",
    "Location": "KnowledgeVersion",
}


def to_graphiti_node(entity, ontology_type):
    """把本体实体转为 Graphiti 节点"""
    temporal_type = TEMPORAL_OBJECT_MAPPING.get(ontology_type, "KnowledgeVersion")

    return {
        "node_type": temporal_type,
        "entity_type": ontology_type,
        "entity_id": entity.id,
        "properties": entity.properties,
        "labels": entity.labels,
        "valid_time": entity.valid_time,  # bi-temporal
        "transaction_time": datetime.utcnow(),
        "source": entity.source,
    }
```

## 与 MCP query_knowledge 的集成

```python
# ontology/mcp_integration.py


def query_with_ontology(query, jurisdiction, entity_types=None, relation_types=None):
    """
    MCP query_knowledge 用本体词汇表精确查询。
    与 LightRAG-Pilot.md 的 query_mode 参数集成。
    """
    return mcp.call_tool(
        "query_knowledge",
        {
            "query": query,
            "country": jurisdiction.country,
            "region": jurisdiction.region,
            "query_mode": "hybrid",
            "entity_types": entity_types or [],  # 用本体过滤
            "relation_types": relation_types or [],
            "ontology_version": "v1.0",
        },
    )
```

## 本体版本管理

```python
# ontology/version.py

ONTOLOGY_VERSION = "v1.0"
ONTOLOGY_RELEASE_DATE = "2026-07"


# 本体变更也要记录到 Graphiti
def register_ontology_change(old_version, new_version, changes):
    graphiti.add_event(
        {
            "event_type": "KnowledgeVersion",
            "entity_type": "Ontology",
            "old_version": old_version,
            "new_version": new_version,
            "changes": changes,  # 新增/修改/废弃的实体类型
            "transaction_time": datetime.utcnow(),
        }
    )
```

## 多语言标签表（核心同义词归一）

| 规范名 | 中文别名 | 英文别名 | 日文别名 |
|--------|---------|---------|---------|
| DeathCertificate | 死亡证明 / 死亡证书 / 死亡医学证明书 | Death Certificate / Certificate of Death | 死亡診断書 / 死体検案書 |
| HouseholdCancellation | 户口注销 / 户籍注销 | Hukou Cancellation / Household Registration Cancellation | 戸籍抹消 |
| EstateAsset | 遗产 / 遗产资产 | Estate / Estate Asset | 遺産 |
| Will | 遗嘱 | Will / Testament | 遺言書 |
| InheritanceDispute | 继承争议 / 遗产纠纷 | Inheritance Dispute / Estate Dispute | 相続争い |
| BodyRepatriation | 遗体运输 / 遗体回国 | Body Repatriation / Repatriation of Remains | 遺体搬送 |
| ConsularAuthentication | 领事认证 / 海牙认证 | Apostille / Consular Authentication | 領事認証 |
| MedicalInsurance | 医保 / 医疗保险 | Health Insurance / Medical Insurance | 健康保険 |
| RealEstate | 房产 / 不动产 | Real Estate / Property | 不動産 |
| TaxObligation | 税务 / 税务义务 | Tax / Tax Obligation | 税務 |

## 评估指标

| 指标 | 目标 | 说明 |
|------|------|------|
| 实体类型覆盖率 | ≥ 0.95 | 6 个智能体使用的实体类型都在本体中 |
| 同义词归一准确率 | ≥ 0.90 | 同义词正确映射到规范名 |
| 跨域关系召回率 | ≥ 0.80 | LightRAG 多跳查询能找到跨域关系 |
| 本体一致性 | 1.0 | 无循环继承、无类型冲突 |
| 多语言标签完整度 | ≥ 0.80 | 中英日三语标签覆盖率 |

## 版本

- v1.0 初始跨域本体（顶层 + 共享层 + 6 领域层 + 跨域关系 + LightRAG/Graphiti/MCP 集成 + 多语言标签表）
```
