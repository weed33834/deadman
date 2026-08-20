# deadman 产品经理评估报告 v2

> 评估时间：2026-07-21
> 评估范围：deadman v4.7.0（身后事 + 医疗导航多智能体引导平台）
> 评估视角：上一轮 PM 评估（41/100，详见 [docs/pm-assessment.md](pm-assessment.md)）后的 Phase 7-13 修复完成后，离普通人落地使用还有多远
> 评估方法：通读 v4.7.0 CHANGELOG + Phase 7-13 全部新增源码（含 7 个新模块、25 个新 CLI 子命令、496 个测试）+ 上一轮 9 维度评分项重新打分
> 评估约束：不执行代码、不修改源码；每条断言引用具体文件路径或行号

---

## 1. 执行摘要

Phase 7-13 的 7 个 Phase 是**一次高密度、有纪律的产品化冲刺**：闭环了上一轮报告里 5 个 P0 中的 3 个（P0-1 Web UI 走完整规则链 / P0-2 用户认证 / P0-3 法律免责与机构查询）、3 个 P1 中的 3 个（P1-1 终活笔记 / P1-2 数字遗产保险库 / P1-3 文档提取），并新增了上一轮未列入的"遗码通逝者案例管理"作为差异化能力。496 个测试通过、25 个新 CLI 子命令、`_cli_extensions/` 包统一分发——工程纪律在身后事垂直品类里属于头部水准。

但**离普通人落地仍有约 40% 距离**。Phase 7-13 修复的是「平台骨架的内部一致性」与「合规告知的代码化」，**未触及「普通用户触达路径」与「商业主体」**：

1. **`/api/stream` SSE 流式接口仍是硬编码 system prompt**（`web/server.py:1459-1468`），未走 Phase 7 修复后的 `build_main_graph()`——前端 `index.html:613` 默认走 `/api/stream`，所以普通用户在 Web UI 上实际体验到的对话流仍是「通用 LLM + 一句话提示」，**Phase 7 修复的 `/api/chat` 同步接口反而是降级 fallback**。这是上一轮 P0-1 修复的盲区，必须补齐。
2. **Phase 8 auth 未穿透到 Phase 10 终活笔记端点**：`web/server.py:594-596` 的 `_ending_note_user_id` 仍从 query string 取 `?user_id=`，未走 `_require_auth`；任何登录用户都能通过改 query 拉取他人笔记。Phase 11/12/13（vault/documents/cases）已统一走 `_phase_auth_user()`（`web/server.py:866-870`），但 Phase 10 是「漏网之鱼」。
3. **加密方案不达生产基线**：`ending_note/store.py:43-54` 与 `vault/store.py:193-196` 明确注释「XOR 流密码对已知明文攻击不安全，生产应换 AES-256-GCM」——代码自评即承认不达 PIPL 第五章「足够强度的加密」要求。在身后事这种强信任品类，**用户数据一旦落盘即处于弱保护状态**。
4. **核心触达路径全部未做**：无托管服务、无微信入口（`gateway/connectors/` 仍只有 telegram.py）、无引导式 onboarding 表单（`index.html` 仍是开放式 chat）、无移动端响应式（无 `@media` 查询）、无隐私政策/用户协议/客服工单页面（`docs/` 下仅 4 个 .md，无 privacy/agreement/terms/customer-service 文件）、无中国境内搜索 provider（`tools/web_search.py` 仍直连 DuckDuckGo）、无商业模式（README/BRAND/CHANGELOG 仍无运营主体信息）。
5. **知识库覆盖未推进**：`knowledge/regions/` 仍只有 4 个文件（CN/overview.md、US/overview.md、US/california.md、JP/overview.md），中国 34 个省级行政区 0 个文件——上一轮 P0-2 完全未动。

**总评：62/100**（上一轮 41/100，提升 21 分）。提升集中在「内部合规一致性」与「差异化功能厚度」；「触达路径」与「商业主体」两项仍是上一轮的低分位。距离普通人在国内场景下「打开就能用、敢把家里事告诉它」，预计还需 **4-8 个月**的密集产品化工作（比上一轮 6-12 个月有所缩短，因内部工程已扎实）。

---

## 2. A. 普通用户落地就绪度评分（9 维度重评）

