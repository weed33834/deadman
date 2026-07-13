# 变更日志

> 本文件记录身后事 + 医疗导航多智能体平台的版本变更。版本号遵循语义化版本（major.minor），日期采用 YYYY-MM 格式。

## v4.4（2026-07）P4 工程化 - 测试 + 容器化 + CI/CD

> 把可运行的代码变成可交付的工程：测试套件 + Docker 容器化 + CI/CD 流水线 + 部署文档。

### 测试套件（src/tests/）

- **11 个测试文件**，**255 个测试全部通过**
- 覆盖 12 个模块：types/rules_loader/memory/reflexion/selfcheck/evaluation/observability/orchestration/mcp_server
- LLM 调用全部用 mock，不依赖外部 API
- pytest fixtures 自动重置全局单例，保证用例隔离
- 覆盖率报告：`pytest --cov=legacy --cov-report=html`

### Docker 容器化（.traecli/）

- **Dockerfile**：多阶段构建（builder + runtime），基于 python:3.11-slim，非 root 用户，tini 作为 PID 1，HEALTHCHECK 配置
- **docker-compose.yml**：主服务 + 可选服务（Neo4j for Graphiti / Langfuse + Postgres for 可观测性 / OTel Collector），用 profiles 按需启动
- **docker/entrypoint.sh**：支持 mcp-server/eval/run 三种模式切换
- **docker/healthcheck.py**：独立健康检查脚本
- **.dockerignore**：排除测试/缓存/密钥

### CI/CD 流水线（.github/workflows/）

- **ci.yml**：push 到 main + PR 触发，lint（ruff）→ test（Python 3.10/3.11/3.12 矩阵）→ build（Docker）→ evaluate（允许失败），缓存 pip 依赖，上传覆盖率
- **sync-to-gitcode.yml**：push 到 main 后自动同步到 GitCode（用 GITCODE_TOKEN secret）
- **release.yml**：打 v* tag 时构建 Docker 镜像推送到 ghcr.io + 自动生成 GitHub Release

### 部署文档（docs/）

- **QUICKSTART.md**：5 分钟上手指南（安装/配置/三种使用方式/核心概念）
- **DEPLOYMENT.md**：完整部署指南（Docker/Compose/环境变量/CI/CD/安全/故障排查）
- **README.md**：更新为面向用户的快速入口

### 安全

- .gitignore 排除 .env / *.key / credentials/
- Docker 非 root 用户运行
- CI/CD 密钥全部用 GitHub Secrets，不硬编码
- 仓库文件中零密钥泄露（已验证）

## v4.3（2026-07）P3 编码落地 - 可运行的 Python 实现

> 把 P0/P1/P2 的方案文档变成可跑的 Python 代码。核心架构仍是 agent.md 驱动，代码层是支撑设施的参考实现。

### 新增 src/ 目录

完整 Python 包 `legacy`（PyPI 名 `legacy-aftercare`），可通过 `pip install -e .` 安装。

### 基础设施

- **`config.py`**：全局配置，全部通过环境变量加载（LLM/MCP/OTel/Langfuse/Memory/Reflexion/SelfCheck/LightRAG/Graphiti/A2A）
- **`types.py`**：核心数据类型（RiskTier/ExecutionMode/TaskState/TransferSummary/SubagentResult/RuleCheckResult/Source/ConfidenceLabel）
- **`llm.py`**：统一 LLM 客户端，支持 OpenAI/Anthropic/智谱，async chat/chat_json/sample_multiple
- **`rules_loader.py`**：规则加载器（L0-L8 优先级链 + 4 补充规则）+ 规则校验器（编造检测/危机检测/风险信号检测）
- **`cli.py`**：CLI 入口（mcp-server/eval/run 三个子命令）

### MCP Server（mcp_server/）

