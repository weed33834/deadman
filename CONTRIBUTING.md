# 贡献指南

> 欢迎为本平台贡献新智能体、新规则、新地域知识库、新测试场景。本指南说明各类贡献的流程与规范。

## 总则

1. 所有贡献必须遵守现有规则体系（`rules/`），不得削弱既有优先级链
2. 不得引入任何"代办""代查""出法律/医学诊断意见""编造不确定信息"的能力
3. 新增内容必须附置信度标注与来源透传
4. 新增/修改后须跑 `tests/golden-cases.md` 全部 20 case + `tests/scenarios.md` 全部 8 场景，确认无回归
5. 提交前在 `CHANGELOG.md` 记录变更

## 如何新增并列智能体

### 步骤

1. **在 `agents/` 新建 `{agent-name}.md`**，遵循统一格式：
   ```markdown
   ---
   name: {agent-name}
   description: |
       多行描述，说明何时触发、能力边界
   tools: Tool1, Tool2
   disallowedTools: OptionalTool
   ---

   # {智能体名}（并列智能体之一）

   ## 一、身份
   ...
   ```

2. **在 `agents/TEAM.md` 更新**：
   - 架构图（添加新智能体节点）
   - 并列智能体清单（添加条目）
   - 转介机制（说明何时转介到新智能体、新智能体何时转介到其他）
   - 私有子智能体（若有）

3. **设计私有子智能体**（可选）：
   - 在 `agents/` 新建 `{agent-name}-{subagent-name}.md`
   - 遵循子智能体规范：只服务于父智能体，不直接面对用户
   - 在父智能体的 system prompt 中说明何时调用哪个子智能体

4. **配置转介触发规则**：
   - 在新智能体的 system prompt 中列出"何时转介给谁"
   - 在其他智能体的 system prompt 中添加"何时转介到新智能体"

5. **遵守 rules/ 优先级链**：
   - 在新智能体的 system prompt 中明确必读 10 个主链规则文件（rules/ 共 14 个）
   - 优先级链与团队一致：safety > integrity > input-guardrails > compliance > risk-tier > transparency > accountability > retrieval-guardrails > tone

6. **更新测试**：
   - 在 `tests/scenarios.md` 添加覆盖新智能体的场景
   - 在 `tests/golden-cases.md` 添加覆盖新智能体的 case
   - 更新 `tests/scenarios.md` 测试覆盖矩阵
   - 更新 `tests/golden-cases.md` 附录 rules 文件覆盖矩阵

7. **更新文档**：
   - 更新 `README.md` 架构概览
   - 在 `CHANGELOG.md` 记录变更

### 注意事项

- 新智能体必须有清晰的**职责边界**（什么不做，比什么做更重要）
- 新智能体必须有自己的**L2 风险强制提示块**模板（若适用）
- 新智能体必须有**转介话术模板**与**转介摘要格式**
- 新智能体不得与现有智能体职责重叠

## 如何新增私有子智能体

### 步骤

1. **在 `agents/` 新建 `{parent-agent-name}-{subagent-name}.md`**
2. **在父智能体的 system prompt 中说明**：
   - 何时调用该子智能体
   - 调用时传入什么参数
   - 接收什么格式的报告
3. **在 `agents/TEAM.md` 更新父智能体的私有子智能体清单**
4. **更新 `README.md` 私有子智能体总数**

### 子智能体硬约束

- 子智能体**只服务于其父智能体**，不直接面对用户
- 子智能体**不服务于其他并列智能体**（隔离）
- 子智能体在独立上下文执行，结果以结构化报告返回给父智能体
- 子智能体的 tools 权限按需配置，**Write 权限**仅限 policy-researcher 的子智能体（建库/改库）

## 如何新增规则

### 步骤

1. **在 `rules/` 新建 `{rule-name}.md`**
2. **在 `rules/conflict-resolution.md` 更新优先级链**，明确新规则的层级
3. **在所有智能体的 system prompt 中添加必读项**
4. **在 `tests/golden-cases.md` 添加覆盖新规则的 case**
5. **更新 `README.md` 规则清单与优先级链**

### 规则硬约束

- 新规则必须明确**弹性等级**（零弹性 / 硬边界 / 弹性）
- 新规则必须说明与现有规则的**优先级关系**（赢谁、输谁）
- 新规则必须有**可客观验证的触发条件与响应要求**
- 新规则不得削弱 safety / integrity / input-guardrails 的零弹性地位

## 如何新增地域知识库

### 步骤

1. **在 `knowledge/regions/{ISO国家代码}/` 新建目录**（如 `GB/`、`AU/`）
2. **创建 `overview.md`**：国家级总览，按 `SCHEMA.md` 格式
3. **（可选）创建 `{地区代码}.md`**：州/省/市级，按 `SCHEMA.md` 格式
4. **在 `README.md` 知识库清单中添加条目**

### 文件命名规范

- 国家代码使用 ISO 3166-1 alpha-2（CN/US/AU/JP/GB/CA/SG/...）
- 地区代码使用国家内通用缩写：
  - 美国：州名小写（california、new-york、texas）
  - 中国：拼音（beijing、shanghai、guangdong）
  - 日本：罗马字（tokyo、osaka）
  - 加拿大：省名小写（ontario、british-columbia）
  - 澳大利亚：州名缩写（nsw、vic、qld）
- 若国家内政策高度统一（如新加坡），可仅有 `overview.md`

### 内容规范（按 `SCHEMA.md`）