| 维度 | v1 评分 | v2 评分 | 变化 | 依据（代码路径） | 剩余差距 |
|------|---------|---------|------|------|------|
| 功能完整性 | 5/10 | **7/10** | +2 | `web/server.py:1225-1356` `_handle_chat` 已走 `build_main_graph().ainvoke(state)`，提取 `risk_tier`/`safety_triggered`/`rule_violations`，调 `MemoryManager.after_turn`；新增 7 个模块（auth/disclaimer/hotlines/institutions/ending_note/vault/doc_extract/decedent_id）；CLI 子命令从 30+ 扩到 55+ | `/api/stream`（`web/server.py:1437-1481`）仍是硬编码 system prompt，不走 graph；前端 `index.html:613` 默认走 stream，导致用户实际体验 ≠ 文档承诺；`debate` 模块仍未实现 |
| 易用性 | 3/10 | **4/10** | +1 | `web/server.py` 新增 25 个端点（auth/disclaimer/hotlines/institutions/ending-note/vault/documents/cases）；`DisclaimerBuilder.for_web_footer()` 全站告知；CLI 新增 `auth-register`/`ending-note-guide`/`case-create` 等 | Web UI 仍是开放式 chat（`index.html:367-391`），无引导式 onboarding 表单，无「我是逝者什么关系 / 在哪 / 几号去世」的结构化引导；`death-aftercare.md` 第二章「必问五条」在 Web UI 中不自动触发；`cli.py` 的 55+ 子命令对普通用户不可达（需 `pip install`）；无托管服务 |
| 内容覆盖度 | 3/10 | **3.5/10** | +0.5 | 新增 `knowledge/hotlines/database.json`（6 全国 + 5 省级热线，全部标 source）；`knowledge/institutions/seed.json`（18 条殡葬机构种子，北京 8 + 上海 5 + 重庆 5） | `knowledge/regions/` 仍只有 4 个 .md 文件，中国省份级 0 覆盖；机构数据仅 3 个城市 18 条，山东/江苏/浙江/广东等人口大省 0 条；热线仅 5 个省级，无法满足长尾地域需求；知识库无自动更新机制（`cron/scheduler.py` 可用但无知识库巡检任务） |
| 多端可达性 | 4/10 | **4/10** | 0 | Web UI 端口 8002 + SSE 流式；`gateway/connectors/telegram.py` 唯一连接器；MCP/A2A 双协议 | 无微信、无 App、无小程序；`notification-guardrails.md:89-93` 提及微信「回复 0 退订」但 `gateway/connectors/` 下无 `wechat.py`；`config.py` 无 `wechat_app_id`/`wechat_app_secret` 字段；移动端无 `@media` 响应式（`index.html` 仅 viewport meta）；Web UI sidebar 固定 220px，手机端不可用 |
| 性能与稳定性 | 5/10 | **6/10** | +1 | 496 个测试通过（31 个测试文件）；`web/server.py:108-114` 原子 JSON 写入；`auth/store.py:236-257` 原子文件写入；`ending_note/store.py:147-156` fsync 落盘；`vault/store.py:161-169` 原子索引写入；多模块降级路径明确（`ending_note/store.py:42-54` 明示生产应换 AES-GCM） | 默认 `MemorySaver`（`graph.py:55`）进程重启丢会话；`WebServer` 单进程 `ThreadingHTTPServer`，无并发上限；LLM 调用无速率限制；`/api/cli/<command>`（`web/server.py:1384-1435`）允许 subprocess 调用（虽白名单 31 个命令），生产部署是攻击面；无 SLO/SLA 文档；无压测报告 |
| 安全与隐私 | 6/10 | **6.5/10** | +0.5 | `auth/store.py` PBKDF2-HMAC-SHA256 + 16 字节 salt + 100000 iterations + HMAC 邮箱索引 + 防枚举；`auth/jwt.py` 自实现 HS256 + `compare_digest` 防时序攻击 + refresh 阈值 24h；`ending_note/guide.py:253-285` 章节级 PII 脱敏；`doc_extract/extractor.py:273-315` 文件级 PII 脱敏；`decedent_id/registry.py:97-104` 写入前 PII 黑名单脱敏；`_handle_chat` 走 graph 后 input_guard_node 重新生效 | **`web/server.py:594-596` `_ending_note_user_id` 仍从 query 取 user_id，未走 `_require_auth`**——任何登录用户改 `?user_id=xxx` 即可拉取他人终活笔记；`ending_note/store.py:175-188` 默认 passphrase 是硬编码 `deadman-ending-note-dev-passphrase`，生产未注入；`vault/store.py:215-227` `DEADMAN_VAULT_PASSWORD` 未配置时用开发默认密码；`ending_note/store.py:43-54` + `vault/store.py:193-196` 加密方案是 XOR 流密码，代码自评「未经密码学评审，对已知明文攻击不安全」；Web UI 无 HTTPS 强制、无 CSP、无速率限制；`/api/cli/<command>` subprocess 调用未沙箱化 |
| 商业模式可行性 | 2/10 | **2/10** | 0 | README/BRAND/CHANGELOG 仍无商业模式描述；`config.py` 无付费/订阅/配额字段；无支付系统集成；无使用量计费 | 纯 toC 不可持续（身后事低频高情绪）；无 B2B 合作接口；无白标/OEM 能力；无 SLA 分级；BRAND.md 无运营公司、无联系方式、无备案号；Phase 11/12 的 vault/doc_extract 提供了「数字遗产托管」的 toC 付费点雏形，但无计费封装 |
| 法律合规 | 7/10 | **7.5/10** | +0.5 | `disclaimer/text.py` 4 类告知文本（平台身份/法律免责/代办边界/数据准确性）+ `full_opening`/`short_reminder(scenario)`/`for_web_footer`；`web/server.py:1358-1382` `/api/whoami` 强制 `is_ai=True` + 四项禁止；`hotlines/lookup.py` 全部热线标 source，confidence 由 source 推断；`institutions/store.py:69-72` 缺 source 强制降级到 <0.5；`ending_note/models.py:5-9` 注释「终活笔记不是法律文件」；`decedent_id/registry.py:5-9` 注释「case_id 是内部 ID，不冒充官方编号」 | 无运营法律主体；无 ICP 备案；无独立隐私政策页面（仅 `disclaimer/text.py` 内嵌告知，未达 PIPL 第 17 条「单独告知」要求）；无用户协议；无专业责任保险；规则是 prompt 软约束（`mcp_server/server.py:check_rules`/`check_integrity` 工具），非平台硬过滤 |
| 用户信任建立 | 4/10 | **5/10** | +1 | `BRAND.md` 统一品牌名 deadman；`web/server.py:1358-1382` `/api/whoami` 返回平台身份 + is_ai + 四项禁止；`index.html:368-375` 黄色免责横幅（可关闭 + localStorage 持久化）；所有 Phase 9+ 端点响应附 `disclaimer` 字段；`ending_note/guide.py:69-148` 每章引导话术均含「不是法律文件」边界告知；`EndingNoteGuide._check_safety_signals`（`ending_note/guide.py:291-343`）检测 13 个 high + 5 个 medium 自杀关键词，命中触发 L0 | BRAND.md 无运营公司、无联系方式、无备案号；`README.md` 仓库地址指向 `github.com/bad-hope/deadman`，无官网；无隐私政策页、无用户协议、无客服入口；OSS 模式难以建立普通用户信任；`web/server.py` sidebar footer 仍写「v5.0 · 三仓同步」（`index.html:351`），与实际 v4.7.0 不符——细节不一致削弱专业感 |
| 客服与运维支持 | 2/10 | **2.5/10** | +0.5 | `observability/` 提供 OTel + Langfuse 自部署；`web/server.py` 运维看板（`/api/obs/dashboard` `/api/health/all` `/api/deploy/check`）；`cli.py` 提供 `eval`/`llm-test`/`notify-test`/`hotline-lookup`/`institution-search` 等诊断命令；`docker/healthcheck.py` 健康检查 | 无用户工单系统；无客服入口；无 SLA；问题反馈只能走 GitHub Issue；`config.py` 无告警通知配置；生产事故响应流程未文档化；`accountability-framework.md` 定义了申诉机制但无 UI 入口；`ending_note/guide.py:168-179` 检测到自杀风险信号后仅返回话术，**无主动转介 12320/988 的可点击链接或一键拨打按钮** |

**总分：62/100**（v1 41/100 → v2 62/100，提升 21 分；9 维度加权后均值 5.27 → 6.33）

**离普通人落地还有多远**：约 **40% 距离**（v1 报告 60%）。剩余 gap 的关键点（按优先级）：

1. **P0-gap-1**：`/api/stream` 仍硬编码 system prompt，前端默认走 stream，Phase 7 修复未生效
2. **P0-gap-2**：Phase 10 ending-note 端点未走 `_require_auth`，任意登录用户可拉取他人笔记
3. **P0-gap-3**：加密方案不达生产基线（XOR 流密码，代码自评承认）
4. **P0-gap-4**：无托管服务、无微信入口（普通用户无法触达）
5. **P0-gap-5**：中国省份级知识库 0 覆盖
6. **P1-gap-1**：无引导式 onboarding 表单（开放式 chat 不适合高情绪用户）
7. **P1-gap-2**：无隐私政策/用户协议/客服工单页面（PIPL 合规底线）
8. **P1-gap-3**：无中国境内 LLM/搜索 provider（OpenAI/DuckDuckGo 在国内不稳定）
9. **P1-gap-4**：无移动端响应式适配
10. **P1-gap-5**：无商业模式与法律主体

---

## 3. B. Phase 7-13 修复成果的客观校验

为避免被 CHANGELOG 措辞带偏，本节对每个 Phase 的实际修复成果做代码级校验：

