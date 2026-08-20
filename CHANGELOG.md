# 变更日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)（SemVer）。
版本号从 `0.1.0` 起小步迭代，`0.x` 阶段保持向后兼容最小承诺。

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
