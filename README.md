# Legacy / 死者为大 / 終活

通用身后事多智能体平台。不绑定任何厂商，适用于所有支持 agent 的平台。

三语品牌名：**Legacy**（英）/ **死者为大**（中）/ **終活**（日）

## 仓库地址

本项目三仓平等维护（均为各自平台的主仓库，非镜像关系），任一仓库均可 clone：

| 平台 | 地址 |
|------|------|
| GitHub | https://github.com/bad-hope/legacy-aftercare |
| GitCode | https://gitcode.com/badhope/legacy-aftercare |
| Gitee | https://gitee.com/badhope/legacy-aftercare |

## 快速开始

### 1. 安装

```bash
cd legacy-aftercare
pip install -e .
```

### 2. 配置环境变量

```bash
export LLM_API_KEY="your-api-key"
export LLM_MODEL="gpt-4o"
export LLM_PROVIDER="openai"
```

### 3. 运行

```bash
# 启动 MCP Server（给智能体平台调用）
legacy mcp-server

# 启动 Web UI（对话界面 + 运维看板 + 测试中心，端口 8002）
legacy-web-server
# 浏览器打开 http://localhost:8002

# 运行单次对话
legacy run "我爸在北京去世了，需要办什么手续？"

# 运行评估
legacy eval -v
```

**Web UI 四个页签**（端口 8002）：
- **对话**：6 智能体切换 + SSE 流式响应
- **运维看板**：13 领域健康汇总 + 记忆状态 + 部署工件
- **测试中心**：13 领域手动测试（一键运行 CLI 命令，真实反馈）
- **资源列表**：智能体 + MCP 工具清单

### 4. Docker 部署

```bash
docker build -t legacy-aftercare .

# MCP Server（默认，端口 8000）
docker run -p 8000:8000 -e LLM_API_KEY=sk-xxx legacy-aftercare

# Web UI（端口 8002）
docker run -p 8002:8002 -e LLM_API_KEY=sk-xxx legacy-aftercare web-server
```

### 5. 全量部署（含 Neo4j + Langfuse + OTel）

```bash
docker compose --profile full up -d
```

## 项目结构

```
legacy-aftercare/
├── README.md / CHANGELOG.md / pyproject.toml   # 项目入口
├── Dockerfile / docker-compose.yml             # 容器化
├── docs/                                       # 快速开始 + 部署指南
└── .traecli/                                   # 业务实现
    ├── agents/           # 22 个智能体定义（6 并列 + 12 子 + 3 机制 + TEAM）
    ├── rules/            # 14 个规则文件（L0-L8 优先级链）
    ├── knowledge/        # 地域知识库 + 知识图谱
    ├── skills/           # 技能定义
    ├── tests/            # 测试 case
    └── src/legacy/       # Python 实现（12 模块）
```

## 文档

- [品牌说明](BRAND.md)
- [变更日志](CHANGELOG.md)
- [平台适配](PLATFORMS.md)
- [源码 README](.traecli/src/README.md)
- [快速开始](docs/QUICKSTART.md)
- [部署指南](docs/DEPLOYMENT.md)
