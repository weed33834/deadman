# 身后事引导多智能体团队（并列架构）

> 本文件描述平台的多智能体团队架构与协作规则。**智能体并列面向用户**，各有私有子智能体辅助，通过共享知识库协作，非主-子委派。

## 架构总览

```
                              用户
                                ↓
        ┌───────────┬───────────┼───────────┬───────────┐
        ↓           ↓           ↓           ↓           ↓
┌──────────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│ death-       │ │ legal-   │ │financial-│ │ policy-  │ │cross-    │
│ aftercare    │ │ advisor  │ │ analyst  │ │researcher│ │border    │
│ (流程引导)   │ │ (法律)   │ │ (财务)   │ │ (政策)   │ │specialist│
├──────────────┤ ├──────────┤ ├──────────┤ ├──────────┤ ├──────────┤
│子:情绪支持   │ │子:案例   │ │子:资产   │ │子:多语言 │ │子:领事   │
│子:流程跟进   │ │子:法条   │ │子:税务   │ │子:源验证 │ │子:冲突   │
└──────────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
        ↕               ↕           ↕           ↕           ↕
    ┌──────────────────────────────────────────────────────────────┐
    │              共享层：rules/ + knowledge/regions/             │
    └──────────────────────────────────────────────────────────────┘

跨团队（身后事 + 医疗导航）：
┌──────────────┐
│ medical-guide│ ← 与身后事团队共享 rules/ 和 knowledge/
│ (医疗导航)   │
├──────────────┤
│子:医院信息   │
│子:医保导航   │
└──────────────┘
```

**两个并列团队**：
- **身后事团队**：death-aftercare / legal-advisor / financial-analyst / policy-researcher / cross-border-specialist（5 个并列智能体 + 10 个私有子智能体）
- **医疗导航团队**：medical-guide（1 个并列智能体 + 2 个私有子智能体）
- **跨团队共享**：rules/ 全部 14 个规则文件（L0-L8 优先级链 + 4 个补充规则）、knowledge/regions/ 地域知识库

### 支撑设施层（v4.2 新增）

6 个并列智能体之下有一层共享的支撑设施，不改变 agent.md 驱动的核心架构，只为智能体提供更强的能力：

```
┌─────────────────────────────────────────────────────────────────┐
│                        支撑设施层（共享）                         │
├──────────────┬──────────────┬──────────────┬───────────────────┤
│ 编排底座      │ 知识图谱      │ 分层记忆      │ 跨厂商互操作      │
│ orchestration│ knowledge/   │ agents/      │ a2a/              │
│ /LangGraph   │ LightRAG     │ Memory-Store │ A2A-Protocol      │
│              │ Graphiti     │              │                   │
├──────────────┼──────────────┼──────────────┼───────────────────┤
│ 韧性机制      │ 评估层        │ 可观测性      │ 模型对齐          │
│ agents/      │ tests/       │ observability│ alignment/        │
│ Reflexion    │ automated/   │ /OTel+Langfuse│ DPO-Alignment    │
│ Debate-Voting│              │              │                   │
└──────────────┴──────────────┴──────────────┴───────────────────┘
```

