# LightRAG 知识图谱增强试点方案

> 本文件定义如何在政策法规域引入 LightRAG（轻量级 GraphRAG），增强 knowledge/regions/ 的检索能力。借鉴 GraphRAG（Microsoft）、LightRAG（港大）、KAG（蚂蚁）、Nano-GraphRAG。

## 为什么需要知识图谱

### 当前痛点

knowledge/regions/ 是 Markdown 文件，检索靠关键词匹配：

```
用户问："北京户籍注销需要带什么材料？"
↓
query_knowledge(country=CN, region=beijing, topic=household_cancellation)
↓
Read knowledge/regions/CN/overview.md
↓
全文返回（可能 5000 字），智能体从中找答案
```

问题：
1. **Context Recall 弱**：跨章节信息散落，单次检索难覆盖（如"户籍注销"涉及"死亡证明"+"派出所"+"时限"三章节）
2. **关联不可见**：用户问"A 流程"，但 A 依赖 B，B 限制 C，关键词检索看不到这种关联链
3. **多跳查询难**：如"我妈在北京去世，我拿着她的死亡证明能否办房产过户？"需要 3 跳推理（死亡证明→继承公证→房产过户）

### 知识图谱解决

把 Markdown 内容结构化为（实体，关系，实体）三元组：

```
(户籍注销, 需要, 死亡证明)
(户籍注销, 办理机构, 派出所)
(户籍注销, 时限, 30天)
(死亡证明, 出具机构, 医院/派出所)
(继承公证, 需要, 死亡证明)
(房产过户, 需要, 继承公证)
```

检索时通过图遍历找到关联，而非全文匹配。

## 为什么选 LightRAG

| 方案 | 优点 | 缺点 | 是否适合 |
|------|------|------|---------|
| **GraphRAG（Microsoft）** | 全量社区检测、全局综合 | 全量重建贵、增量弱 | ❌（政策频繁更新） |
| **LightRAG（港大）** | 增量更新、双层检索、轻量 | 社区综合弱于 GraphRAG | ✅ |
| **KAG（蚂蚁）** | 强结构化、领域友好 | 需要本体设计重 | ⚠️（可借鉴本体） |
| **Nano-GraphRAG** | 极简、易理解 | 功能少 | ❌（生产不够） |
| **Mem0** | 记忆导向 | 非图谱 | ❌ |

**选 LightRAG 的核心理由**：
1. policy-researcher 持续写入新地区知识库，需要**增量更新**而非全量重建
2. 政策法规域实体明确（机构/文档/流程/时限），不需要 GraphRAG 的社区综合
3. 部署轻量，与现有 MCP server 兼容

## 试点范围

### 试点 1：CN/overview.md

选择 `knowledge/regions/CN/overview.md` 作为试点，原因：
- 内容已结构化（按主题分章节）
- 是 13 个 golden case 中最常引用的知识库
- 政策稳定，便于评估

### 后续扩展

试点验证有效后，扩展到：
- CN/overview.md
- US/california.md
- JP/overview.md
- policy-researcher 新写入的所有地区

## 实体类型设计（借鉴 KAG 本体思路）

针对身后事政策法规域，定义 6 类实体：

```yaml
entity_types:
  - name: "Regulation"
    description: "法律法规、政策文件"
    examples: ["民法典继承编", "户口登记条例", "美国遗产税法"]
    attributes:
      - effective_date
      - jurisdiction
      - article_number
  
  - name: "Document"
    description: "需出具的证明文件"
    examples: ["死亡证明", "继承公证书", "户籍注销证明"]
    attributes:
      - issuing_authority
      - required_for
      - validity_period
  
  - name: "Authority"
    description: "办理机构"
    examples: ["派出所", "公证处", "医院", "Social Security Office"]
    attributes:
      - jurisdiction_level
      - contact_channel
  
  - name: "Procedure"
    description: "办理流程"
    examples: ["户籍注销", "继承公证", "房产过户", "银行账户继承"]
    attributes:
      - time_limit
      - required_documents
      - cost
  
  - name: "Role"
    description: "参与角色"
    examples: ["第一顺序继承人", "遗嘱执行人", "代办人"]
    attributes:
      - legal_basis
  
  - name: "TimeLimit"
    description: "时限约束"
    examples: ["户籍注销30天", "继承公证无固定时限"]
    attributes:
      - start_trigger
      - duration
      - consequence_if_expired
```