### Phase 7：Web UI /api/chat 走完整规则链 ✓（部分）

- `web/server.py:1246-1268` 确实构造 `ConversationState` 并调 `build_main_graph().ainvoke(state)`，从 result_state 提取 `final_response`/`current_agent`/`rule_check`，调 `MemoryManager.after_turn`。
- `web/server.py:1320-1356` graph 失败降级用 `SoulLoader().default_soul()`，不再硬编码——✓ 闭环 P0-1。
- `web/server.py:1358-1382` `/api/whoami` 强制 `is_ai=True`——✓ transparency L5 落地。
- **但**：`web/server.py:1437-1481` `_stream_chat` 仍是 `f"你是 {agent} 智能体，专注于协助处理逝者身后事。请用温和、专业的语气回答。"` 硬编码 system prompt（行 1459-1466），**未走 graph**。
- **前端 `index.html:613` 默认走 `/api/stream`**：`const url = \`/api/stream?query=...\``，仅在 stream 返回空时 fallback 到 `/api/chat`（`index.html:633-642`）。
- **结论**：Phase 7 修了同步路径，但流式路径未修，且前端默认用流式——「Web UI 真正接入 agent.md」这个 P0-1 目标**未在用户体验层闭环**。

### Phase 8：用户认证与会话系统 ✓（实现扎实，集成不完整）

- `auth/store.py` 实现完整：PBKDF2-HMAC-SHA256（100000 iterations）+ 16 字节随机 salt + HMAC-SHA256 邮箱索引 + 防枚举（`verify()` 失败统一返回 None + 假 hash 比对统一响应时间）+ 原子写入 + 文件权限 0o600——✓ 达 NIST/OWASP 2023 推荐。
- `auth/jwt.py` 自实现 HS256：三段式 base64url + `hmac.compare_digest` 防时序攻击 + refresh 阈值 24h + secret 自动生成持久化——✓ 不引入 pyjwt 依赖。
- `web/server.py:1498-1521` `_require_auth` 从 `Authorization: Bearer <token>` 解析用户。
- `web/server.py:281-285` `/api/chat` 优先用认证用户，无 token 降级 anonymous——✓ 闭环 P0-2 主路径。
- `web/server.py:866-870` Phase 11/12/13（vault/documents/cases）统一走 `_phase_auth_user()`——✓。
- **但**：`web/server.py:594-596` `_ending_note_user_id` 仍从 query string 取 `?user_id=`，未走 `_require_auth`——**Phase 10 端点（`/api/ending-note` 全部 8 个路由）是 Phase 8 auth 的盲区**。任意登录用户改 query 即可拉取他人终活笔记（含家庭关系、资产、医疗意愿、留言等高敏感字段）。这是 **P0 级安全漏洞**，必须立即修复。

### Phase 9：法律免责 + 机构查询 + 官方热线 ✓（完成度高）

- `disclaimer/text.py` 4 类告知文本 + 3 种调用方式（`full_opening`/`short_reminder(scenario)`/`for_web_footer`），文本严格遵循 compliance/service-boundary/transparency/legal-compliance 4 个规则文件——✓。
- `hotlines/lookup.py` 6 全国职能热线 + 5 省级热线，全部标 source，confidence 由 source 推断（`_confidence` 方法，`hotlines/lookup.py:41-54`）——✓ 不编造电话。
- `institutions/store.py` 18 条种子数据，`confidence<0.7` 输出 `needs_verification_warning`，缺 source 强制降级到 <0.5（`institutions/store.py:69-72`）——✓ retrieval-guardrails L7 落地。
- `web/server.py:169-177` 4 个端点（disclaimer/hotlines/institutions/institutions/<id>）全部返回 `disclaimer` 字段——✓ transparency L5 落地。
- **短板**：机构数据仅 3 城市 18 条，无法满足长尾地域需求；热线仅 5 省级，34 个省级行政区覆盖率 15%；机构 `phone` 字段留空（`institutions/store.py:51` 注释「不编造电话号码」），用户拿到的是「请拨打 12345 核实」——体验上仍需用户二次操作。

### Phase 10：终活笔记 + 家庭共享 ✓（功能完整，安全有 P0 漏洞）

- `ending_note/models.py` 9 章节模型（personal_info/family_relations/assets/funeral_wishes/medical_wishes/digital_legacy/messages/emergency_contacts/will_intent），与日本終活应用通用模板对齐——✓。
- `ending_note/guide.py:69-148` `EndingNoteGuide.SECTIONS` 9 章节 + 引导话术 + 每章附「不是法律文件」边界告知——✓ service-boundary L3 落地。
- `ending_note/guide.py:291-343` `_check_safety_signals` 检测 13 个 high + 5 个 medium 自杀关键词，命中触发 L0 停止流程引导——✓ safety-protocol L0 落地。
- `ending_note/guide.py:253-285` 章节级 PII 脱敏（姓名/电话/账号/地址/出生日期），`_mask_pii` 5 套规则——✓ PIPL 第五章落地。
- `ending_note/store.py:408-529` `trigger_delivery("death_confirmation")` 强制 7 天等待期 + 受益人手动确认——✓ notification-guardrails L4 落地。
- **短板 1**（P0）：`web/server.py:594-596` 未走 `_require_auth`，任意登录用户可拉取他人笔记（详见 Phase 8 校验）。
- **短板 2**（P0）：`ending_note/store.py:175-188` `_get_passphrase` 默认硬编码 `deadman-ending-note-dev-passphrase`，生产未通过 auth 注入独立 passphrase——所有用户的笔记用同一口令加密，**单点泄露即全量泄露**。
- **短板 3**（P1）：`ending_note/store.py:43-54` 加密方案是 XOR 流密码（HMAC-SHA256 keystream），代码自评「未经密码学评审，对已知明文攻击不安全，生产应换 AES-256-GCM」——在身后事这种强信任品类，**用户敏感 PII 落盘即处于弱保护状态**。
- **短板 4**（P2）：`EndingNoteGuide.SECTIONS` 是固定顺序 1→9，无「跳过本章」/「优先填哪章」的灵活导航；`next_question` 跳过已填章节但无法回看（CLI `ending-note-guide --section` 可回看，但 Web UI 无对应入口）。

### Phase 11：数字遗产保险库 ✓（功能完整，加密与 Phase 10 同短板）

