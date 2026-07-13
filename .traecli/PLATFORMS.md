# 平台适配指南

> 本文件说明如何将本平台的智能体定义适配到国内外主流 AI 平台。本平台的智能体定义是**平台无关的**，各平台通过适配层转换。

## 平台无关的智能体定义格式

本平台的所有智能体使用统一的 Markdown + YAML frontmatter 格式：

```markdown
---
name: agent-name
description: |
    多行描述，说明何时触发
tools: Tool1, Tool2
disallowedTools: OptionalTool
model: optional
---

正文是 system prompt
```

这个格式能映射到所有主流平台。

## 各平台适配方式

### 1. TRAE（字节跳动）

**配置位置**：
- 项目级：`{project}/.trae/agents/{name}.md`
- 用户级：`~/.trae-cn/agents/{name}.md`（macOS/Linux）

**适配要点**：
- frontmatter 字段完全兼容（name/description/tools/disallowedTools/model/mcpServers）
- 子智能体通过 TRAE 的 Subagent 机制实现（SOLO Agent 自动按 description 匹配调用）
- 子智能体有独立上下文窗口
- 共享文件系统（rules/ + knowledge/）直接用 Read 工具访问

**子智能体实现**：
- TRAE 原生支持 Subagent，我们的 12 个私有子智能体直接放入 `.trae/agents/`
- 父智能体在 system prompt 中说明何时调用哪个子智能体
- TRAE 会按 description 自动匹配

**转介机制实现**：
- TRAE 无显式转介原语
- 通过 system prompt 指示智能体在检测到转介信号时，建议用户切换到另一个智能体
- 用户在 IDE 中手动切换，或通过 `@agent-name` 调用

### 2. 阿里通义/百炼

**配置位置**：百炼控制台 → 智能体管理

**适配要点**：
- 在百炼创建"智能体"对应我们的并列智能体
- 智能体的"系统提示词"= 我们的 system prompt 正文
- 智能体的"插件/工具"= 我们的 tools
- 共享文件系统需替换为百炼的"知识库"功能（上传 rules/ 和 knowledge/ 文件）

**子智能体实现**：
- 百炼支持"智能体编排"，可在工作流中调用其他智能体
- 我们的 12 个私有子智能体在百炼中创建为独立智能体
- 父智能体通过工作流节点调用子智能体

**转介机制实现**：
- 百炼的智能体编排支持"路由"节点
- 可配置条件路由：检测到法律关键词 → 转到 legal-advisor
- 或通过 system prompt 指示用户手动切换

**差异注意**：
- 百炼的知识库是向量检索，我们的 SCHEMA.md 格式文件需上传后建立索引
- 百炼的工具调用方式是 OpenAPI Spec，需把 tools 转换为 API 描述

### 3. 腾讯混元/元宝

**配置位置**：腾讯元宝/混元大模型平台 → 智能体创建

**适配要点**：
- 在元宝创建"智能体"对应我们的并列智能体
- "人设与回复逻辑"= 我们的 system prompt
- "插件"= 我们的 tools（元宝支持搜索、代码、绘图等内置插件）
- "知识库"= 上传 rules/ 和 knowledge/ 文件

**子智能体实现**：
- 元宝目前不支持原生子智能体调用
- 方案 A：把子智能体的能力内联到父智能体的 system prompt（增加 prompt 长度）
- 方案 B：通过元宝的"工作流"功能编排多个智能体
- 方案 C：用户手动切换智能体

**转介机制实现**：
- 元宝不支持智能体间自动转介
- 通过 system prompt 指示用户手动切换
- 可在回复中提供其他智能体的入口链接

**差异注意**：
- 元宝的知识库支持自动问答，我们的规则文件需适配为 Q&A 对或长文档
- 元宝的工具生态与 TRAE 不同，需替换平台特有工具

### 4. OpenAI Assistants API / Agents SDK

**配置位置**：OpenAI Platform → Assistants

**适配要点**：
- 创建 Assistant 对应我们的并列智能体
- Assistant 的 `instructions` = 我们的 system prompt
- Assistant 的 `tools` = function calling + retrieval + code_interpreter
- 共享文件系统替换为 Assistants 的 `file_search`（上传 rules/ 和 knowledge/ 文件）