- **`server.py`**：FastMCP 风格，11 工具注册（query_knowledge/web_search/read_file/write_file/invoke_subagent/check_integrity/check_rules/query_memory/initiate_debate/call_external_agent/execute_reflexion）。stdio + http 双传输。所有可选依赖缺失时降级。

### 编排底座（orchestration/）

- **`state.py`**：ConversationState TypedDict（19 字段）
- **`graph.py`**：build_main_graph() + SequentialExecutor 降级模式（LangGraph 不可用时按顺序执行）
- **`nodes.py`**：8 个节点（input_guard/router/user_confirm/agent/rule_check/integrity_check/output_guard/respond）+ 3 个路由函数

### 分层记忆（memory/）

- **`working.py`**：WorkingMemory（最近 N 轮，溢出归档）
- **`episodic.py`**：EpisodicMemory（时间+语义双索引，内存模拟向量库）
- **`semantic.py`**：SemanticMemory（UserProfile + Fact，矛盾检测）
- **`procedural.py`**：ProceduralMemory（流程知识 + 用户进度）
- **`manager.py`**：MemoryManager（统一管理 4 层 + build_context_for_llm + PII 脱敏）

### 韧性机制（reflexion/）

- **`engine.py`**：ReflexionEngine（execute_with_reflexion + 10 种预定义调整策略 + LLM 反思 + trace span）

### 幻觉检测（selfcheck/）

- **`checker.py`**：SelfCheckChecker（6 种数字类正则 + 多次采样一致性 + 自适应采样）

### 评估框架（evaluation/）

- **`three_layer.py`**：三层判定（RegexChecker → KeywordChecker → LLMJudge 跨模型共识）
- **`tool_calls.py`**：工具调用序列校验（5 种校验类型 + required 三态 + 5 个评估指标）
- **`runner.py`**：CaseRunner（跑 YAML case + 综合判定）

### 可观测性（observability/）

- **`tracer.py`**：Tracer（11 类 span，OTel 降级为内存记录，trace_tool_span/trace_reflexion_span 辅助函数）
- **`metrics.py`**：MetricsCollector（11 大类 80+ 指标，内存聚合，get_dashboard）

### 设计原则

1. **可选依赖降级**：LangGraph/OTel/LightRAG/Graphiti/Langfuse 任一缺失时自动降级，不阻塞核心功能
2. **全部 async**：所有 I/O 操作都是 async 方法
3. **不修改方案文档**：代码层是方案文档的参考实现，方案文档保持不变
4. **不创建测试文件**：各 subagent 已做冒烟测试验证，正式测试待后续补充

## v4.2（2026-07）前沿对齐 - P2 编排/本体/记忆/互操作/协作/对齐

> 在 P1 评估与知识层之上补齐 P2 级能力：编排底座、跨域本体、分层记忆、跨厂商互操作、辩论协作、模型对齐。核心架构仍是 agent.md 驱动，本版补强运行时与模型层。

### 编排底座（orchestration/）

- **`LangGraph-Orchestration.md`**（P2-1）：把"agent.md + 6 并列智能体 + 转介机制"映射为可执行 StateGraph。ConversationState 状态对象（含转介/子智能体结果/规则校验/Reflexion/trace）；6 agent node + conditional edges（转介映射）+ subgraph（子智能体映射）+ rule_check node（L0-L8 优先级链）+ interrupt（用户确认转介）+ PostgresSaver checkpointer（跨会话续接）；平台无关映射表（TRAE/Coze/Dify/OpenAI/Anthropic/LangChain）；与 MCP/Reflexion/OTel/SelfCheckGPT 集成点

### 跨域本体（knowledge/）

- **`Cross-Domain-Ontology.md`**（P2-2）：统一身后事/医疗/法律/财务/跨境/政策 6 域本体。顶层本体（Thing→Entity/Event/Relation/Property）+ 跨域共享层（Person/Organization/Document/Location/Time/Money/Role）+ 6 领域层（每域含实体类型+属性+关系）；跨域关系总表（死亡事件→继承/保险/税务/账号注销等）；多语言标签表（中英日同义词归一，消除"死亡证明 vs 死亡证书 vs Death Certificate"混乱）；与 LightRAG/Graphiti/MCP query_knowledge 集成

