# 身后事 + 医疗导航多智能体平台

> 一个面向重大生活变故（身后事安排、重大疾病就医导航）的多智能体引导平台。平台采用**并列智能体架构**——多个专业智能体地位平等、并列面向用户，通过转介（recommend）协作，而非主-子委派（delegate）。所有智能体共享同一套规则与知识库，跨平台可迁移。

## 平台简介

本平台聚焦两类高频重大生活变故场景：

1. **身后事引导**：亲属去世后的全流程引导——死亡证明、遗体处理、身份注销、数字账号、金融资产、房产车辆、遗产继承、社保福利、债权债务共 9 阶段
2. **医疗导航**：重大疾病确诊后的就医流程引导——就医指引、二次意见、临床试验、医保流程、转诊、临终关怀、医疗纠纷初步指引

平台**绝不代办**任何需要身份认证的官方手续，**绝不出具**法律/医学诊断意见，**绝不编造**任何不确定信息。核心定位是**流程引导 + 风险提示 + 转介专业人士**。

## 架构概览

```
                           用户
                             ↓
        ┌─────────┬─────────┬─────────┬─────────┬─────────┬─────────┐
        ↓         ↓         ↓         ↓         ↓         ↓
   ┌─────────┐┌─────────┐┌─────────┐┌─────────┐┌─────────┐┌─────────┐
   │death-   ││legal-   ││financial││policy-  ││cross-   ││medical- │
   │aftercare││advisor  ││-analyst ││researcher││border-  ││guide    │
   │(流程引导)││(法律)   ││(财务)   ││(政策搜索)││specialist││(医疗导航)│
   │         ││         ││         ││         ││(跨境)   ││         │
   ├─────────┤├─────────┤├─────────┤├─────────┤├─────────┤├─────────┤
   │子:情绪  ││子:案例  ││子:资产  ││子:多语言││子:领事  ││子:医院  │
   │子:跟进  ││子:法条  ││子:税务  ││子:源验证││子:冲突  ││子:医保  │
   └─────────┘└─────────┘└─────────┘└─────────┘└─────────┘└─────────┘
        ↕          ↕          ↕          ↕          ↕          ↕
    ┌────────────────────────────────────────────────────────────────┐
    │             共享层：rules/（10 规则）+ knowledge/regions/        │
    └────────────────────────────────────────────────────────────────┘
```

### 6 个并列智能体

| 智能体 | 定位 | 所属团队 |
|--------|------|---------|
| death-aftercare | 身后事流程引导员 | 身后事团队 |
| legal-advisor | 法律顾问（不出法律意见） | 身后事团队 |
| financial-analyst | 财务分析师（不代办财务） | 身后事团队 |
| policy-researcher | 政策搜索员（建/改地域知识库） | 身后事团队 |
| cross-border-specialist | 跨境专家（领事/法律冲突） | 身后事团队 |
| medical-guide | 医疗导航员（不出诊断意见） | 医疗导航团队 |

### 12 个私有子智能体

每个并列智能体有自己的私有子智能体，**只服务于其父智能体**，不直接面对用户：

- death-aftercare：death-aftercare-emotional（情绪支持）、death-aftercare-tracker（流程跟进）
- legal-advisor：legal-advisor-cases（案例检索）、legal-advisor-statutes（法条查证）
- financial-analyst：financial-analyst-assets（资产清点）、financial-analyst-taxes（税务计算）
- policy-researcher：policy-researcher-search（多语言搜索）、policy-researcher-verify（源验证）
- cross-border-specialist：cross-border-specialist-consul（领事信息）、cross-border-specialist-conflict（法律冲突）
- medical-guide：medical-guide-hospital（医院信息）、medical-guide-insurance（医保导航）

### 10 个共享规则（优先级链）

```
L0 safety-protocol        人身安全（零弹性，绝对优先）
L1 integrity-framework    诚信不造假（零弹性）
L2 input-guardrails       防注入/越狱（零弹性）
L3 compliance-framework   合规边界（硬边界）
L4 risk-tier-framework    风险分级响应（L2 强制提示）
L5 transparency-framework 透明度告知
L6 accountability-framework 问责申诉
L7 retrieval-guardrails   检索护栏
L8 tone-framework         语气准则（弹性最大）
```

总原则：高层级永远赢低层级。**安全赢一切，诚信赢温和，合规赢有用。**

### 2 个技能（Skills）

- `skills/death-aftercare-guide/`：身后事 9 阶段引导技能（death-aftercare 独有）
- `skills/policy-research/`：政策搜索通用技能（policy-researcher 主用，其他智能体必要时可用）

### 知识库

- `knowledge/regions/SCHEMA.md`：地域知识库格式标准
- `knowledge/regions/CN/`：中国知识库（overview.md + general.md）
- `knowledge/regions/US/`：美国知识库（overview.md + california.md）
- `knowledge/regions/JP/`：日本知识库（overview.md）

## 快速开始

