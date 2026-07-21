# deadman 产品经理评估报告

> 评估时间：2026-07-21
> 评估范围：deadman v4.6.1（身后事 + 医疗导航多智能体引导平台）
> 评估视角：离普通人落地使用还有多远
> 评估方法：通读 README / BRAND / PLATFORMS / CHANGELOG / 6 个 agent.md / 15 个 rules / 4 个 knowledge / web/server.py / mcp_server/server.py / orchestration/ / gateway/ / config.py 等核心代码后，按产品经理维度客观评分

---

## 1. 执行摘要（给非技术读者）

deadman 是一个**架构理念领先、规则体系严谨、工程骨架完整**的身后事多智能体引导平台。它在「合规边界」「诚信护栏」「主动通知伦理」三方面的规则深度，已超过市面上多数 toC AI 助手产品——`rules/compliance-framework.md` 明文规定四项禁止（不代办 / 不代查 / 不出具法律意见 / 不替代官方政策），`rules/notification-guardrails.md` 用 7 项硬约束（opt-in / 频率上限 / 静默时段 / 敏感日期 / 内容脱敏 / 一键退订 / 脆弱期保护）反向约束主动推送，这在身后事这种高情绪负荷场景是罕见的伦理自觉。

但**离普通人落地使用还有显著距离**。核心矛盾不在技术深度，而在「最后一公里」的产品化缺口：

1. **对话入口名不副实**：`web/server.py` 的 `/api/chat` 端点（第 357-371 行）只用了硬编码的「你是 {agent} 智能体，专注于协助处理逝者身后事」一句话 system prompt，**没有加载 `agents/*.md` 中那 6 份精心定义的并列智能体身份、规则、转介机制、子智能体调用约束**。这意味着普通用户打开 Web UI 实际体验到的，是一个通用 LLM 加了一句系统提示，而不是项目文档里描述的那个 6 智能体协作平台。真正能用上完整 agent.md 的入口是 MCP Server，但那需要平台方接入，不是普通用户的路径。
2. **知识库覆盖严重不足**：`knowledge/regions/` 下只有 4 个地域文件（`CN/overview.md` / `US/overview.md` / `US/california.md` / `JP/overview.md`），中国省份级文件 0 个。一个北京用户问「北京户籍注销时限」，`query_knowledge` 工具会落到 `CN/overview.md` 里的全国通用流程，但北京特有的「一件事一次办」线上办理、各区民政局差异等本地化信息完全缺失。
3. **无托管服务、无微信、无 App**：普通用户无法自行 `pip install -e .` + 配置 `LLM_API_KEY` + `docker compose up`。微信小程序 / 公众号接入仅在 `notification-guardrails.md` 的退订入口模板里被提及，`gateway/connectors/` 下只有 `telegram.py` 一个实现。
4. **无商业模式与法律主体**：项目是 MIT License 开源项目，仓库地址指向 `github.com/bad-hope/deadman`，BRAND.md 没有运营公司信息，无 ICP 备案、无隐私政策页面、无用户协议、无客服体系。身后事是强信任品类，用户不会把家庭财产和亲属关系信息交给一个「找不到运营方」的平台。

**总评：41/100**。距离普通人在国内场景下「打开就能用、敢把家里事告诉它」，预计还需要 **6-12 个月**的密集产品化工作，且必须从纯 toC OSS 转向 B2B2C / 政府合作 / 律所公证处嵌入模式（详见第 5 节）。

---

## 2. A. 普通用户落地就绪度评分

