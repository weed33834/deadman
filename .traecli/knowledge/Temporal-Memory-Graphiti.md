# Graphiti 时态记忆方案

> 本文件定义如何引入 Graphiti（Zep 2.0 时态知识图谱）解决"政策随时间变化"和"用户进度跨会话续接"两大需求。借鉴 Graphiti（Zep）、Zep 2.0、MemGPT、Letta、Mem0。

## 为什么需要时态记忆

### 当前痛点

#### 痛点 1：政策时效性无法表达

现有 knowledge/regions/ 只有 `last_updated` 字段：

```yaml
---
country: CN
last_updated: 2026-07-01
---
户籍注销时限：30天
```

无法表达：
- "2024 年前是 30 天，2024 年民法典生效后改为 60 天"
- "2025 年某地试点简化流程，时限改为 15 天"
- "美国联邦遗产税免税额 2024 年是 1361 万美元，2025 年是 1399 万"

#### 痛点 2：用户进度无法续接

death-aftercare-tracker 子智能体需要跨会话跟踪进度：

```
会话 1（7月10日）：用户咨询北京流程
会话 2（7月12日）：用户已办死亡证明
会话 3（7月15日）：用户问"下一步该办什么"
```

现有方案只能把进度存在 Markdown 文件中，无法表达：
- "用户在 7月12日 14:30 完成了死亡证明办理"
- "在 7月10日时，用户尚未开始办任何手续"
- "如果用户 7月15日回来，应推荐户籍注销（30 天时限）"

#### 痛点 3：知识库更新无历史

retrieval-guardrails 要求标注时效状态（fresh/stale/outdated），但无法回答：
- "这条信息在 2025 年 6 月时是什么内容？"
- "上一次更新和这次更新差了什么？"
- "如果用户在 7月1日 看到旧版，7月5日 已更新，应该如何告知？"

## Graphiti 的双时间模型

借鉴 Graphiti（Zep）的 bi-temporal model：

### 两种时间

1. **Valid Time（有效时间）**：事实在现实中有效的时间区间
   - 户籍注销时限 30 天：valid_from=2020-01-01, valid_to=2023-12-31
   - 户籍注销时限 60 天：valid_from=2024-01-01, valid_to=null（至今有效）

2. **Transaction Time（事务时间）**：知识库中记录该事实的时间
   - 2024-01-05 知识库写入"户籍注销时限 60 天"
   - 即使在 2024-01-01 政策已生效，知识库可能在 2024-01-05 才更新

### 双时间表达

```json
{
  "fact": "户籍注销时限为60天",
  "valid_time": {
    "from": "2024-01-01",
    "to": null
  },
  "transaction_time": {
    "from": "2024-01-05T10:00:00Z",
    "to": null
  },
  "source": "民法典继承编 第XX条",
  "supersedes": "户籍注销时限为30天（valid_to=2023-12-31）"
}
```

## 三类时态对象

### 1. 时态政策事实（Policy Fact）

```python
# knowledge/_temporal/policy_facts.py（伪代码）

@dataclass
class PolicyFact:
    fact_id: str                    # 唯一 ID
    fact_type: str                  # time_limit/document_required/procedure_step/fee
    content: str                    # 事实内容
    jurisdiction: str               # CN/beijing, US/california
    valid_time: TimeRange           # 有效时间
    transaction_time: TimeRange     # 记录时间
    source: Source                  # 来源（法规/官方公告）
    supersedes: Optional[str]       # 取代哪个 fact_id
    confidence: str                 # high/medium/low
    
@dataclass
class TimeRange:
    from_: Optional[datetime]
    to: Optional[datetime]          # null 表示至今有效
    
@dataclass
class Source:
    type: str                       # regulation/official_notice/web_search
    url: str
    citation: str                   # "民法典 第XX条"
    trust_level: str                # high/medium/low
    retrieved_at: datetime
```

### 2. 用户进度事件（User Progress Event）

```python
# knowledge/_temporal/user_events.py（伪代码）

@dataclass
class UserProgressEvent:
    event_id: str
    user_id_hash: str               # 用户 ID 哈希（PII 脱敏）
    session_id: str
    event_type: str                 # inquiry_started/document_obtained/procedure_completed/transfer_happened
    procedure: str                  # 户籍注销/继承公证/...
    timestamp: datetime             # 事件发生时间
    location: str                   # 用户所在地
    metadata: dict                  # 附加信息
    
# 示例
event1 = UserProgressEvent(
    event_type="inquiry_started",
    procedure="户籍注销",
    timestamp="2026-07-10T10:00:00Z",
    location="CN/beijing"
)

event2 = UserProgressEvent(
    event_type="document_obtained",
    procedure="死亡证明",
    timestamp="2026-07-12T14:30:00Z",
    location="CN/beijing"
)

event3 = UserProgressEvent(
    event_type="procedure_completed",
    procedure="户籍注销",
    timestamp="2026-07-15T09:00:00Z",
    location="CN/beijing"
)
```