**子智能体实现**（两种方案）：
- **Agents SDK 方案**（推荐）：用 OpenAI Agents SDK 的 `Handoff` 原语实现子智能体调用
  ```python
  # 伪代码示意
  legal_advisor = Agent(name="legal-advisor", instructions=...)
  death_aftercare = Agent(
      name="death-aftercare",
      instructions=...,
      handoffs=[legal_advisor, financial_analyst]
  )
  ```
- **Function calling 方案**：定义 `call_subagent(name, input)` 函数，主智能体调用时返回子智能体结果

**转介机制实现**：
- Agents SDK 的 Handoff 可实现智能体间转交
- 或通过 function calling 定义 `recommend_agent(name, context)` 函数
- 用户端接收转介建议后手动切换

**差异注意**：
- OpenAI 的 `file_search` 是向量检索，我们的结构化知识库需考虑检索效果
- OpenAI 的 tools 定义用 JSON Schema，需把我们的 tools 转换
- Assistants API 的 stateless 特性需配合 Threads 管理对话状态

### 5. Anthropic Claude（tool use + MCP）

**配置位置**：Claude API / Claude Desktop

**适配要点**：
- Claude 无"智能体"概念，通过 system prompt + tool use 实现
- system prompt = 我们的 system prompt 正文
- tools = 用 Claude 的 tool_use 格式定义
- 共享文件系统通过 MCP（Model Context Protocol）文件系统服务器访问

**子智能体实现**：
- 通过 tool use 定义 `invoke_subagent(name, input)` 工具
- Claude 调用该工具时，在服务端启动子智能体执行
- 子智能体结果作为 tool_result 返回

**转介机制实现**：
- 通过 tool use 定义 `recommend_agent(name, reason)` 工具
- Claude 调用该工具时，向用户展示转介建议
- 用户确认后切换上下文

**差异注意**：
- Claude 的 MCP 是开放协议，可连接文件系统/数据库/API
- 我们的 rules/ 和 knowledge/ 可通过 MCP filesystem server 直接访问
- Claude 的 tool use 是并行的，需注意子智能体调用的时序

### 6. 通用 function calling 平台

适用于任何支持 function calling 的 LLM 平台（如 Gemini、Mistral、Cohere 等）。

**适配要点**：
- system prompt = 我们的 system prompt 正文
- 定义以下通用工具：
  - `read_file(path)` → 读取 rules/ 和 knowledge/ 文件
  - `write_file(path, content)` → 写入知识库（仅限有权限的智能体）
  - `web_search(query)` → 搜索
  - `web_fetch(url)` → 抓取页面
  - `invoke_subagent(name, input)` → 调用私有子智能体
  - `recommend_agent(name, reason, context)` → 转介建议

**子智能体实现**：
- `invoke_subagent` 工具在服务端路由到对应智能体
- 子智能体在独立上下文执行
- 结果作为 tool_result 返回

**转介机制实现**：
- `recommend_agent` 工具返回转介建议
- 客户端展示建议，用户确认后切换
- 上下文通过工具参数传递

### 7. Google Vertex AI（Agent Builder）

**配置位置**：Google Cloud Console → Vertex AI Agent Builder

**适配要点**：
- 在 Agent Builder 创建"Agent"对应我们的并列智能体
- Agent 的"指令"（Instructions）= 我们的 system prompt 正文
- Agent 的"工具"（Tools）= function calling + Grounding with Google Search + Code Interpreter
- 共享文件系统替换为 Vertex AI Search（上传 rules/ 和 knowledge/ 文件到数据存储）
- Vertex AI 提供原生 Safety Settings（4 类安全过滤：仇恨/危险/骚扰/色情）

**子智能体实现**：
- 通过 function calling 定义 `invoke_subagent(name, input)` 工具
- 服务端路由到子智能体（子智能体也是独立的 Vertex AI Agent）
- 结果作为 function response 返回

**转介机制实现**：
- 通过 function calling 定义 `recommend_agent(name, reason, context)` 工具
- 或通过 Multi-Agent 编排（Vertex AI Agent Builder 的多智能体编排功能）

