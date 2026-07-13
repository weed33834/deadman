# SelfCheckGPT 数字类输出一致性校验方案

> 本文件定义如何用 SelfCheckGPT 多次采样一致性校验，检测数字类输出的幻觉。借鉴 SelfCheckGPT（Manakul 等, 2023）、Cross-examination、SLOR（Syntactic Log-odds Ratio）。

## 为什么需要 SelfCheckGPT

### 当前痛点

integrity-framework 第八章的"5 关自检"中，hallucination_check 依赖人工或单次 LLM 判定。但 LLM 单次输出可能：
- 编造电话号码（如 05XX-XXXXXXXX）
- 编造时限（如"30 天"实际无依据）
- 编造金额（如"免税额 1361 万"凭印象给出）
- 编造法条号（如"民法典第 1145 条"实际查不到）

单次输出无法区分"真有依据"和"模型脑补但听起来合理"。

### SelfCheckGPT 原理

同一个问题多次采样（不同 temperature），如果模型是真有依据的：
- 多次采样的答案高度一致

如果模型是脑补的：
- 多次采样的答案会发散（数字不同、细节不同）

```
Q: 北京户籍注销时限是多少？

采样 1（temp=0.3）：30 天
采样 2（temp=0.5）：30 天  
采样 3（temp=0.7）：60 天
采样 4（temp=0.4）：30 个工作日
采样 5（temp=0.6）：30 天

一致性分析：
- 30 / 30 / 60 / 30工作日 / 30 → 60 天是离群值
- 但 30 vs 30 工作日 也有差异
- 综合一致性：中等 → 标置信度"中"，建议核实
```

## 适用范围

### 必须校验的输出类型

1. **电话号码**（机构电话、热线）
2. **时限**（流程办理时限、有效期）
3. **金额**（免税额、费用、税率）
4. **法条号**（"民法典第 X 条"）
5. **文档名**（具体证明文件名称）
6. **流程步骤数**（"共 N 步"）
7. **百分比**（"继承份额 X%"）

### 不需要校验的输出

- 通用框架描述（"法定继承第一顺序是配偶/父母/子女"）
- 流程方向引导（"建议先办死亡证明"）
- 情绪支持话术
- 转介话术

## 校验流程

```
┌──────────────────────────────────────────┐
│ 1. 智能体生成初始响应（含数字类 claim）   │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 2. 从响应中提取数字类 claim              │
│    - 电话、时限、金额、法条号等          │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 3. 对每个 claim 触发 SelfCheckGPT        │
│    - 同 prompt 采样 N=5 次               │
│    - 不同 temperature（0.3/0.5/0.7/0.4/0.6）│
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 4. 一致性分析                             │
│    - 数字提取：从每次采样中提取数字       │
│    - 一致性计算：n_unique / n_samples    │
│    - 离群值检测                           │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 5. 调整置信度                             │
│    - 一致性高 → 保留原置信度              │
│    - 一致性中 → 降为"中"                 │
│    - 一致性低 → 降为"未知"，建议核实      │
└────────────────┬─────────────────────────┘
                 ▼
┌──────────────────────────────────────────┐
│ 6. 若一致性低且无 sources → 触发 incident│
└──────────────────────────────────────────┘
```

## 实现

### 在 check_integrity MCP 工具中集成