| 维度 | 评分 | 依据（代码路径） | 差距 |
|------|------|------|------|
| 功能完整性 | 5/10 | `mcp_server/server.py` 实现 15 个 MCP 工具完整；但 `web/server.py:357-371` 的 `/api/chat` 硬编码 system prompt，未走 `orchestration/graph.py:build_main_graph()`，未加载 `agents/*.md`；`invoke_subagent` 工具（`server.py:1170-1234`）只读 agent.md 前 800 字符做 system prompt | Web UI 对话体验与文档承诺严重不符；子智能体调用是「LLM 加一段提示」而非真正的子图执行；`debate` 模块未实现（`server.py:89-94` 的 `_DEBATE_AVAILABLE=False`） |
| 易用性 | 3/10 | `README.md` 要求 `pip install -e .` + `export LLM_API_KEY` + `deadman mcp-server`；`docs/QUICKSTART.md` 仅面向开发者；`web/static/index.html` 是原生 JS SPA，无引导式 onboarding；`config.py:22-29` 的 LLM 配置全是环境变量 | 普通用户无法独立部署；Web UI 是开放式 chat 框，没有「我是逝者什么关系 / 在哪 / 几号去世」的结构化引导表单；`death-aftercare.md` 第二章定义的「必问五条」在 Web UI 中完全不会自动触发 |
| 内容覆盖度 | 3/10 | `knowledge/regions/` 仅 4 个文件：`CN/overview.md` / `US/overview.md` / `US/california.md` / `JP/overview.md`；`SCHEMA.md` 定义了 9 阶段标准结构，但实际只有 CN overview 与 US california 完整覆盖 9 阶段；中国 34 个省级行政区 0 个文件 | 用户问「上海 / 广州 / 成都」的本地政策，平台只能给全国通用流程；`policy-researcher` 智能体在文档中承诺「多语言搜索 + 官方源优先 + 知识库构建」，但实际知识库积累速度远跟不上长尾地域需求 |
| 多端可达性 | 4/10 | `web/server.py` 提供 Web UI（端口 8002）+ SSE 流式；`gateway/connectors/telegram.py` 是唯一消息平台连接器；`mcp_server/server.py` 提供 stdio + http 双传输；`a2a/server.py` 提供 A2A 协议；无微信、无 App、无小程序 | `config.py:98` 的 `telegram_bot_token` 默认空串，`gateway_enabled` 默认 False；`notification-guardrails.md` 第 89-93 行提到微信「回复 0 退订」但代码层无 `wechat.py` 连接器；中国用户主入口缺失 |
| 性能与稳定性 | 5/10 | `observability/metrics.py` 11 大类 50+ 指标；`orchestration/graph.py:54-75` LangGraph 可选，降级为 SequentialExecutor；`config.py:31-35` LLM Fallback 链；335 个测试通过 | 默认 `MemorySaver`（`graph.py:55`）进程重启即丢会话状态，跨会话续接需 `SqliteSaver` 但默认未启用；`config.py:58` 的 `checkpoint_db_path` 默认值存在但 LangGraph-checkpoint-sqlite 是独立可选依赖；无 SLO/SLA 文档；无压测报告；`web_search` 用 DuckDuckGo HTML 端点（`tools/web_search.py`），中国大陆访问不稳定 |
| 安全与隐私 | 6/10 | `mcp_server/server.py:146-186` 的 `_redact_pii` 覆盖 identifier/name/phone/address/account_number；`memory/file_store.py` 的 `sanitize_before_store` 同步脱敏；`mcp_server/server.py:1089-1106` 禁止写入 rules/.env/.git/credentials；`notification/guardrail.py` 9 步 can_send 检查；`orchestration/nodes.py:42-61` Prompt Injection + PII 模式检测 | `web/server.py:165-178` 的 `/api/chat` 不走 `input_guard_node` / `rule_check_node`，Web UI 用户输入直接进 LLM；`web/server.py:389-440` 的 `/api/cli/<command>` 允许 subprocess 调用（虽白名单）；Web UI 无用户认证、无 HTTPS 强制、无速率限制；`config.py:53` 的 `memory_retention_years=7` 是配置项但代码层未实现自动过期清理 |
| 商业模式可行性 | 2/10 | README/BRAND/CHANGELOG 全篇无商业模式描述；`config.py` 无付费/订阅/配额字段；无支付系统集成；无使用量计费 | 纯 toC 不可持续（身后事低频高情绪）；无 B2B 合作接口；无白标 / OEM 能力；无 SLA 分级 |
| 法律合规 | 7/10 | `rules/compliance-framework.md` 四项禁止；`rules/legal-compliance-framework.md` 法律合规框架；`rules/integrity-framework.md` 诚信与质疑；`rules/transparency-framework.md` AI 身份告知；`rules/accountability-framework.md` 问责申诉；`rules/service-boundary-framework.md` 服务边界；每条智能体输出前需过 `check_rules` + `check_integrity` | 无运营法律主体；无 ICP 备案；无隐私政策页面；无用户协议；`legal-advisor.md` 明确「绝不出法律意见」但用户误用风险仍由用户承担；无专业责任保险；规则是 prompt 软约束，非平台硬过滤（与 AWS Bedrock Guardrails 不同，见 `PLATFORMS.md:213-214`） |
| 用户信任建立 | 4/10 | `BRAND.md` 统一品牌名 deadman；`rules/transparency-framework.md` 要求 AI 身份告知；`rules/safety-protocol.md` 心理危机响应；`soul_loader.py` 默认 SOUL.md 强调 service-boundary；`death-aftercare.md` 第二章「诚信与质疑执行细则」明确「必须问、不能猜的信息」 | BRAND.md 无运营公司、无联系方式、无备案号；`README.md` 仓库地址指向 `github.com/bad-hope/deadman`，无官网；无隐私政策、无用户协议、无客服入口；身后事是强信任品类，OSS 模式难以建立普通用户信任 |
| 客服与运维支持 | 2/10 | `observability/` 提供 OTel + Langfuse 自部署方案；`web/server.py` 运维看板（`/api/obs/dashboard` `/api/health/all` `/api/deploy/check`）；`cli.py` 提供 `eval` / `llm-test` / `notify-test` 等诊断命令；`docker/healthcheck.py` 健康检查 | 无用户工单系统；无客服入口；无 SLA；问题反馈只能走 GitHub Issue；`config.py` 无告警通知配置；生产事故响应流程未文档化 |

