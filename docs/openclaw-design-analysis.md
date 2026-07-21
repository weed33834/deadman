# OpenClaw 设计理念分析（与 deadman 架构对比）

> 本文件为 deadman 项目对 OpenClaw（github.com/openclaw/openclaw）的设计理念调研。
> 调研方式：直接克隆 OpenClaw 仓库（Node.js + TypeScript 实现），阅读 README.md / VISION.md / AGENTS.md / CHANGELOG.md / docs/ 关键文档，对照 deadman 现有架构做差异化分析。
> 严格约束：**不搬代码**（OpenClaw 是 TS，deadman 是 Python，技术栈不匹配），只提取**不与 deadman 现有架构冲突**的设计理念，转化为可后续 Phase 决策的参考。
> 调研日期：2026-07。OpenClaw 当前版本：2026.7.1（YYYY.M.PATCH 月度发车制）。

---

## a. OpenClaw 概览

### 项目定位

OpenClaw 自我定位为 **"Personal AI Assistant that actually does things"**——一个跑在用户自己设备上的个人 AI 助手，通过用户已有的消息渠道（WhatsApp / Telegram / Slack / Discord / Google Chat / Signal / iMessage / IRC / Microsoft Teams / Matrix / Feishu / LINE / Mattermost / Nextcloud Talk / Nostr / Synology Chat / Tlon / Twitch / Zalo / WeChat / QQ / WebChat，共 23+ 通道）回答用户。Gateway 是控制面，产品本体是助手本身。

来源：`openclaw/README.md` L17-22，`openclaw/VISION.md` L1-15。

### 技术栈与 License

- **语言**：TypeScript（ESM strict，无 `any`，discriminated unions 优先）
- **运行时**：Node 24.15+ 推荐，Node 22.22.3+ / 25.9+ 也支持；同时保持 Bun 兼容
- **包管理**：pnpm workspace（源码开发），npm 安装用户路径
- **存储**：SQLite（Kysely helpers，禁止 JSON/JSONL/TXT/sidecar 文件存 OpenClaw 运行态）
- **License**：MIT（与 deadman 借鉴的 Hermes Agent 同 License）
- **Star 数**：README 末尾"Star History Chart"未直接给出数字，但社区贡献者墙已超 300 人，CHANGELOG 节奏为月度发车（YYYY.M.PATCH），活跃度高
- **核心作者**：Peter Steinberger（@steipete），项目起因是为一只叫 **Molty** 的"太空龙虾 AI 助手"做的（🦞）

来源：`openclaw/README.md` L11-15 / L96 / L288-293，`openclaw/AGENTS.md` L255-291，`openclaw/VISION.md` L11-13。

### 与 deadman 的核心差异

| 维度 | OpenClaw | deadman |
|------|----------|---------|
| 技术栈 | Node.js + TypeScript（pnpm workspace） | Python（pyproject.toml，stdlib + httpx/tenacity 可选） |
| 场景定位 | 通用个人 AI 助手（23+ 消息通道、桌面 App、Voice Wake、Canvas） | **身后事引导平台**（垂直场景：丧亲家属流程引导，4 层记忆 + 14 规则文件） |
| 主动推送策略 | 默认开启 Heartbeat（30m 唤醒），主动通知是常态 | **默认静默**，主动推送是特权（`notification-guardrails.md` L4 硬边界） |
| 模型路由 | ClawRouter 单 key 多 provider 路由 + 多 harness（openclaw / codex） | LLMClient 多 provider fallback 链，无智能路由 |
| Skills 数量 | 700+（ClawHub 社区市场 + bundled 基线） | 2 个（`death-aftercare-guide` / `policy-research`） |
| 桌面端 | macOS / iOS / Android / Windows Hub 原生 App | 无（Web UI + 消息平台更合适） |
| 沙箱 | Docker / SSH / OpenShell 三后端，`non-main` / `all` 模式 | Docker + Local 双后端，借鉴 Hermes |
| 记忆持久化 | SQLite（Kysely）+ SOUL.md 文件层 | 4 层记忆 + FileMemoryStore + Graphiti/LightRAG 可选 |
| Agent 编排 | 内置 runtime（`src/agents/embedded-agent-runner/`）+ 插件 harness | LangGraph 编排 + Reflexion + Debate-Voting |

