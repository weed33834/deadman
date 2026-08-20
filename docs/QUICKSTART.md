# 快速开始

## 环境要求

- Python 3.10 – 3.13（CI 锁定 3.12；3.14 尚未验证，依赖链可能不兼容）

## 安装

```bash
git clone https://github.com/weed33834/deadman.git
# 或国内镜像：git clone https://gitcode.com/badhope/deadman.git
cd deadman
pip install -e .          # 运行时依赖
pip install -e .[dev]     # 含 pytest / pytest-asyncio / ruff，跑测试与 lint 必装
```

## 配置 LLM

```bash
# OpenAI
export LLM_PROVIDER=openai
export LLM_API_KEY=sk-xxx
export LLM_MODEL=gpt-4o

# Anthropic
export LLM_PROVIDER=anthropic
export LLM_API_KEY=sk-ant-xxx
export LLM_MODEL=claude-3-5-sonnet

# 智谱
export LLM_PROVIDER=zhipu
export LLM_API_KEY=xxx
export LLM_MODEL=glm-4.6
export LLM_BASE_URL=https://open.bigmodel.cn/api/paas
```

本地推理（Ollama / vLLM / llama.cpp）无需 API key，走 OpenAI 兼容接口：

```bash
export LLM_PROVIDER=ollama
export LLM_MODEL=qwen2.5:7b
export LLM_BASE_URL=http://localhost:11434/v1
```

## 使用方式

### CLI 单次对话

```bash
deadman run "我爸在北京去世了，需要办什么手续？"
```

### MCP Server

供智能体平台调用。默认监听 `127.0.0.1:8000`，在 TRAE / Coze / Dify 等平台配置 MCP endpoint 指向 `http://localhost:8000` 即可。

```bash
deadman mcp-server
```

### Web UI

对话界面与运维看板，默认监听 `0.0.0.0:8002`，浏览器打开 `http://localhost:8002`。

```bash
deadman-web-server
```

四个页签：

- **对话** —— 六个智能体可切换，SSE 流式响应
- **运维看板** —— 各领域反馈闭环状态、记忆分层条目数、部署工件校验
- **测试中心** —— 分领域运行诊断命令（LLM / 提示词 / 规则 / 智能体 / 知识库 / MCP / 可观测 / 记忆 / A2A / 部署 / 反思 / 技能），查看延迟与可用性
- **资源列表** —— 智能体与 MCP 工具清单

### Docker

```bash
docker build -t deadman .

# MCP Server
docker run -p 8000:8000 -e LLM_API_KEY=sk-xxx deadman

# Web UI
docker run -p 8002:8002 -e LLM_API_KEY=sk-xxx deadman web-server
```

## 测试

```bash
pytest -v
```

## 评估

```bash
deadman eval -v
```

## 核心概念

### 智能体团队

六个并列智能体，各司其职：

| 智能体 | 职责 |
|--------|------|
| death-aftercare | 身后事全流程引导（主入口） |
| legal-advisor | 法律咨询（继承 / 遗嘱） |
| financial-analyst | 财务分析（资产 / 税务） |
| medical-guide | 医疗指引（死亡证明 / 医保） |
| cross-border-specialist | 跨境死亡处理 |
| policy-researcher | 政策研究 |

### 规则优先级链

L0 至 L8 共九层，前者优先级高于后者：

```
L0 safety           安全（心理危机识别）
L1 integrity        诚信（不编造）
L2 input-guardrails 输入防护
L3 compliance       合规
L4 risk-tier        风险分级
L5 transparency     透明（AI 身份）
L6 accountability   问责
L7 retrieval        检索防护
L8 tone             语气
```

### MCP 工具

共十三个内置工具：

```
query_knowledge       查询地域知识库
web_search            联网搜索
read_file             读取文件
write_file            写入文件
invoke_subagent       调用子智能体
check_integrity       事实复核 + SelfCheckGPT
check_rules           规则校验
query_memory          分层记忆查询
initiate_debate       发起辩论
call_external_agent   A2A 外部调用
execute_reflexion     反思重试
init_transfer         智能体转介
report_incident       上报安全事件
```

## 下一步

- [部署指南](DEPLOYMENT.md) —— 生产环境部署
- [平台适配](../PLATFORMS.md) —— 各平台接入说明
- [品牌说明](../BRAND.md) —— 品牌名