**与 TRAE 的差异**：
- Vertex AI 的 Safety Settings 是平台级硬过滤，我们的 rules/ 是软约束（system prompt 中遵守）——两者可叠加，但 Vertex AI 的过滤不可绕过
- Vertex AI Search 是向量检索 + 关键词混合，我们的 SCHEMA.md 格式文件需建立索引
- Vertex AI 的多智能体编排比 TRAE Subagent 更显式（需配置编排图）

### 8. AWS Bedrock（Agents + Action Groups + Knowledge Bases + Guardrails）

**配置位置**：AWS Console → Amazon Bedrock → Agents / Guardrails / Knowledge Bases

**适配要点**：
- 在 Bedrock Agents 创建"Agent"对应我们的并列智能体
- Agent 的"instructions" = 我们的 system prompt 正文
- Agent 的"Action Groups" = 我们的 tools（OpenAPI Spec 形式）
- 共享文件系统替换为 Bedrock Knowledge Bases（上传 rules/ 和 knowledge/ 到 S3，建立向量索引）
- Bedrock Guardrails 提供内容过滤（hate / insults / sexual / violence / misconduct / prompt attack）

**子智能体实现**：
- 通过 Action Groups 定义 `invoke_subagent(name, input)` 函数
- Lambda 函数路由到子智能体（子智能体也是独立的 Bedrock Agent）
- 结果作为 function response 返回
- 或通过 Bedrock 的 Multi-Agent 协作（若已开放）

**转介机制实现**：
- 通过 Action Groups 定义 `recommend_agent(name, reason, context)` 函数
- 客户端展示建议，用户确认后切换

**与 TRAE 的差异**：
- Bedrock 的 Guardrails 是平台级硬过滤，独立于 system prompt——配置一次即对所有 Agent 生效
- Bedrock 的 Knowledge Bases 需要 S3 + 向量数据库（如 OpenSearch Serverless）+ embedding model，配置较重
- Bedrock 的 Action Groups 必须用 OpenAPI Spec 定义，转换成本较高
- Bedrock 不支持 TRAE 那样的"按 description 自动匹配子智能体"，需显式编排

### 9. Coze（扣子，字节国内版）

**配置位置**：Coze 国内版 https://www.coze.cn → 创建 Bot

**适配要点**：
- 在 Coze 创建"Bot"对应我们的并列智能体
- Bot 的"人设与回复逻辑"（Persona & Prompt）= 我们的 system prompt
- Bot 的"插件"（Plugin）= 我们的 tools（Coze 支持搜索、代码、绘图等内置插件 + 自定义插件）
- 共享文件系统替换为 Coze 的"知识库"（上传 rules/ 和 knowledge/ 文件，自动分段与索引）
- Coze 支持"工作流"（Workflow）编排多步骤任务

**子智能体实现**：
- Coze 支持在工作流中调用其他 Bot（通过"Bot 节点"）
- 我们的 12 个私有子智能体在 Coze 中创建为独立 Bot（不发布到商店，仅工作流内部调用）
- 父 Bot 通过工作流节点调用子 Bot，结果作为工作流变量传递

**转介机制实现**：
- 通过工作流的"条件判断"节点实现路由：检测到法律关键词 → 转到 legal-advisor Bot
- 或通过 system prompt 指示用户手动切换

**与 TRAE 的差异**：
- Coze 是字节国内版，与 TRAE（字节海外版）同源但面向国内市场
- Coze 的知识库支持自动分段 + 问答对两种形式，我们的规则文件需适配
- Coze 的 Bot 可发布到飞书、抖音、微信等多端，TRAE 主要在 IDE 内运行
- Coze 的子 Bot 调用通过工作流显式编排，TRAE 通过 description 自动匹配

### 10. Dify

**配置位置**：Dify 平台 https://cloud.dify.ai 或自部署 → 创建应用

**适配要点**：
- 在 Dify 创建"Agent 应用"对应我们的并列智能体
- Agent 的"指令"（Instruction）= 我们的 system prompt
- Agent 的"工具"（Tools）= Dify 内置工具（搜索、代码、数学等）+ 自定义工具（OpenAPI Spec）
- 共享文件系统替换为 Dify 的"知识库"（Dataset，上传 rules/ 和 knowledge/ 文件，支持多种分段模式）
- Dify 支持"工作流"（Workflow）编排多步骤任务