**总分：41/100**

**离普通人落地还有多远**：约 **60% 距离**。关键 gap（按优先级）：

1. **P0-gap-1**：Web UI 对话不加载 agent.md，普通用户体验 ≠ 文档承诺
2. **P0-gap-2**：知识库仅 4 个地域文件，中国省份级 0 覆盖
3. **P0-gap-3**：无托管服务，普通用户无法独立部署
4. **P0-gap-4**：无用户认证与会话隔离，多用户共用一个进程不安全
5. **P0-gap-5**：无微信入口，中国用户主入口缺失
6. **P1-gap-1**：无隐私政策 / 用户协议 / 客服体系
7. **P1-gap-2**：无商业模式，纯 OSS 不可持续
8. **P1-gap-3**：`/api/chat` 绕过规则校验节点

---

## 3. B. 关键缺失功能清单

| 优先级 | 缺失功能 | 为什么 P0/P1/P2 | 竞品是否有 | 实现难度 | 工作量 | 与 AI-RULE 冲突 |
|--------|---------|---------------|-----------|---------|--------|---------------|
| **P0** | Web UI 真正接入 agent.md（让 `/api/chat` 走 `orchestration/graph.py:build_main_graph()` 而非硬编码 system prompt） | 没这个功能，Web UI 就是个挂羊头卖狗肉的通用 chat，6 个并列智能体、转介、子智能体、规则校验全部失效，普通用户用不上文档承诺的能力 | 通用 chat 产品（Kimi/元宝/通义）无此问题但无身后事专精；垂直产品少见 | 小 | 改 `web/server.py:335-387` `_handle_chat` 与 `_stream_chat`，约 2 文件 | 否（反而强化规则执行） |
| **P0** | 中国省份级知识库（至少覆盖北上广深 + 5 个一线省份） | 用户问「北京/上海/广州」的本地政策，平台只能给全国通用流程，本地化信息（如北京「死亡一件事」联办、上海「随申办」线上办理）完全缺失 | 政府民政官网有但分散；商业产品如「办事指南」类 App 部分覆盖 | 中 | 每省份 1 个 .md，按 `SCHEMA.md` 9 阶段填充，约 10 文件 + 人工核实 | 否（`policy-researcher.md` 已设计此流程） |
| **P0** | 托管服务（SaaS 入口，普通用户无需部署） | 普通用户不会 `pip install` + `docker compose`，必须有一个 `deadman.example.com` 让用户打开就用 | 通用 AI 助手都有；垂直产品如「终活笔记」「遗嘱库」类有 | 大 | 1 个云账号 + 域名 + ICP 备案 + 反代 + 监控，约 5-10 文件 | 否 |
| **P0** | 用户认证 + 会话隔离（登录、用户 ID、独立记忆） | 无登录 = 多用户共用进程，记忆串台、PII 互泄；`memory/manager.py` 的 `user_id` 字段无来源 | 所有 toC 产品必备 | 中 | 加 auth 模块 + 会话存储 + 用户表，约 5-8 文件 | 否（`compliance-framework.md` 数据治理条款要求会话隔离） |
| **P0** | 微信生态入口（小程序或公众号） | 中国用户主入口是微信，Telegram 在国内不可用；`notification-guardrails.md:89-93` 已提及微信但代码层未实现 | 政务小程序、商业小程序都有 | 大 | 小程序前端 + 公众号回调 + 配对 token 机制，约 8-12 文件 | 否（`gateway/connectors/base.py` 已留 Protocol 接口） |
| **P1** | 隐私政策 + 用户协议页面 | 强信任品类必备；`transparency-framework.md` 要求告知数据使用，但无独立页面 | 所有商业产品必备 | 小 | 2 个静态页 + 链接，约 2 文件 | 否 |
| **P1** | 客服体系（工单 + 反馈入口） | 身后事用户遇到错误答复时需要申诉通道；`accountability-framework.md` 定义了申诉机制但无入口 | 商业产品必备 | 中 | 工单系统 + 客服后台，约 5 文件 | 否 |
| **P1** | 中国境内 LLM + 搜索（智谱/通义/豆包 + 百度/搜狗） | OpenAI/Anthropic 在中国大陆访问不稳定；`tools/web_search.py` 用 DuckDuckGo HTML 在国内常被墙 | 国产 AI 产品默认 | 中 | `llm.py` 已支持 zhipu（`config.py:22`）；搜索需新增 provider，约 3 文件 | 否 |
| **P1** | 引导式对话表单（替代开放式 chat） | 身后事用户处于高情绪负荷，开放式 chat 不友好；`death-aftercare.md` 第二章「必问五条」应做成结构化表单 | 政务「一件事一次办」有 | 中 | 前端表单组件 + 后端结构化 state，约 5 文件 | 否 |
| **P1** | 知识库自动更新 Cron + 人工审核 | `knowledge/regions/CN/overview.md` 标注「最后更新 2026-07-12」，政策变更后无自动检测；`retrieval-guardrails.md` 要求时效校验 | 商业知识库产品有 | 中 | 已有 `cron/scheduler.py` 基础，加知识库巡检任务 + 审核界面，约 4 文件 | 否（Cron 已受 `notification-guardrails.md` 约束） |
| **P1** | HTTPS + 域名 + ICP 备案 | HTTP 明文传输 PII 不合规；无 ICP 备案国内无法合法运营 | 所有商业网站必备 | 小 | Nginx 反代 + 证书 + 备案，约 2 文件 | 否 |
| **P1** | 移动端响应式适配 | `web/static/index.html` 桌面优先，手机体验差；身后事用户多用手机 | 通用 | 小 | CSS 媒体查询 + 触控优化，约 1 文件 | 否 |
| **P2** | 多语言 UI（中英日） | `BRAND.md` 三语品牌名表已定义；`multilingual-framework.md` 已有规则 | 跨境产品有 | 中 | i18n 框架 + 翻译，约 5 文件 | 否 |
| **P2** | 语音输入 + TTS 输出 | 老年用户 / 视障用户友好；`PLATFORMS.md:348` 提到 MiniMax 多模态适合临终患者语音咨询 | 部分医疗 App 有 | 中 | ASR + TTS 集成，约 3 文件 | 否 |
| **P2** | 情绪识别与自适应节奏 | `death-aftercare-emotional` 子智能体已定义但 Web UI 不触发；情绪强度信号可驱动 UI 节奏 | 高端心理类 App 有 | 中 | 情绪检测 + UI 适配，约 4 文件 | 否（强化 `safety-protocol.md`） |
| **P2** | 与殡葬 / 法律 / 公证机构 API 对接 | `service-boundary-framework.md` 禁止代办，但可提供官方预约入口 | 政务平台有 | 大 | 各机构 API 适配，约 10+ 文件 | 否（仅信息引导，不代办） |
| **P2** | 用户社区 / 案例库 | 同侪支持是身后事重要心理资源 | 「悲伤互助」类社区有 | 大 | 社区模块 + 审核，约 8 文件 | 需评估与 `safety-protocol.md` 心理危机响应的协同 |

