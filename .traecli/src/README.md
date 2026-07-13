# Legacy / 死者为大 / 終活 - 源码

通用身后事多智能体平台的 Python 实现。不绑定任何厂商，适用于所有支持 agent 的平台。

三语品牌名：
- 中文：**死者为大**
- 英文：**Legacy**
- 日文：**終活**（しゅうかつ）

详见 [BRAND.md](../BRAND.md)。

## 安装

```bash
cd .traecli/src
pip install -e .
```

## 使用

### 启动 MCP Server

```bash
legacy-mcp-server
# 或
legacy mcp-server
```

### 运行评估

```bash
legacy eval -v
```

### 运行单次对话

```bash
legacy run "我爸在北京去世了，需要办什么手续？"
```

## 模块结构

```
legacy/
├── __init__.py            # 包入口
├── config.py              # 全局配置（环境变量）
├── types.py               # 核心数据类型
├── llm.py                 # LLM 客户端（多厂商）
├── rules_loader.py        # 规则加载器 + 规则校验器
├── cli.py                 # CLI 入口
├── mcp_server/            # MCP Server（11 工具）
│   ├── __init__.py
│   └── server.py
├── orchestration/         # LangGraph 编排
│   ├── __init__.py
│   ├── state.py           # ConversationState
│   ├── graph.py           # 主 Graph + SequentialExecutor 降级
│   └── nodes.py           # 8 个节点 + 3 个路由
├── memory/                # 分层记忆
│   ├── __init__.py
│   ├── working.py         # 工作记忆
│   ├── episodic.py        # 情景记忆
│   ├── semantic.py        # 语义记忆（含矛盾检测）
│   ├── procedural.py      # 程序记忆
│   └── manager.py         # MemoryManager
├── reflexion/             # 反思重试
│   ├── __init__.py
│   └── engine.py          # ReflexionEngine
├── selfcheck/             # SelfCheckGPT 数字类幻觉检测
│   ├── __init__.py
│   └── checker.py
├── evaluation/            # 评估框架
│   ├── __init__.py
│   ├── three_layer.py     # 三层判定（正则→关键词→LLM）
│   ├── tool_calls.py      # 工具调用序列校验
│   └── runner.py          # 评估运行器
└── observability/         # 可观测性
    ├── __init__.py
    ├── tracer.py          # Tracer（11 类 span，OTel 降级）
    └── metrics.py         # MetricsCollector（11 大类指标）
```

## 可选依赖

| 依赖 | 用途 | 安装 |
|------|------|------|
| langgraph | 编排底座 | `pip install langgraph` |
| opentelemetry | 分布式追踪 | `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp` |
| langfuse | 可观测性平台 | `pip install langfuse` |
| lightrag-hku | 知识图谱 | `pip install lightrag-hku` |
| graphiti-core | 时态记忆 | `pip install graphiti-core` |

所有可选依赖缺失时自动降级，不阻塞核心功能。
