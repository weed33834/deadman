# 变更日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)（SemVer）。
版本号从 `0.1.0` 起小步迭代，`0.x` 阶段保持向后兼容最小承诺。

## v0.3.0（2026-08）智能体完整性拼图：多模态工具化 + OpenAI 兼容端点

> 深度审计发现两类缺口：能力模块存在但未接入 agent 工具面（多模态五能力），
> 以及生态互通缺口（无 OpenAI 协议入口）。均以"外部标准/既有模块 + 薄适配"补齐。

### 多模态工具化（16 → 21 个工具）

- 新增 5 个工具：`ocr_extract` / `asr_transcribe` / `analyze_image`（READ_ONLY）、
  `text_to_speech` / `generate_image`（WRITE_ASYNC，返回 base64 产物）
- 能力全部复用既有 multimodal 模块（provider 懒加载+降级），零新能力代码
- ReAct 表新增 `multimodal` 统一入口（按 kwargs 键分发五能力）
- flag 关闭/依赖缺失 → ok=False envelope，不抛异常

### OpenAI 兼容端点 `/v1/chat/completions`

- 任何 OpenAI-SDK 客户端（Cherry Studio / LobeChat / OpenWebUI / Cursor 自定义模型）
  可直接把 deadman 当成一个"模型"接入
- 协议映射：messages 末条 user → 编排图 query；`model` 字段即智能体路由器
  （8 个已知名，未知回退默认）；system 中 `agent:<name>` 标记亦可路由
- 双模：非流式 JSON（含 deadman 扩展字段 degraded/risk_tier/disclaimer）+
  SSE chat.completion.chunk 流 + `[DONE]` 终止帧；认证复用 JWT 可选依赖

## v0.2.0（2026-08）CI 全绿 + 去重复造轮子 + 架构收敛

> 质量门禁从红转绿（tests + build 双 workflow，ubuntu/windows 矩阵），
> 系统性清除手写轮子与死代码：净删 1000+ 行重复实现。

### CI 转绿（质量门禁修复）

- 版本断言测试改引单一版本源 `_version`（升版不再失配）
- MCP 客户端子进程强制 `PYTHONIOENCODING=utf-8`（Windows GBK 中文乱码）
- conftest 顶层禁用测试限流（TestClient 共享进程内滑动窗口，全量跑必撞 429）
- Windows runner 环境型失败守卫（Docker read-only 容器 skip；亚毫秒 duration 放宽 ≥0）
- ruff 0.16 全量清零：未用导入 / UP038 / SIM105/118 / E402/E741；
  修复 `deep_research.py` F821 未定义名称真 bug

### 去重复造轮子（用成熟库）

- **删除手写 SequentialExecutor 模拟器**（约 -400 行）：langgraph 是硬依赖，
  模拟 StateGraph 的降级引擎生产不可达且语义分叉无人维护；`build_main_graph`
  收敛为单一 LangGraph 实现
- **ToolResultCache 内核换 `cachetools.TTLCache`**：删除手写 LRU+TTL 条目管理
- **消灭第 3 份手写令牌桶**：mcp_server/gateway 私有 `_TokenBucket` 平替为
  `infrastructure.rate_limiter.TokenBucket` 统一实现
- **HTML 实体清洗换 stdlib `html.unescape()`**：删除手写实体映射表
- **移除零使用的 `requests` 依赖**（HTTP 统一 httpx）；新增 `cachetools>=5.0`

### 能力拼图：浏览器自动化（第 16 个工具）

- 新增 `browser_automation` 工具：能力来自微软官方开源库 playwright，
  本仓仅写薄适配层（navigate/get_text/screenshot/click/fill 五动作）
- 安全边界：URL 仅 http/https、headless、超时上限、文本提取封顶 5 万字符、
  feature flag 默认关闭；未装依赖时降级提示不阻断
- 注册面：MCP server / permissions(WRITE_ASYNC) / ReAct 工具表；pyproject 增 browser extra

### 废弃 server 物理删除 + 打包随包分发

- 删除废弃 `web/server.py`（3665 行 stdlib 实现），10 个测试文件迁移至
  FastAPI TestClient 进程内直调；resources/admin 对旧单例的引用切到 services/chat
- rules/agents/knowledge/skills/prompts 五个数据目录移入 deadman 包内，
  wheel 全量打入（439 文件验证）；config 路径改包内解析
- 修复 agents_store 指向不存在目录的路径 bug（本地智能体加载 0 → 20）
- 补 `POST /api/whoami` 保持旧版 GET/POST 双方法兼容

## v0.1.1（2026-08）测试修复与环境验证

> 在 v0.1.0 基线上修复测试套件中的路径计算 bug，补全缺失依赖，
> 实现全量 3123 测试通过（0 failed / 0 skipped / 0 errors）。

### 修复

- **test_phase17_integration.py**：`parents[3]` → `parents[2]`，修复知识库目录路径多上一层导致 2 个测试被 skip 的 bug（`test_knowledge_freshness_scan_phase16_provinces` / `test_cli_knowledge_freshness_scan_phase16_files`）
- **test_ragas_evaluator.py**：`parent.parent.parent` → `parent.parent`，修复 case 文件路径多上一层导致 `test_load_existing_case` 被 skip 的 bug

### 环境验证

- 全量测试：3123 passed, 0 failed, 0 skipped, 0 errors（147s）
- 118 条 API 路由逐一手动验证：认证 / 结束笔记 / 保险库 / 死人开关 / 案件管理 / 信件生成 / 纪念文 / 热线 / 机构 / 沙箱绘图 / CLI 命令 / 智能体 / 技能 / 工具 / 可观测 / 合规 / 工单 / 前端页面——全部正常
- LLM 网关连通验证通过（newapi 聚合网关）

### 版本同步

- `pyproject.toml` / `_version.py` / `mcp_server/plugin.py` 统一为 `0.1.1`

## v0.1.0（2026-08）项目基线

> 首个规范化基线：将此前分散、无版本的 `6.x` 历史统一收敛为 pre-1.0 语义化版本，
> 重置提交历史为单一干净基线，统一作者身份，建立 GitHub 主仓 + GitCode/Gitee 镜像三端同步。

### 定位

To B 多租户 AI 平台，面向殡葬 / 保险 / 遗产服务机构：
案例管理、审计日志、知识库、团队 RBAC、授权与数据导出。
基于 FastAPI + LangGraph，支持 MCP Server/Client、RAG、语音、沙箱执行、i18n、IAM。

### 本次基线包含

- 对话优先的生前准备 / 身后办理 AI 引导（C 端叙事仍保留，作为 To B 之上层能力）
- 机构工作台：客户档案、案件 DB/文件双轨、状态机、授权码、数据导出（CSV/JSON/zip）
- 多租户：`resolve_tenant_path()` 租户数据隔离、per-tenant 派生加密
- 十大分层架构：LLM 适配器、RAG + 知识库、MCP、沙箱、语音（ASR/TTS）、文件解析、导出、i18n、IAM、可观测
- 工程卫生：去手写 bm25/限流/加密/JWT/cron，统一 `httpx`、`tenacity`、`limits`、`cryptography`、`PyJWT`、`croniter`
- 上一轮去重重构：新增 `utils/jsonio.py` 收敛约 50 处原子写样板；`db_retry` 改 tenacity；`local_llm`/`skill_manager` 统一 httpx；文本分词单一实现；凭证加密统一到 `utils.crypto`

### 健康状态

- 测试：重构相关测试集 401 passed；全项目 311 模块导入无错误
- 版本：`pyproject.toml` / `_version.py` / `mcp_server/plugin.py` 统一为 `0.1.0`