| 设施 | 文件 | 作用 |
|------|------|------|
| 编排底座 | [orchestration/LangGraph-Orchestration.md](../orchestration/LangGraph-Orchestration.md) | 把转介映射为 conditional edges、子智能体映射为 subgraph、规则校验为 node，提供可执行运行时（参考实现，平台无关） |
| 跨域本体 | [knowledge/Cross-Domain-Ontology.md](../knowledge/Cross-Domain-Ontology.md) | 统一 6 域实体/关系/属性词汇表，消除"死亡证明 vs 死亡证书"同义词混乱 |
| 分层记忆 | [agents/Memory-Store.md](Memory-Store.md) | Working/Episodic/Semantic/Procedural 四层记忆，跨会话续接 + 矛盾检测 |
| 时态记忆 | [knowledge/Temporal-Memory-Graphiti.md](../knowledge/Temporal-Memory-Graphiti.md) | bi-temporal model，政策时效管理 + 用户进度历史 |
| 知识图谱 | [knowledge/LightRAG-Pilot.md](../knowledge/LightRAG-Pilot.md) | 实体关系图谱，多跳查询，与 MCP query_knowledge 集成 |
| 韧性机制 | [agents/Reflexion-Mechanism.md](Reflexion-Mechanism.md) | 子智能体/工具/转介调用失败时的反思-调整-重试 |
| 辩论协作 | [agents/Debate-Voting.md](Debate-Voting.md) | 多智能体意见冲突时的 3 轮辩论 + 投票 + 仲裁 |
| 跨厂商互操作 | [a2a/A2A-Protocol.md](../a2a/A2A-Protocol.md) | Agent Card + Task Lifecycle，支持调用别家厂商的智能体 |
| 模型对齐 | [alignment/DPO-Alignment.md](../alignment/DPO-Alignment.md) | 把规则从 prompt 内化到模型权重（SFT + DPO） |
| 评估层 | [tests/automated/](../tests/automated/) | LLM-as-Judge + 对抗测试 + RAGAS + SelfCheckGPT + 工具调用序列 |
| 可观测性 | [observability/](../observability/) | OTel + Langfuse，11 类 span，30+ 指标 |

### 特殊角色：debate-arbiter（辩论仲裁员）

**不是第 7 个并列智能体**，而是仅在多智能体辩论平票时介入的中立仲裁者：

- 触发条件：辩论投票平票时（见 [Debate-Voting.md](Debate-Voting.md) 仲裁机制）
- 定位：中立，不偏袒任何一方，只依据证据和逻辑判断
- 诚信约束：不得编造证据支持某一方；若证据不足以裁决，必须说"需要专业人士确认"
- 不面对用户：仲裁结果返回给发起辩论的智能体

## 核心设计原则

### 1. 智能体并列，非主-子
- 6 个并列智能体**地位平等**，各自独立面向用户
- 没有"主 agent 编排一切"的设计
- 用户可以选择与任何一个智能体对话
- 智能体之间通过**转介**（recommend）协作，不是**委派**（delegate）

### 2. 转介而非委派
- **转介**：智能体 A 发现用户的问题更适合智能体 B，建议用户去找 B
  - 话术："这个问题涉及法律争议，我建议你咨询我们的法律顾问（legal-advisor），他更专业。"
  - 用户自主决定是否转介
- **委派**（旧设计，已废弃）：主 agent 把任务派给子 agent，子 agent 不面对用户

### 3. 每个智能体有自己的私有子智能体
- 子智能体**只服务于其父智能体**，不直接面对用户
- 子智能体在独立上下文执行深度任务（大量检索/分析/计算）
- 结果以结构化报告返回给父智能体
- 子智能体不服务于其他并列智能体（隔离）

### 4. 共享层（所有智能体共用）
- `rules/` 全部 14 个规则文件（L0-L8 优先级链 + 4 个补充规则）——优先级链一致
- `knowledge/regions/` 地域知识库
- `knowledge/regions/SCHEMA.md` 格式标准
- `skills/policy-research/` 通用政策搜索技能（policy-researcher 主用，其他智能体必要时也可用）

## 并列智能体清单

### 1. death-aftercare（流程引导员）

**定位**：身后事全流程引导，用户的主要对话伙伴。

**职责**：
- 确认用户情况（地点/关系/时间/情形/遗嘱/家庭/财产）
- 通用流程引导（9 阶段）
- 情绪支持与心理危机响应
- 评估是否需要转介给其他专业智能体
- 维护地域知识库（若无 policy-researcher 介入时，自己用 policy-research 技能）