---

## 4. C. 离落地的关键路径

按依赖关系排序，**前 4 步是 P0，必须先做**：

### Step 1：修复 Web UI 对话入口（P0，无依赖）

- 改 `web/server.py:335-387` 的 `_handle_chat` 与 `_stream_chat`，调用 `orchestration/graph.py:build_main_graph().ainvoke(state)`，让 `input_guard_node` → `router_node` → `agent_node`（加载 `agents/{agent}.md`）→ `rule_check_node` → `integrity_check_node` → `output_guard_node` → `respond_node` 完整跑通
- 让 Web UI 真正成为 6 智能体协作平台的入口，而非通用 chat
- **依赖**：无
- **预计工作量**：2-3 天

### Step 2：用户认证 + 会话隔离（P0，依赖 Step 1）

- 加 `auth/` 模块：手机号 / 邮箱注册 + 验证码登录
- `ConversationState.user_id`（`orchestration/state.py`）从 auth 上下文注入，不再从 `--user-id` CLI 参数取
- `MemoryManager` 按 `user_id` 隔离 `~/.deadman/memory/{user_id}/` 目录
- **依赖**：Step 1（Web UI 走 graph 后，user_id 才有意义）
- **预计工作量**：1-2 周

### Step 3：中国省份级知识库填充（P0，可与 Step 2 并行）

