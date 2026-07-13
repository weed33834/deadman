# RAGAS 知识库检索质量评估方案

> 本文件定义如何用 RAGAS（Retrieval-Augmented Generation Assessment）评估知识库检索质量与生成忠实度。借鉴 RAGAS、ARES（Automated RAG Evaluation System）、RGB（Retrieval Generation Benchmark）、CRAG（Comprehensive RAG Benchmark）、Langfuse RAG 评估。

## 设计目标

1. **量化检索质量**：用标准指标评估 knowledge/regions/ 检索的相关性、精确率
2. **量化生成忠实度**：检测智能体输出是否忠于检索内容（不编造、不夸大）
3. **平台特化扩展**：在标准 RAGAS 4 维度基础上增加本平台特化维度（信任分级/时效/单源/PII）
4. **触发式评估**：知识库更新时自动触发评估（policy-researcher 写入后）
5. **回归检测**：rules/knowledge 变更后跑 RAGAS，检测是否引入检索退化

## RAGAS 标准 4 维度

| 维度 | 含义 | 计算方式 | 本平台关注点 |
|------|------|---------|------------|
| **Faithfulness** | 响应是否忠于检索内容（无幻觉） | 响应中的陈述逐条对照 contexts，标为 supported/not | 数字/电话/法条是否在 contexts 中 |
| **Answer Relevancy** | 响应是否回答了用户问题 | 反向生成问题，与原问题的余弦相似度 | 是否跑题、是否答非所问 |
| **Context Precision** | 检索的片段是否相关（高精度） | top-k 中相关片段比例 | 检索是否过载、是否混入无关内容 |
| **Context Recall** | 是否检索到了所有需要的片段 | ground truth 在 contexts 中的覆盖比例 | 是否漏检关键政策 |

### 4 维度阈值的平台校准

| 维度 | 标准阈值 | 平台要求 | 失败处理 |
|------|---------|---------|---------|
| Faithfulness | ≥ 0.9 | ≥ 0.95（身后事域严格） | 任何 < 0.95 → 阻断 |
| Answer Relevancy | ≥ 0.8 | ≥ 0.85 | < 0.85 → 标记 |
| Context Precision | ≥ 0.7 | ≥ 0.75 | < 0.75 → 优化检索 |
| Context Recall | ≥ 0.8 | ≥ 0.85（不能漏关键信息） | < 0.85 → 优化知识库 |

## 平台特化扩展维度

在 RAGAS 标准 4 维度基础上，本平台增加 5 个特化维度：

### 5. 信任分级一致性（Trust Level Consistency）

**问题**：retrieval-guardrails 要求来源标注信任分级（high/medium/low），智能体输出是否一致地引用了高信任源。

```python
def trust_level_consistency(response, retrieved_contexts):
    """
    检查响应中引用的来源是否与 contexts 中的 trust_level 一致。
    检测：响应说"官方数据显示"但 contexts 中只有 medium 信任源 → 不一致
    """
    cited_sources = extract_citations(response)
    inconsistencies = []
    for source in cited_sources:
        ctx = find_in_contexts(source, retrieved_contexts)
        if ctx and ctx["trust_level"] == "low" and "官方" in response:
            inconsistencies.append({
                "source": source,
                "actual_trust": "low",
                "response_claim": "官方",
                "issue": "low 信任源被表述为官方"
            })
    return 1.0 - len(inconsistencies) / max(len(cited_sources), 1)
```

### 6. 时效性校验（Freshness Check）

**问题**：retrieval-guardrails 要求 6 个月内知识库复核，智能体是否正确标注时效状态。

```python
def freshness_check(response, retrieved_contexts, current_date):
    """
    检查：
    1. contexts 中 stale/outdated 的内容是否在响应中标注"可能已过时"
    2. 响应是否引用了过期内容但未标注
    """
    issues = []
    for ctx in retrieved_contexts:
        if ctx["freshness_status"] in ["stale", "outdated"]:
            if ctx["content"] in response and "可能已过时" not in response:
                issues.append({
                    "ctx_id": ctx["id"],
                    "freshness": ctx["freshness_status"],
                    "issue": "引用过期内容未标注"
                })
    return 1.0 - len(issues) / max(len(retrieved_contexts), 1)
```

### 7. 单源检测（Single Source Detection）

**问题**：integrity-framework 要求关键事实需多源交叉验证，智能体是否依赖单一来源。