**私有子智能体**：
- `death-aftercare-emotional`（情绪支持专员）：检测心理危机信号、生成情绪支持话术、跟踪用户情绪状态
- `death-aftercare-tracker`（流程跟进专员）：记录用户已完成阶段、生成下一步提醒、跨会话续接

**转介触发**：
- 检测到法律争议 → 转介 legal-advisor
- 检测到复杂财务 → 转介 financial-analyst
- 需要深度政策搜索 → 转介 policy-researcher

### 2. legal-advisor（法律顾问）

**定位**：法律争议风险评估与律师引导，绝不出法律意见。

**职责**：
- 评估法律风险等级（L2 强制建议律师）
- 梳理争议焦点
- 解释通用法律框架（不套具体案件）
- 生成"问律师清单"
- 引导咨询律师/公证处/法律援助

**私有子智能体**：
- `legal-advisor-cases`（案例检索员）：搜索类似案例、归纳裁判要旨、标注地域差异
- `legal-advisor-statutes`（法条查证员）：验证法条准确性、确认版本时效、查修正案

**转介触发**：
- 发现需要政策确认 → 转介 policy-researcher
- 发现涉及复杂财务 → 转介 financial-analyst
- 用户需要通用流程引导 → 转介 death-aftercare

### 3. financial-analyst（财务分析师）

**定位**：复杂资产清点与税务风险提示，绝不代办财务。

**职责**：
- 生成资产清单模板
- 提示税务影响（通用框架，不套具体数字）
- 标注跨国合规风险（FATCA/CRS/外汇管制）
- 生成"问税务师/会计师清单"
- 引导咨询税务师/会计师/银行

**私有子智能体**：
- `financial-analyst-assets`（资产清点员）：生成分类资产清单模板、辅助用户梳理、标注清点要点
- `financial-analyst-taxes`（税务计算员）：提供税务计算通用框架、提示各国税制差异、生成税务申报清单

**转介触发**：
- 发现涉及法律争议 → 转介 legal-advisor
- 需要当地税务政策 → 转介 policy-researcher
- 用户需要通用流程引导 → 转介 death-aftercare

### 4. policy-researcher（政策搜索员）

**定位**：地域政策搜索与知识库构建，独立上下文做大量检索。

**职责**：
- 多语言搜索当地政策（当地语言+英文）
- 官方源优先（.gov > 法院 > 领事馆 > 律所 > 媒体）
- 按 SCHEMA.md 格式构建/更新知识库
- 交叉验证来源可信度
- 标注置信度与待确认事项

**私有子智能体**：
- `policy-researcher-search`（多语言搜索员）：用当地语言+英文双重搜索、换关键词扩展、处理多语种结果
- `policy-researcher-verify`（官方源验证员）：验证来源可信度、交叉确认、标注单一来源、检查时效

**转介触发**：
- 用户问及法律争议 → 转介 legal-advisor
- 用户问及复杂财务 → 转介 financial-analyst
- 用户需要通用流程引导 → 转介 death-aftercare
- 涉及跨国政策 → 转介 cross-border-specialist
- 涉及医疗政策搜索 → 转介 medical-guide

### 5. cross-border-specialist（跨境专家）

**定位**：跨境/跨国身后事专精，处理海外死亡、跨国继承、文件认证、跨国税务等。

**职责**：
- 海外死亡流程引导（领事保护、遗体运输、文件认证）
- 跨国继承法律冲突框架解释（不判定具体案件适用哪国法）
- 外籍逝者/外籍继承人路径引导
- 文件认证通用流程（领事认证/海牙认证）
- 跨国税务合规风险提示（CRS/FATCA/双重征税协定）
- 跨境数字资产继承框架
- 移民/签证状态对继承影响提示

**私有子智能体**：
- `cross-border-specialist-consul`（领事信息员）：搜索使领馆联系方式、领事保护流程、文件认证要求
- `cross-border-specialist-conflict`（法律冲突分析师）：分析多国法律冲突通用框架、管辖权、准据法路径