来源：`openclaw/README.md` L156-166，`openclaw/AGENTS.md` L77-87，`openclaw/docs/agent-runtime-architecture.md` L10-20，`deadman/.traecli/rules/notification-guardrails.md` L1-15，`deadman/.traecli/src/deadman/llm.py` L1-8，`deadman/.traecli/src/deadman/memory/manager.py` L1-5。

### 整体评价

OpenClaw 是一个**工程化程度极高**的通用 AI 助手框架：架构边界清晰（core / plugin-sdk / extensions 三层）、配置/状态严格走 SQLite（禁止文件态）、文档体系完整（docs/ 下近百个 md 文件按 channels / cli / concepts / gateway / install / platforms / providers / tools 8 大类组织）、CI/release 流程严密（Code SHA + Release SHA 双重 immutable identity）。但其**通用场景定位与 deadman 的身后事垂直场景在多个根本点上不兼容**——这是后续判断"是否借鉴"的核心过滤器。

---

## b. 可借鉴的设计理念（按价值排序）

### 1. 智能模型路由（ClawRouter 模式）—— **值得借鉴（按 risk_tier 改造）**

**OpenClaw 是怎么做的**：
- ClawRouter 是 bundled plugin（`enabledByDefault: true`），用一个 policy-scoped key 统一接入多个上游 model provider
- 模型发现通过 `/v1/catalog`（credential-scoped，只暴露该 key 允许的模型）
- 配额通过 `/v1/usage` 上报月度预算与聚合用量
- 模型选择支持 `agents.defaults.model.primary` + per-agent override + 单次 run override（`openclaw agent --model clawrouter/<provider>/<model>`）
- 模型 failover 文档：`docs/concepts/model-failover.md`，按 auth profile 轮转

来源：`openclaw/docs/providers/clawrouter.md` L9-25 / L72-113，`openclaw/docs/concepts/model-failover.md`。

**deadman 现状**：
- `.traecli/src/deadman/llm.py` 已实现多 provider 支持（openai / anthropic / zhipu / ollama），用 `_PROVIDER_DEFAULTS` 字典配置
- 已有 `LLM_FALLBACK_CHAIN` 顺序重试（主 LLM 失败后按链重试），tenacity 自动重试网络错误/限流
- **但无智能路由**：所有任务（R1 信息查询 / R2 流程引导 / R3 即时人身安全）走同一个 `LLM_PRIMARY_MODEL`，没有"按任务类型选模型"的能力
- 已有 `risk-tier-framework.md` 定义 R1-R3 三级风险，但未与 LLM 路由联动

**是否借鉴**：**是，按 risk_tier 改造**

**如何按 deadman 身后事定位改造**：
- **不引入 ClawRouter 这种代理服务**（deadman 是单用户场景，不需要多 key 聚合）
- **借鉴"按任务特征路由到不同模型"思想**，映射到 deadman 的 risk_tier：
  - R1（信息查询、流程说明）→ 用便宜/快模型（如 glm-flash / gpt-4o-mini）
  - R2（流程引导、情绪支持、材料清单）→ 用中等模型（如 glm-4.6 / claude-haiku）
  - R3（即时人身安全、心理危机干预）→ 用最强模型（如 claude-opus / gpt-4.6），并启用 thinking high
- 实现位置：在 `.traecli/src/deadman/llm.py` 新增 `route_model(risk_tier: str) -> str` 函数，配置项加到 `config.py` 的 `settings`
- **不破坏现有 fallback 链**：路由选定模型后，仍走 `LLM_FALLBACK_CHAIN` 兜底
- **后续 Phase 决策点**：是否在 LangGraph node 层做 risk_tier 判定，还是在 LLMClient 层透明路由

---

### 2. Gateway 单进程多通道 —— **部分借鉴（deadman 已部分实现，可补 channel_directory）**

**OpenClaw 是怎么做的**：
- 单个长生命周期 Gateway daemon 拥有所有消息面（WhatsApp via Baileys / Telegram via grammY / Slack / Discord / Signal / iMessage / WebChat）
- 控制面客户端（macOS App / CLI / Web UI / 自动化）通过 WebSocket 连接到 Gateway（默认 `127.0.0.1:18789`）
- Nodes（macOS / iOS / Android / headless）也走同一 WS server，但声明 `role: node` 带显式 caps/commands
- 每主机一个 Gateway，是唯一打开 WhatsApp session 的进程
- 协议：WS text frames + JSON payloads，首帧必须是 `connect`，含 `hello-ok.features.methods/events` discovery metadata
- 鉴权：shared-secret（`connect.params.auth.token`）/ Tailscale Serve / trusted-proxy / `mode: "none"`（仅私有 ingress）
- 幂等：side-effecting 方法（`send` / `agent`）必须带 idempotency key，服务端短期 dedupe cache