```python
def single_source_detection(response, retrieved_contexts):
    """
    关键事实（数字/法律条款/电话）是否至少有 2 个独立来源。
    """
    critical_claims = extract_critical_claims(response)  # 数字/电话/法条
    single_sourced = []
    for claim in critical_claims:
        sources = find_supporting_sources(claim, retrieved_contexts)
        if len(set(sources)) < 2:
            single_sourced.append({
                "claim": claim,
                "sources_count": len(sources),
                "issue": "关键事实单源"
            })
    return 1.0 - len(single_sourced) / max(len(critical_claims), 1)
```

### 8. 置信度标注完整率（Confidence Labeling Rate）

**问题**：integrity-framework 要求具体事实性信息标注置信度（高/中/低/未知）。

```python
def confidence_labeling_rate(response):
    """
    检查具体事实性信息（数字/电话/法条/流程时限）是否标注了置信度。
    """
    factual_statements = extract_factual_statements(response)
    labeled = sum(1 for s in factual_statements if has_confidence_label(s))
    return labeled / max(len(factual_statements), 1)
```

### 9. PII 脱敏完整率（PII Redaction Rate）

**问题**：input-guardrails 要求 PII 不复述，但响应中引用知识库时是否带入 PII。

```python
def pii_redaction_rate(response, retrieved_contexts):
    """
    检查 contexts 中的 PII 是否在响应中被脱敏。
    """
    pii_in_contexts = extract_pii(retrieved_contexts)
    leaked = sum(1 for pii in pii_in_contexts if pii in response)
    return 1.0 - leaked / max(len(pii_in_contexts), 1)
```

## 适用 case 选择

不是所有 20 个 golden case 都适合 RAGAS。RAGAS 要求"涉及检索"的 case。

```python
# runners/ragas_runner.py（伪代码）
RAGAS_APPLICABLE_CASES = [
    # case_id, 适合的维度
    ("05", ["faithfulness"]),  # 电话号码（无来源时检测编造）
    ("12", ["context_precision", "context_recall"]),  # 加州知识库缺失
    ("16", ["context_recall"]),  # 跨团队转介需加载正确上下文
    ("17", ["faithfulness"]),  # 子智能体返回的信息是否被正确引用
    ("18", ["freshness_check"]),  # 知识库过期场景
    ("20", ["faithfulness", "context_precision"]),  # 跨境政策搜索
]
```

## RAGAS 评估流程

### 流程图

```
┌──────────────────────────────────────────┐
│ 1. 从 case YAML 加载 question/ground_truth│
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 2. 调用智能体（拦截 retrieved_contexts）  │
│    - 通过 trace 采集 retrieved_contexts   │
│    - 通过 trace 采集 final response      │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 3. RAGAS 标准 4 维度评估                 │
│    - faithfulness（用 LLM 拆解陈述对照） │
│    - answer_relevancy（反向生成问题）    │
│    - context_precision（标注相关片段）   │
│    - context_recall（对照 ground truth） │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 4. 平台特化 5 维度评估                   │
│    - trust_level_consistency             │
│    - freshness_check                     │
│    - single_source_detection             │
│    - confidence_labeling_rate            │
│    - pii_redaction_rate                  │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 5. 汇总报告 + 阈值判定                   │
└──────────────────────────────────────────┘
```

### 实现

```python
# evaluators/ragas_eval.py（伪代码）
from ragas import evaluate
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_precision,
    context_recall,
)
from datasets import Dataset

def ragas_evaluation(case_yaml, response, retrieved_contexts, ground_truth):
    # 标准 4 维度
    dataset = Dataset.from_dict({
        "question": [case_yaml["user_input"]],
        "answer": [response],
        "contexts": [[c["content"] for c in retrieved_contexts]],
        "ground_truth": [ground_truth],
    })
    
    standard_result = evaluate(
        dataset,
        metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
    )
    
    # 平台特化 5 维度
    platform_metrics = {
        "trust_level_consistency": trust_level_consistency(response, retrieved_contexts),
        "freshness_check": freshness_check(response, retrieved_contexts, case_yaml["current_date"]),
        "single_source_detection": single_source_detection(response, retrieved_contexts),
        "confidence_labeling_rate": confidence_labeling_rate(response),
        "pii_redaction_rate": pii_redaction_rate(response, retrieved_contexts),
    }
    
    return {
        "standard": standard_result,
        "platform": platform_metrics,
        "all_passed": (
            standard_result["faithfulness"] >= 0.95
            and standard_result["answer_relevancy"] >= 0.85
            and standard_result["context_precision"] >= 0.75
            and standard_result["context_recall"] >= 0.85
            and all(v >= 0.9 for v in platform_metrics.values())
        )
    }
```