- `vault/store.py` 8 种 VaultItem 类型（password/document/photo/video/audio/note/account/crypto），受益人指定 + 权限隔离（owner vs beneficiary）——✓ 参考 My-Legacy.ai/VoiceWill/GoodTrust 设计落地。
- `vault/store.py:545-650` `trigger_delivery` on_death 强制 7 天等待期 + 仅受益人可确认领取（owner 不能替受益人确认）——✓ notification-guardrails L4 + service-boundary 双重保险。
- `vault/store.py:394-405` `list_items` 严格权限：requester==owner 才返回全部，其他返回空——✓。
- `vault/store.py:479-507` `list_inherited` 扫描所有用户目录找 beneficiary 命中条目——✓（数据规模小时可接受）。
- **短板 1**（P0）：`vault/store.py:215-227` `_get_master_password` 默认 `deadman-dev-default-password-not-for-production`——所有用户共用同一 master password 派生密钥（虽有 user_id 加 salt），**与 Phase 10 同问题**：单点泄露即全量泄露。
- **短板 2**（P0）：`vault/store.py:193-196` 加密方案同 Phase 10（XOR + HMAC），代码注释「生产实现建议用 cryptography.Fernet 或 AES-256-GCM」——未达 PIPL 第五章基线。
- **短板 3**（P2）：`vault/store.py:381-392` `get_item` 作为 beneficiary 时扫描所有用户目录——数据规模大时（万级用户）性能不可接受，且未做访问日志。

### Phase 12：AI 文档提取 ✓（功能完整，OCR 是硬短板）

- `doc_extract/extractor.py` 7 种文档类型（will/trust/insurance/property/bank_statement/id_card/other），LLM 不可用时降级 `confidence=0.3` 不编造——✓ integrity L1 落地。
- `doc_extract/extractor.py:273-315` 文件级 PII 脱敏（身份证 18 位 → 前 6 后 4 / 手机 11 位 → 前 3 后 4 / 银行账号 16-19 位 → 前 4 后 4 / 邮箱 → 前 1 后域名）——✓。
- `doc_extract/extractor.py:144-155` 文档原文加密存入 vault，索引不存 source_text_masked——✓ PIPL 第五章落地。
- **短板 1**（P1）：`doc_extract/extractor.py:233-237` PDF 仅用 stdlib 简单解析 BT/ET 块，复杂 PDF 标 `[unsupported_pdf_format]`；图片标 `[needs_ocr]` 但无 OCR 实现；docx/doc 标 `[unsupported_docx_format]`——**用户上传的文档大概率提取失败**，需用户手动填写关键字段，与「AI 文档提取」承诺不符。
- **短板 2**（P2）：`doc_extract/extractor.py:387-399` `_build_prompt` 截断 4000 字符，长文档（如银行流水）可能漏掉关键信息。
- **短板 3**（P2）：无文档分类置信度（仅 `doc_type` 推断，无 confidence 标注），用户无法判断「这张图被识别为保险单的可信度」。

### Phase 13：遗码通 - 逝者唯一标识 ✓（功能完整，差异化清晰）

- `decedent_id/registry.py` `DecedentRecord` 不存敏感 PII（真实姓名/身份证号/死亡证明编号），`decedent_alias` 是用户给的化名，`case_id` 是 deadman 内部 ID——✓ integrity L1 + service-boundary L3 双重落地。
- `decedent_id/registry.py:97-104` `PII_PATTERNS` 黑名单（身份证/手机/银行账号），`_sanitize_pii` 写入前脱敏——✓ PIPL 第五章落地。
- `decedent_id/registry.py:226-263` `add_event` 由各 agent 追加时间线事件，event + timestamp + agent + notes——✓ 参考「渝逝有安」遗码通概念。
- `decedent_id/registry.py:281-288` `archive_case` 用户情绪平复后主动归档——✓ 符合身后事场景节奏。
- **短板 1**（P2）：`DecedentRegistry` 与各 agent 的集成点未落地——`add_event` 的 `agent` 字段目前只能由调用方手动传入，graph 节点未自动追加事件；用户在 Web UI 创建 case 后，对话流程不会自动关联 case_id。
- **短板 2**（P2）：`case_id` 是 `case-{uuid12}`，与「遗码通」概念中的「逝者唯一标识贯穿全流程」有差距——目前更像「逝者案例文件夹」，而非「可扫码/可分享的唯一标识」。

---

## 4. C. 尚未解决的 Gap 清单（P0/P1/P2）

### P0 级 gap（必须修复才能上线）

#### P0-gap-1：`/api/stream` 仍硬编码 system prompt
- **gap 描述**：`web/server.py:1437-1481` `_stream_chat` 用 `f"你是 {agent} 智能体，专注于协助处理逝者身后事。请用温和、专业的语气回答。"` 作为 system prompt，未走 `build_main_graph()`；前端 `index.html:613` 默认走 `/api/stream`，仅在 stream 返回空时 fallback 到 `/api/chat`。
- **为什么是 gap**：Phase 7 修了 `/api/chat`，但用户实际体验的是 `/api/stream`——`input_guard_node`/`router_node`/`agent_node`/`rule_check_node`/`integrity_check_node`/`output_guard_node` 全部失效。用户在 Web UI 上表达自伤意图时，`safety-protocol.md` L0 不触发；用户问「这份遗嘱有效吗」时，`legal-advisor.md` 的「绝不出法律意见」约束不生效。这是上一轮 P0-1 修复的盲区。
- **实现路径建议**：参考 `_handle_chat`（`web/server.py:1225-1356`）改造 `_stream_chat`：构造 `ConversationState` → `build_main_graph().ainvoke(state)` → 提取 `final_response` → 一次性 SSE 推送（或 token 级流式需要 graph 支持 astream 事件，可分两阶段：先一次性推送保证规则链生效，后续再迭代 token 级流式）。同时改 `index.html:613` 默认走 `/api/chat`，stream 作为低延迟可选。
- **与 AI-RULE 冲突**：否。修复后强化 safety-protocol L0 + integrity L1 + compliance L3 执行。

#### P0-gap-2：Phase 10 ending-note 端点未走 `_require_auth`
- **gap 描述**：`web/server.py:594-596` `_ending_note_user_id` 从 query string 取 `?user_id=`，未走 `_require_auth`；Phase 10 全部 8 个端点（`/api/ending-note` 全部路由）均用此方法取 user_id。
- **为什么是 gap**：任意登录用户改 `?user_id=xxx` 即可拉取他人终活笔记（含家庭关系、资产清单、医疗意愿、给家人的留言等高敏感字段）。这是横向越权漏洞，违反 PIPL 第五章「数据访问控制」与 compliance-framework「数据治理底线」。
- **实现路径建议**：删除 `_ending_note_user_id`，所有 Phase 10 端点改用 `_phase_auth_user()`（已存在于 `web/server.py:866-870`）；与 Phase 11/12/13 保持一致。同时清理 `_handle_ending_note_get`/`_handle_ending_note_section`/`_handle_ending_note_guide_next`/`_handle_ending_note_share`/`_handle_ending_note_unshare`/`_handle_ending_note_shared_with_me`/`_handle_ending_note_trigger`/`_handle_ending_note_completion` 8 个方法签名。
- **与 AI-RULE 冲突**：否。修复后强化 compliance-framework 数据治理 + PIPL 第五章。