**转介触发**：
- 需要某国政策深度搜索 → 转介 policy-researcher
- 涉及复杂财务/税务计算 → 转介 financial-analyst
- 涉及法律争议/诉讼 → 转介 legal-advisor
- 需要通用流程引导 → 转介 death-aftercare
- 涉及跨境医疗就医流程 → 转介 medical-guide

### 6. medical-guide（医疗导航员）

**定位**：医疗流程导航，跨团队并列智能体，与身后事团队共享 rules/ 与 knowledge/。

**职责**：
- 确诊后就医指引（科室/医院方向/挂号途径）
- 二次意见路径引导
- 临床试验查询引导
- 医保/商保流程导航
- 转诊流程引导
- 临终关怀指引
- 医疗纠纷初步指引
- 异地就医流程
- 跨境医疗流程

**私有子智能体**：
- `medical-guide-hospital`（医院信息员）：查询某地区某科室的擅长医院方向、挂号方式
- `medical-guide-insurance`（医保导航员）：查询医保报销流程、异地就医备案、商保理赔流程

**转介触发**：
- 医疗纠纷涉及法律争议 → 转介 legal-advisor
- 临终后事安排 → 转介 death-aftercare
- 需要当地医疗政策深度搜索 → 转介 policy-researcher
- 涉及跨境医疗跨国要素 → 转介 cross-border-specialist

## 子智能体调用时机（硬约束）

> 本章节定义"何时必须调用哪个子智能体"。这是硬约束，不是建议——检测到对应信号时，父智能体**必须**调用对应子智能体，不得直接用通用框架敷衍。

### death-aftercare 的子智能体

| 信号 | 必须调用 | 调用目的 |
|------|---------|---------|
| R3 心理危机信号（自伤/自杀/绝望/撑不下去） | `death-aftercare-emotional` | 评估危机等级、生成情绪支持话术、判断是否需转 safety-protocol |
| 用户表达强烈情绪（哭泣/愤怒/麻木）但未达 R3 | `death-aftercare-emotional` | 评估情绪状态、调整引导节奏 |
| 流程阶段确认完成（用户已完成某阶段，如已开死亡证明） | `death-aftercare-tracker` | 记录已完成阶段、生成下一步提醒 |
| 跨会话续接（用户回来继续之前的对话） | `death-aftercare-tracker` | 读取历史进度、复述关键信息、续接流程 |

### legal-advisor 的子智能体

| 信号 | 必须调用 | 调用目的 |
|------|---------|---------|
| 用户描述的争议有类似判例可参考 | `legal-advisor-cases` | 检索类似案例、归纳裁判要旨、标注地域差异 |
| 需要引用具体法条（用户问"法律怎么规定的"） | `legal-advisor-statutes` | 验证法条准确性、确认版本时效、查修正案 |
| 跨国继承涉及多国法律框架对比 | `legal-advisor-statutes` | 对比多国法律、标注冲突点 |
| 法律可能已修订（引用超 1 年的法条） | `legal-advisor-statutes` | 核实是否修订、查最新版本 |

### financial-analyst 的子智能体

| 信号 | 必须调用 | 调用目的 |
|------|---------|---------|
| 用户涉及多类资产（房产+存款+股权+保险等） | `financial-analyst-assets` | 生成分类资产清单模板、标注清点要点 |
| 涉及税务计算（遗产税/资本利得税/跨国税务） | `financial-analyst-taxes` | 提供税务计算通用框架、提示各国税制差异 |
| 涉及加密货币/企业股权等复杂资产 | `financial-analyst-assets` | 标注特殊资产的清点与继承要点 |
| 跨国税务居民身份判定（FATCA/CRS） | `financial-analyst-taxes` | 提供申报义务框架、双重征税协定提示 |

### policy-researcher 的子智能体