## 关系类型设计

```yaml
relation_types:
  - name: "requires"           # Procedure requires Document
    from: Procedure
    to: Document
    example: "户籍注销 requires 死亡证明"
  
  - name: "issued_by"          # Document issued_by Authority
    from: Document
    to: Authority
    example: "死亡证明 issued_by 医院"
  
  - name: "processed_at"       # Procedure processed_at Authority
    from: Procedure
    to: Authority
    example: "户籍注销 processed_at 派出所"
  
  - name: "depends_on"         # Procedure depends_on Procedure
    from: Procedure
    to: Procedure
    example: "房产过户 depends_on 继承公证"
  
  - name: "restricted_by"      # Procedure restricted_by Regulation
    from: Procedure
    to: Regulation
    example: "户籍注销 restricted_by 户口登记条例"
  
  - name: "eligibility"        # Procedure eligibility Role
    from: Procedure
    to: Role
    example: "继承公证 eligibility 第一顺序继承人"
  
  - name: "time_bound"         # Procedure time_bound TimeLimit
    from: Procedure
    to: TimeLimit
    example: "户籍注销 time_bound 30天"
  
  - name: "supersedes"         # Regulation supersedes Regulation
    from: Regulation
    to: Regulation
    example: "民法典继承编 supersedes 继承法"
```

## 图谱构建

### 从 Markdown 提取三元组

```python
# knowledge/_graph/extract.py（伪代码）
"""
从 knowledge/regions/{country}/{region}.md 提取实体关系三元组。
借鉴 LightRAG 的 entity extraction prompt。
"""
import re
from pathlib import Path

EXTRACT_PROMPT = """
从以下政策法规文档中提取实体和关系。

实体类型：Regulation, Document, Authority, Procedure, Role, TimeLimit
关系类型：requires, issued_by, processed_at, depends_on, restricted_by, eligibility, time_bound, supersedes

## 文档
{document_content}

## 输出格式（JSONL）
每行一个实体或关系：

实体：
{"type": "entity", "entity_type": "Procedure", "name": "户籍注销", "attributes": {...}}

关系：
{"type": "relation", "relation_type": "requires", "from": "户籍注销", "to": "死亡证明"}

## 要求
1. 只提取文档中明确表述的内容，不推断
2. 实体名用全称（不简写）
3. 关系必须有 from 和 to
4. 时限、机构、文档名等属性填入 attributes
"""

def extract_from_markdown(md_path):
    content = Path(md_path).read_text()
    prompt = EXTRACT_PROMPT.format(document_content=content)
    result = call_llm(prompt)
    return parse_jsonl(result)

def build_graph(country, region):
    """构建某地区的知识图谱"""
    md_path = f"knowledge/regions/{country}/{region}.md"
    triples = extract_from_markdown(md_path)
    
    # 存储到 _graph/ 目录
    graph_dir = Path(f"knowledge/_graph/{country}/{region}/")
    graph_dir.mkdir(parents=True, exist_ok=True)
    
    # 实体表
    entities = [t for t in triples if t["type"] == "entity"]
    (graph_dir / "entities.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entities)
    )
    
    # 关系表
    relations = [t for t in triples if t["type"] == "relation"]
    (graph_dir / "relations.jsonl").write_text(
        "\n".join(json.dumps(r) for r in relations)
    )
    
    # 构建向量索引（实体名 + 描述）
    build_vector_index(entities, graph_dir / "entity_index")
```

### 增量更新

