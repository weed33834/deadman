# 可观测性（Observability）

> 本目录定义平台的可观测性方案。借鉴 Langfuse（自部署）、OpenTelemetry GenAI Semantic Conventions、Arize Phoenix 的设计，为多智能体系统提供结构化 trace、指标采集、评估闭环。

## 核心目标

1. **看见智能体在干什么**——每次对话、每次工具调用、每次转介、每次规则触发都记录为 span
2. **量化智能体做得怎么样**——延迟、成本、规则违反率、工具调用准确率、转介完整率
3. **支持事故复盘**——accountability-framework 的事故记录从"摘要式"升级为"可回放 trace"
4. **支持 CI 回归**——trace 可反向构造测试集，线上 Case 自动沉淀为新 golden case
5. **数据合规**——自部署，数据不出本地，满足 PIPL/GDPR 要求

## 目录结构

```
observability/
├── README.md                    # 本文件
├── OTel-Integration-Guide.md    # OTel GenAI 接入指南
├── Span-Model.md                # 11 类 span 模型定义（v1.0 基础 6 类 + v1.1 新增 5 类）
├── Metrics.md                    # 指标体系（v1.0 基础 4 类 + v1.1 新增 7 类，共 11 大类 50+ 指标）
└── platforms/                    # 各平台适配
    ├── trae.md                   # TRAE 适配
    ├── langfuse.md               # Langfuse 自部署方案
    └── otel-conventions.md       # OTel GenAI 标准属性
```

## 设计原则

1. **不破坏 agent.md 驱动的核心**——可观测性是支撑设施，智能体本身的行为逻辑不变
2. **平台无关**——用 OTel 标准属性，13 个平台适配方式一致
3. **最小侵入**——智能体 system prompt 只加一行"运行时上报 trace"，其余由运行时层处理
4. **结构化优先**——所有 trace 用 JSONL，可机器解析，可导入 Langfuse/Phoenix/Jaeger
5. **合规优先**——PII 脱敏在 trace 写入前完成，原始对话内容按留存策略处理

## 与现有规则的关系

- **accountability-framework.md**：事故记录升级为结构化 trace
- **compliance-framework.md**：trace 数据遵循数据治理（最小化/留存期限/删除权）
- **integrity-framework.md**：trace 记录"5 关输出自检"的结果
- **retrieval-guardrails.md**：trace 记录知识库信任分级与来源
- **conflict-resolution.md**：trace 可视化优先级链裁决过程

## 版本
- v1.1 更新 span 数量描述（6→11 类）+ 指标数量描述（4→11 大类），对应 v4.2 支撑设施
- v1.0 初始可观测性方案