来源：`openclaw/docs/concepts/architecture.md` L8-100，`openclaw/README.md` L156-166。

**deadman 现状**：
- deadman 即将实现 Gateway + Telegram / Cron / Web search + Sandbox（任务描述明确）
- 已有 `.traecli/src/deadman/web/server.py`（ThreadingHTTPServer + SSE 流式 + 多端点）
- 已有 `a2a/server.py`（A2A Protocol 跨智能体通信）
- 已有 `mcp_server/server.py`（MCP 工具暴露）
- **但目前无统一 channel_directory / platform_registry**——Web / A2A / MCP / 即将到来的 Telegram 是分散入口

**是否借鉴**：**部分借鉴**

**如何按 deadman 身后事定位改造**：
- **借鉴"单 Gateway 进程 + 通道注册表"思想**：所有入站通道（Telegram / Web / 未来微信 / 邮件）统一在一个 `channel_registry.py` 注册，每通道声明 `name / inbound_handler / outbound_handler / consent_required / guardrail_level`
- **不借鉴 OpenClaw 的 WS + 多 App 客户端架构**——身后事用户不会装桌面 App，deadman 的"客户端"就是消息平台本身
- **借鉴幂等 key 思想**：主动推送（满足 `notification-guardrails.md` 七项硬约束后）必须带 idempotency key，避免 Cron 重试导致重复推送（这在身后事场景是 L4 级事故）
- **借鉴"Gateway process stays on host, sandbox only for tool execution"**：deadman 的 Gateway 进程跑在主机，仅沙箱化文件写入（已实现 `sandbox/__init__.py` 的 Docker 后端）

---

### 3. SQLite-first 状态存储 —— **部分借鉴（用于 notifications/consent 持久化）**

**OpenClaw 是怎么做的**：
- "Storage default: SQLite only. Do not add JSON/JSONL/TXT/sidecar files for OpenClaw-owned runtime state, caches, queues, registries, indexes, cursors, checkpoints, or plugin scratch data."（AGENTS.md L78，原文大写强调）
- 共享状态 DB：`state/openclaw.sqlite`（全局运行态 + 插件 KV）
- 每 agent DB：`agents/<agentId>/agent/openclaw-agent.sqlite`（agent-scoped 状态/缓存）
- SQLite 改 schema 必须 explicit user discussion，agent 不可自主 bump schema version
- 纯新增表（向下兼容）不 bump version，用 idempotent lazy ensure
- 写事务必须是 synchronous commit section，事务回调用禁止 `await`/Promise

来源：`openclaw/AGENTS.md` L77-89，`openclaw/docs/concepts/architecture.md`。

**deadman 现状**：
- FileMemoryStore 已实现（`memory/file_store.py`）：YAML frontmatter + markdown body，原子写入（`.tmp` + `os.replace` + `fsync`）
- 三个文件：`USER.md` / `MEMORY.md` / `EPISODES.md`
- Graphiti（Neo4j）+ LightRAG 双可选后端，三层降级链：Graphiti → LightRAG → FileMemoryStore
- 通知 consent 目前规划存 `~/.deadman/notifications/consent.json`（JSON 文件）

**是否借鉴**：**部分借鉴（仅限 notifications/consent 层）**

**如何按 deadman 身后事定位改造**：
- **不替换 FileMemoryStore**——它借鉴自 Hermes，已是 deadman v4.5.1 的稳定设计，且 markdown 文件可读性强（用户可手动查看/编辑自己的记忆），符合身后事场景的"用户数据主权"原则
- **不引入 SQLite 作为主记忆后端**——deadman 的 Graphiti/LightRAG 路径已是更高级的图记忆方案
- **可借鉴 SQLite 用于通知 consent / 频率上限计数**：当前规划是 JSON 文件，但通知 consent 需要事务性（opt-in + 时间戳 + 原文 + 频率计数 + 静默窗口判定），SQLite 比 JSON 文件更可靠
  - 建议表结构：`notification_consents(user_id, task_id, opt_in_text, opt_in_at, expires_at)`、`notification_log(user_id, sent_at, channel, content_hash, idempotency_key)`
  - 频率上限校验（单日 1 条 / 单周 3 条 / 单月 8 条）用 SQL 查询比读 JSON 后内存计算更可靠