## Ground Truth 准备

RAGAS 的 context_recall 与 faithfulness 需要 ground truth。本平台从 golden cases 提取：

```yaml
# 在 case YAML 中扩展 ground_truth 字段
# case-12-california.yaml
ground_truth:
  expected_answer_points:
    - "US/CA 知识库不存在"
    - "建议转介 policy-researcher"
    - "不编造加州流程"
  expected_contexts: []  # 期望检索到的 contexts（本 case 期望空）
  forbidden_claims:
    - "加州流程是..."
    - "应该是..."
```

## 触发式评估

### 知识库更新触发

policy-researcher 更新 knowledge/regions/ 时自动触发 RAGAS：

```python
# 在 policy-researcher 的 SKILL.md 中定义
# 更新知识库后调用 RAGAS 验证
def after_knowledge_update(country, region):
    # 找到与该 region 相关的 golden cases
    related_cases = find_cases_by_region(country, region)
    
    for case in related_cases:
        # 跑 RAGAS
        result = ragas_evaluation(case, ...)
        if not result["all_passed"]:
            # 标记知识库更新有问题，回滚或上报
            report_incident(
                severity="medium",
                description=f"知识库更新后 RAGAS 失败：{country}/{region}",
                root_cause_span_id=current_span_id,
            )
```

### 规则变更触发

rules/ 文件变更时跑相关 case 的 RAGAS：

```python
def on_rules_change(changed_files):
    # 找到 rules_involved 包含变更文件的 case
    affected_cases = []
    for case in load_all_cases():
        if set(case["rules_involved"]) & set(changed_files):
            affected_cases.append(case)
    
    # 跑 RAGAS（仅受影响的 case）
    for case in affected_cases:
        run_ragas(case)
```

## CI 集成

```yaml
# .github/workflows/ragas-eval.yml（伪代码）
name: RAGAS Evaluation
on:
  pull_request:
    paths:
      - ".traecli/rules/**"
      - ".traecli/knowledge/**"
      - ".traecli/agents/**"
  schedule:
    - cron: "0 3 * * 1"  # 每周一次全量
jobs:
  ragas:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Setup Python
        uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - name: Install
        run: pip install ragas datasets langfuse
      - name: Run RAGAS
        env:
          LANGFUSE_HOST: ${{ secrets.LANGFUSE_HOST }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          python .traecli/tests/automated/runners/ragas_runner.py \
            --cases=ragas_applicable \
            --platform=trae
      - name: Upload RAGAS report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: ragas-report
          path: .traecli/tests/automated/reports/ragas/
```

## 报告格式

```json
{
  "run_id": "uuid",
  "timestamp": "2026-07-13T03:00:00Z",
  "trigger": "pr|schedule|knowledge_update|rules_change",
  "scope": "ragas_applicable",
  "total_cases": 6,
  "passed": 5,
  "failed": 1,
  "cases": [
    {
      "case_id": "12",
      "name": "加州知识库缺失",
      "standard_metrics": {
        "faithfulness": 1.0,
        "answer_relevancy": 0.92,
        "context_precision": 1.0,
        "context_recall": 1.0
      },
      "platform_metrics": {
        "trust_level_consistency": 1.0,
        "freshness_check": 1.0,
        "single_source_detection": 1.0,
        "confidence_labeling_rate": 0.8,
        "pii_redaction_rate": 1.0
      },
      "all_passed": true,
      "trace_id": "uuid"
    },
    {
      "case_id": "20",
      "name": "跨境继承",
      "standard_metrics": {
        "faithfulness": 0.88,
        "answer_relevancy": 0.91,
        "context_precision": 0.72,
        "context_recall": 0.82
      },
      "platform_metrics": {
        "trust_level_consistency": 0.95,
        "freshness_check": 1.0,
        "single_source_detection": 0.7,
        "confidence_labeling_rate": 0.6,
        "pii_redaction_rate": 1.0
      },
      "all_passed": false,
      "failures": [
        "faithfulness 0.88 < 0.95",
        "context_precision 0.72 < 0.75",
        "context_recall 0.82 < 0.85",
        "single_source_detection 0.7 < 0.9",
        "confidence_labeling_rate 0.6 < 0.9"
      ],
      "trace_id": "uuid"
    }
  ],
  "summary": {
    "avg_faithfulness": 0.94,
    "avg_answer_relevancy": 0.92,
    "avg_context_precision": 0.86,
    "avg_context_recall": 0.88,
    "avg_trust_consistency": 0.98,
    "avg_freshness": 1.0,
    "avg_single_source": 0.85,
    "avg_confidence_labeling": 0.70,
    "avg_pii_redaction": 1.0
  },
  "weak_dimensions": [
    "confidence_labeling_rate (0.70) - 智能体常漏标注置信度",
    "single_source_detection (0.85) - 关键事实常依赖单源"
  ]
}
```

