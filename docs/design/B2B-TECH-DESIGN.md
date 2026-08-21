# deadman To B 技术设计文档

> 版本：v1.0（草稿）
> 状态：设计评审
> 关联：[产品设计文档](./B2B-PRODUCT-DESIGN.md)
> 代码基线：v0.1.0（`src/deadman`）

---

## 1. 架构总览

### 1.1 现状（C 端单机）

```text
浏览器 ──> FastAPI app.py（13 个路由模块）──> 业务 store ──> ~/.deadman/<module>/*.json
                                          └─> 编排/LLM/MCP（平台层）
auth.UserStore（单用户, role=user/admin）
infrastructure.multi_tenant（存在但默认关闭, 业务模块未接入）
db/（SQLAlchemy + Alembic, 双写骨架已铺, 业务未全切）
```

### 1.2 目标（To B 多租户）

```text
浏览器 ──> FastAPI app.py
            ├── 新增 org 路由（/api/org/**）: 机构/成员/客户/案件/审计/导出
            ├── 既有业务路由: 全部接入 TenantContext 强制租户路由
            └── 中间件: JWT → tenant_id/org_role → 设置 TenantContext
业务 store ──> 统一走 resolve_tenant_path()（文件模式）
          └─> 关键业务表迁 SQLite/PostgreSQL（db/ 双写 → 主库）
部署: SaaS = 多租户实例；私有化 = 单租户实例（default 租户）
```

### 1.3 分层职责

| 层 | 现有代码 | 改造 |
|---|---|---|
| 认证 | `auth/store.py` + `auth/jwt.py` | JWT 增加 `tenant_id`/`org_role`；双层级角色 |
| 租户 | `infrastructure/multi_tenant.py` | 默认开启；业务 store 全部接入 |
| 组织 | 新增 `org/` 模块 | 机构/成员/邀请/RBAC |
| 业务 | `ending_note`/`vault`/`cases`/... | 加 `tenant_id` 路由 + 客户档案绑定 |
| 数据 | `db/` + `db/repositories.py` | 新增组织/客户/案件表；文件→DB 渐进迁移 |
| 前端 | `admin.html` 技术栈 | 新增机构工作台 |
| 交付 | `billing/` + `compliance/` | 授权码 + 审计导出 |

---

## 2. 域模型与术语

| 术语 | 定义 |
|---|---|
| Organization | 机构 = 租户。独立数据空间、成员、套餐。 |
| Membership | 机构与用户的关联（机构内角色）。一个用户可属于多个机构。 |
| Customer | 机构管理的客户档案（本人 + 关系人），数据归属机构。 |
| Case | 客户下的办理案件（状态机 + 任务 + 分配 + 时间线）。 |
| CaseEvent / AuditLog | 操作留痕，按租户独立。 |
| 平台公共库 | 34 省政策 / 机构名录 / 热线，只读共享（非租户数据）。 |
| 机构私有库 | 机构自建知识（民俗模板 / SOP / 价格），按租户隔离。 |

关键原则：**业务数据归属机构，不归属个人员工**。员工离职，客户/案件留在机构。

---

## 3. 数据模型（DB 新增表）

基于现有 `db/models.py` 扩展，新增 Alembic 迁移 `0002_organization_tenant.py`。

### 3.1 organizations（机构）

```python
class Organization(Base):
    __tablename__ = "organizations"
    id: str = Column(String(36), primary_key=True)      # uuid4
    name: str = Column(String(120), nullable=False)
    slug: str = Column(String(80), unique=True, index=True)   # 客户门户域名
    industry_template: str = Column(String(32), default="funeral")  # 行业模板
    status: str = Column(String(16), default="active")   # active|suspended|expired
    plan: str = Column(String(32), default="free")        # free|pro|enterprise
    features: Column(JSON, default=list)                  # 模块开关
    quotas: Column(JSON, default=dict)                    # token/存储/工具 配额
    license_key: Column(String(128), nullable=True)       # 私有化授权码
    created_at: Column(DateTime)
    expires_at: Column(DateTime, nullable=True)
```

### 3.2 memberships（成员关系）

```python
class Membership(Base):
    __tablename__ = "memberships"
    org_id: str = Column(String(36), ForeignKey("organizations.id"), primary_key=True)
    user_id: str = Column(String(36), ForeignKey("users.id"), primary_key=True)
    org_role: str = Column(String(24), nullable=False)   # org_admin|case_manager|consultant|viewer
    status: str = Column(String(16), default="active")   # active|disabled
    invited_by: Column(String(36))
    joined_at: Column(DateTime)
```