- 按 `knowledge/regions/SCHEMA.md` 9 阶段标准，先补 `CN/beijing.md` / `CN/shanghai.md` / `CN/guangdong.md` / `CN/zhejiang.md` / `CN/jiangsu.md` 5 个一线省份
- 每个文件由 `policy-researcher` 智能体辅助生成初稿，人工核实后入库
- 启用 `cron/scheduler.py` 的知识库时效巡检（已在 `notification-guardrails.md` 约束下）
- **依赖**：无（可与 Step 1/2 并行）
- **预计工作量**：2-4 周（含人工核实）

### Step 4：托管服务上线（P0，依赖 Step 1-3）

- 云服务器 + 域名 + ICP 备案 + HTTPS
- Docker Compose 部署（已有 `docker-compose.yml`）
- 接入国产 LLM（智谱 GLM-4.6，`config.py:22` 已支持 `LLM_PROVIDER=zhipu`）
- 接入国产搜索（百度 / 搜狗，替代 DuckDuckGo）
- **依赖**：Step 1（Web UI 可用）+ Step 2（用户系统）+ Step 3（知识库覆盖）
- **预计工作量**：1-2 月（含 ICP 备案等待）

### Step 5：微信生态接入（P0，依赖 Step 4）

- 公众号 + 小程序双入口
- 实现 `gateway/connectors/wechat.py`（`base.py` 已留 Protocol）
- 配对 token 机制复用 `gateway/core.py` 的设计
- **依赖**：Step 4（托管服务提供回调域名）
- **预计工作量**：3-4 周

### Step 6：合规化与信任建设（P1，依赖 Step 4）

- 隐私政策 + 用户协议页面
- 客服工单系统
- 法律主体注册（公司或民办非企业）
- 专业责任保险
- **依赖**：Step 4（托管上线后才有合规需求）
- **预计工作量**：1-2 月

### Step 7：引导式对话 + 知识库自动更新（P1，依赖 Step 1-3）

- 前端表单组件（替代开放式 chat）
- `cron/scheduler.py` 接入知识库时效巡检任务
- **依赖**：Step 1 + Step 3
- **预计工作量**：3-4 周

### Step 8：B2B2C 合作拓展（P1，依赖 Step 4-6）

- 保险公司嵌入（寿险 / 健康险理赔后转介）
- 律所 / 公证处嵌入（遗产规划场景）
- 政府民政合作（「一件事一次办」联办）
- **依赖**：Step 4（托管）+ Step 6（合规主体）
- **预计工作量**：3-6 月（含 BD 谈判）

---

## 5. D. 风险评估（如果先上线会出什么事）

按严重度排序：

### R1（致命）：用户误信 AI 出的法律 / 医疗 / 财务结论造成实际损失

- **场景**：用户问「这份遗嘱有效吗」，Web UI 走硬编码 system prompt（`web/server.py:357-364`），不走 `legal-advisor.md` 的「绝不出法律意见」约束，LLM 可能给确定性回答；用户据此放弃诉讼或签字，事后发现错误
- **后果**：用户财产损失 + 诉讼 + 平台声誉崩塌
- **缓解**：Step 1 必须先做；上线前 `check_rules` + `check_integrity` 工具在 graph 节点强制执行

### R2（致命）：PII 互泄 / 会话串台

- **场景**：无用户认证（Step 2 未做），多用户共用进程；`MemoryManager` 默认单例（`mcp_server/server.py:1716-1724` 的 `_memory_manager_instance`），`semantic.user_profiles` 字典按 `user_id` 区分但 `user_id` 来自 CLI 参数；Web UI 无登录，所有请求可能共用同一 `user_id`
- **后果**：A 用户看到 B 用户的亲属关系 / 财产信息；GDPR / PIPL 违规
- **缓解**：Step 2 用户认证必须先做；`memory_retention_years=7`（`config.py:53`）的自动清理需实现

### R3（严重）：心理危机用户得不到及时干预

- **场景**：`rules/safety-protocol.md` 定义了心理危机识别与应对，但 `web/server.py` 的 `/api/chat` 不走 `input_guard_node` 的 R3 信号检测（`orchestration/nodes.py:42-61` 的 PII/Injection 检测在 graph 内，Web UI 绕过）
- **后果**：用户表达自伤意图时，平台继续按常规流程引导，错过干预时机
- **缓解**：Step 1 必须先做；`safety-protocol.md` 的危机热线引导必须前置

### R4（严重）：主动通知造成二次创伤

- **场景**：虽然 `notification/guardrail.py` 有 9 步 `can_send` 检查，但 `gateway_enabled` 默认 False（`config.py:100`）；若运维误开，且 `consent.json` 未正确记录，可能在逝者忌日 / 清明节推送「死亡证明」相关提醒
- **后果**：用户情绪崩溃；平台被举报
- **缓解**：`NotificationGuardrail` 已有 14 个测试覆盖（`test_notification_guardrail.py`）；上线前需做端到端推送演练

