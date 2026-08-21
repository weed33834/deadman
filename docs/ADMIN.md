# 管理控制台 / 语音 / MCP 客户端 / 文本处理 —— 使用文档

> 本文档对应 v5.4.0 补齐的能力：管理台（Admin Console）、语音输入输出、MCP 客户端、底层文本处理。
> 对照 agent-builder-skill 完整版（万能 Agent 构建器）10 层架构 + deep-spec 深度规格体系。

## 一、管理台（Admin Console）

**入口**：启动 FastAPI 入口后访问 `http://<host>:<port>/admin`（主站侧边栏「运维 → 管理台」也有入口）。

```bash
uvicorn deadman.web.app:app --host 0.0.0.0 --port 8002
```

### 资源化管理（真实增删改调）

所有资源持久化到 `~/.deadman/admin/*.json`，支持配置/测试/运行/审计四段式：

| 资源 | 面板 | 管理能力 | 关键接口 |
|------|------|----------|----------|
| 提示词 | 提示词 | 增删改（每次保存递增版本）/ AI 生成 5 动作 / 试跑 | `GET/POST /api/admin/prompts`、`GET/PUT/DELETE /prompts/{name}`、`POST /prompts/generate`、`POST /prompts/{name}/test` |
| Agent | Agent | 创建/删除 / 试跑 | `GET/POST /api/admin/agents`、`DELETE /agents/{id}`、`POST /agents/{id}/test` |
| 工具 | 工具 | 启停 / **TestRunner 试跑** / 审计 | `POST /api/admin/tools/test`、`GET /tools/runs` |
| 音色 | 音色 | 增删改 / 设默认 | `GET/POST/PUT/DELETE /api/admin/voices`、`POST /voices/{id}/set-default` |
| 模型 | 模型 | 运行时切换 / **连通性测试** | `GET/POST /api/admin/models`、`POST /models/test` |
| 外部 MCP | MCP 客户端 | 新增/连接/断开/删除 Server | `GET/POST /api/mcp/servers`、`POST|DELETE .../{name}` |
| 记忆/监控/评估 | 观测 | 只读看板 | `/api/admin/memory|monitoring|evaluation` |
| 设置 | 设置 | 编辑 env 子集（热加载 + 持久化 .env） | `GET/PUT /api/admin/settings` |
| 备份 | 备份 | 全量配置包导出 / 导入 | `GET /api/admin/backup/export`、`POST /backup/import` |

## 二、语音输入与输出

| 能力 | 接口 | 说明 |
|------|------|------|
| ASR 转写 | `POST /api/voice/transcribe` | multipart 上传音频 → `{text, confidence, language, provider}` |
| TTS 合成 | `GET /api/voice/speak?text=&voice_id=&rate=` | 返回音频流；未启用/失败结构化降级 |
| 状态 | `GET /api/voice/status` | ASR/TTS 能力状态 |

- 前端：主站聊天输入框 🎤 麦克风按钮（MediaRecorder 录音 → 上传 → 回填）。
- 配置：`DEADMAN_MULTIMODAL_ENABLED=1` + 任一 ASR provider 后转写生效；TTS 依赖 `multimodal` 可选依赖。
- 降级：TTS 失败前端回退浏览器 `speechSynthesis`；ASR 不可用提示改用文字。

## 三、MCP 客户端（接入外部 MCP Server）

平台可作客户端连接外部第三方 MCP Server，拉取其工具并注册进本地注册表（`ext_<server>_<tool>`），智能体可直接调用。

```bash
DEADMAN_MCP_CLIENTS='[{"name":"filesystem","transport":"stdio","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}]'
# 或管理台「MCP 客户端」面板添加（持久化到 ~/.deadman/mcp_clients.json）
```

- 传输：`stdio` / `http`（POST JSON-RPC `/mcp`）/ `sse`（需官方 mcp 包）。
- 实现：官方 `mcp.ClientSession` 优先 + 纯 asyncio JSON-RPC 降级；全局专用事件循环。
- 管理接口：`GET/POST /api/mcp/servers`、`POST /servers/{name}/connect|disconnect`、`DELETE /servers/{name}`、`POST /connect-all`。

## 四、底层文本处理（textproc）

`deadman/textproc/`（零必装依赖，jieba 可选，`pip install deadman[text]`）：

| 模块 | 功能 |
|------|------|
| `tokenize` | 中文分词（jieba 精确模式，缺省退化中英混合分词） |
| `clean` | 清洗管道（去 HTML/实体/emoji、全半角归一、去零宽字符）+ 停用词 + 断句 |
| `keywords` | **关键词提取：TF-IDF 基线 + TextRank 增强**，输出带权重 Top-N |
| `similarity` | 余弦相似度（词袋/向量）+ Jaccard |
| `bm25` | BM25 关键词检索（中文先分词） |
| `hybrid` | **BM25 + 向量 RRF 混合检索**，权重可配 |

API：`GET /api/text/status`、`POST /api/text/keywords`、`POST /api/text/analyze`、`GET /api/text/index`、`POST /api/text/search`。管理台「文本分析」面板可视化使用。