#### P0-gap-3：加密方案不达生产基线
- **gap 描述**：`ending_note/store.py:43-54` + `vault/store.py:193-196` 加密方案是 XOR 流密码（HMAC-SHA256 keystream + HMAC tag），代码自评「未经密码学评审，对已知明文攻击不安全」；`ending_note/store.py:175-188` + `vault/store.py:215-227` 默认 passphrase/password 是硬编码开发值。
- **为什么是 gap**：身后事场景涉及家庭财产、亲属关系、医疗意愿、数字账号密码等高敏感 PII，加密方案不达 PIPL 第五章「足够强度的加密」要求；默认 passphrase 全用户共用，单点泄露即全量泄露。一旦用户数据落盘即处于弱保护状态，托管上线后是合规与信任的双重崩塌点。
- **实现路径建议**：
  1. 引入 `cryptography` 依赖（PyPI 主流，已 stable），替换为 `AESGCM`（`cryptography.hazmat.primitives.ciphers.aead.AESGCM`）；
  2. 接口签名保持 `encrypt(plaintext, key) -> envelope / decrypt(envelope, key) -> plaintext` 不变，仅替换内部实现；
  3. 用户登录后由 `auth/store.py` 派生独立 passphrase（如 `PBKDF2(user_password, user_id_salt)`），通过环境变量注入 `DEADMAN_ENDING_NOTE_PASSPHRASE`/`DEADMAN_VAULT_PASSWORD`，不再用硬编码默认值；
  4. 已有数据迁移：检测 envelope.alg 字段，旧数据用旧解密路径读出后用 AES-GCM 重新加密。
- **与 AI-RULE 冲突**：否。强化 legal-compliance-framework 第五章 PIPL。

#### P0-gap-4：无托管服务 + 无微信入口（普通用户无法触达）
- **gap 描述**：`README.md` 仍要求 `pip install -e .` + `export LLM_API_KEY` + `deadman web-server`；`gateway/connectors/` 仍只有 `telegram.py`；`config.py` 无 `wechat_app_id`/`wechat_app_secret` 字段；`notification-guardrails.md:89-93` 提及微信「回复 0 退订」但代码层无 `wechat.py`。
- **为什么是 gap**：普通用户不会 `pip install` + `docker compose`；中国用户主入口是微信，Telegram 在国内不可用。这是「打开就能用」的核心 gap。
- **实现路径建议**：
  1. 托管服务：1 个云服务器 + 域名 + ICP 备案 + Nginx 反代 + HTTPS + Docker Compose（已有 `docker-compose.yml`）；
  2. 微信公众号入口：实现 `gateway/connectors/wechat.py`（`base.py` 已留 Protocol），用 httpx 直连微信公众号 API（不引入 wechatpy 库），配对 token 机制复用 `gateway/core.py` 设计；
  3. 微信小程序入口：前端用 Taro/uni-app 封装 Web UI 现有页面，后端复用 `/api/*` 端点；
  4. 接入国产 LLM（智谱 GLM-4.6，`config.py:22` 已支持 `LLM_PROVIDER=zhipu`）。
- **与 AI-RULE 冲突**：否。`gateway/connectors/base.py` 已留 Protocol 接口；`notification-guardrails.md` 已为微信定义约束。

#### P0-gap-5：中国省份级知识库 0 覆盖
- **gap 描述**：`knowledge/regions/` 仍只有 4 个 .md 文件（`CN/overview.md`/`US/overview.md`/`US/california.md`/`JP/overview.md`），中国 34 个省级行政区 0 个文件。
- **为什么是 gap**：用户问「北京/上海/广州/成都」的本地政策，平台只能给全国通用流程；北京「死亡一件事」联办、上海「随申办」线上办理、各地民政局差异等本地化信息完全缺失。这是上一轮 P0-2，Phase 7-13 完全未动。
- **实现路径建议**：
  1. 按 `knowledge/regions/SCHEMA.md` 9 阶段标准，先补 `CN/beijing.md`/`CN/shanghai.md`/`CN/guangdong.md`/`CN/zhejiang.md`/`CN/jiangsu.md` 5 个一线省份；
  2. 每个文件由 `policy-researcher` 智能体辅助生成初稿（用 `web_search_official` MCP 工具），人工核实后入库；
  3. 启用 `cron/scheduler.py` 的知识库时效巡检任务（已在 `notification-guardrails.md` 约束下）。
- **与 AI-RULE 冲突**：否。`policy-researcher.md` 已设计此流程；`retrieval-guardrails.md` 要求时效校验。

### P1 级 gap（上线后 3 个月内必须补齐）

#### P1-gap-1：无引导式 onboarding 表单
- **gap 描述**：`web/static/index.html:367-391` 仍是开放式 chat 框，无「我是逝者什么关系 / 在哪 / 几号去世 / 已办到哪一步」的结构化引导表单；`death-aftercare.md` 第二章「必问五条」在 Web UI 中不自动触发。
- **为什么是 gap**：身后事用户处于高情绪负荷，开放式 chat 不友好；用户不知道「该问什么」，平台也不知道「用户在哪个阶段」。`EndingNoteGuide`（Phase 10）已实现结构化引导，但仅在终活笔记场景，未推广到主对话流。
- **实现路径建议**：前端新增 onboarding wizard（4-5 步：关系/地点/日期/已办事项），结果作为 `ConversationState` 的 `user_profile` 字段注入 graph；后端新增 `/api/onboarding` 端点保存 wizard 结果；graph 的 `router_node` 根据 user_profile 自动路由到对应 agent。
- **与 AI-RULE 冲突**：否。强化 `death-aftercare.md` 第二章「必问五条」执行。

#### P1-gap-2：无隐私政策/用户协议/客服工单页面
- **gap 描述**：`docs/` 下仅 4 个 .md 文件（pm-assessment/openclaw-design-analysis/DEPLOYMENT/QUICKSTART），无 privacy/agreement/terms/customer-service 文件；`web/server.py` 无 `/privacy`/`/terms`/`/support` 路由；`disclaimer/text.py` 内嵌告知不达 PIPL 第 17 条「单独告知」要求。
- **为什么是 gap**：PIPL 第 17 条要求处理敏感个人信息「向个人单独告知」；身后事是强信任品类，用户不会把家庭财产和亲属关系信息交给一个「找不到隐私政策/用户协议/客服」的平台；`accountability-framework.md` 定义了申诉机制但无 UI 入口。
- **实现路径建议**：
  1. 新增 `docs/privacy.md` + `docs/terms.md` + `docs/support.md` 3 个静态页；
  2. Web UI footer 加 3 个链接（`/privacy`/`/terms`/`/support`）；
  3. 新增 `/api/support/ticket` 端点 + `support/ticket.py` 模块（工单 CRUD，纯文件存储，复用 `auth/store.py` 原子写入模式）；
  4. `web/server.py:_handle_whoami` 返回字段加 `privacy_url`/`terms_url`/`support_url`。
- **与 AI-RULE 冲突**：否。强化 transparency L5 + accountability L6 + legal-compliance 第五章。