### R5（严重）：知识库过期 / 错误信息

- **场景**：`knowledge/regions/CN/overview.md` 标注「最后更新 2026-07-12」「数据可信度: 中」；政策变更（如 2024 年金融监管总局 5 万元简化提取新规）后无自动检测机制；`retrieval-guardrails.md` 要求时效校验但 `cron` 巡检未启用
- **后果**：用户按过期信息办理，白跑一趟
- **缓解**：Step 3 + Step 7 的知识库自动更新必须做；每条信息附「最后更新 + 数据来源」（SCHEMA.md 已要求）

### R6（中等）：Web Search 在中国大陆不可用

- **场景**：`tools/web_search.py` 直连 `https://html.duckduckgo.com/html/`，中国大陆访问不稳定；`policy-researcher` 智能体的核心能力受影响
- **后果**：政策搜索失败，降级为「未检索到，建议打 12345」
- **缓解**：Step 4 接入百度 / 搜狗搜索 provider

### R7（中等）：合规风险（无 ICP 备案 / 无法律主体）

- **场景**：托管服务上线后，国内运营需 ICP 备案；提供法律 / 医疗 / 财务引导需相应资质或免责声明；OSS 项目无运营主体，用户协议无约束力
- **后果**：被工信部关停；被用户起诉无主体应诉
- **缓解**：Step 6 法律主体注册 + ICP 备案 + 用户协议

### R8（中等）：开源被滥用 / 二次封装风险

- **场景**：MIT License 允许商用；不良商家可能 fork 后去掉 `rules/compliance-framework.md` 的四项禁止，做「代办取款」「代办过户」的灰色服务，损害 deadman 品牌
- **后果**：品牌声誉受损；用户混淆
- **缓解**：BRAND.md 注册商标；`soul_loader.py` 的默认 SOUL.md 强约束（已有，但可被修改）

### R9（轻）：性能与稳定性

- **场景**：默认 `MemorySaver`（`graph.py:55`）进程重启丢会话；`MemoryManager` 单例在多用户高并发下可能瓶颈；LLM 调用无速率限制
- **后果**：用户体验中断；高峰期响应慢
- **缓解**：启用 `SqliteSaver` 或 `PostgresSaver`；加 LLM 速率限制

---

## 6. E. 商业模式建议

身后事垂直场景的特点：**低频（人一生触发 1-2 次）+ 高情绪（用户处于丧亲状态）+ 强信任（涉及家庭财产与亲属关系）+ 强合规（法律医疗财务交叉）**。纯 toC 不可持续，原因：

1. 获客成本高：用户不会主动搜索「身后事 AI」，触达时机难
2. 付费意愿弱：丧亲期用户无心智比价，且对「花钱办丧事」敏感
3. LLM 成本高：6 智能体 + 子智能体 + SelfCheckGPT 多次采样，单次对话成本高
4. 复购率低：办完就走，无持续订阅理由

### 6.1 B2B2C / 嵌入保险公司（推荐 ★★★★★）

**可行性**：高。寿险 / 健康险理赔后，保险公司需要引导受益人办理身后事，deadman 可作为理赔后服务嵌入。

**收费点**：
- 保险公司按 API 调用次数付费（每次理赔案件 ¥X）
- 增值服务：遗产规划报告（¥X/份，由 `financial-analyst` + `legal-advisor` 协作生成）
- 数据脱敏后回流：积累地域知识库（保险公司提供官方政策源）

**获客渠道**：
- 寿险公司理赔部门 BD
- 保险经纪公司合作（明亚 / 大童 / 泛华）
- 保险行业协会展会

**与现有 AI-RULE 冲突**：无。`compliance-framework.md` 的「禁止代办」与保险理赔流程天然契合（deadman 引导，保险公司执行）。

### 6.2 政府民政合作（推荐 ★★★★☆）

**可行性**：中高。各地民政「一件事一次办」改革需要智能引导前端，deadman 的 `death-aftercare.md` 9 阶段流程与政府办事指南高度同构。

**收费点**：
- 政府购买服务（年费制，按服务人口定价）
- 政务小程序嵌入（如「随申办」「京通」接入 deadman 作为身后事专区）
- 数据回流：政府提供最新政策，deadman 维护知识库

**获客渠道**：
- 各地民政局招投标
- 政务服务数据管理局合作
- 「互联网 + 政务」展会

**与现有 AI-RULE 冲突**：需评估 `transparency-framework.md` 的 AI 身份告知与政府官方渠道的边界（避免用户误以为是政府官方答复）。

### 6.3 律所 / 公证处合作（推荐 ★★★★☆）