**子智能体实现**：
- Dify 的工作流支持"Agent 节点"，可在工作流中调用其他 Agent 应用
- 我们的 12 个私有子智能体在 Dify 中创建为独立 Agent 应用（不发布，仅工作流内部调用）
- 父 Agent 通过工作流 Agent 节点调用子 Agent，结果作为工作流变量传递
- 或通过 function calling 定义 `invoke_subagent` 工具

**转介机制实现**：
- 通过工作流的"条件分支"节点实现路由
- 或通过 system prompt 指示用户手动切换应用
- Dify 的"标注回复"（Annotation）功能可缓存常见转介场景

**与 TRAE 的差异**：
- Dify 是开源平台，可自部署，数据自主性更强
- Dify 的知识库支持 Q&A 模式与全文模式，我们的规则文件需选择合适模式
- Dify 的工作流比 TRAE Subagent 更显式（需画流程图）
- Dify 不支持 TRAE 那样的"按 description 自动匹配子智能体"

### 11. 智谱 GLM（Assistant API + function calling）

**配置位置**：智谱开放平台 https://open.bigmodel.cn → 创建智能体

**适配要点**：
- 在智谱开放平台创建"智能体"对应我们的并列智能体
- 智能体的"提示词"= 我们的 system prompt
- 智能体的"能力"（function calling）= 我们的 tools（JSON Schema 定义）
- 共享文件系统通过智谱的"知识库"功能上传（rules/ 和 knowledge/ 文件）
- 智谱 GLM-4 系列模型支持 function calling 与检索增强（RAG）

**子智能体实现**：
- 通过 function calling 定义 `invoke_subagent(name, input)` 工具
- 服务端路由到子智能体（子智能体也是独立的智谱智能体）
- 结果作为 function response 返回

**转介机制实现**：
- 通过 function calling 定义 `recommend_agent(name, reason, context)` 工具
- 客户端展示建议，用户确认后切换

**与 TRAE 的差异**：
- 智谱 GLM 是国产大模型，中文场景表现优秀，适合国内身后事/医疗导航场景
- 智谱的智能体编排较轻量，无显式工作流画布（不如 Dify/Coze 灵活）
- 智谱的知识库是向量检索，我们的 SCHEMA.md 格式文件需建立索引
- 智谱不支持 TRAE 那样的"按 description 自动匹配子智能体"，需在 system prompt 中显式指示

### 12. 月之暗面 Kimi（Assistant + tool use）

**配置位置**：月之暗面开放平台 https://platform.moonshot.cn → 创建 Assistant

**适配要点**：
- 在月之暗面平台创建"Assistant"对应我们的并列智能体
- Assistant 的"instructions" = 我们的 system prompt
- Assistant 的"tools" = function calling（JSON Schema 定义）+ 内置工具（如 web search）
- 共享文件系统通过 Kimi 的 file upload 功能 + RAG 实现（上传 rules/ 和 knowledge/ 文件）
- Kimi 支持长上下文（200 万 tokens），可一次性加载全部 rules/ 与 knowledge/

**子智能体实现**：
- 通过 function calling 定义 `invoke_subagent(name, input)` 工具
- 服务端路由到子智能体（子智能体也是独立的 Kimi Assistant）
- 结果作为 function response 返回
- 凭借长上下文优势，子智能体可在单次调用中处理大量检索/分析任务

**转介机制实现**：
- 通过 function calling 定义 `recommend_agent(name, reason, context)` 工具
- 客户端展示建议，用户确认后切换

**与 TRAE 的差异**：
- Kimi 的长上下文优势适合加载完整的 rules/ + knowledge/，无需向量检索即可引用
- Kimi 的智能体编排较轻量，无显式工作流画布
- Kimi 的知识库是文件 + RAG，与智谱类似
- Kimi 不支持 TRAE 那样的"按 description 自动匹配子智能体"，需在 system prompt 中显式指示

### 13. MiniMax（Assistant + plugin）