### 3. 知识库版本（Knowledge Version）

```python
# knowledge/_temporal/kb_versions.py（伪代码）

@dataclass
class KnowledgeVersion:
    version_id: str
    file_path: str                  # knowledge/regions/CN/overview.md
    version_hash: str               # 文件内容哈希
    created_at: datetime
    changes: List[ChangeRecord]     # 本次版本变更的内容
    author: str                     # policy-researcher
    
@dataclass
class ChangeRecord:
    change_type: str                # added/modified/deleted
    section: str                    # 章节路径
    old_content: Optional[str]
    new_content: Optional[str]
    reason: str                     # 变更原因（如"民法典生效"）
```

## 时态查询

### 查询 1：当前有效事实

```python
def get_current_fact(jurisdiction, fact_type, as_of=None):
    """获取某地在某时间点有效的事实"""
    as_of = as_of or datetime.now()
    return query(
        """
        MATCH (f:PolicyFact)
        WHERE f.jurisdiction = $jurisdiction
          AND f.fact_type = $fact_type
          AND f.valid_time.from <= $as_of
          AND (f.valid_time.to IS NULL OR f.valid_time.to >= $as_of)
        RETURN f
        ORDER BY f.transaction_time.from DESC
        LIMIT 1
        """,
        jurisdiction=jurisdiction,
        fact_type=fact_type,
        as_of=as_of
    )

# 示例
get_current_fact("CN/beijing", "household_cancellation_time_limit")
# 返回：户籍注销时限为60天（valid_from=2024-01-01）
```

### 查询 2：历史事实

```python
def get_fact_at_time(jurisdiction, fact_type, at_time):
    """查询某时间点的事实（用于处理历史 case）"""
    return query(
        """
        MATCH (f:PolicyFact)
        WHERE f.jurisdiction = $jurisdiction
          AND f.fact_type = $fact_type
          AND f.valid_time.from <= $at_time
          AND (f.valid_time.to IS NULL OR f.valid_time.to >= $at_time)
        RETURN f
        """,
        jurisdiction=jurisdiction,
        fact_type=fact_type,
        at_time=at_time
    )

# 示例：用户在 2023-06-15 咨询的 case
get_fact_at_time("CN/beijing", "household_cancellation_time_limit", "2023-06-15")
# 返回：户籍注销时限为30天（valid_to=2023-12-31）
```

### 查询 3：用户进度时间线

```python
def get_user_timeline(user_id_hash, procedure=None):
    """获取用户在某流程上的进度时间线"""
    return query(
        """
        MATCH (e:UserProgressEvent)
        WHERE e.user_id_hash = $user_id_hash
          AND ($procedure IS NULL OR e.procedure = $procedure)
        RETURN e
        ORDER BY e.timestamp ASC
        """,
        user_id_hash=user_id_hash,
        procedure=procedure
    )

# 示例返回：
[
    {"event": "inquiry_started", "procedure": "户籍注销", "timestamp": "2026-07-10T10:00:00Z"},
    {"event": "document_obtained", "procedure": "死亡证明", "timestamp": "2026-07-12T14:30:00Z"},
    {"event": "procedure_completed", "procedure": "户籍注销", "timestamp": "2026-07-15T09:00:00Z"}
]
```

### 查询 4：变更影响分析

```python
def get_changes_affecting_user(user_id_hash, since_date):
    """查询用户咨询后发生的政策变更"""
    # 1. 找到用户咨询的流程
    inquiries = query(
        "MATCH (e:UserProgressEvent {event_type: 'inquiry_started'}) RETURN e.procedure"
    )
    
    # 2. 找到这些流程在 since_date 后的变更
    changes = []
    for proc in inquiries:
        new_facts = query(
            """
            MATCH (f:PolicyFact)
            WHERE f.fact_type CONTAINS $proc
              AND f.transaction_time.from >= $since_date
            RETURN f
            """,
            proc=proc, since_date=since_date
        )
        if new_facts:
            changes.append({
                "procedure": proc,
                "new_facts": new_facts,
                "user_action_needed": True
            })
    return changes
```

## 与现有规则/智能体的集成

### 1. 与 retrieval-guardrails 集成