```python
# mcp_server/selfcheck.py（伪代码）

import re
from typing import List
from statistics import mean

# 数字类 claim 的正则
NUMBER_PATTERNS = {
    "phone": r"\b\d{3,4}[-\s]?\d{7,8}\b|\b\d{11}\b",  # 电话
    "days": r"\b\d+\s*(?:天|个工作日|日)\b",            # 时限
    "money": r"\b\d+(?:\.\d+)?\s*(?:万|元|美元|人民币)\b",  # 金额
    "percent": r"\b\d+(?:\.\d+)?\s*%",                  # 百分比
    "article": r"第\s*\d+\s*条",                        # 法条号
    "step_count": r"\b\d+\s*(?:步|个阶段|个环节)\b",    # 步骤数
}

def extract_numeric_claims(response: str) -> List[dict]:
    """从响应中提取所有数字类 claim"""
    claims = []
    for claim_type, pattern in NUMBER_PATTERNS.items():
        matches = re.finditer(pattern, response)
        for m in matches:
            # 提取上下文（前后 50 字符）
            start = max(0, m.start() - 50)
            end = min(len(response), m.end() + 50)
            context = response[start:end]
            
            claims.append({
                "claim_type": claim_type,
                "value": m.group(),
                "context": context,
                "position": m.start()
            })
    return claims


async def selfcheck_claim(claim: dict, original_prompt: str, n_samples: int = 5) -> dict:
    """
    对单个数字类 claim 进行 SelfCheckGPT 校验。
    多次采样同一 prompt，检查 claim 是否在所有采样中一致出现。
    """
    temperatures = [0.3, 0.5, 0.7, 0.4, 0.6][:n_samples]
    
    sampled_responses = []
    for temp in temperatures:
        # 重新生成响应（同 prompt，不同 temperature）
        response = await call_llm(
            prompt=original_prompt,
            temperature=temp,
            # 用与原响应相同的上下文（知识库、规则）
        )
        sampled_responses.append(response)
    
    # 从每次采样中提取相同类型的数字
    sampled_values = []
    for resp in sampled_responses:
        # 在采样响应中找与 claim 同类型的数字
        # 优先找位置接近的（基于 context 相似度）
        values_in_resp = extract_numeric_claims_by_type(resp, claim["claim_type"])
        if values_in_resp:
            # 找最接近 claim.context 的那个
            closest = find_closest_by_context(values_in_resp, claim["context"])
            sampled_values.append(closest["value"])
        else:
            sampled_values.append(None)  # 该采样中未出现此类数字
    
    # 一致性计算
    valid_values = [v for v in sampled_values if v is not None]
    if not valid_values:
        # 5 次采样都没出现此类数字 → 高度可疑
        return {
            "claim": claim,
            "consistency": 0.0,
            "verdict": "highly_suspicious",
            "sampled_values": sampled_values,
            "reason": "5 次采样中均未出现此类数字，可能为单次幻觉"
        }
    
    # 数字归一化（提取纯数字）
    normalized = [extract_pure_number(v) for v in valid_values]
    unique_values = set(normalized)
    
    consistency = 1.0 - (len(unique_values) - 1) / len(valid_values)
    
    if consistency >= 0.8:
        verdict = "consistent"
    elif consistency >= 0.5:
        verdict = "moderate"
    else:
        verdict = "inconsistent"
    
    return {
        "claim": claim,
        "consistency": consistency,
        "verdict": verdict,
        "sampled_values": sampled_values,
        "unique_values": list(unique_values),
        "reason": interpret_verdict(verdict, unique_values)
    }


def extract_pure_number(value: str) -> str:
    """从 '30天' / '30 个工作日' 中提取 '30'"""
    m = re.search(r"\d+(?:\.\d+)?", value)
    return m.group() if m else value


def interpret_verdict(verdict: str, unique_values: set) -> str:
    if verdict == "consistent":
        return f"多次采样一致（{unique_values}），可信度高"
    elif verdict == "moderate":
        return f"多次采样有分歧（{unique_values}），建议标注中置信度"
    else:
        return f"多次采样严重不一致（{unique_values}），可能为幻觉，建议核实"


async def selfcheck_response(response: str, original_prompt: str) -> dict:
    """对完整响应做 SelfCheckGPT 校验"""
    claims = extract_numeric_claims(response)
    
    if not claims:
        return {
            "claims_checked": 0,
            "all_consistent": True,
            "results": []
        }
    
    results = []
    for claim in claims:
        result = await selfcheck_claim(claim, original_prompt)
        results.append(result)
    
    # 综合判定
    suspicious_claims = [r for r in results if r["verdict"] in ["inconsistent", "highly_suspicious"]]
    moderate_claims = [r for r in results if r["verdict"] == "moderate"]
    
    return {
        "claims_checked": len(claims),
        "all_consistent": len(suspicious_claims) == 0,
        "suspicious_claims": suspicious_claims,
        "moderate_claims": moderate_claims,
        "results": results
    }
```

### 在 check_integrity 中调用

