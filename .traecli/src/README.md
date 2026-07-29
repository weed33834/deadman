# deadman - 源码

身后事多智能体引导平台的 Python 实现。不绑定任何厂商，适用于所有支持 agent 的平台。

统一品牌名：**deadman**

详见 [BRAND.md](../../BRAND.md)。

## 安装

```bash
cd deadman
pip install -e .
```

## 使用

### 启动 MCP Server

```bash
deadman-mcp-server
# 或
deadman mcp-server
```

### 运行评估

```bash
deadman eval -v
```

### 运行单次对话

```bash
deadman run "我爸在北京去世了，需要办什么手续？"
```

## 模块结构

```
deadman/
├── __init__.py            # 包入口
├── config.py              # 全局配置（环境变量）
├── types.py               # 核心数据类型
├── llm.py                 # LLM 客户端（多厂商）
├── rules_loader.py        # 规则加载器 + 规则校验器
├── cli.py                 # CLI 入口
├── agents_store.py        # 智能体定义加载（agents/*.md）
├── cost.py                # 成本核算
├── knowledge_store.py     # 知识库存储
├── logging_config.py      # structlog 配置
├── prompts.py             # Prompt 模板（Jinja2 降级）
├── repl.py                # 交互式 REPL
├── soul_loader.py         # 灵魂加载器
├── _cli_extensions/       # CLI 子命令扩展（phase7-16）
│
├── mcp_server/            # MCP Server（15 工具，13 个用 tool_auto 自动 schema）
│   ├── server.py          # 主服务 + 15 个工具
│   ├── cache.py           # 工具结果缓存
│   ├── gateway.py         # 6 层网关
│   ├── permissions.py     # 工具权限
│   └── signing.py         # 工具签名
│
├── a2a/                   # A2A v1.0/v1.2 协议（AgentCard + tasks/send）
├── alignment/             # 对齐训练（DPO / SFT / MoE 路由）
├── auth/                  # JWT 会话 + 用户存储
├── billing/               # 计费（计量 / 发票 / 订阅 / 配额路由）
├── compliance/            # 合规（AI 标注 / 审计 / 同意 / 数据驻留 / 留存 / 删除权）
├── cron/                  # 定时任务（croniter 表达式 + 调度器）
├── deadman_switch/        # 死亡开关（触发条件 + 动作）
├── debate/                # 辩论编排（多智能体投票）
├── decedent_id/           # 逝者身份登记
├── disclaimer/            # 免责声明
├── doc_extract/           # 文档抽取
├── ending_note/           # 结语（guide / store）
├── evaluation/            # 评估框架（三层判定 / 工具调用 / RAGAS / runner）
├── gateway/               # 多渠道网关（Telegram / WeChat）
├── governance/            # 治理（AI 红线 / 伦理委员会 / 模型卡 / 透明度）
├── hotlines/              # 热线查询
├── i18n/                  # 国际化（货币 / 法律适配 / 时区 / 翻译）
├── infrastructure/        # 基础设施
│   ├── circuit_breaker.py # 断路器
│   ├── credential_vault.py # 凭证保险柜（AES-256-GCM）
│   ├── durable_execution.py # 持久化执行
│   ├── feature_flags.py   # 特性开关
│   ├── multi_tenant.py    # 多租户
│   ├── prompt_versioning.py # Prompt 版本管理
│   ├── quota.py           # 配额
│   ├── rate_limiter.py    # 限流
│   ├── web_middleware.py  # Web 中间件
│   └── defense/           # 防御层（链路断路器 / PII / 缓存保护 / 高级检测）
├── institutions/          # 机构查询
├── knowledge/             # 知识图谱（LightRAG / Graphiti / 融合 / 信任 / 新鲜度）
├── marketplace/           # 技能市场（注册 / 评分 / 审核 / 沙箱）
├── memorial_writer/       # 悼词生成
├── memory/                # 分层记忆（working / episodic / semantic / procedural / vector）
├── multimodal/            # 多模态（OCR / Vision / ASR / TTS / ImageGen）
├── notification/          # 通知护栏
├── notification_letters/  # 通知信生成
├── observability/         # 可观测性（Tracer / Metrics / Drift / Replay / RootCause）
├── onboarding/            # 引导向导
├── orchestration/         # LangGraph 编排（graph / nodes / handoff / scratchpad / tot / react）
├── plan_score/            # 方案评分
├── reflexion/             # 反思重试
├── sandbox/               # 代码沙箱
├── security/              # 安全（审计 / 蜜罐 / JIT / 红队 / 内容沙箱）
├── selfcheck/             # SelfCheckGPT 数字类幻觉检测
├── support/               # 工单
├── tools/                 # 通用工具（web_search）
├── utils/                 # 工具函数（crypto / text_similarity）
├── vault/                 # Vault 存储
└── web/                   # Web 服务（server / schemas / rate_limiter / static）
```

各模块详细文档见 `docs/` 与各子包内的 `README.md`。

## 可选依赖

| 依赖 | 用途 | 安装 |
|------|------|------|
| langgraph | 编排底座 | `pip install langgraph` |
| opentelemetry | 分布式追踪 | `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp` |
| langfuse | 可观测性平台 | `pip install langfuse` |
| lightrag-hku | 知识图谱 | `pip install lightrag-hku` |
| graphiti-core | 时态记忆 | `pip install graphiti-core` |

所有可选依赖缺失时自动降级，不阻塞核心功能。