### 3.3 customers（客户档案）

```python
class Customer(Base):
    __tablename__ = "customers"
    id: str = Column(String(36), primary_key=True)
    org_id: str = Column(String(36), ForeignKey("organizations.id"), index=True)
    display_name: Column(String(120))
    province: Column(String(32))                          # 关联政策库省份
    stage: Column(String(32), default="planning")         # planning|funeral|settlement|done
    owner_user_id: Column(String(36), nullable=True)      # 主办员工
    relationships: Column(JSON, default=list)             # 家属/紧急联系人/律师/继承人
    tags: Column(JSON, default=list)
    created_at / updated_at: Column(DateTime)
```

### 3.4 cases（案件）+ case_events（状态/审计）

```python
class Case(Base):
    __tablename__ = "cases"
    id / org_id(索引) / customer_id(外键)
    case_type: str          # 治丧 / 遗产 / 理赔 / 公证 ...
    status: str             # 状态机，见 §3.5
    stage: str
    assignee_user_id: str | None
    priority: str = "normal"
    source: str = "manual"  # manual|portal|chat
    created_at / updated_at / closed_at

class CaseEvent(Base):  # 兼作审计（谁/何时/对哪个案件/做了什么）
    __tablename__ = "case_events"
    id / org_id(索引) / case_id
    actor_user_id: str
    action: str                 # case.create|assign|status_change|material_generate|...
    detail: Column(JSON)        # 变更前后值
    created_at: Column(DateTime)
```

### 3.5 案件状态机（复用现有 `deadman_switch` 状态机模式）

```text
created → assigned → in_progress → pending_input（等客户材料）
   ↓            ↓            ↓
   └─────── closed(done|cancelled) ← 可重开
校验规则：仅 case_manager/org_admin 可改状态；状态变更必须落 CaseEvent。
```

---

## 4. 租户隔离设计（核心）

### 4.1 三层强制

```text
L1 路由层：业务 store 的 data_dir 统一走 resolve_tenant_path()
   ~/.deadman/tenants/<tenant_id>/ending_note/...
L2 运行层：请求中间件从 JWT 取 tenant_id → TenantContext 压栈
   所有 store 读写自动落到当前租户目录
L3 校验层：测试断言「无 TenantContext 时写库直接抛错」
```

### 4.2 改造 `multi_tenant.py`

- `DEADMAN_MULTI_TENANT_ENABLED` 默认值改为 `1`（平台强制）。
- `TenantInfo` 增加 `industry_template`、`license_key` 字段。
- `get_current_tenant()` 在 `None` 时，从 ContextVar 兜底读取请求级缓存。

### 4.3 需要接线的 store 清单（逐个改 `data_dir` 来源）

| store | 现状路径 | 改后 |
|---|---|---|
| `auth/store.py` | `~/.deadman/auth` | 用户表是全局的（跨机构），**不隔离**；membership 负责归属 |
| `ending_note/store.py` | `~/.deadman/ending_note` | `tenants/<tid>/ending_note` |
| `vault/store.py` | `~/.deadman/vault` | `tenants/<tid>/vault` |
| `deadman_switch/store.py` | `~/.deadman/deadman_switch` | `tenants/<tid>/deadman_switch` |
| `support/store.py` | `~/.deadman/support` | `tenants/<tid>/support` |
| `onboarding/store.py` | `~/.deadman/onboarding` | `tenants/<tid>/onboarding` |
| `digital_legacy/*` | 依 data_dir | `tenants/<tid>/digital_legacy` |
| `plan_score`（只读计算） | — | 从客户数据推导，无独立存储 |

> 平台公共数据（`src/knowledge`、`src/rules`、`src/agents`、institutions、hotlines）
> **不租户化**，作为只读公共库保留在包内。

### 4.4 中间件

新增 `web/middleware.py` 的 `TenantMiddleware`（或复用现有中间件链）：

```python
async def TenantMiddleware(request, call_next):
    token = 从 Authorization Bearer 解析 JWT
    tenant_id, org_role = jwt.get("tenant_id"), jwt.get("org_role")
    if not tenant_id:
        tenant_id = DEFAULT_TENANT_ID          # 单租户/私有化
    tenant = registry.get(tenant_id) or TenantInfo(id=tenant_id, name="default")
    with TenantContext(tenant):
        request.state.tenant = tenant
        request.state.org_role = org_role
        return await call_next(request)
```