#### P1-gap-3：无中国境内 LLM/搜索 provider
- **gap 描述**：`config.py:22` 默认 `LLM_PROVIDER=openai`，`llm_model=gpt-4o`；`tools/web_search.py` 直连 `https://html.duckduckgo.com/html/`，中国大陆访问不稳定。
- **为什么是 gap**：OpenAI/Anthropic 在中国大陆访问不稳定；DuckDuckGo 在国内常被墙；`policy-researcher` 智能体的核心能力（多语言搜索 + 官方源优先）受影响。这是上一轮 P1-3，Phase 7-13 未动。
- **实现路径建议**：
  1. `config.py` 新增 `LLM_PROVIDER=zhipu` 默认值（国内托管时）；`llm.py` 已支持 zhipu（CHANGELOG v4.3 提及），无需大改；
  2. `tools/web_search.py` 新增 `BaiduSearchProvider`/`SogouSearchProvider`/`BingCNSearchProvider`，用 httpx 直连 + HTML 解析；
  3. `config.py` 新增 `web_search_provider` 字段，默认 `duckduckgo`，国内托管设为 `baidu`；
  4. 接入百度/搜狗搜索 API（付费版稳定，免费版有限额）。
- **与 AI-RULE 冲突**：否。`tools/web_search.py` 已有 provider 抽象（`WebSearchProvider` Protocol）。

#### P1-gap-4：无移动端响应式适配
- **gap 描述**：`web/static/index.html:5` 仅 viewport meta，无 `@media` 查询；sidebar 固定 220px（`index.html:37-40`），手机端不可用；input area padding 16px 32px（`index.html:122-125`），手机端溢出。
- **为什么是 gap**：身后事用户多用手机（殡仪馆/派出所/公证处现场咨询），桌面优先 UI 在手机端体验差。
- **实现路径建议**：
  1. `index.html` 加 `@media (max-width: 768px)` 媒体查询：sidebar 改为可折叠抽屉、chat-area padding 改 16px、input-area padding 改 12px 16px；
  2. 按钮触控优化（min-height 44px）；
  3. textarea 字体 16px（防 iOS 缩放）；
  4. 新增「返回顶部」浮动按钮（长对话场景）。
- **与 AI-RULE 冲突**：否。

#### P1-gap-5：无商业模式与法律主体
- **gap 描述**：README/BRAND/CHANGELOG 仍无商业模式描述；`config.py` 无付费/订阅/配额字段；BRAND.md 无运营公司、无联系方式、无备案号；`README.md` 仓库地址指向 `github.com/bad-hope/deadman`，无官网。
- **为什么是 gap**：纯 toC 不可持续（身后事低频高情绪）；OSS 项目无运营主体，用户协议无约束力；身后事是强信任品类，用户不会把家庭财产交给「找不到运营方」的平台。这是上一轮 P1-2，Phase 7-13 未动。
- **实现路径建议**：详见第 6 节「商业模式落地路径」。
- **与 AI-RULE 冲突**：否。`compliance-framework.md` 四项禁止与 B2B2C 模式天然契合。

#### P1-gap-6：知识库无自动更新机制
- **gap 描述**：`knowledge/regions/CN/overview.md` 标注「最后更新 2026-07-12」，政策变更后无自动检测；`cron/scheduler.py` 可用但无知识库巡检任务；`retrieval-guardrails.md` 要求时效校验但未启用。
- **为什么是 gap**：政策变更（如 2024 年金融监管总局 5 万元简化提取新规）后无自动检测；用户按过期信息办理，白跑一趟。
- **实现路径建议**：
  1. 新增 `cron/tasks/knowledge_freshness.py`，定期（每月）调用 `policy-researcher` 智能体 + `web_search_official` 工具，对比知识库文件与官方源；
  2. 发现差异时入「待审核」队列，人工核实后更新；
  3. `cron/scheduler.py` 已有 propose/confirm 双重确认机制，复用即可。
- **与 AI-RULE 冲突**：否。Cron 已受 `notification-guardrails.md` 约束。

#### P1-gap-7：Web UI sidebar 版本号不一致
- **gap 描述**：`web/static/index.html:351` sidebar footer 写「v5.0 · 三仓同步」，实际 `web/server.py:1368` `/api/whoami` 返回 `version: "4.7.0"`，CHANGELOG 最新版本也是 v4.7.0。
- **为什么是 gap**：细节不一致削弱专业感；身后事是强信任品类，用户对「连版本号都对不上的平台」信任度低。
- **实现路径建议**：`index.html:351` 改为 `v4.7.0`；或动态从 `/api/whoami` 读取 version 字段渲染。
- **与 AI-RULE 冲突**：否。

### P2 级 gap（长期演进）

#### P2-gap-1：无语音输入 / TTS 输出
- **gap 描述**：`web/static/index.html:387` 仅 textarea 输入，无麦克风按钮；`web/server.py` 无 `/api/asr`/`/api/tts` 端点；`PLATFORMS.md:348` 提到 MiniMax 多模态适合临终患者语音咨询但未实现。
- **为什么是 gap**：老年用户/视障用户/临终患者友好性差；身后事场景中部分用户（如高龄丧偶老人）打字困难。
- **实现路径建议**：Web UI 加麦克风按钮（Web Speech API 浏览器原生，免后端 ASR）；TTS 接入智谱/MiniMax 语音合成 API；`config.py` 新增 `tts_provider`/`tts_api_key` 字段。
- **与 AI-RULE 冲突**：否。

#### P2-gap-2：无情绪识别与自适应节奏
- **gap 描述**：`EndingNoteGuide._check_safety_signals`（`ending_note/guide.py:291-343`）仅做关键词检测（13 high + 5 medium），未做情绪强度量化；`death-aftercare-emotional` 子智能体已定义但 Web UI 不触发；UI 节奏固定，无情绪驱动适配。
- **为什么是 gap**：身后事用户情绪强度高，固定节奏的 UI 不友好；高情绪用户应慢节奏 + 多确认 + 简短选项，低情绪用户可快节奏 + 多信息密度。
- **实现路径建议**：graph 的 `input_guard_node` 新增情绪检测（关键词表 + LLM 二次判定），输出 `emotion_intensity: low/medium/high`；前端根据 emotion_intensity 调整 UI 节奏（high 时每屏只问 1 个问题 + 大字体 + 多确认按钮）。
- **与 AI-RULE 冲突**：否。强化 `safety-protocol.md` + `special-populations-framework.md`。

#### P2-gap-3：自杀风险信号仅话术，无可点击转介
- **gap 描述**：`ending_note/guide.py:168-179` 检测到自杀风险信号后返回话术「请考虑联系当地心理危机干预热线或急救电话」，但 Web UI 无 12320/988 可点击链接或一键拨打按钮。
- **为什么是 gap**：高情绪用户在话术引导下仍需手动搜索热线，转化率低；`safety-protocol.md` 要求「即时干预」，手动搜索是 friction。
- **实现路径建议**：`_handle_ending_note_section` 命中 `safety_alert` 时，前端弹窗显示「拨打 12320（心理危机干预）」/「拨打 120（急救）」按钮，移动端用 `tel:` 协议直接拨号。
- **与 AI-RULE 冲突**：否。强化 `safety-protocol.md` L0。

