# 部署指南

## 部署模式

| 模式 | 适用场景 | 命令 |
|------|---------|------|
| 本地开发 | 开发调试 | `pip install -e .` + `deadman mcp-server` |
| Docker 单容器 | 小规模部署 | `docker run` |
| Docker Compose | 生产部署（含可观测性） | `docker compose --profile full up` |
| K8s | 大规模集群 | （待补充 Helm chart） |

## Docker 部署

### 构建镜像

```bash
docker build -t deadman:latest .
```

### 运行（MCP Server 模式）

```bash
docker run -d \
  --name deadman \
  -p 8000:8000 \
  -e LLM_API_KEY=sk-xxx \
  -e LLM_MODEL=gpt-4o \
  -e LLM_PROVIDER=openai \
  -v $(pwd)/data:/app/data \
  deadman:latest
```

### 运行模式切换

```bash
# MCP Server（默认，端口 8000）
docker run -p 8000:8000 deadman mcp-server

# Web UI（对话 + 运维看板 + 测试中心，端口 8002）
docker run -p 8002:8002 deadman web-server

# 运行评估
docker run deadman eval

# 单次对话
docker run deadman run "你的问题"
```

**三种服务端口**：
| 端口 | 服务 | 启动方式 |
|------|------|---------|
| 8000 | MCP Server（JSON-RPC） | `deadman mcp-server` 或 `docker run ... mcp-server` |
| 8001 | A2A Server（JSON-RPC 2.0） | `deadman-a2a-server` |
| 8002 | Web UI（HTTP + SSE） | `deadman-web-server` 或 `docker run ... web-server` |

### 健康检查

```bash
curl http://localhost:8000/health
# 或
docker exec deadman python /app/docker/healthcheck.py
```

## Docker Compose 全量部署

含 Neo4j（Graphiti 时态记忆）+ Langfuse（可观测性）+ OTel Collector。

### 1. 创建 .env 文件

```bash
cat > .env << 'EOF'
LLM_API_KEY=sk-xxx
LLM_MODEL=gpt-4o
LLM_PROVIDER=openai
LANGFUSE_SECRET_KEY=sk-lf-xxx
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
NEO4J_PASSWORD=your-password
EOF
```

### 2. 启动全量服务

```bash
docker compose --profile full up -d
```

### 3. 服务列表

| 服务 | 端口 | 用途 |
|------|------|------|
| deadman | 8000 | MCP Server |
| neo4j | 7687, 7474 | Graphiti 时态记忆 |
| langfuse | 3000 | 可观测性平台 |
| postgres | 5432 | Langfuse 数据库 |
| otel-collector | 4317, 4318 | OTel 中转 |

### 4. 按需启动

```bash
# 只启动 deadman
docker compose up -d deadman

# 启动 deadman + Graphiti
docker compose --profile graphiti up -d

# 启动 deadman + 可观测性
docker compose --profile observability up -d
```

## 环境变量参考

### 必需

| 变量 | 说明 | 默认值 |
|------|------|--------|
| LLM_API_KEY | LLM API 密钥 | （空） |
| LLM_PROVIDER | LLM 厂商 | openai |
| LLM_MODEL | 模型名 | gpt-4o |

### 可选 - MCP Server

| 变量 | 说明 | 默认值 |
|------|------|--------|
| MCP_SERVER_HOST | 监听地址 | 127.0.0.1 |
| MCP_SERVER_PORT | 监听端口 | 8000 |

### 可选 - 可观测性

| 变量 | 说明 | 默认值 |
|------|------|--------|
| OTEL_ENDPOINT | OTel 端点 | http://localhost:4317 |
| LANGFUSE_HOST | Langfuse 地址 | http://localhost:3000 |
| LANGFUSE_SECRET_KEY | Langfuse 密钥 | （空） |
| LANGFUSE_PUBLIC_KEY | Langfuse 公钥 | （空） |

### 可选 - 记忆与知识图谱

| 变量 | 说明 | 默认值 |
|------|------|--------|
| MEMORY_MAX_TURNS | 工作记忆轮数 | 10 |
| LIGHTRAG_ENABLED | 启用 LightRAG | false |
| GRAPHITI_ENABLED | 启用 Graphiti | false |
| GRAPHITI_NEO4J_URI | Neo4j 地址 | bolt://localhost:7687 |
| GRAPHITI_NEO4J_USER | Neo4j 用户 | neo4j |
| GRAPHITI_NEO4J_PASSWORD | Neo4j 密码 | （空） |

### 可选 - 评估与韧性

| 变量 | 说明 | 默认值 |
|------|------|--------|
| JUDGE_MODELS | LLM-as-Judge 模型 | gpt-4o,claude-3-5-sonnet,glm-4.6 |
| REFLEXION_MAX_RETRIES | 反思重试次数 | 3 |
| SELFCHECK_SAMPLE_COUNT | SelfCheckGPT 采样次数 | 5 |

### 可选 - A2A

| 变量 | 说明 | 默认值 |
|------|------|--------|
| A2A_REGISTRY_URL | A2A 注册中心 | https://a2a-registry.example.com |
| A2A_SELF_AGENT_ID | 本机 agent ID | deadman |

## CI/CD

项目配置了精简的 GitHub Actions（仅保留必要项）：

- **tests.yml**：push 到 main 或提交 PR 时自动跑 pytest（Python 3.12），无需任何 Secrets
- 不配置 release.yml / Docker 自动构建 / Dependabot / 自动合并 / 机器人

> 本项目两仓平等维护（均为各自平台的主仓库，非镜像关系）：
> - GitHub: https://github.com/weed33834/deadman（国际镜像 + CI）
> - GitCode: https://gitcode.com/badhope/deadman（国内主仓库）
>
> 不再使用镜像同步流水线，两仓通过本地 `sync.sh` 手动同步推送。

## 安全注意事项

1. **密钥不进仓库**：.gitignore 已排除 .env / *.key / credentials/
2. **Docker 非 root 运行**：镜像用 `deadman` 用户
3. **PII 脱敏**：记忆层自动脱敏 identifier/name/phone/address
4. **A2A 数据脱敏**：外部调用出口自动脱敏 PII
5. **用户同意**：A2A 外部调用必须获用户同意

## 故障排查

### MCP Server 无法启动

```bash
# 检查端口占用
lsof -i :8000

# 检查环境变量
docker exec deadman env | grep LLM

# 查看日志
docker logs deadman
```

### LLM 调用失败

```bash
# 测试 LLM 连通性
docker exec deadman python -c "
from deadman.llm import LLMClient
import asyncio
c = LLMClient()
print(asyncio.run(c.chat([{'role':'user','content':'hi'}])))
"
```

### 测试失败

```bash
python -m pytest .traecli/src/tests/ -v --tb=long
```
