# deadman

> 身后事 + 医疗导航多智能体引导平台。不绑定任何厂商，适用于所有支持 agent 的平台。

[![tests](https://github.com/weed33834/deadman/actions/workflows/tests.yml/badge.svg)](https://github.com/weed33834/deadman/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-5.0.0-6b5d4f.svg)](CHANGELOG.md)

---

## 项目定位

deadman 是一个面向「身后事 + 医疗导航」垂直场景的**多智能体引导平台**。它不代办、不出具法律 / 医学诊断意见、不编造不确定信息，仅做信息引导与流程梳理。

适用场景：

- 亲人刚去世，不知道接下来该办什么手续
- 跨地域（中国 34 省级行政区 / 跨国）身后事流程查询
- 终活笔记（エンディングノート）填写引导
- 数字遗产保险库（密码 / 文档 / 账号 / 加密货币）整理与受益人投递
- 遗嘱 / 信托 / 保险 / 公证材料 AI 文档提取
- 逝者唯一标识（遗码通）案例管理与时间线追踪
- AI 悼文 / 讣告 / 答谢词 / 墓志铭 / 追思会致辞生成
- 8 类中国本土化通知信函生成（户口注销 / 社保丧葬费 / 公积金提取等）
- Dead Man Switch（多因子死亡推定状态机）
- 身后事规划完整度评分（5 维度）

## 仓库地址

本项目两仓平等维护（均为各自平台的主仓库，非镜像关系），任一仓库均可 clone：

| 平台 | 地址 | 用途 |
|------|------|------|
| GitHub | https://github.com/weed33834/deadman | 国际镜像 + CI |
| GitCode | https://gitcode.com/badhope/deadman | 国内主仓库 |

## 核心特性

### 多智能体架构

6 个并列智能体（详见 [`.traecli/agents/`](.traecli/agents/)）：

| 智能体 | 职责 |
|--------|------|
| `death-aftercare` | 身后事流程引导（9 阶段：死亡证明 → 债权债务） |
| `legal-advisor` | 法律边界告知（绝不出法律意见） |
| `financial-analyst` | 财产 / 资产 / 税务风险提示 |
| `policy-researcher` | 跨地域政策调研（中国 34 省级 + 美国 + 日本） |
| `cross-border-specialist` | 跨境身后事（领事馆 / 海外资产 / 跨境继承） |
| `medical-guide` | 医疗政策导航（医保 / 大病 / 临终关怀） |

每个并列智能体下挂多个私有子智能体（共 12 个），通过 LangGraph 编排。

### 规则优先级链 L0-L8

15 个规则文件构成硬约束（详见 [`.traecli/rules/`](.traecli/rules/)）：

```
safety(L0) > integrity(L1) > input-guardrails(L2) > compliance(L3) >
risk-tier(L4) > transparency(L5) > accountability(L6) >
retrieval-guardrails(L7) > tone(L8) > notification-guardrails(L4 补充)
```

- **L0 safety**：自杀 / 他杀 / 非正常死亡风险信号即时干预
- **L1 integrity**：不编造、不代办、不出具专业意见
- **L2 input-guardrails**：Prompt Injection 防御、PII 输入仅作 URL params
- **L3 compliance**：平台身份告知、四项禁止、数据治理底线
- **L4 notification-guardrails**：默认静默、7 项硬约束、双重确认、7 天等待期

### 4 层记忆系统

`working / episodic / semantic / procedural memory`，跨会话上下文保留。

### 加密与隐私

- 用户密码：PBKDF2-HMAC-SHA256（100k 迭代）+ 16 字节随机 salt + 防枚举
- JWT：自实现 HS256 + `hmac.compare_digest` 防时序攻击（无 pyjwt 依赖）
- 终活笔记 / 保险库：per-user passphrase 派生（PBKDF2 + HMAC-SHA256 keystream + 完整性 tag）
- PII 脱敏：姓名 / 身份证 / 电话 / 账号 / 地址 / 出生日期 落盘前掩码
- 文件级原子写入 + fsync + 0o600 权限

详见 [SECURITY.md](SECURITY.md)。

## 快速开始

### 安装

```bash
git clone https://gitcode.com/badhope/deadman.git
# 或：git clone https://github.com/weed33834/deadman.git
cd deadman
pip install -e .
```

### 配置环境变量

```bash
export LLM_API_KEY="your-api-key"
export LLM_MODEL="gpt-4o"
export LLM_PROVIDER="openai"
# 国内可用智谱：
# export LLM_PROVIDER="zhipu"
# export LLM_MODEL="glm-4.6"

# 生产部署必须设置（否则用开发默认值并打印警告）：
export DEADMAN_ENDING_NOTE_PASSPHRASE="<强随机串>"
export DEADMAN_VAULT_PASSWORD="<强随机串>"
export JWT_SECRET="<强随机串>"
```

完整环境变量见 [`.env.example`](.env.example)。

### 运行

平台提供四种入口，按需选择：

```bash
# MCP Server —— 供智能体平台调用（JSON-RPC，端口 8000）
deadman mcp-server

# Web UI —— 对话界面 + 运维看板 + 测试中心（端口 8002）
deadman-web-server

# A2A Server —— 跨智能体协议（端口 8001）
deadman-a2a-server

# CLI 单次对话
deadman run "我爸在北京去世了，需要办什么手续？"

# 评估套件
deadman eval -v
```

Web UI（`http://localhost:8002`）包含：

- **对话** —— 六个智能体可切换，SSE 流式响应，移动端响应式
- **运维看板** —— 各领域反馈闭环状态、记忆分层条目数、部署工件校验
- **测试中心** —— 分领域运行诊断命令，查看延迟与可用性
- **资源列表** —— 智能体与 MCP 工具清单
- **onboarding 向导** —— 5 步引导（关系 / 地点 / 日期 / 已办事项 / 知情同意）
- **工单系统** —— 用户提交 / 追踪 / 关闭工单
- **合规页面** —— [隐私政策](docs/privacy.md) / [用户协议](docs/terms.md) / [帮助与支持](docs/support.md)

### Docker 部署

```bash
docker build -t deadman .

# MCP Server
docker run -p 8000:8000 -e LLM_API_KEY=sk-xxx deadman

# Web UI
docker run -p 8002:8002 -e LLM_API_KEY=sk-xxx deadman web-server
```

全量部署（含 Neo4j / Langfuse / OTel Collector）：

```bash
docker compose --profile full up -d
```

详见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 项目结构

```
deadman/
├── README.md / CHANGELOG.md / LICENSE      # 项目入口
├── CONTRIBUTING.md / CODE_OF_CONDUCT.md    # 贡献规范
├── SECURITY.md                              # 安全策略
├── BRAND.md / PLATFORMS.md                  # 品牌 / 平台适配
├── Dockerfile / docker-compose.yml          # 容器化
├── pyproject.toml                           # Python 包定义
├── docs/                                    # 文档（含隐私/协议/支持/部署/竞品调研/PM 评估）
└── .traecli/                                # 业务实现
    ├── agents/                              # 智能体定义（6 并列 + 12 子智能体）
    ├── rules/                               # 规则文件（L0-L8 优先级链 15 个）
    ├── knowledge/                           # 地域知识库（CN 5 省 + US + JP）
    │   └── regions/                         #   SCHEMA.md + 各地域 9 阶段政策
    ├── skills/                              # 技能定义
    ├── tests/                               # 联调场景 + golden cases
    └── src/
        ├── deadman/                         # Python 实现
        │   ├── cli.py                       #   CLI 入口（80+ 子命令）
        │   ├── _cli_extensions/             #   分 Phase 注册的 CLI 子命令
        │   ├── web/                         #   Web UI + API（30+ 端点）
        │   ├── mcp_server/                  #   MCP Server（15 工具）
        │   ├── a2a/                         #   A2A 协议
        │   ├── orchestration/               #   LangGraph 编排
        │   ├── memory/                      #   4 层记忆
        │   ├── auth/                        #   用户认证 + JWT
        │   ├── ending_note/                 #   终活笔记（9 章节 + 加密）
        │   ├── vault/                       #   数字遗产保险库（8 类型）
        │   ├── doc_extract/                 #   AI 文档提取（7 类型）
        │   ├── decedent_id/                 #   遗码通逝者案例
        │   ├── memorial_writer/             #   AI 悼文生成
        │   ├── notification_letters/        #   8 类通知信函
        │   ├── deadman_switch/              #   多因子死亡推定状态机
        │   ├── plan_score/                  #   规划完整度评分
        │   ├── support/                     #   客服工单
        │   ├── onboarding/                  #   5 步引导向导
        │   ├── gateway/                     #   平台连接器（Telegram + 微信）
        │   ├── disclaimer/                  #   法律免责
        │   ├── hotlines/                    #   官方热线查询
        │   ├── institutions/                #   殡葬机构查询
        │   ├── cron/                        #   定时任务 + 知识库时效巡检
        │   ├── tools/                       #   Web 搜索（DuckDuckGo + Baidu + Bing CN）
        │   ├── notification/                #   主动通知护栏
        │   ├── observability/               #   OTel + Langfuse
        │   └── ...                          #   其余模块
        └── tests/                           #   pytest 测试（800+）
```

## CLI 子命令总览

```bash
deadman --help
```

主要分组：

- **基础**：`version` / `eval` / `eval-list` / `run` / `chat`
- **LLM**：`llm-test` / `llm-sync-models` / `llm-cost`
- **认证**：`auth-register` / `auth-login` / `auth-me` / `auth-user-list`
- **告知**：`disclaimer-show` / `hotline-lookup` / `institution-search`
- **终活笔记**：`ending-note-show` / `ending-note-guide` / `ending-note-share` / `ending-note-completion`
- **保险库**：`vault-add` / `vault-list` / `vault-get` / `vault-delete` / `vault-beneficiaries` / `vault-inherited` / `vault-trigger`
- **文档提取**：`doc-extract` / `doc-list` / `doc-get` / `doc-delete`
- **遗码通**：`case-create` / `case-list` / `case-get` / `case-event-add` / `case-archive` / `case-timeline`
- **悼文生成**：`memorial-generate` / `memorial-list-types`
- **通知信函**：`letter-generate` / `letter-list-types` / `letter-template`
- **Dead Man Switch**：`switch-init` / `switch-checkin` / `switch-status` / `switch-tick` / `switch-verify-contact` / `switch-verify-heir` / `switch-cancel` / `switch-list-actions` / `switch-execute`
- **规划评分**：`plan-score` / `plan-score-detail`
- **工单**：`ticket-create` / `ticket-list` / `ticket-get` / `ticket-reply` / `ticket-close`
- **Onboarding**：`onboarding-show` / `onboarding-save` / `onboarding-steps`
- **知识库巡检**：`knowledge-freshness-scan` / `knowledge-freshness-check`
- **搜索**：`search-baidu` / `search-bing-cn`
- **微信**：`wechat-webhook-test`
- **运维**：`obs-dashboard` / `obs-test` / `deploy-check` / `cron-list` / `cron-validate` 等

完整列表见 `deadman --help`。

## 文档

| 文档 | 说明 |
|------|------|
| [CHANGELOG.md](CHANGELOG.md) | 变更日志（当前 v5.0.0） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 行为准则 |
| [SECURITY.md](SECURITY.md) | 安全策略与漏洞报告 |
| [BRAND.md](BRAND.md) | 品牌名规范 |
| [PLATFORMS.md](PLATFORMS.md) | 平台适配（LLM / 搜索 / 智能体平台） |
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | 快速开始 |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | 部署指南 |
| [docs/privacy.md](docs/privacy.md) | 隐私政策 |
| [docs/terms.md](docs/terms.md) | 用户协议 |
| [docs/support.md](docs/support.md) | 帮助与支持 |
| [docs/pm-assessment-v2.md](docs/pm-assessment-v2.md) | PM v2 评估报告（62/100） |
| [docs/competitive-research-round2.md](docs/competitive-research-round2.md) | 第二轮竞品调研（15 家国际产品） |
| [.traecli/src/README.md](.traecli/src/README.md) | 源码 README |
| [.traecli/tests/scenarios.md](.traecli/tests/scenarios.md) | 8 个联调场景 |
| [.traecli/tests/golden-cases.md](.traecli/tests/golden-cases.md) | 20 个 golden case |

## 测试

```bash
# 全量回归（800+ 测试）
cd deadman
python -m pytest .traecli/src/tests/ -q

# 联调场景（需手动按 scenarios.md 执行）
# 见 .traecli/tests/scenarios.md
```

当前测试规模：**820 passed + 1 skipped + 0 failed**。

## 贡献

欢迎贡献新智能体 / 新规则 / 新地域知识库 / 新测试场景。

请先阅读：

1. [CONTRIBUTING.md](CONTRIBUTING.md) — 贡献流程与规范
2. [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — 行为准则
3. [SECURITY.md](SECURITY.md) — 安全相关贡献的额外要求

**核心约束**（不可妥协）：

- 不引入代办 / 代查 / 出具法律 / 医学诊断意见 / 编造不确定信息
- 不削弱 L0-L8 优先级链
- 新增内容附置信度标注与来源透传
- PII 字段落盘前必须脱敏
- 主动通知场景遵守 `notification-guardrails.md`（默认静默 / 频率上限 / 7 天等待期 / 退订机制）

## 自动化策略

本项目刻意保持**最低限度自动化**：

- ✅ CI 仅运行 pytest（[`.github/workflows/tests.yml`](.github/workflows/tests.yml)），不自动合并
- ❌ 不配置 Dependabot / Renovate（依赖由维护者手动更新）
- ❌ 不配置 release 自动化机器人（手动打 tag + 手动写 CHANGELOG）
- ❌ 不配置 auto-assign / stale / welcome 等 GitHub App 机器人
- ❌ 不配置 AI 自动 PR review

理由：身后事是强信任品类，任何自动行为都可能引入未审慎的变更。维护者更倾向于手动 review + 手动合并。

## License

[MIT](LICENSE) © deadman Team

## 致谢

本项目在设计中参考了以下开源 / 商业产品的优秀实践（仅借鉴设计思路，未直接使用其代码）：

- **OpenClaw** / **Hermes Agent** — 多智能体编排与平台连接器抽象
- **Cake / Everplans / Lantern / Empathy / Tomorrow / Fabric** — 身后事规划产品
- **Nolo WillMaker / Trust & Will / GoodTrust / FreeWill** — 遗产规划工具
- **Better Place Forests / eFuneral / Toast / Afterword / Willing** — 殡葬与悼念服务
- **日本わが家ノート / SouSou / そなえ / 遺言ネット** — 終活应用
- **重庆「渝逝有安」/ 山东「白事一点通」/ 铜陵「身后一件事」** — 国内政务小程序

竞品调研详见 [docs/competitive-research-round2.md](docs/competitive-research-round2.md)。

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=weed33834/deadman&type=Date)](https://star-history.com/#weed33834/deadman&Date)