**配置位置**：MiniMax 开放平台 https://platform.minimaxi.com → 创建 Assistant

**适配要点**：
- 在 MiniMax 平台创建"Assistant"对应我们的并列智能体
- Assistant 的"instructions" = 我们的 system prompt
- Assistant 的"plugins" = 我们的 tools（MiniMax 支持内置插件 + 自定义插件）
- 共享文件系统通过 MiniMax 的文件上传 + RAG 实现（上传 rules/ 和 knowledge/ 文件）
- MiniMax 支持 function calling 与多模态（文本/语音/视频）

**子智能体实现**：
- 通过 function calling 定义 `invoke_subagent(name, input)` 工具
- 服务端路由到子智能体（子智能体也是独立的 MiniMax Assistant）
- 结果作为 function response 返回

**转介机制实现**：
- 通过 function calling 定义 `recommend_agent(name, reason, context)` 工具
- 客户端展示建议，用户确认后切换

**与 TRAE 的差异**：
- MiniMax 的多模态能力适合医疗导航场景的语音交互（如临终患者语音咨询）
- MiniMax 的智能体编排较轻量，无显式工作流画布
- MiniMax 的插件生态与 TRAE 不同，需替换平台特有工具
- MiniMax 不支持 TRAE 那样的"按 description 自动匹配子智能体"，需在 system prompt 中显式指示

## 共享文件系统的平台适配

本平台的核心设计是**通过共享文件系统协作**（rules/ + knowledge/）。各平台适配方式：

| 平台 | 文件系统适配方式 |
|------|----------------|
| TRAE | 原生支持，Read 工具直接读 .traecli/ |
| 阿里百炼 | 上传到"知识库"，自动向量检索 |
| 腾讯元宝 | 上传到"知识库"，自动问答匹配 |
| OpenAI Assistants | 上传文件 + file_search 工具 |
| Anthropic Claude | MCP filesystem server |
| 通用 function calling | 服务端实现 read_file 工具 |
| Google Vertex AI | Vertex AI Search（数据存储 + 向量检索） |
| AWS Bedrock | Bedrock Knowledge Bases（S3 + 向量数据库 + embedding） |
| Coze | Coze 知识库（自动分段 + 索引） |
| Dify | Dify 知识库（Dataset，支持 Q&A 与全文模式） |
| 智谱 GLM | 智谱知识库（向量检索 + RAG） |
| 月之暗面 Kimi | file upload + RAG（长上下文可直接加载全文） |
| MiniMax | 文件上传 + RAG |

## 转介机制的跨平台一致性

无论哪个平台，转介的核心逻辑一致：
1. 智能体检测到转介信号（法律/财务/政策）
2. 智能体向用户提出转介建议
3. 用户确认或拒绝
4. 如确认，传递上下文摘要给目标智能体
5. 目标智能体接收并继续对话

差异仅在实现方式：
- TRAE：system prompt 指示 + 用户手动切换
- 百炼/元宝：工作流路由或手动切换
- OpenAI Agents SDK：Handoff 原语
- Claude：tool use
- 通用：function calling
- Vertex AI：function calling 或 Multi-Agent 编排
- AWS Bedrock：Action Groups + Lambda
- Coze/Dify：工作流条件分支
- 智谱/Kimi/MiniMax：function calling

## 智能体定义的跨平台迁移