```python
# mcp_server/server.py（伪代码扩展）

@mcp.tool()
async def check_integrity(output_text: str, claims_to_verify: list, original_prompt: str = None):
    """
    5 关事实复核 + SelfCheckGPT 校验
    """
    # 原有 5 关校验
    source_check = check_sources(claims_to_verify)
    hallucination_check = check_hallucination(output_text, claims_to_verify)
    freshness_check = check_freshness(claims_to_verify)
    single_source_check = check_single_source(claims_to_verify)
    boundary_check = check_boundary(output_text)
    
    # 新增：SelfCheckGPT 校验（仅对数字类 claim）
    if original_prompt:
        selfcheck_result = await selfcheck_response(output_text, original_prompt)
        
        # 根据 SelfCheckGPT 结果调整置信度
        adjusted_confidence = adjust_confidence_labels(
            output_text, 
            selfcheck_result
        )
    else:
        selfcheck_result = None
        adjusted_confidence = None
    
    return {
        "passed": (
            source_check["passed"]
            and hallucination_check["passed"]
            and freshness_check["passed"]
            and single_source_check["passed"]
            and boundary_check["passed"]
            and (selfcheck_result is None or selfcheck_result["all_consistent"])
        ),
        "check_results": {
            "source_check": source_check,
            "hallucination_check": hallucination_check,
            "freshness_check": freshness_check,
            "single_source_check": single_source_check,
            "boundary_check": boundary_check,
            "selfcheck_gpt": selfcheck_result
        },
        "confidence_labels": adjusted_confidence
    }


def adjust_confidence_labels(response: str, selfcheck_result: dict) -> list:
    """根据 SelfCheckGPT 结果调整置信度标注"""
    labels = []
    
    for result in selfcheck_result["results"]:
        claim = result["claim"]
        verdict = result["verdict"]
        
        if verdict == "consistent":
            confidence = "高"
        elif verdict == "moderate":
            confidence = "中"
        else:  # inconsistent / highly_suspicious
            confidence = "未知"
        
        labels.append({
            "claim": claim["value"],
            "claim_type": claim["claim_type"],
            "confidence": confidence,
            "selfcheck_consistency": result["consistency"],
            "selfcheck_reason": result["reason"]
        })
    
    return labels
```

## 触发时机

SelfCheckGPT 不是每次都跑，只在以下情况触发：

```python
def should_run_selfcheck(response: str, context: dict) -> bool:
    """判断是否需要触发 SelfCheckGPT"""
    
    # 1. 响应中含数字类 claim
    claims = extract_numeric_claims(response)
    if not claims:
        return False
    
    # 2. 至少一个 claim 无 source（有 source 的不强制 SelfCheck）
    unsourced = [c for c in claims if not has_source(c, context)]
    if not unsourced:
        return False
    
    # 3. 风险等级 R2/R3 时强制跑
    if context.get("risk_tier") in ["R2", "R3"]:
        return True
    
    # 4. 一般情况：unsourced claim 数 ≥ 2 时跑
    return len(unsourced) >= 2
```

## 成本控制

### 分层采样

```python
async def adaptive_selfcheck(claim: dict, original_prompt: str):
    """自适应采样：先用 3 次，不一致再补到 5 次"""
    
    # 第一轮：3 次采样
    result = await selfcheck_claim(claim, original_prompt, n_samples=3)
    
    if result["verdict"] == "consistent":
        # 3 次一致 → 高置信度，不再采样
        return result
    
    # 不一致 → 补 2 次
    additional = await selfcheck_claim(claim, original_prompt, n_samples=2)
    
    # 合并 5 次结果
    return merge_results(result, additional)
```

### 成本估算

| 场景 | 单次成本 | 频率 |
|------|---------|------|
| 单个数字 claim 自适应校验（3-5 次采样） | ~$0.01 | 每次响应平均 1-2 个数字 claim |
| 每个用户请求的 SelfCheckGPT 总成本 | ~$0.02 | 假设 50% 请求触发 |

按 10000 请求/天计算：~$100/天，~$3000/月。

**优化方向**：
- 只对 unsourced claim 跑（有 source 的不跑）
- 缓存常见问题的采样结果（如"北京户籍注销时限"全国用户问，第二次直接用缓存）

## 与 trace 的联动

```json
{
  "span_type": "tool",
  "name": "tool.selfcheck_gpt",
  "attributes": {
    "tool_name": "selfcheck_gpt",
    "claims_checked": 3,
    "all_consistent": false,
    "suspicious_claims": [
      {
        "claim": "30天",
        "claim_type": "days",
        "consistency": 0.4,
        "verdict": "inconsistent",
        "sampled_values": ["30天", "60天", "30天", "30个工作日", null],
        "unique_values": ["30", "60", "30"]
      }
    ],
    "n_samples_per_claim": 5,
    "latency_ms": 8500,
    "cost_usd": 0.025
  }
}
```

