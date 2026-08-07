# deadman

> 身后事 + 医疗导航多智能体引导平台。不绑定任何厂商，适用于所有支持 agent 的平台。

[![tests](https://github.com/weed33834/deadman/actions/workflows/tests.yml/badge.svg)](https://github.com/weed33834/deadman/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-5.1.0-6b5d4f.svg)](CHANGELOG.md)

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

本项目三端同步维护，GitHub 为主仓库（自动化工作流 / CI / Dependabot 在此生效），其余为国内镜像：

| 平台 | 地址 | 角色 |
|------|------|------|
| **GitHub** | https://github.com/weed33834/deadman | **主仓库**（CI + 自动化） |
| GitCode | https://gitcode.com/badhope/deadman | 国内镜像 |
| Gitee | https://gitee.com/badhope/deadman | 国内镜像 |

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

### 编排韧性与可观测（v5.1）

- **可组合终止条件**（借鉴 AutoGen `TerminationCondition`）：6 个具体子类（MaxSteps / StuckAgent / TokenUsage / MessageCount / External / TextMention），用 `|`（OR 短路）/ `&`（AND 全满足）自由组合。`default_termination()` 等价 P4 的 `MAX_STEPS | STUCK_AGENT_REPEAT_LIMIT`，向后兼容
- **本轮 token 累计**：LLM 调用后 `last_usage` → `state["metrics"]["token_usage"]` 累加（不走 cost_tracker 跨会话串扰），供 `TokenUsageTermination` 评估
- **对话维度看板**：`/api/dashboard` 端点 + 前端 4 张柱状图 + 最近 trace span 表，进程内聚合统计（重启清零，不持久化不跨会话）
- **MCP 工具 schema 自动化**：13 个工具迁移到 `@mcp.tool_auto`，靠 type hints + Google-style docstring 自动生成 JSON Schema，参数与函数签名单一来源

### 企业级落地（v5.1）

- **Handoff 默认开启**：智能体转交作为一等公民机制生产默认启用（`HANDOFF_ENABLED=1`），跨智能体上下文压缩与传递无需显式配置；审计链同步开启（`HANDOFF_AUDIT_ENABLED=1`）。显式 `DEADMAN_HANDOFF_ENABLED=0` 仍可关闭
- **Sentry 错误监控**：FastAPI lifespan 启动时初始化 Sentry SDK，兜底异常处理器与请求日志中间件自动上报未处理异常并关联 `request_id`。DSN 留空或 SDK 未装时零开销降级（no-op，不阻塞主流程）；`send_default_pii=False` 满足 PIPL 合规
- **密码重置**：完整流程 `request`（生成令牌）→ 邮件下发（SMTP 配置时）→ `confirm`（消费令牌重置密码）。256 bit 令牌 + 30 分钟 TTL + 单次使用 + 防枚举响应，符合 OWASP 最佳实践
- **Dead Man Switch 通知通道接通**：`_do_notify_lawyer` / `_do_notify_heirs` 通过 `EmailSender.send_sync()`（stdlib smtplib，无新依赖）真正发送邮件；SMTP 未配置时降级为 `manual_todo`（不阻塞状态机），发送失败时 `retryable`
- **CI 质量门禁**：test（pytest）+ lint（ruff check + ruff format）+ security（pip-audit CVE 扫描）三 job 并行，任一失败阻断合并

### 加密与隐私

- 用户密码：PBKDF2-HMAC-SHA256（100k 迭代）+ 16 字节随机 salt + 防枚举
- JWT：PyJWT 2.8+ 实现 HS256 签发/验证/刷新，过期与篡改由 SDK 校验
- 终活笔记 / 保险库：per-user passphrase 派生（PBKDF2 + AES-256-GCM AEAD 加密 v3，utils/crypto.py 共享模块）
- PII 脱敏：姓名 / 身份证 / 电话 / 账号 / 地址 / 出生日期 落盘前掩码
- 文件级原子写入 + fsync + 0o600 权限

详见 [SECURITY.md](SECURITY.md)。

## 系统架构

```
                          ┌──────────────────────────────────────────────┐
                          │              用户接入层（4 入口）              │
                          │  CLI · Web UI · MCP Server · A2A Server        │
                          └──────────────┬───────────────────────────────┘
                                         │
                ┌────────────────────────┼─────────────────────────┐
                │                        │                         │
        ┌───────▼────────┐    ┌──────────▼──────────┐    ┌────────▼────────┐
        │ input-guardrails│    │  safety-protocol L0 │    │ compliance L3   │
        │  (Prompt Inj.)  │    │ (自杀/他杀风险干预) │    │  (PIPL/边界)    │
        └───────┬────────┘    └──────────┬──────────┘    └────────┬────────┘
                └────────────────────────┼─────────────────────────┘
                                         │
                          ┌──────────────▼───────────────────────┐
                          │      LangGraph build_main_graph      │
                          │   规则链 L0→L8（15 个规则文件）        │
                          └──────────────┬───────────────────────┘
                                         │
        ┌────────────┬────────────┬──────┴───────┬────────────┬────────────┐
        ▼            ▼            ▼              ▼            ▼            ▼
┌──────────────┐┌────────────┐┌────────────┐┌────────────┐┌────────────┐┌────────────┐
│ death-       ││ legal-     ││ financial- ││ policy-    ││ cross-     ││ medical-   │
│ aftercare    ││ advisor    ││ analyst    ││ researcher ││ border-    ││ guide      │
│              ││            ││            ││            ││ specialist ││            │
│ 9 阶段流程   ││ 法律边界   ││ 资产/税务  ││ 跨地域政策 ││ 跨境身后事 ││ 医保/大病  │
└──────┬───────┘└─────┬──────┘└─────┬──────┘└─────┬──────┘└─────┬──────┘└─────┬──────┘
       │              │              │              │              │              │
       └──────────────┴──────┬───────┴──────────────┴──────────────┴──────────────┘
                             │
                  ┌──────────▼──────────┐
                  │  12 个私有子智能体  │
                  │  (regional/legal/   │
                  │   financial/...)    │
                  └──────────┬──────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼────────┐  ┌────────▼────────┐  ┌────────▼────────┐
│  4 层记忆系统  │  │  知识库 + 工具  │  │  数据落地层     │
│  working/epi/  │  │  CN 34 省/自治区/直辖市 + US + │  │  ending_note/   │
│  semantic/proc │  │  JP + WebSearch │  │  vault/cases/   │
│                │  │  + 15 MCP 工具  │  │  switch/tickets │
└────────────────┘  └─────────────────┘  └─────────────────┘
       全部加密落盘（per-user passphrase + PBKDF2 + AES-256-GCM v3）
```

## 智能体详解

### death-aftercare（身后事流程引导）

主智能体，9 阶段标准流程：

1. **死亡证明** —— 在医院 / 家中 / 非正常死亡三种情形下分别办理
2. **遗体处理** —— 殡仪馆接运 / 冷藏 / 火化 / 土葬许可
3. **身份注销** —— 户口 / 身份证 / 护照 / 港澳台通行证
4. **数字账号** —— 微信 / 支付宝 / 银行 App / 网盘 / 游戏账号
5. **金融资产** —— 银行存款 / 股票 / 基金 / 保险 / 住房公积金
6. **不动产车辆** —— 房产过户 / 车辆继承 / 公证材料清单
7. **遗产继承** —— 法定 / 遗嘱 / 遗赠扶养 / 跨代继承
8. **社保福利** —— 丧葬费 / 抚恤金 / 个人账户余额
9. **债权债务** —— 债务清偿 / 债权追索 / 失踪宣告

### legal-advisor（法律边界告知）

绝不出具法律意见。仅告知：
- 当前场景可能涉及的法律边界
- 需要专业律师介入的信号
- 公证材料清单的合法性要求
- 遗嘱效力的形式要件

### financial-analyst（财产风险提示）

- 遗产税（中国目前无，但跨境可能触发）
- 资产隐匿风险
- 银行账户冻结流程
- 保险受益人指定 vs 法定继承
- 跨境资产申报

### policy-researcher（跨地域政策调研）

中国 34 省级行政区 + 美国 + 日本，每地 9 阶段政策文件：
- 北京 / 上海 / 广东 / 浙江 / 江苏（已覆盖）
- 其他省份持续完善中（按需求优先级）
- 美国（加州已覆盖）
- 日本（已覆盖）

### cross-border-specialist（跨境身后事）

- 中国公民在境外去世：领事馆协助流程
- 外籍人士在中国去世：大使馆通报 + 遗体出境许可
- 跨境资产继承：双重视角下的税务居民身份
- 涉外公证与海牙认证

### medical-guide（医疗政策导航）

- 医保账户注销与余额继承
- 大病保险理赔
- 临终关怀（安宁疗护）资源
- 慢性病管理档案处置

## 安全模型

### 数据流加密

```
用户输入 → input-guardrails → safety(L0) → graph(L1-L8) → 智能体
                                                         ↓
                                            PII 脱敏（姓名/身份证/电话/账号/地址/出生日期）
                                                         ↓
                                            per-user passphrase（HMAC-SHA256(global, user_id)）
                                                         ↓
                                            PBKDF2-HMAC-SHA256（100k 迭代）派生 enc_key
                                                         ↓
                                            AES-256-GCM（AEAD）加密 + 认证 tag 完整性 v3
                                                         ↓
                                            原子写入 ~/.deadman/（0o600 权限）
```

### 威胁模型

| 威胁 | 缓解 |
|------|------|
| 攻击者获取 `~/.deadman/` 离线数据 | per-user passphrase 派生，无全局密钥可解 |
| 攻击者篡改 envelope | AES-256-GCM AEAD 认证 tag 验签失败拒绝解密 |
| 用户越权访问他人笔记 | 所有端点走 `_phase_auth_user()` JWT 认证 + ownership 校验 |
| Prompt Injection 绕过规则链 | L2 input-guardrails + L0 safety 双层防御 |
| 自杀风险信号触发代办 | L0 即时拦截，输出 12320 / 988 热线，绝不代办 |
| 主动通知扰民 | L4 notification-guardrails 默认静默 + 7 天等待期 + 退订机制 |
| 伪造/篡改 JWT | PyJWT HS256 签名校验，过期/签名错误统一拒绝 |
| 邮箱枚举 | 登录失败统一返回"邮箱或密码错误"；密码重置无论邮箱是否存在统一返回成功 |
| 密码爆破 | PBKDF2 100k 迭代 + 16B salt |
| 密码重置令牌重放 | 256 bit 令牌 + 30 分钟 TTL + 单次消费（consume 即删） |

## 常见问题 FAQ

### Q1：这是不是代办身后事的服务？

不是。deadman 严格遵循 `compliance-framework.md` 四项禁止：

- **不代办** —— 不替用户跑腿、不替用户填写表格
- **不代查** —— 不查询用户银行账户、社保账户
- **不出具法律意见** —— 不替律师做判断
- **不与殡葬机构分成** —— 不收返佣、不导流特定机构

deadman 仅做信息引导与流程梳理，让用户知道"接下来该办什么、找谁办、需要带什么材料"。

### Q2：数据安全吗？我的隐私怎么保证？

- 用户密码用 PBKDF2-HMAC-SHA256 加盐哈希（100k 迭代）
- 终活笔记 / 保险库内容用 per-user passphrase 派生密钥后 AES-256-GCM 加密落盘（v3）
- PII 字段（姓名 / 身份证 / 电话 / 账号 / 地址 / 出生日期）落盘前掩码
- 文件权限 0o600，目录权限 0o700
- 全程本地部署，数据不出你的机器（自托管模式）
- 详见 [SECURITY.md](SECURITY.md) 与 [docs/privacy.md](docs/privacy.md)

### Q3：支持哪些 LLM？

- OpenAI（GPT-4o / GPT-4 Turbo / GPT-3.5）
- Anthropic（Claude 3.5 Sonnet / Claude 3 Opus）
- 智谱（GLM-4.6 / GLM-4-Plus）—— 国内首选
- 通义千问（阿里云 DashScope，qwen-max / qwen-plus / qwen-turbo / qwen-long）
- DeepSeek（deepseek-chat / deepseek-reasoner）
- 文心一言（百度智能云千帆，ernie-4.5 / ernie-speed / ernie-lite）
- 任何 OpenAI 兼容协议的 LLM（通过 `LLM_BASE_URL` 配置）
- 支持多 provider fallback 链：主 LLM 失败按序尝试备选

### Q4：MCP Server 和 A2A Server 是什么？

- **MCP Server**（端口 8000）：标准 MCP 协议，供 Claude Desktop / TRAE / 任何 MCP 客户端调用 15 个工具
- **A2A Server**（端口 8001）：跨智能体协议，让 deadman 与其他智能体平台互联
- **Web UI**（端口 8002）：对话界面 + 运维看板 + 测试中心

四个入口共享同一套智能体、规则链、知识库，按需选择。

### Q5：Dead Man Switch 是什么？怎么用？

借鉴 GoodTrust 的 Dead Man Switch 功能并升级为多因子验证：

1. 用户初始化 switch，设定 checkin 周期（如 30 天）
2. 用户定期 checkin 重置计时器
3. 超期未 checkin → 状态从 ACTIVE → SUSPECTED
4. 系统通过邮件 / 短信联系用户，无响应 → VERIFYING
5. 联系紧急联系人 / 律师 / 法定继承人确认 → CONFIRMED
6. 7 天等待期后 → EXECUTED，触发预设动作（投递终活笔记 / 通知受益人）

避免单一信号误判导致的死亡推定。

### Q6：和 Cake / Everplans / Trust & Will 这些海外产品有什么区别？

详见 [docs/competitive-research-round2.md](docs/competitive-research-round2.md)。

主要差异：

- **本土化**：deadman 覆盖中国全部 34 个省级行政区（含港澳台）+ 8 类通知信函 + 微信公众号接入，海外产品只做美国
- **多智能体**：deadman 6 智能体 + 12 子智能体协作，海外产品是单体应用
- **规则优先级链**：deadman 有 L0-L8 共 15 个规则文件硬约束，海外产品无此机制
- **开源**：deadman MIT 开源，海外产品全部闭源 SaaS
- **不绑定 LLM**：deadman 支持 OpenAI/Anthropic/智谱任一，海外产品绑死自家模型

### Q7：如何贡献新的省份知识库？

1. 阅读 [.traecli/knowledge/regions/SCHEMA.md](.traecli/knowledge/regions/SCHEMA.md) 9 阶段标准
2. 参考 [beijing.md](.traecli/knowledge/regions/CN/beijing.md) 模板
3. 填写 9 阶段 + 紧急联系方式 + 特殊情形 + 医疗政策补充
4. 元信息标"数据来源"真实 URL，不确定的电话写"请拨打 12345 核实"
5. 跑 `deadman knowledge-freshness-scan --regions-dir .traecli/knowledge/regions` 确认无 stale
6. 提交 PR（按 [CONTRIBUTING.md](CONTRIBUTING.md) 流程）

### Q8：生产部署需要做什么？

必做：

1. 设置环境变量 `DEADMAN_ENDING_NOTE_PASSPHRASE` / `DEADMAN_VAULT_PASSWORD` / `JWT_SECRET`（用强随机串）
2. 启用 HTTPS（Nginx 反代 + Let's Encrypt）
3. `DEADMAN_ALLOW_QUERY_USER_ID=0`（强制 JWT 认证）
4. `LOG_LEVEL=WARNING`（避免 PII 进日志）
5. `~/.deadman/` 目录加密备份

详见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

### Q9：可以用于商业用途吗？

可以。deadman 使用 MIT 协议，允许商业使用、修改、分发、再授权。但请：

- 保留 LICENSE 文件与版权声明
- 自行承担合规风险（身后事在中国大陆受 PIPL / 殡葬管理条例等约束）
- 不要用 deadman 名义做未授权的担保
- 商业部署建议联系法律顾问确认合规边界

### Q10：项目路线图？

近期（v5.x）已完成：

- [x] AES-256-GCM 加密升级（utils/crypto.py 共享模块，替换 HMAC-SHA256 keystream 流密码）
- [x] L0 安全检查修复（同时检测 user_input 和 output_text）
- [x] Web API 加固（速率限制 + CORS + Pydantic 校验 + 安全头）
- [x] 结构化日志升级（structlog 集成）
- [x] 场景 2 & 8 完整测试覆盖
- [x] 编排韧性（可组合终止条件 + 对话维度看板 + MCP tool_auto schema 自动化）
- [x] Handoff 默认开启（跨智能体上下文压缩与传递无需显式配置）
- [x] Sentry 错误监控集成（可选依赖，零开销降级）
- [x] 密码重置流程（令牌 + TTL + 防枚举）
- [x] Dead Man Switch 通知邮件通道接通（EmailSender.send_sync）
- [x] CI 质量门禁（ruff lint + format + pip-audit 安全扫描）

近期（v5.x）进行中：

- [x] 补齐全国 34 个省级行政区知识库（含港澳台，统一渲染器 + 数据纪律校验）
- [x] 国产 LLM 接入（智谱 / 通义千问 / DeepSeek / 文心一言，均 OpenAI 兼容）
- [ ] 移动端 App（React Native）

中期（v6.x）：

- [ ] 托管服务（SaaS 模式，仍开源）
- [ ] 法律主体注册与 ICP 备案
- [ ] 多语言 UI（中 / 英 / 日）

长期（v7+）：

- [ ] 跨境身后事一站式中台
- [ ] 与公证处 / 殡仪馆系统直连（合规前提下）

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
# 国产大模型（均 OpenAI 兼容，LLM_BASE_URL 可留空自动解析）：
# export LLM_PROVIDER="qwen"      && export DASHSCOPE_API_KEY="sk-xxx"
# export LLM_PROVIDER="deepseek"  && export DEEPSEEK_API_KEY="sk-xxx"
# export LLM_PROVIDER="ernie"     && export QIANFAN_API_KEY="xxx"
# fallback 链亦可混合国产模型：
# export LLM_FALLBACK_CHAIN="zhipu:glm-4.6,deepseek:deepseek-chat,qwen:qwen-max"

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
- **对话维度看板**（v5.1）—— 进程内对话统计：智能体调用次数 / 风险分级分布 / span 类型分布 / 终止触发原因 / token 累计 / 降级计数 + 最近 20 条 trace span 表（仅展示聚合维度，不展示用户输入/响应内容）
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
    ├── knowledge/                           # 地域知识库（CN 34 省/自治区/直辖市 + US + JP）
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
        │   ├── orchestration/               #   LangGraph 编排 + 可组合终止条件（v5.1）
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
        └── tests/                           #   pytest 测试（2586+）
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
| [CHANGELOG.md](CHANGELOG.md) | 变更日志（当前 v5.1.0） |
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
# 全量回归（2586+ 测试）
cd deadman
pytest -q

# 联调场景（需手动按 scenarios.md 执行）
# 见 .traecli/tests/scenarios.md
```

当前测试规模：**2586 passed + 1 skipped + 0 failed**。

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