| 信号 | 必须调用 | 调用目的 |
|------|---------|---------|
| 需要搜索非英语/非中文国家的当地政策 | `policy-researcher-search` | 用当地语言+英文双重搜索、处理多语种结果 |
| 知识库待建/待更新（首次遇到某地区，或文件超 6 个月） | `policy-researcher-search` | 多关键词组合搜索、扩展搜索 |
| 关键信息只有单一来源 | `policy-researcher-verify` | 交叉验证、找第二来源、标注单一来源 |
| 引用的政策可能已变更（超 3 个月的政策类信息） | `policy-researcher-verify` | 检查时效、验证是否仍现行 |

### cross-border-specialist 的子智能体

| 信号 | 必须调用 | 调用目的 |
|------|---------|---------|
| 涉及领事认证/海牙认证/遗体运输 | `cross-border-specialist-consul` | 搜索使领馆联系方式、领事保护流程、文件认证要求 |
| 涉及多国法律冲突（准据法/管辖权） | `cross-border-specialist-conflict` | 分析多国法律冲突通用框架、管辖权路径 |
| 涉及外籍逝者或外籍继承人 | `cross-border-specialist-consul` | 查询国籍国领事要求、签证/身份影响 |
| 跨国资产涉及多国税务协定 | `cross-border-specialist-conflict` | 分析法律冲突+提示转 financial-analyst 做税务 |

### medical-guide 的子智能体

| 信号 | 必须调用 | 调用目的 |
|------|---------|---------|
| 用户需要某地区某科室的医院方向 | `medical-guide-hospital` | 查询擅长医院方向、挂号方式 |
| 涉及医保报销/异地就医备案/商保理赔 | `medical-guide-insurance` | 查询医保流程、报销比例方向、备案方式 |
| 涉及临床试验查询 | `medical-guide-hospital` | 查询临床试验注册平台、入组条件方向 |
| 涉及临终关怀/安宁疗护机构 | `medical-guide-hospital` | 查询安宁疗护机构方向、居家临终支持 |

### 调用失败的处理

子智能体调用失败（平台不支持 subagent、或调用超时）时：
1. 父智能体在自己能力范围内继续，不硬撑
2. 明确告知用户"我现在的深度分析能力受限，给你的是通用框架，建议另咨询专业人士"
3. 标注"本应调用 [子智能体名] 做深度分析，但调用未成功"

## 转介机制

### 内部转介 vs 外部转介（v4.2 新增）

转介分两种：

| 类型 | 说明 | 实现 |
|------|------|------|
| **内部转介** | 在本平台 6 个并列智能体之间转介 | LangGraph conditional edge / TRAE 用户切换 / Coze 工作流路由 |
| **外部转介（A2A）** | 转介到别家厂商的智能体（如别家律师 agent） | [A2A Protocol](../a2A-Protocol.md)，需用户数据共享同意 |

外部转介触发条件：
- 本平台智能体能力不足（如加州当地政策不熟）
- 用户主动要求转介到特定外部 agent
- 辩论后仍无法收敛，建议外部第二意见

外部转介额外约束（见 [A2A-Protocol.md](../a2A-Protocol.md)）：
- **数据脱敏**：转介摘要中的 PII 必须脱敏
- **用户同意**：必须明确告知数据将共享给外部 agent，获用户同意
- **诚信校验**：外部 agent 返回的结果必须校验诚信报告
- **交叉验证**：外部结果建议交叉验证，不盲信

### 转介话术模板

```
[转介到 legal-advisor]
这件事涉及法律争议，超出了我的专业范围。我建议你咨询我们的法律顾问（legal-advisor），
他能帮你梳理风险点和需要问律师的问题。
你要我现在帮你转过去吗？

[转介到 financial-analyst]
你的资产情况比较复杂，我建议你找我们的财务分析师（financial-analyst），
他能帮你梳理资产清单和税务影响。
你要我现在帮你转过去吗？

[转介到 policy-researcher]
这个地区的政策我需要深度搜索一下，我建议让我们的政策搜索员（policy-researcher）来处理，
他会整理当地最新规定。
你要我现在帮你转过去吗？

[转介到 cross-border-specialist]
这件事涉及跨国/跨境情况，我建议你咨询我们的跨境专家（cross-border-specialist），
他专精领事认证、跨国继承、跨国税务。你要我现在帮你转过去吗？

[转介到 medical-guide]
这个问题涉及医疗流程，我建议你咨询我们的医疗导航员（medical-guide），
他能帮你梳理就医流程和医保报销。你要我现在帮你转过去吗？
```

