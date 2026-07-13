# 快速开始

## 5 分钟上手

### 1. 安装

```bash
git clone https://github.com/MS33834/legacy-aftercare.git
cd legacy-aftercare/.traecli/src
pip install -e .
```

### 2. 配置 LLM

```bash
# OpenAI
export LLM_PROVIDER=openai
export LLM_API_KEY=sk-xxx
export LLM_MODEL=gpt-4o

# 或 Anthropic
export LLM_PROVIDER=anthropic
export LLM_API_KEY=sk-ant-xxx
export LLM_MODEL=claude-3-5-sonnet

# 或 智谱
export LLM_PROVIDER=zhipu
export LLM_API_KEY=xxx
export LLM_MODEL=glm-4.6
export LLM_BASE_URL=https://open.bigmodel.cn/api/paas
```

### 3. 三种使用方式

#### 方式 A：CLI 单次对话

```bash
legacy run "我爸在北京去世了，需要办什么手续？"
```

#### 方式 B：MCP Server（给智能体平台调用）

```bash
legacy mcp-server
# 默认监听 127.0.0.1:8000
# 在 TRAE/Coze/Dify 等平台配置 MCP endpoint 指向 http://localhost:8000
```

#### 方式 C：Docker

```bash
cd .traecli
docker build -t legacy-aftercare .
docker run -p 8000:8000 -e LLM_API_KEY=sk-xxx legacy-aftercare
```

### 4. 运行测试

```bash
cd .traecli/src
python -m pytest tests/ -v
```

### 5. 运行评估

```bash
legacy eval -v
```

## 核心概念

### 智能体团队（6 并列）

| 智能体 | 职责 |
|--------|------|
| death-aftercare | 身后事全流程引导（主入口） |
| legal-advisor | 法律咨询（继承/遗嘱） |
| financial-analyst | 财务分析（资产/税务） |
| medical-guide | 医疗指引（死亡证明/医保） |
| cross-border-specialist | 跨境死亡处理 |
| policy-researcher | 政策研究（深度搜索） |

### 规则优先级链（L0-L8）

```
L0 safety          > 安全（心理危机识别）
L1 integrity       > 诚信（不编造）
L2 input-guardrails > 输入防护
L3 compliance      > 合规
L4 risk-tier       > 风险分级
L5 transparency    > 透明（AI 身份）
L6 accountability  > 问责
L7 retrieval       > 检索防护
L8 tone            > 语气
```

### MCP 工具（11 个）

```
query_knowledge    - 查询地域知识库
web_search         - 联网搜索
read_file          - 读取文件
write_file         - 写入文件
invoke_subagent    - 调用子智能体
check_integrity    - 5 关事实复核 + SelfCheckGPT
check_rules        - 规则校验
query_memory       - 分层记忆查询
initiate_debate    - 发起辩论
call_external_agent - A2A 外部调用
execute_reflexion  - 反思重试
```

## 下一步

- [部署指南](DEPLOYMENT.md) - 生产环境部署
- [平台适配](../PLATFORMS.md) - 13 个平台适配
- [品牌说明](../BRAND.md) - 三语品牌名