- **后续 Phase 决策点**：是否在 v4.7.0 引入 SQLite（需要新增 aiosqlite 依赖），还是继续用 JSON 文件 + 文件锁

---

### 4. Skills 加载优先级链 —— **借鉴（deadman 当前只有 2 skill，但加载逻辑可前置设计）**

**OpenClaw 是怎么做的**：
- 6 级加载优先级（高到低）：Workspace skills → Project agent skills → Personal agent skills → Managed/local skills → Bundled skills → Extra dirs + plugin skills
- 同名 skill 高优先级覆盖低优先级
- Skill 名来自 YAML frontmatter `name` 字段（缺失时回退到目录名）
- 加载时按 environment / config / binary presence 过滤
- Per-agent allowlist 控制可见性（与 location precedence 解耦）
- ClawHub 社区市场负责 skill 分发，core 不收新 skill（"New core skills when they can live on ClawHub" — VISION.md L117）

来源：`openclaw/docs/tools/skills.md` L32-100，`openclaw/VISION.md` L87-92 / L117。

**deadman 现状**：
- 2 个 skill：`skills/death-aftercare-guide/`（9 个 stage 文件 + checklists + special-cases）+ `skills/policy-research/`
- Skill 间无优先级覆盖机制，无 allowlist
- `death-aftercare-guide` 是平台级核心 skill，不允许用户覆盖（AI-RULE 严格保护）

**是否借鉴**：**是（仅借鉴优先级分层思想，不借鉴 ClawHub 市场模式）**

**如何按 deadman 身后事定位改造**：
- deadman 不需要 6 级优先级（场景垂直，用户不会装第三方 skill）
- **借鉴 2 级优先级**：平台级 skill（`skills/death-aftercare-guide/` / `skills/policy-research/`，AI-RULE 保护）+ 用户级 skill（`~/.deadman/skills/<name>/SKILL.md`，允许用户自添加，如"本地殡仪馆联系方式查询"）
- **借鉴 allowlist 思想**：每 agent 显式声明可用的 skill 列表（防止 legal-advisor 误用 medical-guide 的 skill）
- **不借鉴 ClawHub 市场分发**——身后事场景的 skill 需要专业审核（合规、心理安全），社区市场模式不适用
- **后续 Phase 决策点**：是否在 v4.8.0 实现 user-level skill 加载，由 SoulLoader 兼管

---

### 5. AGENTS.md 工作树级规则文档 —— **借鉴（文档结构层面）**

**OpenClaw 是怎么做的**：
- AGENTS.md 是"telegraph style, root rules only"的根策略文件
- 每个 subtree 有自己的 scoped AGENTS.md（`extensions/AGENTS.md` / `scripts/AGENTS.md` / `docs/AGENTS.md` 等）
- 工作流要求："Read scoped `AGENTS.md` before subtree work"
- 根 AGENTS.md 拥有 hard policy + routing，skill 自己拥有 workflow
- ClawSweeper review policy 在 AGENTS.md 中明文规定

来源：`openclaw/AGENTS.md` L1-5 / L25-50。

**deadman 现状**：
- `.traecli/rules/` 下 14 个规则文件（integrity-framework / safety-protocol / input-guardrails / compliance-framework / notification-guardrails / risk-tier-framework / tone-framework / service-boundary-framework / accountability-framework / transparency-framework / multilingual-framework / retrieval-guardrails / legal-compliance-framework / special-populations-framework / conflict-resolution）
- `.traecli/agents/` 下按智能体分文件（death-aftercare / legal-advisor / financial-analyst / medical-guide / policy-researcher 等，每个智能体还有特化子文件如 `legal-advisor-cases.md` / `legal-advisor-statutes.md`）
- **但目前没有"工作树级 scoped 规则"**——所有规则都是平台级全局规则

**是否借鉴**：**是（轻量借鉴）**