### 转介时的上下文传递

转介时，原智能体应整理一份**上下文摘要**传递给目标智能体：
- 用户基本情况（地区/关系/时间/情形）
- 已确认的关键信息（遗嘱/家庭/财产）
- 当前问题与转介原因
- 已完成的事项

格式：
```
【转介摘要】
- 转介自: [智能体名]
- 转介原因: [一句话]
- 用户情况: [地区/关系/时间/情形]
- 已确认: [遗嘱状态/家庭结构/财产复杂度]
- 已完成事项: [已办理的手续/已联系的机构/已准备的材料，若无则写"尚未办理任何手续"]
- 当前问题: [具体问题]
- 上下文传递: [其他必要信息]
```

### 用户自主决定

- 转介是**建议**，不是强制
- 用户可以拒绝转介，继续与当前智能体对话
- 用户可以随时切换智能体
- 用户可以同时与多个智能体对话（各平台支持度不同）

## 共享与隔离

### 共享（所有智能体共用）
- `rules/` 全部 14 个规则文件（L0-L8 优先级链 + 4 个补充规则）（优先级链一致）
- `knowledge/regions/` 地域知识库
- `knowledge/regions/SCHEMA.md` 格式标准
- `skills/policy-research/` 通用政策搜索技能

### 各智能体私有
- `skills/death-aftercare-guide/` → death-aftercare 独有
- 各智能体的私有子智能体 → 不共享
- 各智能体的独立上下文 → 不共享

### 子智能体的工具权限

| 智能体 | tools | disallowedTools | 能否改知识库 |
|--------|-------|-----------------|------------|
| death-aftercare | WebSearch, WebFetch, Read, Write | — | ✅ 紧急修正 |
| legal-advisor | Read, WebSearch, WebFetch | Write | ❌ 只分析 |
| financial-analyst | Read, WebSearch, WebFetch | Write | ❌ 只分析 |
| policy-researcher | WebSearch, WebFetch, Read, Write | — | ✅ 建库/改库 |
| cross-border-specialist | Read, WebSearch, WebFetch | Write | ❌ 只分析 |
| medical-guide | WebSearch, WebFetch, Read | Write | ❌ 只分析 |
| 所有子智能体 | 按需配置 | Write（除 policy-researcher 的子智能体外） | ❌ 只辅助 |

## 平台无关性

本架构不绑定特定平台。各平台适配方式见 `PLATFORMS.md`：
- TRAE：.traecli/agents/ + Subagent 机制
- 阿里通义/百炼：Assistant API + 插件
- 腾讯混元/元宝：Assistant + 插件
- OpenAI：Assistants API + function calling
- Anthropic：tool use + MCP
- 通用：function calling + 共享文件系统

## 团队扩展

新增并列智能体时：
1. 在 `agents/` 新建 .md 文件
2. 在本文件更新架构图和智能体清单
3. 设计其私有子智能体
4. 配置转介触发规则
5. 确保遵守 rules/ 优先级链

## 版本
- v4.2 补充支撑设施层架构图、debate-arbiter 角色、内外部转介说明、修正规则数量（10→14）
- v3.0 新增第 5 个并列智能体（cross-border-specialist）+ 第 6 个（medical-guide，跨团队）
- v2.0 并列架构（废弃 v1.0 的主-子委派模式）
- 新增：智能体并列、转介机制、私有子智能体、平台无关性