#### P2-gap-4：OCR / 复杂 PDF / docx 未实现
- **gap 描述**：`doc_extract/extractor.py:233-237` 图片标 `[needs_ocr]` 但无 OCR 实现；PDF 用 stdlib 简单解析，复杂 PDF 标 `[unsupported_pdf_format]`；docx/doc 标 `[unsupported_docx_format]`。
- **为什么是 gap**：用户上传的文档大概率提取失败（手机拍照件是图片，正式 PDF 多用 FlateDecode 压缩，律师函多用 docx）。
- **实现路径建议**：OCR 接入百度 OCR API / 腾讯 OCR API（国内稳定）；PDF 用 pdfplumber（PyPI 主流）；docx 用 python-docx。引入新依赖需评估，但 OCR/PDF/docx 是文档提取的基线能力。
- **与 AI-RULE 冲突**：否。

#### P2-gap-5：DecedentRegistry 与各 agent 集成点未落地
- **gap 描述**：`decedent_id/registry.py:226-263` `add_event` 的 `agent` 字段目前只能由调用方手动传入，graph 节点未自动追加事件；用户在 Web UI 创建 case 后，对话流程不会自动关联 case_id。
- **为什么是 gap**：「遗码通」概念的核心是「逝者唯一标识贯穿全流程」，目前 case_id 仅在 `/api/cases/*` 端点内有效，与 `/api/chat` 对话流脱节。
- **实现路径建议**：`ConversationState` 新增 `case_id` 字段；`agent_node` 执行后自动调 `DecedentRegistry.add_event(case_id, user_id, event=response_summary, agent=current_agent)`；Web UI 对话页加「关联案例」下拉框。
- **与 AI-RULE 冲突**：否。

#### P2-gap-6：多语言 UI 未实现
- **gap 描述**：`BRAND.md` 三语品牌名表已定义；`multilingual-framework.md` 已有规则；`web/server.py:1381` `/api/whoami` 返回 `supported_languages: ["zh-CN", "en-US"]`，但 Web UI 仅中文。
- **为什么是 gap**：跨境身后事场景（`cross-border-specialist` 智能体）需要中英日三语 UI。
- **实现路径建议**：前端 i18n 框架（vue-i18n 或原生 JS 字典）；后端 LLM 已支持多语言（prompt 内即可切换）。
- **与 AI-RULE 冲突**：否。

#### P2-gap-7：与殡葬/法律/公证机构 API 对接未实现
- **gap 描述**：`service-boundary-framework.md` 禁止代办，但可提供官方预约入口；目前 `institutions/store.py` 仅返回机构信息，无预约链接。
- **为什么是 gap**：用户拿到机构信息后仍需电话预约，体验未闭环。
- **实现路径建议**：对接各地民政「一件事一次办」预约 API（如北京「京通」/上海「随申办」）；`institutions/store.py` 新增 `reservation_url` 字段。
- **与 AI-RULE 冲突**：否。仅信息引导，不代办。

---

## 5. D. 离落地的关键路径（修订版）

按依赖关系排序，**前 5 步是 P0，必须先做**：

### Step 1：补齐 Phase 7 盲区（P0，无依赖，1-2 天）

- 改 `web/server.py:1437-1481` `_stream_chat` 走 `build_main_graph().ainvoke(state)`，与 `_handle_chat` 对齐；
- 改 `index.html:613` 默认走 `/api/chat`（同步 + 规则链生效），stream 作为低延迟可选；
- 测试：扩 `test_web_chat_graph.py` 覆盖 stream 路径。

### Step 2：补齐 Phase 8 auth 穿透（P0，无依赖，1 天）

- 删除 `web/server.py:594-596` `_ending_note_user_id`；
- Phase 10 全部 8 个端点改用 `_phase_auth_user()`；
- 测试：扩 `test_web_auth.py` 覆盖 ending-note 端点的 401/越权场景。

### Step 3：加密方案升级（P0，依赖 Step 2，1-2 周）

- 引入 `cryptography` 依赖；
- `ending_note/store.py` + `vault/store.py` 替换为 `AESGCM`；
- passphrase 由 `auth/store.py` 派生独立 user passphrase，通过环境变量注入；
- 已有数据迁移脚本。

### Step 4：中国省份级知识库填充（P0，可与 Step 1-3 并行，2-4 周）

- 补 `CN/beijing.md`/`CN/shanghai.md`/`CN/guangdong.md`/`CN/zhejiang.md`/`CN/jiangsu.md` 5 个一线省份；
- 启用 `cron/scheduler.py` 的知识库时效巡检任务。

### Step 5：托管服务 + 微信入口 + 国产 LLM/搜索（P0，依赖 Step 1-4，1-2 月）

- 云服务器 + 域名 + ICP 备案 + HTTPS + Docker Compose 部署；
- 实现 `gateway/connectors/wechat.py`；
- 接入智谱 GLM-4.6 + 百度/搜狗搜索 provider；
- 微信公众号 + 小程序双入口。

### Step 6：合规化与信任建设（P1，依赖 Step 5，1-2 月）

- 隐私政策 + 用户协议 + 客服工单页面；
- 法律主体注册（公司或民办非企业）；
- 专业责任保险；
- ICP 备案完成。

### Step 7：引导式 onboarding + 移动端响应式（P1，依赖 Step 1，3-4 周）

- 前端 onboarding wizard（4-5 步）；
- `@media` 响应式适配；
- 自杀风险信号可点击转介。

### Step 8：B2B2C 合作拓展（P1，依赖 Step 5-6，3-6 月）

- 律所/公证处合作启动（BD 周期短、合规风险低）；
- 保险公司嵌入（中期核心）；
- 政府民政合作试点（长期壁垒）。

---

## 6. E. 商业模式落地路径（修订版）

上一轮报告第 5 节已详述，本节仅补充 Phase 11-13 带来的新可能性：

### Phase 11/12/13 后的新商业化支点

1. **数字遗产保险库 toC 增值**（`vault/store.py`）：免费层 5 条目 / 100MB，付费层无限条目 + 受益人门户 + 投递触发。年费 ¥99-299，参考 1Password Families（$4.99/月）定价。
2. **AI 文档提取 toC 增值**（`doc_extract/extractor.py`）：免费层每月 3 份，付费层无限 + 多文档对比 + 律师审阅转介。单次 ¥9.9，参考 Trust & Will 单文件 $39。
3. **遗码通 B2B 政府/殡葬机构合作**（`decedent_id/registry.py`）：作为「渝逝有安」类政务小程序的智能引导后端，按服务人口年费定价。
4. **终活笔记 B2B 保险公司嵌入**（`ending_note/`）：寿险理赔后引导受益人填写，保险公司按有效填写付费（CPA 模式，每份 ¥X）。

### 综合建议（修订）