retrieval-guardrails 的 freshness_check 现在可以查询 Graphiti：

```python
# 扩展 retrieval-guardrails
def freshness_check(fact, current_date):
    # 1. 检查 fact 是否在 valid_time 范围内
    if not fact.valid_time.contains(current_date):
        return "outdated", f"该事实在 {fact.valid_time.to} 失效"
    
    # 2. 检查是否有新版本
    latest = get_current_fact(fact.jurisdiction, fact.fact_type)
    if latest.fact_id != fact.fact_id:
        return "superseded", f"已被新版本取代：{latest.content}"
    
    # 3. 检查 transaction_time 与当前差距
    age = current_date - fact.transaction_time.from
    if age.days > 180:
        return "stale", "记录超过 6 个月，建议复核"
    
    return "fresh", None
```

### 2. 与 integrity-framework 集成

integrity-framework 第八章的"时效校验"现在可以更精确：

```python
def check_temporal_consistency(response, query_date):
    """
    检查响应中的事实在 query_date 时是否有效。
    例如用户问"2023年时户籍注销时限是多少"，响应必须给 30天（而非当前的 60天）。
    """
    facts_in_response = extract_facts(response)
    issues = []
    for fact in facts_in_response:
        actual_at_time = get_fact_at_time(
            fact.jurisdiction, fact.fact_type, query_date
        )
        if actual_at_time.content != fact.content:
            issues.append({
                "fact": fact.content,
                "actual_at_time": actual_at_time.content,
                "issue": f"在 {query_date} 时该事实为：{actual_at_time.content}"
            })
    return issues
```

### 3. 与 death-aftercare-tracker 集成

子智能体 tracker 用 Graphiti 存储和查询用户进度：

```python
# death-aftercare-tracker 子智能体扩展

def record_user_event(user_id_hash, event_type, procedure, **metadata):
    """记录用户进度事件"""
    event = UserProgressEvent(
        event_id=uuid4(),
        user_id_hash=user_id_hash,
        session_id=current_session_id,
        event_type=event_type,
        procedure=procedure,
        timestamp=datetime.now(),
        location=current_user_location,
        metadata=metadata
    )
    graphiti.add(event)
    # 同时记录 trace span
    log_trace(span_type="tool", span_name="tool.graphiti.record_event", attributes={...})


def get_progress_report(user_id_hash):
    """生成用户进度报告"""
    timeline = get_user_timeline(user_id_hash)
    
    # 1. 已完成的事项
    completed = [e for e in timeline if e.event_type == "procedure_completed"]
    
    # 2. 已获取的文档
    documents = [e for e in timeline if e.event_type == "document_obtained"]
    
    # 3. 下一步建议（基于当前时态事实）
    next_steps = recommend_next_steps(timeline)
    
    # 4. 时限警告（基于 valid_time）
    warnings = check_time_limits(timeline)
    
    return {
        "completed_procedures": completed,
        "obtained_documents": documents,
        "next_steps": next_steps,
        "warnings": warnings,
        "last_activity": timeline[-1].timestamp if timeline else None
    }


def check_time_limits(timeline):
    """检查时限：某流程从开始到现在是否超时"""
    warnings = []
    for event in timeline:
        if event.event_type == "inquiry_started":
            # 查询该流程的有效时限
            time_limit = get_current_fact(
                event.location, 
                f"{event.procedure}_time_limit"
            )
            if time_limit:
                days_since = (datetime.now() - event.timestamp).days
                limit_days = extract_days(time_limit.content)
                if days_since > limit_days:
                    warnings.append({
                        "procedure": event.procedure,
                        "days_since": days_since,
                        "limit": limit_days,
                        "warning": f"{event.procedure} 已超时 {days_since - limit_days} 天"
                    })
    return warnings
```

### 4. 与 policy-researcher 集成

policy-researcher 写入新知识库时，自动提取时态事实：