```python
# knowledge/_graph/incremental_update.py（伪代码）
"""
policy-researcher 写入新地区/更新已有地区时，增量更新图谱。
关键：只重建变更部分，不全量重建。
"""

def on_knowledge_update(country, region, change_type="added"|"modified"):
    graph_dir = Path(f"knowledge/_graph/{country}/{region}/")
    
    if change_type == "modified":
        # 1. 读取旧图谱
        old_entities = load_jsonl(graph_dir / "entities.jsonl")
        old_relations = load_jsonl(graph_dir / "relations.jsonl")
        
        # 2. 重新提取（只对变更的章节）
        new_triples = extract_from_markdown(f"knowledge/regions/{country}/{region}.md")
        
        # 3. 计算差异
        diff = compute_diff(old_entities + old_relations, new_triples)
        
        # 4. 只更新差异部分
        apply_diff(graph_dir, diff)
        
        # 5. 记录更新日志
        log_update(country, region, diff)
    else:
        # 新增：全量构建
        build_graph(country, region)
```

## 检索模式（借鉴 LightRAG 双层检索）

### 三种检索模式

```python
# knowledge/_graph/query.py（伪代码）

def query_graph(question, mode="hybrid"):
    """
    mode:
    - local: 实体邻居检索（适合具体问题）
    - global: 全局主题检索（适合概述问题）
    - hybrid: local + global（默认）
    """
    if mode in ["local", "hybrid"]:
        local_result = local_search(question)
    if mode in ["global", "hybrid"]:
        global_result = global_search(question)
    
    if mode == "local":
        return local_result
    elif mode == "global":
        return global_result
    else:
        return merge_results(local_result, global_result)


def local_search(question):
    """
    1. 从问题中抽取实体（NER）
    2. 在实体向量索引中找 top-k 相似实体
    3. 对每个实体，遍历一跳邻居
    4. 返回 (实体, 关系, 邻居) 三元组列表
    """
    entities = extract_entities_from_question(question)
    similar = vector_search(entities, top_k=5)
    
    results = []
    for entity in similar:
        neighbors = get_neighbors(entity)
        results.append({
            "center_entity": entity,
            "neighbors": neighbors,
            "relations": get_relations(entity)
        })
    return results


def global_search(question):
    """
    1. 把问题映射到主题（如"继承"主题）
    2. 找该主题下所有相关实体
    3. 返回实体的全局视图
    """
    topic = classify_topic(question)
    entities = get_entities_by_topic(topic)
    return {
        "topic": topic,
        "entities": entities,
        "summary": summarize_topic(topic)
    }
```

### 检索示例

**用户问**："我妈在北京去世，我拿着她的死亡证明能否办房产过户？"

```python
result = query_graph(
    "我妈在北京去世，我拿着她的死亡证明能否办房产过户？",
    mode="hybrid"
)
```

**返回**：

```json
{
  "local_result": [
    {
      "center_entity": "房产过户",
      "neighbors": ["继承公证", "房产登记中心", "不动产权证"],
      "relations": [
        {"type": "requires", "from": "房产过户", "to": "继承公证"},
        {"type": "processed_at", "from": "房产过户", "to": "房产登记中心"}
      ]
    },
    {
      "center_entity": "继承公证",
      "neighbors": ["死亡证明", "公证处", "第一顺序继承人"],
      "relations": [
        {"type": "requires", "from": "继承公证", "to": "死亡证明"},
        {"type": "processed_at", "from": "继承公证", "to": "公证处"}
      ]
    }
  ],
  "global_result": {
    "topic": "继承流程",
    "summary": "房产过户依赖继承公证，继承公证依赖死亡证明。仅凭死亡证明无法直接办房产过户。"
  },
  "reasoning_path": "死亡证明 → 继承公证 → 房产过户（2 跳）"
}
```

智能体据此可回答："仅凭死亡证明不能直接办房产过户，需要先办继承公证。继承公证需要死亡证明作为材料之一。"

## 与 MCP server 集成

### 扩展 query_knowledge 工具