从一个平台迁移到另一个平台时：
1. **system prompt**：直接复制（平台无关）
2. **tools**：转换为目标平台格式（JSON Schema / OpenAPI / MCP）
3. **rules/ 和 knowledge/**：上传到目标平台的知识库或文件系统
4. **子智能体**：按目标平台机制重新实现（Subagent / Handoff / tool use / 工作流）
5. **转介**：按目标平台机制实现（system prompt 指示 / 路由 / Handoff / tool use）

## A2A 协议的跨平台适配（v4.2 新增）

[A2A-Protocol.md](a2a/A2A-Protocol.md) 定义了跨厂商智能体互操作。各平台对 A2A 的支持程度不同：

| 平台 | A2A 支持 | 适配方式 |
|------|---------|---------|
| TRAE | 原生支持 | 直接用 A2A SDK，Agent Card 发布到发现服务 |
| OpenAI Agents SDK | 通过 Handoff + function | custom function 调 A2A endpoint |
| Anthropic Claude | 通过 tool use | tool 调 A2A endpoint |
| Google Vertex AI | 通过 Multi-Agent 编排 | 编排节点调 A2A |
| AWS Bedrock | 通过 Action Groups + Lambda | Lambda 调 A2A |
| Coze | 通过工作流 HTTP 节点 | HTTP 节点调 A2A endpoint |
| Dify | 通过 HTTP 节点 | HTTP 节点调 A2A endpoint |
| 阿里百炼/腾讯元宝/智谱/Kimi/MiniMax | 通过 function calling | 自定义 function 调 A2A |

**A2A 适配要点**：
1. 每个平台的 6 个智能体都发布 Agent Card（能力声明）到 A2A Registry
2. 外部转介时，先发现外部 agent，获用户数据共享同意，脱敏后调用
3. 外部返回结果必须校验 integrity_report
4. 不支持 A2A 的平台降级为：只支持内部转介，外部转介提示用户自行咨询

## DPO 对齐的跨平台适配（v4.2 新增）

[DPO-Alignment.md](alignment/DPO-Alignment.md) 定义了把规则从 prompt 内化到模型权重。各平台对微调的支持不同：

| 平台/模型 | 微调支持 | DPO 适配方式 |
|----------|---------|-------------|
| 开源模型（Qwen/Llama/GLM） | 完全支持 | TRL/DPO 训练，本地或云 GPU |
| 字节豆包 | 支持微调 | 字节火山引擎微调 API |
| 智谱 GLM | 支持微调 | 智谱微调 API |
| 百度文心 | 支持微调 | 百度千帆微调 API |
| OpenAI GPT-4 | 不支持微调 | 降级：强化 system prompt + few-shot（用 DPO 的 chosen 回答作为示例） |
| Anthropic Claude | 不支持微调 | 降级：Constitutional AI 式 prompt + few-shot |
| Google Gemini | 部分支持 | Vertex AI 微调（Gemini 1.5 支持微调） |

**DPO 降级策略**（不支持微调的平台）：
1. 把 DPO 训练数据中的 chosen 回答作为 few-shot 示例注入 prompt
2. 把 DPO 学到的"偏好规则"显式写进 system prompt（如"优先拒绝编造数字，即使被催促"）
3. 用 LLM-as-Judge 在线评估，对 rejected 风格的回答实时拦截

## LangGraph 编排的跨平台适配（v4.2 新增）

[LangGraph-Orchestration.md](orchestration/LangGraph-Orchestration.md) 定义了编排底座，包含平台无关的映射规则：

| 现有概念 | LangGraph | TRAE | Coze | Dify | OpenAI |
|---------|-----------|------|------|------|--------|
| 6 并列智能体 | 6 个 node | 6 个 agent.md | 6 个 Bot | 6 个 Agent 节点 | 6 个 Assistant |
| 私有子智能体 | subgraph | subagent | 插件调用 | 子流程 | Handoff |
| 转介机制 | conditional edge | 推荐 agent | 意图路由 | 条件分支 | Handoff |
| 用户确认转介 | interrupt | 用户输入节点 | 确认卡片 | 人工审批 | 用户输入 |
| 规则校验 | pre/post hook | rules/ 加载 | 知识库约束 | LLM 前置约束 | instructions |

**LangGraph 适配要点**：
- LangGraph 是参考实现，不是强制运行时
- TRAE/Coze/Dify 等平台可用自己的可视化编排或 SDK 实现同样的映射
- 核心是映射规则（转介→条件路由、子智能体→子流程），不是绑定 LangGraph

## 版本
- v3.0 新增 A2A 协议适配 + DPO 对齐适配 + LangGraph 编排跨平台映射（对应 v4.2 支撑设施）
- v2.0 新增 7 个平台适配（Google Vertex AI / AWS Bedrock / Coze / Dify / 智谱 GLM / 月之暗面 Kimi / MiniMax），修复"8 个私有子智能体"为"12 个私有子智能体"
- v1.0 初始版本，覆盖 TRAE/阿里/腾讯/OpenAI/Anthropic/通用
