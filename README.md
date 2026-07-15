# Legacy / 死者为大 / 終活

身后事多智能体平台。不绑定任何厂商，适用于所有支持 agent 的平台。

三语品牌名：**Legacy**（英）/ **死者为大**（中）/ **終活**（日）

## 仓库地址

本项目三仓平等维护（均为各自平台的主仓库，非镜像关系），任一仓库均可 clone：

| 平台 | 地址 |
|------|------|
| GitHub | https://github.com/bad-hope/legacy-aftercare |
| GitCode | https://gitcode.com/badhope/legacy-aftercare |
| Gitee | https://gitee.com/badhope/legacy-aftercare |

## 快速开始

### 安装

```bash
cd legacy-aftercare
pip install -e .
```

### 配置环境变量

```bash
export LLM_API_KEY="your-api-key"
export LLM_MODEL="gpt-4o"
export LLM_PROVIDER="openai"
```

### 运行

平台提供三种入口，按需选择：

```bash
# MCP Server —— 供智能体平台调用（JSON-RPC，端口 8000）
legacy mcp-server

# Web UI —— 对话界面与运维看板（端口 8002）
legacy-web-server

# CLI 单次对话
legacy run "我爸在北京去世了，需要办什么手续？"

# 评估套件
legacy eval -v
```

Web UI（`http://localhost:8002`）包含四个部分：

- **对话** —— 六个智能体可切换，支持 SSE 流式响应
- **运维看板** —— 各领域反馈闭环状态、记忆分层条目数、部署工件校验
- **测试中心** —— 分领域运行诊断命令，查看延迟与可用性
- **资源列表** —— 智能体与 MCP 工具清单

### Docker 部署

```bash
docker build -t legacy-aftercare .

# MCP Server
docker run -p 8000:8000 -e LLM_API_KEY=sk-xxx legacy-aftercare

# Web UI
docker run -p 8002:8002 -e LLM_API_KEY=sk-xxx legacy-aftercare web-server
```

全量部署（含 Neo4j / Langfuse / OTel Collector）：

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
    ├── agents/           # 智能体定义（6 并列 + 子智能体 + 机制）
    ├── rules/            # 规则文件（L0-L8 优先级链）
    ├── knowledge/        # 地域知识库 + 知识图谱
    ├── skills/           # 技能定义
    ├── tests/            # 测试 case
    └── src/legacy/       # Python 实现
        ├── web/          # Web UI 与 API
        ├── mcp_server/   # MCP Server（13 工具）
        ├── a2a/          # A2A 协议
        ├── memory/       # 四层记忆
        └── ...           # 其余模块
```

## 文档

- [品牌说明](BRAND.md)
- [变更日志](CHANGELOG.md)
- [平台适配](PLATFORMS.md)
- [源码 README](.traecli/src/README.md)
- [快速开始](docs/QUICKSTART.md)
- [部署指南](docs/DEPLOYMENT.md)