```python
# policy-researcher 扩展

def update_knowledge_with_temporal(country, region, new_content, old_content=None):
    """更新知识库时同步更新时态记忆"""
    # 1. 提取新内容中的事实
    new_facts = extract_facts_from_markdown(new_content)
    
    # 2. 对比旧内容，找出变更
    if old_content:
        old_facts = extract_facts_from_markdown(old_content)
        changes = diff_facts(old_facts, new_facts)
        
        for change in changes:
            if change.type == "modified":
                # 旧事实的 valid_time.to 设为现在
                old_fact = graphiti.get(change.old_fact_id)
                old_fact.valid_time.to = datetime.now()
                graphiti.update(old_fact)
                
                # 新事实的 valid_time.from 设为现在
                new_fact = change.new_fact
                new_fact.valid_time.from = datetime.now()
                new_fact.supersedes = change.old_fact_id
                graphiti.add(new_fact)
                
            elif change.type == "added":
                new_fact = change.new_fact
                new_fact.valid_time.from = datetime.now()
                graphiti.add(new_fact)
    
    else:
        # 全新知识库，所有事实从现在开始有效
        for fact in new_facts:
            fact.valid_time.from = datetime.now()
            graphiti.add(fact)
    
    # 3. 记录知识库版本
    version = KnowledgeVersion(
        version_id=uuid4(),
        file_path=f"knowledge/regions/{country}/{region}.md",
        version_hash=hash(new_content),
        created_at=datetime.now(),
        changes=changes if old_content else None,
        author="policy-researcher"
    )
    graphiti.add(version)
```

## Graphiti 实现

### 用 Graphiti Python SDK

```python
# knowledge/_temporal/graphiti_client.py（伪代码）
"""
基于 Graphiti（Zep 2.0）的时态记忆客户端。
Graphiti 提供原生时态图查询，避免自己实现 Neo4j + 时态扩展。
"""
from graphiti import Graphiti
from graphiti.nodes import EntityNode, EpisodicNode
from graphiti.edges import EntityEdge

graphiti = Graphiti(
    neo4j_uri="bolt://localhost:7687",  # 自部署 Neo4j
    neo4j_user="neo4j",
    neo4j_password="...",
    llm_client=openai_client,  # 用于实体提取
)

# 添加时态事实
async def add_policy_fact(fact: PolicyFact):
    node = EntityNode(
        name=f"{fact.jurisdiction}_{fact.fact_type}",
        labels=["PolicyFact"],
        attributes={
            "content": fact.content,
            "fact_type": fact.fact_type,
            "jurisdiction": fact.jurisdiction,
            "valid_from": fact.valid_time.from_,
            "valid_to": fact.valid_time.to,
            "source": fact.source.citation,
            "source_url": fact.source.url,
            "trust_level": fact.source.trust_level,
            "supersedes": fact.supersedes,
        }
    )
    await graphiti.add_node(node)

# 时态查询
async def query_facts_at_time(jurisdiction, fact_type, at_time):
    return await graphiti.query(
        """
        MATCH (f:PolicyFact)
        WHERE f.jurisdiction = $jurisdiction
          AND f.fact_type = $fact_type
          AND f.valid_from <= $at_time
          AND (f.valid_to IS NULL OR f.valid_to >= $at_time)
        RETURN f
        """,
        params={
            "jurisdiction": jurisdiction,
            "fact_type": fact_type,
            "at_time": at_time
        }
    )
```

### 自部署 Neo4j（数据不出本地）

```yaml
# docker-compose.graphiti.yml（伪代码）
version: "3.8"
services:
  neo4j:
    image: neo4j:5.20
    environment:
      - NEO4J_AUTH=neo4j/change_me_in_prod
      - NEO4J_PLUGINS=["apoc"]
    ports:
      - "7687:7687"
      - "7474:7474"
    volumes:
      - neo4j_data:/data
    # 数据不出本地（满足 PIPL/GDPR）

volumes:
  neo4j_data:
```

## 与 LightRAG 的分工

| 维度 | LightRAG（无时态） | Graphiti（时态） |
|------|------------------|----------------|
| 实体类型 | 政策实体（流程/文档/机构） | 时态事实 + 用户事件 + KB 版本 |
| 关系类型 | 静态关系（requires/issued_by） | 时态关系（supersedes/preceded_by） |
| 查询 | "房产过户需要什么" | "2023 年时房产过户需要什么" |
| 更新 | 增量替换 | 保留历史，新增版本 |
| 用途 | 知识检索增强 | 时效校验 + 用户进度跟踪 |

**互补关系**：
- LightRAG 回答"现在是什么"
- Graphiti 回答"过去是什么"、"用户做到哪了"、"变了什么"

## 用例场景

### 用例 1：用户咨询历史政策

```
用户：我妈 2023 年 6 月去世，当时户籍注销时限是多少？
智能体：调用 get_fact_at_time("CN/beijing", "household_cancellation_time_limit", "2023-06-15")
       返回：30 天（valid_to=2023-12-31）
       响应：2023 年 6 月时户籍注销时限是 30 天（依据户口登记条例）。
            2024 年起改为 60 天（依据民法典继承编）。
```

### 用例 2：用户跨会话续接