### 分层记忆（agents/）

- **`Memory-Store.md`**（P2-3）：4 层记忆模型。Working Memory（工作记忆，最近 N 轮，溢出归档）+ Episodic Memory（情景记忆，时间+语义双索引，向量库+Graphiti）+ Semantic Memory（语义记忆，UserProfile + Fact，矛盾检测）+ Procedural Memory（程序记忆，流程知识+用户进度，可从用户反馈学习）；MemoryManager 统一管理，selective recall 而非全量塞入；与 LangGraph state 注入；与 Graphiti bi-temporal 同步；PII 脱敏 + GDPR/PIPL 数据保留策略

### 跨厂商互操作（a2a/）

- **`A2A-Protocol.md`**（P2-4）：Google A2A Protocol 适配。Agent Card（6 智能体能力声明，含 jurisdiction/input_schema/output_schema/integrity_guarantees）；Task Lifecycle（7 状态：submitted→received→in_progress→completed/failed/rejected）；AgentDiscovery（能力发现服务）；HybridTransferManager（内部转介走 LangGraph edge + 外部转介走 A2A）；A2ASecurityManager（OAuth2 + PII 脱敏 + 诚信报告交换）；MCP vs A2A 边界明确（MCP=工具，A2A=智能体）

### 辩论/投票（agents/）

- **`Debate-Voting.md`**（P2-5）：多智能体意见冲突的协作模式。3 轮辩论流程（Opening 陈述→Rebuttal 交叉质询→Closing 总结）；4 种投票策略（Majority/Weighted/ConfidenceWeighted/Consensus 2/3 阈值）；仲裁机制（debate-arbiter agent，平票时介入）；冲突检测（LLM 判断实质冲突 vs 表述差异）；适用场景表（跨域/法律适用/风险等级分歧启用，常规/事实查询不启用）；诚信约束（不编造法条/案例，投票必须给理由）

### 模型对齐（alignment/）

- **`DPO-Alignment.md`**（P2-6）：把规则从 prompt 内化到模型权重。偏好数据收集 6 来源（golden cases 生成对比/对抗测试结果/辩论胜败/Reflexion 重试前后/人工标注/生产日志）；PreferenceDataQualityChecker（5 项质量检查）；SFT + DPO 两阶段训练（TRL 实现，β=0.1）；评估（规则遵守率≥95% + 通用能力退化≤2%，用 MMLU/CMMLU/GSM8K/HumanEval/BBH）；持续迭代（4 种重训触发）；平台适配（开源模型支持微调，OpenAI/Anthropic 降级为强化 prompt + few-shot）

### 与现有架构的关系