```python
# mcp_server/server.py（伪代码扩展）

@mcp.tool()
def query_knowledge(country: str, region: str = None, topic: str = None,
                    query_mode: str = "markdown"|"graph"|"hybrid",
                    fallback_to_search: bool = True) -> dict:
    """
    查询地域知识库。
    
    query_mode:
    - markdown: 仅查 Markdown（原行为）
    - graph: 仅查知识图谱
    - hybrid: 同时查 Markdown + 图谱，合并结果（默认）
    """
    if query_mode == "markdown":
        return query_markdown(country, region, topic)
    elif query_mode == "graph":
        return query_graph_knowledge(country, region, topic)
    else:  # hybrid
        md_result = query_markdown(country, region, topic)
        graph_result = query_graph_knowledge(country, region, topic)
        return merge_results(md_result, graph_result)


def query_graph_knowledge(country, region, topic):
    graph_dir = Path(f"knowledge/_graph/{country}/{region}/")
    if not graph_dir.exists():
        return {
            "found": False,
            "needs_research": True,
            "research_suggestion": f"建议触发 policy-researcher 构建 {country}/{region} 图谱"
        }
    
    # 调用图谱检索
    question = f"{country}/{region} {topic}"
    result = query_graph(question, mode="hybrid")
    
    return {
        "found": True,
        "data": {
            "content": result["global_result"]["summary"],
            "reasoning_path": result["reasoning_path"],
            "entities": [e["name"] for e in result["local_result"]],
            "relations": [...],
            "sources": [f"knowledge/regions/{country}/{region}.md"],
            "trust_level": "high",
            "freshness_status": "fresh"
        }
    }
```

### agent.md 工具声明

```yaml
# 智能体的 tools 声明不变
tools: WebSearch, WebFetch
mcp_servers:
  - deadman-platform  # query_knowledge 自动支持 graph/hybrid 模式
```

智能体调用时指定 mode：

```
query_knowledge(country="CN", region="beijing", topic="房产过户", query_mode="hybrid")
```

## 评估

### 与 RAGAS 联动

LightRAG 试点效果通过 RAGAS 的 context_recall 与 context_precision 量化：

```python
# tests/automated/runners/lightrag_eval.py（伪代码）

def evaluate_lightrag():
    """对比 Markdown-only vs Hybrid 检索效果"""
    cases = load_ragas_applicable_cases()
    
    results = {"markdown_only": [], "hybrid": []}
    
    for case in cases:
        # Markdown-only 模式
        md_response, md_contexts = run_agent(case, query_mode="markdown")
        md_ragas = ragas_evaluation(case, md_response, md_contexts, case.ground_truth)
        results["markdown_only"].append(md_ragas)
        
        # Hybrid 模式（Markdown + 图谱）
        hybrid_response, hybrid_contexts = run_agent(case, query_mode="hybrid")
        hybrid_ragas = ragas_evaluation(case, hybrid_response, hybrid_contexts, case.ground_truth)
        results["hybrid"].append(hybrid_ragas)
    
    # 对比
    return {
        "context_recall_improvement": avg(hybrid_ragas.context_recall) - avg(md_ragas.context_recall),
        "context_precision_improvement": avg(hybrid_ragas.context_precision) - avg(md_ragas.context_precision),
        "faithfulness_improvement": avg(hybrid_ragas.faithfulness) - avg(md_ragas.faithfulness),
    }
```

**预期效果**：
- context_recall 提升 10-20%（多跳查询能找到关联）
- context_precision 略降或持平（图谱可能引入更多上下文）
- faithfulness 提升 5-10%（结构化数据降低幻觉）

### 多跳查询专项评估

```python
MULTIHOP_TEST_CASES = [
    {
        "question": "我妈在北京去世，我拿着她的死亡证明能否办房产过户？",
        "expected_hops": 2,
        "expected_path": "死亡证明 → 继承公证 → 房产过户",
    },
    {
        "question": "北京户籍注销要等多久才能办继承公证？",
        "expected_hops": 2,
        "expected_path": "户籍注销 → 死亡证明 → 继承公证",
    },
    {
        "question": "我没办继承公证能直接取我爸的银行存款吗？",
        "expected_hops": 2,
        "expected_path": "银行账户继承 → 继承公证 → 死亡证明",
    },
]

def evaluate_multihop():
    results = []
    for case in MULTIHOP_TEST_CASES:
        result = query_graph(case["question"], mode="hybrid")
        hops_matched = result["reasoning_path"] == case["expected_path"]
        results.append({
            "question": case["question"],
            "expected_hops": case["expected_hops"],
            "actual_hops": count_hops(result["reasoning_path"]),
            "path_matched": hops_matched,
        })
    return results
```