**如何按 deadman 身后事定位改造**：
- 不需要照搬 OpenClaw 的"每 subtree 一个 AGENTS.md"（deadman 不是 monorepo）
- **借鉴"scoped rules"思想**：每个 skill 目录可有一个可选的 `SCOPE.md`，声明本 skill 特化的规则覆盖（如 `death-aftercare-guide/SCOPE.md` 可声明"本 skill 在涉及未成年子女监护时必须提示民政介入"——这是该 skill 特化条款，不属于全局规则）
- 这与 deadman 现有的"领域特化合规补充"（`death-aftercare-guide/SKILL.md` L24-30）思想一致，只是把它独立成文件
- **后续 Phase 决策点**：是否在 v4.6.x 把 `SKILL.md` 中的"领域特化合规补充"章节抽离到独立 `SCOPE.md`

---

### 6. SecretRef 凭证隔离 —— **借鉴（用于 LLM API key 与 Telegram bot token 隔离）**

**OpenClaw 是怎么做的**：
- 渠道/provider 凭证存 `~/.openclaw/credentials/`
- 模型 auth profile 存 `~/.openclaw/agents/<agentId>/agent/auth-profiles.json`
- SecretRef 失败时 isolate 到最小已知 owning surface，未知 owner 时 fail closed
- Gateway 启动仅当自有 ingress protection 无法建立、配置结构无效、或 owning surface 未知时才拒绝启动
- 否则启动 + 标记具体 capability/account/route 为 configured-unavailable + 发出 redacted diagnostic + 禁止隐式凭证 fallback
- reload 时仅对 unchanged ref+provider 保留 last-known-good，changed unresolved ref 让该 owner cold

来源：`openclaw/AGENTS.md` L332-339。