| 层 | v4.1 状态 | v4.2 补强 | 变了吗 |
|----|---------|----------|--------|
| 智能体定义 | agents/*.md（19 文件） | + Memory-Store + Debate-Voting | ➕ 新增 2 |
| 规则 | rules/*.md（14 文件） | 不变 | ❌ |
| 知识库 | knowledge/（含 LightRAG/Graphiti） | + Cross-Domain-Ontology | ➕ 新增 1 |
| 可观测性 | observability/（4 文件） | 不变 | ❌ |
| 自动化评估 | tests/automated/（10 文件） | 不变 | ❌ |
| MCP 封装 | mcp_server/ | 不变 | ❌ |
| 编排底座 | 无 | + orchestration/LangGraph-Orchestration | ➕ 新增 1 |
| 跨厂商互操作 | 无 | + a2a/A2A-Protocol | ➕ 新增 1 |
| 模型对齐 | 无 | + alignment/DPO-Alignment | ➕ 新增 1 |
| 运行方式 | agent.md → 智能体 → 遵守规则 | 不变（LangGraph 是参考实现） | ❌ |

### 关于敏感词

按用户指示，测试中遇敏感词一律跳过。所有对抗 payload 用占位符机制（如 `[INJECTION_PAYLOAD: ...]`）写入文档，不入仓库，CI 时通过 secret 注入。详见 Adversarial-Testing.md 的 payload vault 章节。

## v4.1（2026-07）前沿对齐 - P1 评估/知识/韧性补强

> 在 P0 基础设施之上补齐 P1 级能力：评估三件套（语义判定 + 对抗测试 + 检索质量）、知识图谱与时态记忆、工具调用序列标注、数字类幻觉检测、反思重试机制。核心架构仍是 agent.md 驱动，本版只补强支撑设施层。

### 评估三件套（tests/automated/）

- **`LLM-as-Judge.md`**（P1-5）：G-Eval CoT 评审模板 + 跨模型共识（3 模型，67% 通过 / 50% 失败 / 其余人工复核）+ Pairwise Comparison（消除 position bias）+ Elo 排名（13 平台对比）+ 分层调用（正则→关键词→LLM）控制成本 ~$1.0/次 CI
- **`Adversarial-Testing.md`**（P1-6）：OWASP LLM Top 10 → 平台攻击向量映射，6 个攻击向量（跨语言注入/转介链注入/知识库投毒/递进越狱/PII 复述/工具越权）；Promptfoo + Garak + PyRIT 三工具分工；payload vault 机制（不入仓库，CI secret 注入）；ASR 阈值表（LLM01 <5% / LLM03 <5% / LLM06 =0% / LLM07 <10%）；每周全量 + PR 轻量版
- **`RAGAS-Evaluation.md`**（P1-7）：标准 4 维度（faithfulness≥0.95 / answer_relevancy≥0.85 / context_precision≥0.75 / context_recall≥0.85）+ 平台特化 5 维度（trust_level_consistency / freshness_check / single_source_detection / confidence_labeling_rate / pii_redaction_rate）；触发式评估（知识库更新/规则变更触发）；与 LLM-as-Judge 分工

### 知识图谱与时态记忆（knowledge/）

- **`LightRAG-Pilot.md`**（P1-8）：6 类实体（Regulation/Document/Authority/Procedure/Role/TimeLimit）+ 8 种关系（requires/issued_by/processed_at/depends_on/restricted_by/eligibility/time_bound/supersedes）；三检索模式（local/global/hybrid）；从 Markdown 提取三元组；与 MCP `query_knowledge` 集成（query_mode 参数）；多跳查询专项；每条关系记录 source_text 保诚信
- **`Temporal-Memory-Graphiti.md`**（P1-9）：bi-temporal model（valid_time + transaction_time）；3 类时态对象（PolicyFact/UserProgressEvent/KnowledgeVersion）；4 种时态查询（当前有效/历史事实/用户时间线/变更影响分析）；与 4 个规则/智能体集成（retrieval-guardrails/integrity-framework/death-aftercare-tracker/policy-researcher）；Neo4j 自部署；数据保留策略（policy_facts 永久 / user_events 7 年）

### 评估精度补强（tests/automated/）

- **`Expected-Tool-Calls.md` + 5 个 case YAML**（P1-10）：从"只评文本"升级为"文本 + 工具调用"双维度。expected_tool_calls 字段含 5 种校验类型（exact/contains/regex/non_empty/optional）+ required 三态（true/false/forbidden）；5 个评估指标（tool_selection_accuracy / argument_accuracy / order_match / unnecessary_calls / result_match_rate）。已改造 case-01/06/11/13/20
- **`SelfCheckGPT.md`**（P1-11）：数字类输出幻觉检测，补强 integrity-framework 第八章。6 种数字类 claim 正则提取（phone/days/money/percent/article/step_count）；多次采样（temp=0.3/0.5/0.7/0.4/0.6）；自适应采样（3 次→5 次）；一致性阈值（≥0.8 高 / 0.5-0.8 中 / <0.5 未知）；与 check_integrity MCP 工具集成；与 RAGAS faithfulness 互补

### 韧性机制（agents/）

- **`Reflexion-Mechanism.md`**（P1-12）：子智能体/工具/转介调用失败时的反思-调整-重试。Reflexion Engine 实现（反思-调整-重试流程）；3 类失败场景；预定义调整策略表（10 种失败模式快速路径）；与 Graphiti 记忆集成（跨会话学习）；MAX_RETRIES=3；评估目标 fallback 率降低 ≥20%

### 与现有架构的关系

| 层 | v4.0 状态 | v4.1 补强 | 变了吗 |
|----|---------|----------|--------|
| 智能体定义 | agents/*.md（18 文件） | + Reflexion-Mechanism.md | ➕ 新增 1 |
| 规则 | rules/*.md（14 文件） | 不变 | ❌ |
| 知识库 | knowledge/regions/ | + LightRAG-Pilot + Temporal-Memory-Graphiti | ➕ 新增 2 |
| 可观测性 | observability/（4 文件） | 不变 | ❌ |
| 自动化评估 | tests/automated/（5 文件 + 5 case） | + LLM-as-Judge + Adversarial-Testing + RAGAS + Expected-Tool-Calls + SelfCheckGPT；5 case YAML 加 expected_tool_calls | ➕ 新增 5，改造 5 |
| MCP 封装 | mcp_server/ | 不变 | ❌ |
| 运行方式 | agent.md → 智能体 → 遵守规则 | 不变 | ❌ |

### 关于敏感词

按用户指示，测试中遇敏感词一律跳过。所有对抗 payload 用占位符机制（如 `[INJECTION_PAYLOAD: ...]`）写入文档，不入仓库，CI 时通过 secret 注入。详见 Adversarial-Testing.md 的 payload vault 章节。

## v4.0（2026-07）前沿对齐 - P0 基础设施

> 基于前沿架构对比（多智能体/知识图谱/评估可观测性/安全对齐），补齐 P0 级基础设施。核心架构不变（agent.md 驱动），新增支撑设施层。

### 新增目录

- **`observability/`** 可观测性方案（4 文件）
  - `README.md`：方案总览
  - `Span-Model.md`：6 类 span 模型（root/agent/subagent/transfer/rule/tool）
  - `Metrics.md`：4 大类 20+ 指标体系
  - `OTel-Integration-Guide.md`：OTel GenAI 接入指南 + Langfuse 自部署方案
- **`tests/automated/`** 自动化评估（5 文件）
  - `README.md`：三层判定方案（正则黑名单 → 关键词必中 → LLM-as-judge）
  - `cases/case-01-no-fabrication.yaml`：诚信场景示例
  - `cases/case-06-psychological-crisis.yaml`：安全场景示例
  - `cases/case-11-transfer-to-legal.yaml`：转介场景示例
  - `cases/case-13-injection-defense.yaml`：防御场景示例（含 Promptfoo 对抗扩展）
  - `cases/case-20-cross-border.yaml`：跨团队场景示例（含 RAGAS 评估）
- **`mcp_server/`** MCP Server 封装方案
  - `README.md`：7 个标准 MCP 工具定义 + FastMCP 实现 + 部署方式

### 核心设计

1. **可观测性**：借鉴 Langfuse（自部署）+ OpenTelemetry GenAI conventions + Phoenix。6 类 span 覆盖智能体运行全过程，事故记录从摘要式升级为结构化 trace，数据不出本地满足 PIPL/GDPR。
2. **自动化评估**：借鉴 DeepEval（pytest 风格）+ RAGAS + SWE-bench + τ-bench。三层判定（正则→关键词→LLM-as-judge）平衡精度与成本，CI 自动跑 20 个 golden case，Promptfoo 扩展对抗变体。
3. **MCP 封装**：借鉴 Anthropic MCP（2024.11）+ FastMCP。7 个标准工具（check_rules/query_knowledge/init_transfer/accept_transfer/check_integrity/log_trace/report_incident），一次实现 13 平台复用。
4. **架构不变**：核心依然是 agent.md 驱动，可观测性/MCP/评估都是支撑设施层。

### 与现有架构的关系

| 层 | 现状 | v4.0 补强 | 变了吗 |
|----|------|----------|--------|
| 智能体定义 | agents/*.md | 不变 | ❌ |
| 规则 | rules/*.md | 不变 | ❌ |
| 知识库 | knowledge/regions/ | 不变 | ❌ |
| 运行方式 | agent.md → 智能体 → 遵守规则 | 不变 | ❌ |
| 可观测性 | 无 | observability/ | ➕ 新增 |
| 自动化评估 | 人工核对 | tests/automated/ | ➕ 新增 |
| 工具复用 | 13 平台各自适配 | mcp_server/ | ➕ 新增 |

## v3.1（2026-07）测试后修复

> 基于 11 个场景联调测试（10 通过 + 1 跳过）发现的 13 个问题，全部修复。

### P0 修复（阻塞性）

- **`agents/TEAM.md`** 新增"子智能体调用时机（硬约束）"章节：定义 6 个父智能体何时必须调用哪个子智能体，含信号表 + 调用目的 + 调用失败处理（修复问题 1+6，4 次发现的最严重问题）
- **`rules/special-populations-framework.md`** 心理危机资源补充来源标注：12320/12355/988 等号码全部标注官方来源，联动 integrity-framework 置信度标注（修复问题 3）

### P1 修复（高价值）

- **`rules/service-boundary-framework.md`** 新增"代理人/协助者场景"章节：5 类代问人层级表 + 信息引导 vs 代理决策区分 + 话术示例（修复问题 4）
- **`rules/integrity-framework.md`** 新增"七·补：安全优先时的延后质疑机制"：L0 触发时质疑延后但不取消，含恢复时机与示例（修复问题 8）
- **`agents/legal-advisor.md`** description 补充 20+ 法律争议识别信号：从 7 个扩到继承份额/债务清偿/医疗事故鉴定/房产过户/数字遗产/抚恤金分配/遗产管理人/遗赠扶养/代位继承/放弃继承效力/保险金继承等（修复问题 2）
- **`rules/special-populations-framework.md`** 未成年人分支新增"即时危险检测"：4 类即时危险信号表 + L0 优先处理（修复问题 10）
- **`knowledge/regions/SCHEMA.md`** 新增"医疗政策补充"区块：8 个医疗字段（医保体系/门诊特殊病种/异地就医/大病保险/商保理赔/医疗纠纷/临终关怀），供 medical-guide 团队使用（修复问题 7）

### P2 修复（优化）

- **转介摘要模板** 补"已完成事项"字段：TEAM.md + 6 个 agent 文件（death-aftercare/legal-advisor/financial-analyst/policy-researcher/cross-border-specialist/medical-guide）全部同步更新（修复问题 5）
- **`rules/multilingual-framework.md`** "多用户冲突"章节迁移至 service-boundary-framework 第四章，multilingual 保留跨代际沟通（修复问题 9）
- **`rules/input-guardrails.md`** 新增第八章"Few-shot 防御示例"：4 个组合攻击示例（角色扮演越狱+情感施压、逐步施压+指令覆盖、情感操纵+边界试探、间接注入），提高识别稳定性（修复问题 11）

### 关于场景 5 跳过

场景 5（Prompt Injection 防御）因测试时复述攻击原文触发敏感词而跳过。这是测试设计问题，非平台问题。场景 D 已部分覆盖注入防御。建议未来用 Promptfoo 等对抗性测试框架在沙箱跑完整测试集。

## v3.0（2026-07）

### 架构变更
- 新增第 5 个并列智能体：`cross-border-specialist`（跨境专家）——专精跨国身后事、领事认证、多国法律冲突
- 新增第 6 个并列智能体：`medical-guide`（医疗导航员）——跨团队，与身后事团队并列，共享 rules/ 与 knowledge/
- 新增 4 个私有子智能体：cross-border-specialist-consul / cross-border-specialist-conflict / medical-guide-hospital / medical-guide-insurance
- 私有子智能体总数从 8 个增至 **12 个**

### 知识库扩展
- 新增 `knowledge/regions/US/overview.md`（美国国家级总览）
- 新增 `knowledge/regions/US/california.md`（加州地区，9 阶段完整覆盖，含 Heggstad Petition、Small Estate Affidavit $184,500 门槛、社区财产州规则）
- 新增 `knowledge/regions/JP/overview.md`（日本国家级总览，9 阶段日本特色：戸籍/火葬/相続放棄）

### 测试扩展
- `tests/scenarios.md` 新增场景 6/7/8：
  - 场景 6：跨境身后事（cross-border-specialist 测试）
  - 场景 7：医疗导航（medical-guide 测试）
  - 场景 8：跨团队转介（双向测试，death-aftercare → medical-guide → legal-advisor）
- `tests/golden-cases.md` 新增 Case 16-20：
  - Case 16：跨团队转介（medical-guide → death-aftercare）
  - Case 17：子智能体调用（death-aftercare-tracker）
  - Case 18：知识库过期（retrieval-guardrails 时效校验）
  - Case 19：多信号叠加（integrity + risk-tier + safety，优先级链裁决）
  - Case 20：跨境场景（cross-border-specialist）

### 文档新增
- 新增 `README.md`：入门指南
- 新增 `CONTRIBUTING.md`：贡献指南
- 新增 `CHANGELOG.md`：本文件

### 平台适配扩展
- `PLATFORMS.md` 新增 7 个平台适配：
  - Google Vertex AI（Agent Builder + tools + memory + safety）
  - AWS Bedrock（Agents + Action Groups + Knowledge Bases + Guardrails）
  - Coze（扣子，字节国内版，plugin + workflow + knowledge）
  - Dify（workflow + dataset + annotation）
  - 智谱 GLM（Assistant API + function calling）
  - 月之暗面 Kimi（Assistant + tool use）
  - MiniMax（Assistant + plugin）
- 修复"8 个私有子智能体"为"12 个私有子智能体"

## v2.0（2026-07）

### 架构变更
- **废弃主-子委派（delegate）模式**
- 改为**并列架构 + 转介（recommend）机制**
- 4 个智能体（death-aftercare / legal-advisor / financial-analyst / policy-researcher）地位平等、并列面向用户
- 智能体之间通过转介协作，不是委派
- 转介时整理【转介摘要】传递上下文，用户自主决定

### 子智能体调整
- 各并列智能体保留自己的私有子智能体（共 8 个）
- 子智能体只服务于其父智能体，不直接面对用户
- 子智能体在独立上下文执行深度任务，结果以结构化报告返回

### 规则体系
- 共享层 `rules/` 全部 10 个规则文件——所有智能体共用，优先级链一致
- 新增 `rules/conflict-resolution.md`：定义优先级链 safety > integrity > input-guardrails > compliance > risk-tier > transparency > accountability > retrieval-guardrails > tone

### 知识库
- 沿用 `knowledge/regions/CN/` 中国知识库
- 沿用 `knowledge/regions/SCHEMA.md` 格式标准

## v1.0（2026-07）

### 初始版本
- 4 个智能体：death-aftercare / legal-advisor / financial-analyst / policy-researcher
- 4 个规则：safety-protocol / integrity-framework / compliance-framework / retrieval-guardrails
- 中国知识库：`knowledge/regions/CN/overview.md` + `knowledge/regions/CN/general.md`
- 主-子委派架构（后在 v2.0 废弃）