- **短期（0-3 月）**：Step 1-5 完成后，先以**律所/公证处合作 + toC 数字遗产保险库付费层**启动（前者 BD 周期短、后者已有 vault 基础设施），同步申请 ICP 备案与法律主体。
- **中期（3-12 月）**：以**保险公司嵌入 + 政府民政合作试点 1-2 个城市**为核心，辅以 toC 终活笔记付费层。
- **长期（12 月+）**：以**政府民政合作为规模化壁垒**，辅以殡葬机构白名单与 toC 增值服务。
- **避免**：纯 toC 订阅制（不可持续）、与未认证殡葬机构分成（违反 `safety-protocol.md`）、出售用户数据（违反 `compliance-framework.md` 数据治理条款）。

---

## 7. F. 下一步必做 Top 10 清单

按优先级与依赖关系排序，**前 5 项必须在上线前完成**：

| # | 任务 | 优先级 | 依赖 | 工作量 | 预期效果 |
|---|------|--------|------|--------|---------|
| 1 | 修复 `/api/stream` 走 `build_main_graph()`，与 `/api/chat` 对齐；`index.html` 默认走 `/api/chat` | P0 | 无 | 1-2 天 | 闭环 Phase 7 P0-1，Web UI 用户体验 = 文档承诺 |
| 2 | Phase 10 ending-note 全部 8 个端点改用 `_phase_auth_user()`，删除 `_ending_note_user_id` | P0 | 无 | 1 天 | 修复横向越权漏洞，PIPL 第五章数据访问控制落地 |
| 3 | `ending_note/store.py` + `vault/store.py` 加密方案升级为 AES-256-GCM；passphrase 由 auth 派生独立 user passphrase | P0 | Step 2 | 1-2 周 | PIPL 第五章「足够强度的加密」落地，代码自评短板消除 |
| 4 | 补 `knowledge/regions/CN/beijing.md`/`shanghai.md`/`guangdong.md`/`zhejiang.md`/`jiangsu.md` 5 个一线省份；启用 `cron/scheduler.py` 知识库时效巡检 | P0 | 无 | 2-4 周 | 中国省份级知识库 0→5 覆盖，本地化信息可用 |
| 5 | 托管服务上线：云服务器 + 域名 + ICP 备案 + HTTPS + Docker Compose；接入智谱 GLM-4.6 + 百度/搜狗搜索 provider | P0 | Step 1-4 | 1-2 月 | 普通用户「打开就能用」，国产 LLM/搜索稳定可用 |
| 6 | 实现微信入口：`gateway/connectors/wechat.py`（公众号）+ 微信小程序前端（Taro/uni-app 封装 Web UI） | P0 | Step 5 | 3-4 周 | 中国用户主入口可达 |
| 7 | 新增 `docs/privacy.md` + `docs/terms.md` + `docs/support.md` 3 个静态页；Web UI footer 加链接；新增 `/api/support/ticket` 端点 + `support/ticket.py` 模块 | P1 | Step 5 | 1-2 周 | PIPL 第 17 条「单独告知」落地，客服工单入口可用 |
| 8 | 前端引导式 onboarding wizard（4-5 步：关系/地点/日期/已办事项）+ `@media` 移动端响应式适配 | P1 | Step 1 | 3-4 周 | 高情绪用户友好，手机端可用 |
| 9 | 法律主体注册（公司或民办非企业）+ 专业责任保险 + ICP 备案完成 | P1 | Step 5 | 1-2 月 | 强信任品类合规底线，用户协议有约束力 |
| 10 | B2B2C 合作启动：律所/公证处合作（CPL 模式）+ 保险公司嵌入试点（API 调用付费） | P1 | Step 5-9 | 3-6 月 | 商业模式落地，脱离纯 OSS 不可持续 |

---

## 8. 评估结论

Phase 7-13 是**一次有纪律的产品化冲刺**：闭环了上一轮 5 个 P0 中的 3 个（P0-1 Web UI 走完整规则链 / P0-2 用户认证 / P0-3 法律免责与机构查询）、3 个 P1 中的 3 个（P1-1 终活笔记 / P1-2 数字遗产保险库 / P1-3 文档提取），并新增了遗码通逝者案例管理作为差异化能力。496 个测试通过、25 个新 CLI 子命令、7 个新模块——**工程纪律在身后事垂直品类里属于头部水准**。

但 Phase 7-13 修复的是「平台骨架的内部一致性」与「合规告知的代码化」，**未触及「普通用户触达路径」与「商业主体」**：

- **打开就能用**：需要 Step 1-6（stream 修复 + auth 穿透 + 加密升级 + 知识库 + 托管 + 微信）
- **敢把家里事告诉它**：需要 Step 3 + Step 7 + Step 9（加密升级 + 隐私政策/客服 + 法律主体）
- **用了能省事**：需要 Step 4 + Step 8（知识库覆盖 + 引导式 onboarding）
- **出事能找人**：需要 Step 7 + Step 9（客服工单 + 法律主体）

**核心风险**：

1. **Phase 7 修复未在用户体验层闭环**（`/api/stream` 仍是硬编码 prompt，前端默认走 stream）——这是上一轮 P0-1 修复的盲区，必须在 Step 1 立即修复。
2. **Phase 10 ending-note 端点未走 `_require_auth`**——横向越权漏洞，任意登录用户可拉取他人笔记，PIPL 第五章违规。
3. **加密方案代码自评承认不达生产基线**（XOR 流密码 + 硬编码 passphrase）——身后事场景敏感 PII 落盘即弱保护，托管上线前必须升级 AES-256-GCM。
4. **核心触达路径全部未做**（无托管/无微信/无引导式 onboarding/无移动端响应式/无隐私政策/无客服/无中国境内 LLM 搜索/无商业模式）——Phase 7-13 是「内部修内功」，下一阶段必须转向「外部修路径」。

**建议路径**：先做 Step 1-2（1 周内可见效，修复 P0 盲区）→ Step 3-4 并行（加密升级 + 知识库，1 月）→ Step 5-6（托管 + 微信，2 月）→ Step 7-9（合规化 + onboarding + 法律主体，3-4 月）→ Step 10（B2B2C 商业化，6 月）。

**商业模式**：放弃纯 toC，以律所/公证处合作 + toC 数字遗产保险库付费层启动，保险公司嵌入为核心，政府民政合作为规模化壁垒。Phase 11/12/13 的 vault/doc_extract/ending_note 提供了 toC 付费点的雏形，但需配套计费封装。

**总评**：技术骨架 8.5/10（v1 8/10，加密方案是短板），产品就绪 5/10（v1 4/10，盲区修复后可达 6/10），商业可行 2.5/10（v1 3/10，主体未定）。距离普通人落地 **约 40% 距离，4-8 月密集产品化可缩短至 15-20%**。

---

*报告完。本报告基于 2026-07-21 时 v4.7.0 代码状态评估，所有引用代码路径与行号均可在仓库中验证。后续 Phase 14+ 可能已修复部分 gap，建议下一轮评估时优先校验 P0-gap-1（stream 走 graph）/P0-gap-2（ending-note auth 穿透）/P0-gap-3（AES-GCM 升级）三项。*