- **元信息**必填：国家、地区、ISO代码、最后更新、数据来源、数据可信度
- **核心字段**必填：紧急联系方式、9 阶段流程
- **扩展字段**按当地实际情况填写，无则标注"本地区不适用"
- **未知字段**写"未知，需用户咨询当地[机构]"，不要留空，不要编造
- **金额**标注币种和金额范围
- **时限**写具体天数，不要写"尽快"
- **政策依据**尽可能标注法律/法规名称和条文
- **数据来源**每个关键信息后标注来源 URL 或官方文件名

### 更新规则

- 每次搜索生成新文件时，必须填写"最后更新"日期
- 超过 6 个月未更新的文件，agent 应提示用户"此信息可能已过时"
- 政策变更高发领域（税务/社保/银行），建议 3 个月复核一次

## 如何新增测试场景

### 联调测试场景（`tests/scenarios.md`）

1. **在现有场景后追加新场景**，编号递增（场景 9、10、...）
2. **场景结构**：
   - 用户输入（模拟）
   - 风险分级（L1/L2/L3 + 信号分析）
   - 涉及的智能体（并列 + 私有子）
   - 期望行为（步骤）
   - 验证点清单（可勾选的客观标准）
   - 反例（应避免的响应）
3. **更新测试覆盖矩阵**
4. **更新测试执行说明**（若新场景验证新的维度）
5. **更新版本号**

### Golden Case（`tests/golden-cases.md`）

1. **在现有 case 后追加新 case**，编号递增（Case 21、22、...）
2. **case 必须属于现有五类之一**（诚信/安全/转介/防御/跨团队转介与多信号），或新增类别时同步更新本文件结构
3. **case 必须标注**：
   - 类别
   - 场景上下文
   - 用户输入
   - 期望响应要点（可客观验证的特征）
   - 禁止响应（具体的话术或行为）
   - 验证规则（引用哪些 rules 文件）
   - 涉及智能体（含转介路径）
   - 优先级（验证哪条优先级链）
   - 回归判定（明确通过/失败的客观标准）
4. **更新覆盖范围汇总**（按类别统计、验证维度覆盖、跨团队覆盖）
5. **更新附录 rules 文件覆盖矩阵**
6. **更新版本号**

### case 失效的判定

- 某个 rules 文件被删除或合并 → 引用该文件的 case 需更新引用或标记失效
- 某个智能体被删除 → 引用该智能体的 case 需更新或标记失效
- 某个转介路径被修改 → 转介类 case 需更新预期路径
- case 失效**不删除**，标记为 `[已失效]` 并说明原因，保留历史追溯

## 文件命名规范

### 智能体文件
- 并列智能体：`{agent-name}.md`（如 `death-aftercare.md`、`medical-guide.md`）
- 私有子智能体：`{parent-agent-name}-{subagent-name}.md`（如 `death-aftercare-tracker.md`）

### 规则文件
- `{rule-name}.md`（如 `safety-protocol.md`、`integrity-framework.md`）

### 知识库文件
- 国家级总览：`knowledge/regions/{ISO国家代码}/overview.md`
- 地区级：`knowledge/regions/{ISO国家代码}/{地区代码}.md`

### 测试文件
- 联调测试场景：`tests/scenarios.md`
- 回归测试 case：`tests/golden-cases.md`

### 文档文件
- 顶级文档使用全大写：`README.md` / `CHANGELOG.md` / `CONTRIBUTING.md` / `PLATFORMS.md`
- 子目录文档使用大写：`agents/TEAM.md` / `knowledge/regions/SCHEMA.md`

## 版本号规范

### 格式
`v{major}.{minor}`（如 v3.0、v2.0）

### 递增规则
- **major**：架构变更（如新增/删除并列智能体、废弃既有架构、规则优先级链调整）
- **minor**：内容扩展（如新增子智能体、新增规则、新增地域知识库、新增测试场景）

### 记录位置
- 在 `CHANGELOG.md` 添加新版本条目，按时间倒序排列
- 在各文件末尾的"版本"章节追加条目

### 日期格式
`YYYY-MM`（如 2026-07）

## 提交检查清单

提交前确认：

- [ ] 新增/修改内容遵守现有规则体系，不削弱既有优先级链
- [ ] 未引入代办/代查/出法律医学诊断意见/编造不确定信息的能力
- [ ] 新增内容附置信度标注与来源透传
- [ ] `python -m pytest .traecli/src/tests/ -q` 全量通过（当前 918 passed + 1 skipped）
- [ ] `tests/golden-cases.md` 全部 20 case 通过
- [ ] `tests/scenarios.md` 全部 8 场景通过
- [ ] `agents/TEAM.md` 架构图与清单已更新（若涉及智能体变更）
- [ ] `README.md` 已更新（若涉及架构/智能体/规则变更）
- [ ] `CHANGELOG.md` 已记录变更
- [ ] 文件命名符合规范
- [ ] 版本号已递增

### pytest 测试规范

- 新增功能必须配套 pytest 测试（`src/tests/` 目录），不依赖外部 LLM API（用 `mock_llm_client` fixture）
- 涉及 Web 端点的改动优先用真实 `ThreadingHTTPServer` + 随机端口，不 mock HTTP 层
- 涉及 SSE 流的端到端验证参考 `e2e_frontend_user_flow.py`（httpx + SSE 解析模拟浏览器）
- 涉及编排终止条件的改动参考 `test_p10_termination.py`（frozen dataclass 用 `==` 断言）
- 测试用 `tmp_path` 隔离数据目录，不污染 `~/.deadman`
- 不引入新 pip 依赖（stdlib 优先）

## 联系与讨论

- 重大架构变更请先在团队内讨论，达成共识后再实施
- 规则优先级链调整属重大变更，须全体规则文件复核
- 测试场景/case 失效时，标记 `[已失效]` 而非删除，保留历史追溯