## 与 integrity-framework 的联动

integrity-framework 第八章新增"SelfCheckGPT 校验"章节：

```markdown
## 八、输出前事实复核（5 关自检）

### 8.1 - 8.5（原有 5 关）

### 8.6 SelfCheckGPT 数字类校验（新增）

对响应中的数字类 claim（电话/时限/金额/法条号等）执行多次采样一致性校验：

- **触发条件**：响应含 unsourced 数字 claim，且风险等级 R2/R3，或 unsourced claim ≥ 2
- **采样次数**：3-5 次（自适应）
- **一致性阈值**：
  - ≥ 0.8 → 置信度"高"
  - 0.5-0.8 → 置信度"中"
  - < 0.5 → 置信度"未知"，建议核实
- **失败处理**：一致性 < 0.5 且无 source → 阻止输出该数字，或附加"未经验证，请核实"显著标注
```

## 与 RAGAS 的联动

SelfCheckGPT 与 RAGAS faithfulness 互补：

| 维度 | SelfCheckGPT | RAGAS faithfulness |
|------|-------------|-------------------|
| 检测方式 | 多次采样比对 | 响应对照 contexts |
| 适合 | 无 sources 的数字 claim | 有 contexts 的事实陈述 |
| 成本 | 中（多次采样） | 低（单次 LLM 判定） |
| 误报率 | 中（可能因为采样发散误判） | 低 |

集成策略：
1. 优先用 RAGAS faithfulness（有 contexts 时）
2. 无 contexts 或 faithfulness 失败时，用 SelfCheckGPT
3. 两者都失败 → 高置信度判为幻觉

## 评估 SelfCheckGPT 本身

SelfCheckGPT 的判定也可能误报，需要评估：

```python
# tests/automated/runners/selfcheck_eval.py（伪代码）

# 100 个人工标注的 case
# 50 个真实数字（有 source）
# 50 个编造数字（无 source）

def evaluate_selfcheck():
    tp = fp = tn = fn = 0
    
    for case in BENCHMARK_CASES:
        result = selfcheck_claim(case["claim"], case["prompt"])
        predicted_suspicious = result["verdict"] in ["inconsistent", "highly_suspicious"]
        actual_suspicious = case["is_fabricated"]
        
        if predicted_suspicious and actual_suspicious:
            tp += 1
        elif predicted_suspicious and not actual_suspicious:
            fp += 1
        elif not predicted_suspicious and not actual_suspicious:
            tn += 1
        else:
            fn += 1
    
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    f1 = 2 * precision * recall / (precision + recall)
    
    return {"precision": precision, "recall": recall, "f1": f1}
```

**目标**：F1 ≥ 0.85

## 试点 case

### Case 01（异地就医备案时限）

用户问"备案多久"，无 fixture 时智能体可能编造"7 个工作日"。SelfCheckGPT 应能检测：
- 5 次采样可能给出：7 天 / 5 天 / 10 天 / 7 个工作日 / 3 天
- 一致性 < 0.5 → 标"未知"，拒绝输出数字

### Case 05（电话号码）

用户问某县医保局电话，知识库未收录。智能体可能编造号码。SelfCheckGPT 应能检测：
- 5 次采样给出不同号码 → 一致性 0 → 标"未知"，引导官方渠道

### Case 08（美国遗产税免税额）

用户问"美国遗产税免税额是多少"，知识库过期。SelfCheckGPT 应能检测：
- 5 次采样可能给出：1361 万 / 1399 万 / 1200 万 / 1170 万
- 一致性中等 → 标"中"，建议核实最新政策

## 失败回退

若 SelfCheckGPT 误报率高（F1 < 0.7）：
1. 调整采样次数（增加到 7-10 次）
2. 调整一致性阈值（从 0.5 调到 0.4）
3. 改用 cross-examination（让两个 LLM 互相质询）
4. 退化为仅 RAGAS faithfulness

## 版本
- v1.0 初始 SelfCheckGPT 方案（数字类 claim 提取 + 多次采样 + 一致性分析 + 自适应采样 + 与 check_integrity 集成 + 成本控制 + 评估方法）