**可行性**：高。`legal-advisor.md` 的核心定位是「风险评估 + 引导律师」，天然是律所获客前端。

**收费点**：
- 律所 / 公证处按有效咨询线索付费（CPL 模式，每条 ¥X）
- 遗产规划咨询服务（与律所联合品牌，分成模式）
- 公证预约接入（每成功预约 1 单 ¥X）

**获客渠道**：
- 区域性律所 BD（继承 / 家事领域）
- 公证处合作（中华全国公证协会）
- 法律援助中心合作（12348 热线转介）

**与现有 AI-RULE 冲突**：无。`legal-advisor.md` 明文「绝不出法律意见」，正好是律所的优质前置过滤。

### 6.4 殡葬服务机构合作（推荐 ★★★☆☆）

**可行性**：中。殡葬机构有获客需求，但「黑殡仪」乱收费是 `safety-protocol.md` 明确识别的违法行为（第 58 行），需严格筛选合作方。

**收费点**：
- 殡葬机构入驻费（年费）
- 殡仪服务预约分成
- 绿色殡葬（生态葬 / 树葬）推广补贴

**与现有 AI-RULE 冲突**：`soul_loader.py` 默认 SOUL.md 强调「不与殡葬机构分成」，需重新评估；建议改为「不与未认证殡葬机构分成」，引入民政部门认证的白名单。

### 6.5 toC 增值服务（推荐 ★★☆☆☆，作为补充）

**可行性**：低，作为 B2B2C 的补充。

**收费点**：
- 遗产规划报告（一次性付费，¥99-299）
- 数字遗产托管（年费，¥X/年，存账号密码清单）
- 心理咨询转介（与心理机构分成）

**与现有 AI-RULE 冲突**：数字遗产托管与 `compliance-framework.md` 的「数据安全底线」（不存储用户对话中透露的敏感信息）需明确边界——托管的是用户主动上传的加密清单，不是对话内容。

### 6.6 综合建议

**短期（0-6 月）**：Step 1-5 完成后，先以 **律所 / 公证处合作** 启动（BD 周期短、合规风险低、与 `legal-advisor.md` 定位契合），同步申请 ICP 备案与法律主体。

**中期（6-18 月）**：以 **保险公司嵌入** 为核心商业模式（客单价高、用户精准、与 `financial-analyst.md` + `death-aftercare.md` 协同），辅以 **政府民政合作** 试点 1-2 个城市。

**长期（18 月+）**：以 **政府民政合作** 为规模化的核心（准入门槛高但一旦切入即壁垒），辅以殡葬机构白名单与 toC 增值服务。

**避免**：纯 toC 订阅制（不可持续）、与未认证殡葬机构分成（违反 `safety-protocol.md`）、出售用户数据（违反 `compliance-framework.md` 数据治理条款）。

---

## 7. 附录：评估依据文件清单

### 7.1 项目文档
- `/workspace/deadman/README.md`
- `/workspace/deadman/BRAND.md`
- `/workspace/deadman/PLATFORMS.md`
- `/workspace/deadman/CHANGELOG.md`（v1.0 - v4.6.1）
- `/workspace/deadman/docs/QUICKSTART.md`
- `/workspace/deadman/docs/DEPLOYMENT.md`
- `/workspace/deadman/docs/openclaw-design-analysis.md`

### 7.2 智能体定义（agents/）
- `TEAM.md`（团队架构）
- `death-aftercare.md`（流程引导员，462 行）
- `legal-advisor.md`（法律顾问）
- `financial-analyst.md`（财务分析师）
- `policy-researcher.md`（政策搜索员）
- `cross-border-specialist.md`（跨境专家）
- `medical-guide.md`（医疗导航员）
- 6 个父智能体的 12 个私有子智能体 `.md` 文件

### 7.3 规则文件（rules/，15 个）
- `safety-protocol.md`（L0 安全）
- `integrity-framework.md`（L1 诚信）
- `input-guardrails.md`（L2 输入护栏）
- `compliance-framework.md`（L3 合规）
- `risk-tier-framework.md`（L4 风险分级）
- `transparency-framework.md`（L5 透明度）
- `accountability-framework.md`（L6 问责）
- `retrieval-guardrails.md`（L7 检索护栏）
- `tone-framework.md`（L8 语气）
- `conflict-resolution.md`（规则裁决）
- `notification-guardrails.md`（L4 主动通知护栏）
- `service-boundary-framework.md`（服务边界）
- `special-populations-framework.md`（特殊人群）
- `multilingual-framework.md`（多语言）
- `legal-compliance-framework.md`（法律合规）