## 与 trace 的联动

```json
{
  "span_type": "tool",
  "name": "tool.query_knowledge_graph",
  "attributes": {
    "tool_name": "query_knowledge",
    "query_mode": "hybrid",
    "country": "CN",
    "region": "beijing",
    "topic": "房产过户",
    "local_entities_found": 3,
    "global_topic": "继承流程",
    "reasoning_path": "死亡证明 → 继承公证 → 房产过户",
    "hops": 2,
    "latency_ms": 450,
    "graph_version": "2026-07-13-v1"
  }
}
```

## 图谱版本管理

```
knowledge/_graph/
├── CN/
│   ├── general/
│   │   ├── entities.jsonl
│   │   ├── relations.jsonl
│   │   ├── entity_index/       # 向量索引
│   │   └── _meta.json          # 版本信息
│   └── overview/
├── US/
│   └── california/
└── _global/                    # 跨地区通用图谱（如海牙认证流程）
```

`_meta.json` 示例：

```json
{
  "version": "2026-07-13-v1",
  "source_file": "knowledge/regions/CN/overview.md",
  "source_file_hash": "sha256:...",
  "extracted_at": "2026-07-13T10:00:00Z",
  "entity_count": 45,
  "relation_count": 89,
  "extraction_model": "gpt-4o",
  "last_incremental_update": "2026-07-13T10:00:00Z"
}
```

policy-researcher 写入新版本时，比较 source_file_hash，若变化则触发增量更新。

## 与 integrity-framework 的联动

图谱构建必须遵守诚信约束：

1. **只提取文档明确表述的关系**：不推断、不脑补
2. **关系必须有原文支撑**：每条关系记录 `source_text` 字段（原文片段）
3. **时限/数字必须可溯源**：从原文逐字摘录

```jsonl
{"type": "relation", "relation_type": "time_bound", "from": "户籍注销", "to": "30天", "source_text": "公民死亡后，家属应在30日内到户口所在地派出所办理户籍注销", "source_section": "CN/overview.md#户籍注销"}
```

若智能体输出的关系在图谱中无 source_text 支撑，触发 integrity-framework 违反。

## 试点步骤

1. **第 1 步**：手动标注 CN/overview.md 的实体关系（10-20 个实体，30-50 个关系）
2. **第 2 步**：实现 extract_from_markdown，用 LLM 自动提取，与人工标注对比
3. **第 3 步**：实现 query_graph 的 local/global/hybrid 三模式
4. **第 4 步**：扩展 query_knowledge MCP 工具支持 query_mode
5. **第 5 步**：在 6 个 RAGAS applicable case 上跑对比评估
6. **第 6 步**：在 3 个多跳查询专项 case 上跑评估
7. **第 7 步**：若 context_recall 提升 ≥ 10%，扩展到其他地区
8. **第 8 步**：policy-researcher 写入新地区时自动触发 build_graph

## 失败回退

若 LightRAG 试点效果不佳（context_recall 未提升或 faithfulness 下降）：

1. **回退到 Markdown-only**：query_mode 默认改为 markdown
2. **保留图谱作为辅助**：仅在智能体主动请求时返回图谱信息
3. **分析失败原因**：
   - 是否实体类型设计不当？
   - 是否关系提取不准？
   - 是否检索模式选择错误？

## 成本估算

| 操作 | 频率 | 单次成本 |
|------|------|---------|
| 图谱构建（全量） | 每地区 1 次 | ~$0.5（LLM 提取三元组） |
| 增量更新 | 每次知识库变更 | ~$0.1 |
| 图谱查询 | 每次 query_knowledge | ~$0.005（向量检索 + 图遍历） |

按 100 个地区计算：
- 一次性构建：~$50
- 每月增量更新：~$10（假设 20% 地区每月更新）
- 每月查询：~$50（按 10000 次查询计）

## 版本
- v1.0 初始 LightRAG 试点方案（实体关系设计 + 三检索模式 + MCP 集成 + 增量更新 + RAGAS 评估 + 多跳专项）