```
会话 1（7月10日）：
  用户：我妈刚去世，需要办哪些手续？
  智能体：引导流程，记录 inquiry_started
  
会话 2（7月12日）：
  用户：我已经办完死亡证明了
  智能体：记录 document_obtained（死亡证明）
  
会话 3（7月15日）：
  用户：我办到哪一步了？
  智能体：调用 get_progress_report(user_id_hash)
       返回：
       - 已获取：死亡证明（7月12日）
       - 下一步：户籍注销（时限 60 天，剩余 55 天）
       - 警告：无
       响应：您已完成死亡证明办理（7月12日）。
            下一步是户籍注销，需要在 9月10日前办理（60 天时限）。
```

### 用例 3：政策变更通知

```
背景：用户 7月10日咨询时，某地户籍注销时限是 30 天
     7月12日，政策更新为 60 天
     7月15日，用户回来

智能体：
  1. 调用 get_changes_affecting_user(user_id_hash, since_date="2026-07-10")
  2. 发现：户籍注销时限从 30 天变更为 60 天
  3. 响应：提醒用户，户籍注销时限已更新为 60 天，您原本需要在 8月9日前办理，
          现在延长至 9月10日。
```

## 与 trace 的联动

```json
{
  "span_type": "tool",
  "name": "tool.graphiti.query",
  "attributes": {
    "tool_name": "graphiti_query",
    "query_type": "fact_at_time|user_timeline|changes_affecting_user",
    "jurisdiction": "CN/beijing",
    "fact_type": "household_cancellation_time_limit",
    "at_time": "2023-06-15",
    "result_count": 1,
    "result_summary": "户籍注销时限30天（valid_to=2023-12-31）",
    "latency_ms": 180
  }
}
```

## 数据保留策略

```python
# knowledge/_temporal/retention.py（伪代码）

RETENTION_POLICY = {
    "policy_facts": {
        "retention": "permanent",  # 永久保留（历史事实不能删）
        "archive_after": None
    },
    "user_progress_events": {
        "retention": "7_years",  # 7 年（继承诉讼时效）
        "archive_after": "1_year",  # 1 年后归档
        "pii_redact_on_archive": True  # 归档时进一步脱敏
    },
    "knowledge_versions": {
        "retention": "permanent",  # 永久保留
        "archive_after": "5_years"
    }
}

def apply_retention():
    """定期执行保留策略"""
    for event in get_old_user_events(older_than="7_years"):
        delete_event(event)
    
    for event in get_old_user_events(older_than="1_year"):
        archive_and_redact(event)
```

## 与 accountability-framework 的联动

事故记录可以引用 Graphiti 中的事实版本：

```json
{
  "incident_id": "uuid",
  "description": "智能体输出了过时的户籍注销时限",
  "trace_id": "uuid",
  "fact_id": "graphiti:policy_fact:12345",
  "fact_valid_time": "2020-01-01 to 2023-12-31",
  "response_time": "2026-07-13",
  "issue": "响应中使用了 valid_to=2023-12-31 的事实，但当前时间是 2026-07-13",
  "root_cause": "未调用 get_current_fact，使用了缓存"
}
```

## 试点范围

### 阶段 1：政策时态事实（P1）

- 仅 CN/overview.md
- 用 policy-researcher 手动注入 3-5 个时态事实
- 验证 get_current_fact / get_fact_at_time 查询

### 阶段 2：用户进度事件（P2）

- 接入 death-aftercare-tracker 子智能体
- 在 golden case 17（子智能体调用）上验证

### 阶段 3：变更通知（P2）

- 实现 get_changes_affecting_user
- 在新 case 中验证（用户跨会话场景）

### 阶段 4：扩展（P3）

- 扩展到所有地区
- 接入医疗政策（医疗政策时效性强）

## 成本估算

| 组件 | 月成本 |
|------|-------|
| Neo4j 自部署（4GB RAM） | $20（云 VM） |
| 时态事实存储（100 地区 × 50 事实） | 极低（图数据库） |
| 用户事件存储（10000 用户 × 10 事件） | 极低 |
| 查询 LLM 调用（实体提取） | $10/月 |

总成本：~$30/月

## 失败回退

若 Graphiti 试点失败：
1. 回退到 Markdown + last_updated 字段
2. 用户进度用 JSON 文件存储（牺牲时态查询能力）
3. 政策变更通知改为人工触发

## 版本
- v1.0 初始 Graphiti 时态记忆方案（双时间模型 + 3 类时态对象 + 4 种查询 + 与规则/智能体集成 + 用例场景 + 保留策略）