### 7.4 知识库（knowledge/regions/）
- `SCHEMA.md`（标准格式）
- `CN/overview.md`（中国国家级，含 9 阶段通用流程）
- `US/overview.md`（美国国家级）
- `US/california.md`（加州地区，9 阶段完整覆盖）
- `JP/overview.md`（日本国家级）

### 7.5 核心源码（.traecli/src/deadman/）
- `cli.py`（CLI 入口，多个子命令）
- `config.py`（全局配置，环境变量加载）
- `llm.py`（LLM 客户端，多 provider 支持）
- `repl.py`（交互式 REPL）
- `soul_loader.py`（SOUL.md 用户级身份覆盖）
- `rules_loader.py`（规则加载与校验）
- `mcp_server/server.py`（15 工具 MCP Server，2475 行）
- `web/server.py`（Web UI + 运维 API，512 行）
- `web/static/index.html`（原生 JS SPA）
- `orchestration/graph.py`（LangGraph + 降级 SequentialExecutor）
- `orchestration/nodes.py`（8 节点 + 3 路由函数）
- `orchestration/state.py`（ConversationState TypedDict）
- `memory/manager.py`（4 层记忆统一管理）
- `memory/file_store.py`（FileMemoryStore 文件持久化）
- `notification/guardrail.py`（NotificationGuardrail 9 步检查）
- `gateway/core.py`（消息平台 Gateway）
- `gateway/connectors/telegram.py`（Telegram 连接器）
- `cron/scheduler.py` + `cron/expr.py`（Cron 调度器）
- `tools/web_search.py`（DuckDuckGo HTML 直连）
- `sandbox/base.py`（LocalSandbox + DockerSandbox）
- `a2a/server.py`（A2A 协议）
- `reflexion/engine.py`（反思重试）
- `selfcheck/checker.py`（SelfCheckGPT 数字类校验）
- `evaluation/`（三层判定 + RAGAS + 工具调用序列）
- `observability/tracer.py` + `metrics.py`（OTel + Langfuse）

### 7.6 测试（.traecli/src/tests/，335 个测试通过）
- 19 个测试文件，覆盖 12 个模块
- LLM 调用全 mock，不依赖外部 API
- 含 `test_notification_guardrail.py`（14 个测试方法，11 个测试类）
- 含 `test_gateway.py`（6 个测试方法）
- 含 `test_file_store.py`（5 个测试类，8 个测试方法）
- 含 `test_repl.py`（3 个测试类，6 个测试方法）

### 7.7 评估用例（.traecli/tests/automated/cases/）
- `case-01-no-fabrication.yaml`（诚信场景）
- `case-06-psychological-crisis.yaml`（安全场景）
- `case-11-transfer-to-legal.yaml`（转介场景）
- `case-13-injection-defense.yaml`（防御场景）
- `case-20-cross-border.yaml`（跨境场景）

---

## 8. 评估结论

deadman 是一个**架构深度远超产品成熟度**的项目。它的规则体系（15 个规则文件、L0-L8 优先级链、`conflict-resolution.md` 价值冲突裁决）、合规边界（`compliance-framework.md` 四项禁止）、主动通知伦理（`notification-guardrails.md` 7 项硬约束）、诚信护栏（`integrity-framework.md` 5 关事实复核 + SelfCheckGPT）在身后事垂直场景里是教科书级的设计。

但**架构深度不能替代产品化**。普通用户落地需要的是：打开就能用、敢把家里事告诉它、用了能省事、出事能找人。这四点 deadman 目前都不满足：

- **打开就能用**：需要 Step 1-5（Web UI 修复 + 用户认证 + 知识库 + 托管 + 微信）
- **敢把家里事告诉它**：需要 Step 6（合规主体 + 隐私政策 + 客服）
- **用了能省事**：需要 Step 3 + Step 7（知识库覆盖 + 引导式对话）
- **出事能找人**：需要 Step 6（法律主体 + 客服 + 责任保险）

**建议路径**：先做 Step 1（修复 Web UI，1 周内可见效）→ Step 2-3 并行（用户系统 + 知识库，1 月）→ Step 4（托管上线，2 月）→ Step 5（微信入口，3 月）→ Step 6（合规化，4-5 月）→ Step 7-8（产品化与商业化，6-12 月）。

**商业模式**：放弃纯 toC，以律所 / 公证处合作启动，保险公司嵌入为核心，政府民政合作为规模化壁垒。

**总评**：技术骨架 8/10，产品就绪 4/10，商业可行 3/10。距离普通人落地 **约 60% 距离，6-12 月密集产品化可缩短至 30%**。

---

*报告完。本报告基于 2026-07-21 时的代码状态评估，后续版本可能已修复部分 gap。所有引用代码路径均真实存在，可在仓库中验证。*