---

## 5. 认证与授权（双层 RBAC）

### 5.1 JWT 载荷扩展

现有 JWT（`sub=user_id`）扩展为：

```json
{
  "sub": "user-xxx",
  "tenant_id": "org-xxx",
  "org_role": "case_manager",
  "platform_role": "user"
}
```

> 一个用户多机构：每次登录选择当前机构（`active_org`），重新签发 JWT。
> 切换机构 = 重新签发（后端校验 membership 存在且 active）。

### 5.2 双层角色

| 平台层 | 机构层 | 说明 |
|---|---|---|
| platform_admin | — | 平台方，管理所有机构/套餐/封禁 |
| user | org_admin | 机构管理员 |
| user | case_manager | 办理员 |
| user | consultant | 顾问（查看为主） |
| user | viewer | 只读 |

### 5.3 机构内权限矩阵

| 能力 | org_admin | case_manager | consultant | viewer |
|---|---|---|---|---|
| 查看机构仪表盘 | ✅ | ✅ | ✅ | ✅ |
| 成员管理（邀请/改角色/禁用） | ✅ | ❌ | ❌ | ❌ |
| 客户 CRUD | ✅ | ✅ | ❌ | ❌ |
| 查看客户（含关系人） | ✅ | ✅ | ✅ | ✅ |
| 案件创建/分配/推进 | ✅ | ✅ | ❌ | ❌ |
| 生成材料包/通知信函 | ✅ | ✅ | ✅ | ❌ |
| 编辑机构知识库 | ✅ | ✅ | ❌ | ❌ |
| 查看审计日志 | ✅ | ❌ | ❌ | ❌ |
| 数据导出 | ✅ | ❌ | ❌ | ❌ |
| 套餐/授权码 | ✅ | ❌ | ❌ | ❌ |

### 5.4 实现

- 复用 `web/routes/iam.py` 的角色枚举与权限矩阵模式，新增 `org/` 模块的 `ORG_ROLES` / `ORG_PERMISSIONS`。
- 新增依赖函数（`web/deps.py`）：`require_org_role("case_manager")`，同时校验 membership active。
- 现有 `require_admin` 语义变更为「platform_admin 或 该机构 org_admin」并注入 tenant 校验。

---

## 6. API 设计（新增 `/api/org` 路由）

### 6.1 机构与成员

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/orgs` | platform_admin | 创建机构（含行业模板/套餐） |
| GET | `/api/orgs/me` | 任意机构成员 | 当前机构资料+套餐+配额 |
| PATCH | `/api/orgs/{org_id}` | org_admin / platform_admin | 更新机构 |
| POST | `/api/orgs/{org_id}/members/invite` | org_admin | 生成邀请（email+token, 24h） |
| POST | `/api/orgs/invites/accept` | 用户（登录态） | 凭 token 加入机构 |
| GET | `/api/orgs/{org_id}/members` | org_admin | 成员列表 |
| PATCH | `/api/orgs/{org_id}/members/{user_id}` | org_admin | 改角色/禁用 |
| DELETE | `/api/orgs/{org_id}/members/{user_id}` | org_admin | 移除成员（客户留机构） |
| POST | `/api/orgs/{org_id}/switch` | 机构成员 | 切换当前机构（重签 JWT） |

### 6.2 客户与案件

| 方法 | 路径 | 权限 |
|---|---|---|
| GET/POST | `/api/org/customers` | case_manager+ |
| GET/PATCH/DELETE | `/api/org/customers/{id}` | case_manager+（DELETE 需 org_admin） |
| GET | `/api/org/customers/{id}/profile` | case_manager+（档案+关系人+进度汇总） |
| GET/POST | `/api/org/cases?customer_id=` | case_manager+ |
| GET/PATCH | `/api/org/cases/{id}` | case_manager+ |
| POST | `/api/org/cases/{id}/assign` | org_admin / case_manager |
| POST | `/api/org/cases/{id}/events` | case_manager+（留痕） |
| POST | `/api/org/cases/{id}/material` | case_manager+（复用 memorial/letters 生成器） |

### 6.3 审计与导出

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/org/audit-logs?filters` | org_admin | 按租户查询审计（分页） |
| GET | `/api/org/audit-logs/export` | org_admin | CSV/JSON 导出 |
| POST | `/api/org/export` | org_admin | 全量数据导出（异步，可下载） |