**deadman 现状**：
- LLM API key 通过环境变量读取（`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `ZHIPU_API_KEY`），见 `llm.py` L63-80
- 即将引入 Telegram bot token，也需要凭证管理
- **无显式 SecretRef 抽象**——所有凭证直接走 env var

**是否借鉴**：**是（用于多通道凭证隔离）**

**如何按 deadman 身后事定位改造**：
- **不引入 OpenClaw 式的复杂 SecretRef 系统**（deadman 是单用户场景）
- **借鉴"凭证 owner 显式声明 + 失败 isolate 到最小 surface"思想**：
  - Telegram bot token 失败不应阻塞 LLM 调用（仅标记 Telegram 通道不可用）
  - LLM provider A 的 key 失败应自动走 fallback chain（已实现），不应影响 Telegram 通道
- 实现位置：`config.py` 新增 `credential_status` 字典，每通道/provider 一个状态（`active` / `degraded` / `unavailable`）
- **不借鉴 last-known-good 保留**——身后事场景下凭证失效应明确告知用户"该通道当前不可用"，不偷偷用旧 key

---

## c. 不借鉴的设计（明确说明为什么）

### 1. Heartbeat 心跳机制 —— **不借鉴（违反 notification-guardrails.md L4 硬边界）**

**OpenClaw 做法**：
- 默认每 30 分钟在 main session 触发一次 agent turn（"periodic agent turns so the model can surface anything that needs attention without spamming you"）
- Anthropic OAuth/token auth 时默认改 1h
- prompt 默认：`Read HEARTBEAT.md if it exists... Follow it strictly... If nothing needs attention, reply HEARTBEAT_OK.`
- 可选 `target: "last"` 把心跳消息路由到最近联系人
- 可选 `includeReasoning: true` 单独发一条 Thinking 消息
- 可选 `isolatedSession: true` 每次心跳开新 session

来源：`openclaw/docs/gateway/heartbeat.md` L13-90。

**不借鉴原因**：
- **直接违反 `notification-guardrails.md` 第一章"默认禁止的推送场景"**：用户最后一次对话后 72 小时内不得推送；静默时段 22:00-08:00 不得推送；R3 即时人身安全风险后 14 天内不得推送；情绪强度"高"后 7 天内不得推送
- **违反第二章约束 1（显式 opt-in）**：心跳是平台主动行为，不是用户 opt-in 的具体任务
- **违反第二章约束 4（频率上限）**：30 分钟一次 = 单日 48 条，远超单日 1 条上限
- **违反核心原则**："默认静默，主动推送是特权而非默认"——心跳把主动推送当默认
- `notification-guardrails.md` L8-9 明确点名："通用 Agent 框架（如 Hermes、OpenClaw）默认主动推送是正常功能，deadman 必须反向约束"
- **结论**：Heartbeat 是身后事场景的禁忌，**绝对不借鉴**

---

### 2. 700+ Skills 数量模式 —— **不借鉴（身后事需要少而精，不需要广度）**

**OpenClaw 做法**：
- ClawHub 社区市场分发 700+ skill（任务描述提及，OpenClaw 文档强调"New skills should be published through ClawHub first, not added to core by default"，`VISION.md` L90）
- bundled skills 仅做 baseline UX
- Skill Workshop 允许 agent 起草 skill 提案，用户审批后入库（`docs/tools/skill-workshop.md`）
- 自学习（self-learning）从 session 历史挖掘 skill 想法

来源：`openclaw/VISION.md` L87-92 / L117，`openclaw/docs/tools/skills.md`，`openclaw/docs/tools/skill-workshop.md`。

**不借鉴原因**：
- **身后事场景的 skill 需要专业审核**（合规、心理安全、法律边界），不能走社区市场模式
- **deadman 的 2 个 skill 已经覆盖核心场景**：
  - `death-aftercare-guide`：9 个 stage（死亡证明 → 遗体处理 → 户籍注销 → 数字账户 → 财务资产 → 房产 → 遗产继承 → 社保 → 债务）+ checklists + special-cases，深度足够
  - `policy-research`：地域政策查询（CN/JP/US 三国 + california 子区域）
- **数量不是优势**：700+ skill 在身后事场景反而增加认知负担（用户处于脆弱状态，不需要"选 skill"，需要的是被引导）
- **Skill Workshop 自学习不适用**：身后事场景的 session 内容高度敏感（丧亲细节、家庭矛盾、财务状况），自动挖掘 skill 想法有隐私风险
- **结论**：deadman 保持 2 个深度 skill，未来按地域扩展（更多 region overview.md）而非按场景扩展

---

### 3. 桌面 Companion Apps（macOS / iOS / Android / Windows Hub） —— **不借鉴（场景不匹配）**

**OpenClaw 做法**：
- macOS App：菜单栏 Gateway 控制 + Voice Wake + push-to-talk + WebChat + SSH 远程控制
- iOS / Android Node：通过 WS 配对为 node，提供 Voice trigger forwarding + Canvas surface + Camera + Screen capture
- Windows Hub：原生桌面 App，setup + tray status + chat + node mode + local MCP mode
- Voice Wake + Talk Mode：macOS/iOS 唤醒词，Android 持续语音（ElevenLabs + system TTS fallback）
- Live Canvas：agent-driven visual workspace with A2UI

来源：`openclaw/README.md` L156-166 / L192-219，`openclaw/docs/concepts/architecture.md` L38-46。

**不借鉴原因**：
- **身后事用户不会装桌面 App**：用户处于丧亲高情绪负荷状态，首次接触 deadman 大概率是在消息平台（Telegram / 微信）上被亲友推荐，不会主动去装一个 App
- **Web UI + 消息平台已足够**：deadman 已有 `web/server.py`（ThreadingHTTPServer + SSE 流式 + 多端点）+ 即将到来的 Telegram 通道
- **Voice Wake 不适用**：身后事场景需要文字留痕（用户可能需要回看流程清单），语音不是首选
- **Canvas 不适用**：身后事是流程引导，不是可视化工作空间
- **iOS/Android Node 的 Camera/Screen capture 不适用**：身后事不需要看用户的摄像头或屏幕
- **结论**：deadman 维持 Web + 消息平台双入口，不投入桌面 App 研发

---

### 4. Node.js monorepo + pnpm workspace —— **不借鉴（技术栈不匹配）**

**OpenClaw 做法**：
- pnpm workspace（`pnpm-workspace.yaml`）管理 core / extensions / packages / ui 多包
- bundled plugins 在 `extensions/*` 下，开发时直接加载本地代码
- TypeScript strict + ESM + discriminated unions + zod schema
- 构建工具：tsdown（基于 tsdown.ai.config.ts）
- 包发布：npm shrinkwrap（不用 package-lock）
- CI：vitest + madge import cycles + oxlint + tsgo（不用 tsc --noEmit）

来源：`openclaw/AGENTS.md` L121-134 / L255-291，`openclaw/pnpm-workspace.yaml`，`openclaw/package.json`。

**不借鉴原因**：
- **deadman 是纯 Python**（pyproject.toml + stdlib + httpx/tenacity 可选）
- **不引入 Node.js 依赖**（任务约束第 2 条明文规定）
- **不搬 OpenClaw 代码**（任务约束第 3 条）
- **deadman 的依赖最小化原则**：FileMemoryStore 仅用 stdlib + pyyaml，REPL 仅用 asyncio + input()，避免 readline/curses
- **结论**：纯设计理念借鉴，零代码搬运

---

### 5. Live Canvas / A2UI —— **不借鉴（场景不匹配）**

**OpenClaw 做法**：
- agent 可驱动 visual workspace，通过 A2UI（Agent-to-UI）协议动态生成 HTML/CSS/JS
- Canvas host 由 Gateway HTTP server 在 `/__openclaw__/canvas/` 与 `/__openclaw__/a2ui/` 提供
- macOS / iOS / Linux 都有 Canvas 实现

来源：`openclaw/docs/concepts/architecture.md` L16-22，`openclaw/docs/platforms/mac/canvas.md`。

**不借鉴原因**：
- 身后事场景是流程引导 + 情绪支持，不需要可视化工作空间
- deadman 的 Web UI（`web/static/index.html`，多页签 SPA，原生 JS，无构建依赖）已足够
- A2UI 协议复杂度高，与 deadman 的"依赖最小化"原则冲突
- **结论**：完全不借鉴

---

## d. 建议的轻量改进（供后续 Phase 决策，不在本 Phase 实施）

### 改进 1：LLMClient 增加 risk_tier 路由（v4.7.0 候选）

**来源**：OpenClaw ClawRouter 的"按任务特征路由到不同模型"思想
**改造**：在 `llm.py` 新增 `route_model(risk_tier: str) -> str`，配置项加到 `config.py` 的 `settings`：
```
LLM_MODEL_R1=glm-flash         # 信息查询
LLM_MODEL_R2=glm-4.6           # 流程引导
LLM_MODEL_R3=claude-opus       # 即时人身安全
```
**成本**：低（< 50 行 Python，无新依赖）
**风险**：低（fallback chain 保留，路由失败降级到 LLM_PRIMARY_MODEL）
**决策点**：是否在 v4.7.0 实施

### 改进 2：notifications/consent 改用 SQLite（v4.7.0 候选）

**来源**：OpenClaw "SQLite-first 状态存储"原则
**改造**：把 `~/.deadman/notifications/consent.json` 改为 `~/.deadman/notifications.db`，两张表（consents / log），用 aiosqlite
**成本**：中（新增 aiosqlite 依赖，~200 行 Python）
**风险**：中（首次引入 SQLite 依赖，需要测试覆盖）
**决策点**：是否在 v4.7.0 引入 SQLite，还是继续用 JSON 文件 + 文件锁（更轻量）

### 改进 3：channel_registry.py 通道注册表（v4.6.0 候选，与 Gateway 同 Phase）

**来源**：OpenClaw Gateway 的"单进程多通道 + 通道注册表"思想
**改造**：新建 `.traecli/src/deadman/channels/registry.py`，每通道声明 `name / inbound_handler / outbound_handler / consent_required / guardrail_level`
**成本**：低（< 100 行 Python，无新依赖）
**风险**：低（与现有 web/a2a/mcp server 解耦，不影响已有功能）
**决策点**：与 Gateway + Telegram 通道同 Phase 实施

### 改进 4：每 skill 独立 SCOPE.md（v4.8.0 候选）

**来源**：OpenClaw scoped AGENTS.md 思想
**改造**：把 `SKILL.md` 中的"领域特化合规补充"章节抽离到独立 `SCOPE.md`，由 `rules_loader.py` 加载
**成本**：低（< 50 行 Python 改 rules_loader.py）
**风险**：低（纯文档结构重组，无功能变化）
**决策点**：是否值得做（当前 SKILL.md 内嵌也够用）

### 改进 5：用户级 skill 加载（v4.8.0 候选）

**来源**：OpenClaw 6 级 skill 加载优先级（deadman 简化为 2 级）
**改造**：SoulLoader 兼管 `~/.deadman/skills/<name>/SKILL.md` 加载，平台级 skill 不可被覆盖
**成本**：低（< 80 行 Python）
**风险**：中（需要 allowlist 机制防止智能体误用 user skill）
**决策点**：是否在 v4.8.0 实施

### 改进 6：credential_status 凭证状态字典（v4.6.0 候选）

**来源**：OpenClaw SecretRef "失败 isolate 到最小 surface" 思想
**改造**：`config.py` 新增 `credential_status: dict[str, str]`，每通道/provider 一个状态
**成本**：低（< 30 行 Python）
**风险**：低（纯状态记录，无副作用）
**决策点**：与多通道接入同 Phase 实施

### 不建议的改进（明确否决）

- ❌ 引入 Heartbeat / 主动唤醒（违反 notification-guardrails.md L4）
- ❌ 引入 ClawHub 式 skill 市场（场景不匹配）
- ❌ 引入 Live Canvas / A2UI（场景不匹配）
- ❌ 引入桌面 Companion App（场景不匹配）
- ❌ 把 FileMemoryStore 改为 SQLite（破坏 v4.5.1 稳定设计，markdown 文件可读性是优势）
- ❌ 引入 pnpm workspace 式 monorepo（技术栈不匹配）

---

## 附录：OpenClaw 文档体系可借鉴的组织方式

OpenClaw 的 `docs/` 按功能域分类（channels / cli / concepts / gateway / install / platforms / providers / tools / plugins / automation / security），每文件 YAML frontmatter 带 `summary` / `title` / `read_when` 三个字段，其中 `read_when` 是"何时该读这个文件"的触发条件列表——这是一种很高效的文档导航设计。

deadman 当前 `docs/` 只有 `DEPLOYMENT.md` 与 `QUICKSTART.md`，规则文件集中在 `.traecli/rules/`。后续可借鉴 OpenClaw 的 frontmatter 风格为 deadman 文档加 `read_when` 字段，但不在本 Phase 实施。

---

## 总结

| 维度 | OpenClaw | deadman 现状 | 借鉴决策 |
|------|----------|--------------|----------|
| 智能模型路由 | ClawRouter 单 key 多 provider | LLMClient 多 provider fallback 无路由 | **借鉴**（按 risk_tier） |
| Gateway 单进程多通道 | 单 WS Gateway + 23 通道 | 即将实现 Gateway + Telegram | **部分借鉴**（channel_registry） |
| SQLite-first 状态 | SQLite only，禁文件态 | FileMemoryStore + Graphiti/LightRAG | **部分借鉴**（仅 consent 层） |
| Skills 加载优先级 | 6 级优先级 + ClawHub 市场 | 2 个平台级 skill 无优先级 | **借鉴**（2 级简化版） |
| AGENTS.md scoped | 每 subtree 一份 | 14 个全局规则文件 | **轻量借鉴**（SCOPE.md） |
| SecretRef 凭证隔离 | 失败 isolate 到最小 surface | env var 直接读 | **借鉴**（credential_status） |
| Heartbeat 心跳 | 30m 唤醒 | 默认静默（notification-guardrails） | **不借鉴**（违反 L4） |
| 700+ Skills | ClawHub 市场 | 2 个深度 skill | **不借鉴**（场景不需要广度） |
| 桌面 Companion App | macOS/iOS/Android/Windows | Web UI + 消息平台 | **不借鉴**（场景不匹配） |
| Node.js monorepo | pnpm workspace | Python pyproject.toml | **不借鉴**（技术栈不匹配） |
| Live Canvas / A2UI | agent-driven visual workspace | 无 | **不借鉴**（场景不匹配） |

**可借鉴理念数**：6（智能模型路由 / Gateway 通道注册 / SQLite 用于 consent / Skills 优先级 / Scoped 规则 / 凭证隔离）
**不借鉴理念数**：5（Heartbeat / 700+ Skills / 桌面 App / Node.js monorepo / Live Canvas）
**轻量改进建议数**：6 项（v4.6.0 / v4.7.0 / v4.8.0 候选）

**核心结论**：OpenClaw 是一个工程化程度极高的通用 AI 助手框架，但其"通用场景 + 主动推送常态 + 桌面 App + 社区 skill 市场"的核心定位与 deadman 的"身后事垂直场景 + 默认静默 + Web/消息平台 + 平台审核 skill"在根本点上不兼容。可借鉴的主要是**架构层面的工程思想**（路由、注册表、状态隔离、加载优先级），而非产品层面的功能特性。所有借鉴项均不破坏 deadman 现有架构（4 层记忆 + 14 规则文件 + 2 平台 skill + LangGraph 编排），可在后续 Phase 增量落地。