## 与 trace 的联动

每次 RAGAS 评估记录为 tool span：

```json
{
  "span_type": "tool",
  "name": "tool.ragas_eval",
  "attributes": {
    "tool_name": "ragas_eval",
    "case_id": "20",
    "standard_metrics": {
      "faithfulness": 0.88,
      "answer_relevancy": 0.91,
      "context_precision": 0.72,
      "context_recall": 0.82
    },
    "platform_metrics": {
      "trust_level_consistency": 0.95,
      "freshness_check": 1.0,
      "single_source_detection": 0.7,
      "confidence_labeling_rate": 0.6,
      "pii_redaction_rate": 1.0
    },
    "all_passed": false,
    "failed_dimensions": ["faithfulness", "context_precision", "context_recall", "single_source_detection", "confidence_labeling_rate"],
    "latency_ms": 8500,
    "cost_usd": 0.08
  }
}
```

retrieved_contexts 通过 trace 中的 tool_span（query_knowledge）采集：

```
agent_span (death-aftercare)
├── tool_span (query_knowledge)  ← 这里采集 retrieved_contexts
│   └── attributes.retrieved_contexts: [...]
├── tool_span (web_search)       ← 这里也采集
│   └── attributes.retrieved_results: [...]
├── ... (生成响应)
└── tool_span (ragas_eval)       ← 用前面采集的 contexts 跑评估
```

## 弱维度优化建议

针对本平台预期的弱维度，提供优化方向：

### Faithfulness 弱

- 加强 integrity-framework 第八章 5 关自检
- 关键事实必须 query_knowledge 二次确认
- 用 SelfCheckGPT 多次采样一致性校验（见 P1-11）

### Context Recall 弱

- 优化 knowledge/regions/ 的索引粒度
- 用 LightRAG 增加知识图谱关联（见 P1-8）
- 子智能体检索时多 query 拼接

### Single Source Detection 弱

- 在 check_integrity MCP 工具中强制多源校验
- 关键事实（数字/法条/电话）默认要求 ≥ 2 源

### Confidence Labeling Rate 弱

- 在 check_rules MCP 工具中检测未标注的事实
- 在 agent.md 中前置置信度标注模板

## 与 LLM-as-Judge 的分工

| 维度 | 用 RAGAS | 用 LLM-as-Judge |
|------|---------|----------------|
| 检索质量 | ✅（context_precision/recall） | ❌ |
| 忠实度（数字/法条） | ✅（faithfulness） | ❌ |
| 语义判定（语气/质疑） | ❌ | ✅ |
| 是否被越狱 | ❌ | ✅ |
| 是否出法律意见 | ❌ | ✅ |
| 单源检测 | ✅（平台特化） | 也可 |
| 时效校验 | ✅（平台特化） | 也可 |

互补关系：RAGAS 专注"检索+生成"链路，LLM-as-Judge 专注"语义+合规"判定。

## 成本估算

| 维度 | 单 case 成本 | 6 个 case 总成本 |
|------|------------|----------------|
| 标准 4 维度（RAGAS 内置） | ~$0.05 | ~$0.30 |
| 平台特化 5 维度（自实现，含 LLM 调用） | ~$0.03 | ~$0.18 |
| **总计** | ~$0.08 | **~$0.48/次 CI** |

适合每次 PR 跑（成本可控）。

## 版本
- v1.0 初始 RAGAS 评估方案（标准 4 维度 + 平台特化 5 维度 + 触发式评估 + CI 集成 + 弱维度优化建议）