### 6.4 机构知识库

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/org/kb` | 机构成员（平台公共库 + 机构私有库合并视图） |
| POST/PATCH/DELETE | `/api/org/kb/{doc_id}` | case_manager+ |

---

## 7. 安全与合规落地

### 7.1 越权防护（强制，不是靠自觉）

1. 所有 `/api/org/**` 路由依赖 `require_org_role`，从 JWT 取 tenant，**不接受客户端传 tenant_id 覆盖**。
2. store 层：`tenant_id` 一律来自 `get_current_tenant_id()`，业务代码不可覆盖。
3. 新增专项测试 `test_tenant_isolation.py`：
   - 机构 A 的 token 访问机构 B 的 customer/case → 403
   - 无 JWT → 401
   - 无 TenantContext 写库 → 抛错
   - 已禁用成员 → 403
4. 策略注入攻击（`/api/org/export` 路径穿越、材料包模板注入）复用现有 `input-guardrails` 检测。

### 7.2 政策输出四要素（数据纪律）

复用 `RuleChecker`，对知识库检索结果强制：

```text
政策断言 = 内容 + [来源URL] + [核实日期] + 「如有变更以官方为准」 + 转人工/机构链接
缺失来源的政策断言：系统层拦截（返回 400，不进入 LLM 自由发挥）
```

### 7.3 加密边界

- `ending_note`/`vault` 的 per-user passphrase 需改为 **per-tenant 密钥**：
  `HMAC(platform_secret, "tenant:" + tenant_id)`，机构内成员共享解密（机构是数据主体）。
- 客户身份证/银行账号仍走既有 `_mask_pii()` 脱敏规则。

### 7.4 审计

- `CaseEvent` 落库即审计；机构级操作（成员/套餐/导出）写入 `compliance/audit_report`。
- 审计日志只增不改，删除仅限 platform_admin + 留痕。

---

## 8. 前端工作台

### 8.1 技术方案

- 新增 `web/static/org.html`（机构控制台），复用 `admin.html` 的技术栈（原生 JS + fetch + 同一套 CSS 美学），不引入前端框架。
- 移动端保持 C 端 `mobile.html` 不进入工作台（机构员工主要桌面办公）。

### 8.2 页面结构

```text
org.html（SPA 侧边导航）
├── dashboard.html-slot   仪表盘卡片 + 待办 + 图表
├── customers           客户列表（搜索/筛选/分页）→ 客户详情
├── cases               案件列表 → 案件详情（状态机 + 时间线 + 材料包）
├── kb                  知识库（平台库只读 + 机构库编辑）
├── members             成员列表 / 邀请表单 / 角色下拉
├── audit               审计日志表 + 导出按钮
└── settings            机构资料 / 模板 / 套餐配额 / 数据导出
```

### 8.3 复用与新增

| 复用 | 新增 |
|---|---|
| `admin.html` 的布局/样式/图表 | org.html 整体 + 客户/案件/成员/审计面板 |
| `web/routes/resources.py` 的资源 CRUD 模式 | `/api/org/**` 前后端联调 |
| 材料包生成（memorial/letters） | 客户详情页内嵌「生成材料包」流程 |

---

## 9. 存储迁移策略（文件 → DB）

### 9.1 原则

- **不一步到位**：保留文件存储为兼容层，新增 DB 表承载组织/客户/案件/审计。
- 业务数据（ending_note/vault 等加密内容）继续走租户文件目录（加密要求高、查询模式简单）。
- 结构化查询数据（客户/案件/成员/审计）走 DB。两套并存，接口由 store 层统一。

### 9.2 迁移动作

1. `db/models.py` 增加 §3 的表；Alembic 生成 `0002_organization_tenant.py`。
2. `db/repositories.py` 新增 `OrganizationRepository` / `CustomerRepository` / `CaseRepository`。
3. 存量 C 端数据迁移到 `default` 租户：
   ```bash
   deadman org migrate-legacy   # 把 ~/.deadman/{ending_note,vault,...} 复制到 tenants/default/
   ```
4. `DATABASE_URL` 为空时：`/api/org/**` 降级到 JSON 文件实现（私有化单机可用，SaaS 必配 DB）。

---

## 10. 部署形态

| 形态 | 配置 | 说明 |
|---|---|---|
| SaaS 多租户 | `DEADMAN_MULTI_TENANT_ENABLED=1` + PostgreSQL | 平台托管，机构按租户隔离 |
| 私有化单租户 | `DEADMAN_MULTI_TENANT_ENABLED=0`（default 租户）+ SQLite | 交付镜像 + 授权码 |
| 现有 C 端 | 不变（default 租户） | 向后兼容 |

私有化镜像改造点（现有 `Dockerfile`）：
- 去掉默认 `CMD mcp-server`，改为 `web-server`。
- 授权码挂载：`/app/license.lic` 或 `DEADMAN_LICENSE_KEY` 环境变量。
- 修复陈旧 `ARG DEADMAN_VERSION=5.1` / `LABEL license=MIT`，统一由 `pyproject` 单源注入。

---

## 11. 里程碑实施计划（细化到文件）

### M1 组织地基（目标 3–5 天）

- [ ] 新增 `src/deadman/org/` 模块：
  - `models.py`（Organization/Membership + store）
  - `invites.py`（邀请 token 生成/消费，复用 `auth/password_reset.py` 模式）
  - `rbac.py`（ORG_ROLES/ORG_PERMISSIONS + `require_org_role` 依赖）
- [ ] `auth/jwt.py`：JWT 增加 `tenant_id`/`org_role`；登录/切换机构逻辑
- [ ] `web/routes/org.py`：§6.1 路由
- [ ] `db/models.py` + 迁移 `0002`：organizations / memberships 表
- [ ] 测试：`test_org_base.py`（机构 CRUD / 邀请 / 角色）

### M2 数据隔离（目标 3–5 天）

- [ ] `multi_tenant.py`：默认开启 + TenantInfo 扩展
- [ ] `web/middleware.py`：TenantMiddleware
- [ ] 业务 store 接入 `resolve_tenant_path`（§4.3 清单，7 个 store）
- [ ] 测试：`test_tenant_isolation.py`（跨租户 403 / 无上下文抛错）

### M3 客户与案件（目标 4–6 天）

- [ ] `db` 新增 customers / cases / case_events 表 + repositories
- [ ] `web/routes/org_customers.py`、`web/routes/org_cases.py`（§6.2）
- [ ] 案件状态机 + 分配 + 时间线
- [ ] 测试：`test_org_cases.py`（全流程 + 越权）

### M4 工作台（目标 5–7 天）

- [ ] `web/static/org.html` 骨架 + 导航
- [ ] 仪表盘 / 客户列表 / 客户详情 / 案件详情 / 成员管理 面板
- [ ] 材料包生成嵌入客户详情
- [ ] 联调 + 冒烟测试

### M5 交付与合规（目标 4–5 天）

- [ ] `billing/license.py`：授权码签发/校验（HMAC-SHA256）
- [ ] `web/routes/org_export.py`：数据导出 + 审计导出
- [ ] 私有化 Docker 交付调整（默认 web-server + license 注入）
- [ ] 知识更新订阅端点（套餐开关）

### M6 清理与收敛（并行）

- [ ] 双 web server 收敛（`server.py` → `app.py` 单入口）
- [ ] mcp/a2a/debate/reflexion/marketplace/plugins/sandbox → `deadman_legacy/`
- [ ] 版本单源（pyproject）+ 修 Dockerfile 陈旧引用
- [ ] 更新 README/CHANGELOG 叙事为 To B

---

## 12. 兼容性与风险

| 风险 | 缓解 |
|---|---|
| 存量 C 端用户数据迁移 | `org migrate-legacy` 迁移到 default 租户，原路径只读兼容一个版本 |
| 文件→DB 双轨不一致 | store 层单入口；审计/客户/案件只进 DB，业务密文留文件 |
| 越权回归 | `test_tenant_isolation.py` 进 CI，作为 P0 门禁 |
| LLM 用量被机构滥用 | 复用 `billing/usage_tracker` 按租户配额，超限降级 |
| 私有化客户无 DB | `DATABASE_URL` 空时 `/api/org` 文件降级实现，M5 明确边界 |

---

## 13. 验收清单（技术侧）

1. `pip install .` 后（非 editable）`uvicorn deadman.web.app:app` 可运行，rules/knowledge 正常加载。
2. 机构 A / 机构 B 完全隔离，交叉访问 403。
3. 案件全流程（建客户→办案→材料包→归档）无人工干预可跑通。
4. 审计可追溯：每一次状态变更都有 actor/action/detail。
5. 授权码：无码试用 30 天，过期只读，卸载不泄露。
6. 全量测试通过 + ruff/mypy 通过。