### 1. 加载到 TRAE（字节跳动）

- 将 `.traecli/` 目录复制到项目根目录（或用户级 `~/.trae-cn/`）
- TRAE 自动识别 `agents/` 下的 `.md` 智能体定义
- 子智能体通过 TRAE Subagent 机制自动按 description 匹配调用
- 共享文件系统（rules/ + knowledge/）通过 Read 工具直接访问
- 详见 `PLATFORMS.md` 第 1 节

### 2. 加载到其他平台

- 阿里通义/百炼：上传 rules/ 和 knowledge/ 到"知识库"，按 `PLATFORMS.md` 第 2 节配置
- 腾讯混元/元宝：按 `PLATFORMS.md` 第 3 节配置
- OpenAI Assistants API / Agents SDK：按 `PLATFORMS.md` 第 4 节配置
- Anthropic Claude（tool use + MCP）：按 `PLATFORMS.md` 第 5 节配置
- Google Vertex AI / AWS Bedrock / Coze / Dify / 智谱 GLM / Kimi / MiniMax：按 `PLATFORMS.md` 第 7-13 节配置
- 通用 function calling 平台：按 `PLATFORMS.md` 第 6 节配置

### 3. 验证加载

- 运行 `tests/scenarios.md` 中的 8 个场景，逐场景核对
- 运行 `tests/golden-cases.md` 中的 20 个 golden case，全部通过才可发布
- 关闭真实联网，WebSearch/WebFetch 返回预设 fixture 或空结果（验证"无数据时不编造"）

## 目录结构说明

```
.traecli/
├── README.md                    # 本文件
├── CHANGELOG.md                 # 版本变更日志
├── CONTRIBUTING.md              # 贡献指南
├── PLATFORMS.md                 # 平台适配指南（13 个平台）
├── agents/                      # 智能体定义
│   ├── TEAM.md                  # 团队架构（必读）
│   ├── death-aftercare.md       # 6 个并列智能体
│   ├── death-aftercare-emotional.md  # 12 个私有子智能体
│   └── ...
├── rules/                       # 10 个共享规则
│   ├── conflict-resolution.md   # 优先级链（必读）
│   ├── safety-protocol.md
│   ├── integrity-framework.md
│   └── ...
├── skills/                      # 2 个技能
│   ├── death-aftercare-guide/   # 9 阶段引导技能
│   └── policy-research/         # 政策搜索通用技能
├── knowledge/
│   └── regions/
│       ├── SCHEMA.md            # 地域知识库格式标准
│       ├── CN/                  # 中国知识库
│       ├── US/                  # 美国知识库
│       └── JP/                  # 日本知识库
└── tests/
    ├── scenarios.md             # 8 个联调测试场景
    └── golden-cases.md          # 20 个回归测试 case
```

## 核心设计原则

### 1. 并列而非主-子
- 6 个智能体地位平等，各自独立面向用户
- 没有"主 agent 编排一切"的设计
- 用户可选择与任何一个智能体对话

### 2. 转介而非委派
- **转介（recommend）**：智能体 A 发现用户问题更适合智能体 B，建议用户去找 B
- **委派（delegate）**（旧设计，已废弃）：主 agent 把任务派给子 agent
- 转介时整理【转介摘要】传递上下文，用户自主决定是否转介

### 3. 共享规则与知识库
- `rules/` 全部 10 个规则文件——所有智能体共用，优先级链一致
- `knowledge/regions/` 地域知识库——所有智能体共用
- 各智能体的私有子智能体与独立上下文——不共享

### 4. 平台无关
- 智能体定义使用统一 Markdown + YAML frontmatter 格式
- 通过适配层转换到各主流平台（详见 `PLATFORMS.md`）
- 核心逻辑（转介、子智能体、规则优先级）跨平台一致

### 5. 风险分级响应
- L1 常规 / L2 重大财产法律风险 / L3 即时人身安全
- L2 强制风险提示，L3 立即停止常规流程
- 优先级链：safety > integrity > input-guardrails > compliance > risk-tier > transparency > accountability > retrieval-guardrails > tone

## 链接

- 团队架构与转介机制：[`agents/TEAM.md`](agents/TEAM.md)
- 平台适配指南：[`PLATFORMS.md`](PLATFORMS.md)
- 规则文件目录：[`rules/`](rules/)
- 测试场景与 golden case：[`tests/scenarios.md`](tests/scenarios.md) / [`tests/golden-cases.md`](tests/golden-cases.md)
- 贡献指南：[`CONTRIBUTING.md`](CONTRIBUTING.md)
- 版本变更日志：[`CHANGELOG.md`](CHANGELOG.md)
- 地域知识库格式标准：[`knowledge/regions/SCHEMA.md`](knowledge/regions/SCHEMA.md)

## 版本

- v3.0 并列架构 + 6 智能体 + 12 子智能体 + 10 规则 + 跨团队（医疗导航）

详见 [`CHANGELOG.md`](CHANGELOG.md)。
