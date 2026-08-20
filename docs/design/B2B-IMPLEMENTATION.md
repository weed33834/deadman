# deadman To B 改造实施方案

> 版本：v1.0（草稿）
> 状态：实施前评审
> 依据：[产品设计](./B2B-PRODUCT-DESIGN.md) · [技术设计](./B2B-TECH-DESIGN.md)
> 目标：把现有 C 端单机改造为「多租户机构平台」，同时保持 C 端向后兼容

---

## 0. 改造总原则

1. **先隔离后迁移**：组织/客户/案件走新 `org/` 域，不污染既有业务模块；业务 store 只改「默认目录来源」，不动读写逻辑。
2. **部署模式由环境变量决定，不写死**：`DEADMAN_TENANT_MODE=single|multi`，单机 C 端/私有化 = single，SaaS = multi。
3. **每步可独立验收、可回滚**：每步以提交为单位，含测试。
4. **加密边界一次改到位**：`ending_note`/`vault` 从 per-user passphrase 改 per-tenant 派生（§Step 4），避免二次迁移。

> 现存问题一并修复（涉及 `web/deps.py:144`）：`require_admin(strict=True)` 检查 `user.get("is_admin")`，但 `auth/store.py` 的用户记录和 `_public_view` 都没有 `is_admin` 字段——strict 模式实际永远 403。改造时并入双层角色体系。

---

## Step 1 · 骨架与配置

### 1.1 新增租户模式配置

**改 `src/deadman/infrastructure/multi_tenant.py`**

```python
# 顶部新增
TENANT_MODE = os.environ.get("DEADMAN_TENANT_MODE", "single")  # single | multi

def is_multi_tenant_enabled() -> bool:
    # single（私有化/C 端）→ False，路径保持 ~/.deadman/（向后兼容）
    # multi（SaaS）→ True，路径切 ~/.deadman/tenants/<tid>/
    if TENANT_MODE == "multi":
        return True
    return is_enabled("multi_tenant")  # 保留旧 feature flag 兜底
```

- 不改 `_DEFAULTS["multi_tenant"]`（避免破坏现有 `data/feature_flags.json` 语义）。
- 默认 `single`：现有所有数据原地可用，零迁移。

### 1.2 新增 org 数据目录配置

**改 `src/deadman/config.py`**，新增：

```python
# 机构域数据目录（成员/机构资料/邀请；客户与案件走 DB，见 Step 5）
org_data_dir: Path = Path(os.getenv("DEADMAN_ORG_DATA_DIR",
    str(Path.home() / ".deadman" / "org")))
```

**改 `.env.example`**：补 `DEADMAN_TENANT_MODE=single` 与 `DEADMAN_ORG_DATA_DIR=`。

### 1.3 验收

- `python -c "from deadman.infrastructure.multi_tenant import is_multi_tenant_enabled; print(is_multi_tenant_enabled())"` → `False`
- `DEADMAN_TENANT_MODE=multi python -c ...` → `True`
- 全量测试仍绿。

---

## Step 2 · 机构域模型（文件版，先不依赖 DB）

> 决策：M1–M3 阶段机构/成员先用文件 store（复用现有各 store 的 JSON 原子写模式），
> 客户/案件/审计在 Step 5 直接建 DB 表。理由：文件版先跑通逻辑与测试，DB 表随后并行落地，避免一开始就绑 SQLAlchemy。

### 2.1 新增 `src/deadman/org/` 包

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `models.py` | Organization / Membership 数据类 + 状态机 | `Organization.to_dict()` |
| `store.py` | 机构与成员的 JSON 存储 | `create_org / get_org / update_org / add_member / get_member / list_members / set_role / set_status` |
| `invites.py` | 邀请令牌 | `create_invite(org_id, email, role, ttl=24h) / consume_invite(token)`（复用 `auth/password_reset.py` 的 token store 模式） |
| `rbac.py` | 机构角色与权限 | `ORG_ROLES` / `ORG_PERMISSIONS` / `can(role, action)` |

`store.py` 存储路径：`settings.org_data_dir / "orgs.json"`，原子写 + `threading.RLock`（与 `auth/store.py` 一致）。

### 2.2 数据模型（文件版，与设计文档 §3 对齐）

```python
@dataclass
class Organization:
    org_id: str
    name: str
    slug: str
    industry_template: str = "funeral"   # 行业模板
    status: str = "active"               # active|suspended|expired
    plan: str = "free"                   # free|pro|enterprise
    features: list[str] = field(default_factory=list)
    quotas: dict = field(default_factory=dict)
    created_at: float = 0.0
    expires_at: float | None = None

@dataclass
class Membership:
    org_id: str
    user_id: str
    org_role: str = "viewer"             # org_admin|case_manager|consultant|viewer
    status: str = "active"               # active|disabled
    invited_by: str | None = None
    joined_at: float = 0.0
```

### 2.3 验收

- 新增 `src/tests/test_org_base.py`：建机构 / 邀请 / 消费邀请加入 / 改角色 / 禁用 / 移除；两个机构互不影响。
- 测试全绿。

---

## Step 3 · JWT 扩展 + 机构切换

### 3.1 改 `src/deadman/auth/jwt.py`

`issue()` 增加可选机构上下文，并新增切换方法：

```python
def issue(self, user: dict, tenant_id: str | None = None,
          org_role: str | None = None) -> str:
    payload = {
        "user_id": user.get("user_id"),
        "email": user.get("email"),
        "role": user.get("role", "user"),          # 平台层角色（保留）
        "tenant_id": tenant_id,                    # 机构 id
        "org_role": org_role,                      # 机构内角色
        "family_id": user.get("family_id"),
        "iat": now, "exp": now + self.expiry_seconds,
    }
    ...

def switch_org(self, user: dict, org_id: str, org_role: str) -> str:
    """切换当前机构：校验 membership active 后重签 JWT"""
    return self.issue(user, tenant_id=org_id, org_role=org_role)
```

### 3.2 新增登录侧接口

- 登录/注册端点（在 `web/app.py` 或既有 auth 路由）支持 `active_org`：
  - 用户存在唯一 active membership → 自动带入 `tenant_id/org_role`。
  - 无机构 → `tenant_id=None`，进入 C 端匿名/单租户路径。
- 新增 `POST /api/orgs/switch`：body `{org_id}` → 校验 membership → 重签 JWT 返回。

### 3.3 验收

- 签发→校验往返正确；无机构时 `tenant_id` 为 `None`；切换后 payload 变化。

---

## Step 4 · 租户中间件 + 业务 store 接线

### 4.1 新增 `web/middleware.py::TenantMiddleware`

```python
async def TenantMiddleware(request, call_next):
    tenant_id, org_role = DEFAULT_TENANT_ID, None
    if TENANT_MODE == "multi":          # 仅多租户模式强制绑定
        token = 从 Authorization 头解析
        payload = JWTManager.verify(token)
        tenant_id = payload.get("tenant_id") or DEFAULT_TENANT_ID
        org_role = payload.get("org_role")
    tenant = registry.get(tenant_id) or TenantInfo(tenant_id=tenant_id, name="default")
    with TenantContext(tenant):
        request.state.tenant_id = tenant_id
        request.state.org_role = org_role
        return await call_next(request)
```

> 单租户（`single`）模式走 `DEFAULT_TENANT_ID`，路径与现状完全一致，C 端不受影响。

### 4.2 业务 store 改「默认目录来源」

每个 store 只改一行默认值 + 加密密钥来源，不动读写逻辑：

| store | 原默认目录 | 改后 |
|---|---|---|
| `ending_note/store.py:196` | `Path.home()/".deadman"/"ending_notes"` | `resolve_tenant_path("ending_notes")` |
| `vault/store.py:123` | `Path.home()/".deadman"/"vault"` | `resolve_tenant_path("vault")` |
| `deadman_switch/store.py` | `settings.switch_data_dir` | `resolve_tenant_path("deadman_switch")` |
| `support/store.py` | `settings.support_data_dir` | `resolve_tenant_path("support")` |
| `onboarding/store.py` | `settings.onboarding_data_dir` | `resolve_tenant_path("onboarding")` |
| `digital_legacy/*` | 各自 data_dir | `resolve_tenant_path("digital_legacy")` |

改法示意（`ending_note/store.py:190`）：

```python
def __init__(self, data_dir: Path | None = None) -> None:
    self.data_dir = data_dir or resolve_tenant_path("ending_notes")
```

`config.py` 中对应 `*_data_dir` 的默认值同样改为 `resolve_tenant_path(...)`（保持 single 模式向后兼容）。

### 4.3 加密从 per-user 改 per-tenant（一次改到位）

- `ending_note/store.py` 与 `deadman_switch/store.py` 的 passphrase 派生：
  ```python
  # 原: HMAC(global_secret, "ending-note:" + user_id)
  # 改: HMAC(platform_secret, "tenant:" + tenant_id + ":ending-note")
  # 保留 _decrypt_v1() 兼容路径，读旧 per-user 数据后迁移
  ```
- 新增迁移 CLI：`deadman org migrate-crypto`（旧 envelope → 新 envelope）。
- `vault` 同理。

### 4.4 越权测试（P0 门禁）

新增 `src/tests/test_tenant_isolation.py`：
- `TENANT_MODE=multi` 下，机构 A 的 token 读写机构 B 的 ending_note/vault/case → 403。
- 无 TenantContext 时写库 → 抛 `RuntimeError`（在 `resolve_tenant_path` 增加单测钩子断言）。
- 已禁用成员 → 403。

### 4.5 验收

- single 模式：既有全部测试不变绿。
- multi 模式：隔离测试全绿。

---

## Step 5 · 客户档案 + 案件（DB 版）

### 5.1 建表

**改 `src/deadman/db/models.py`**，新增 `customers` / `cases` / `case_events`（结构见技术设计 §3.3–3.4）。
**新增 `migrations/versions/0002_organization_tenant.py`**，`alembic upgrade head` 可用。

**改 `src/deadman/db/repositories.py`**，新增：
`CustomerRepository`（按 org_id 过滤，`list_by_org(org_id)` / `get(org_id, id)` 双键校验越权）、
`CaseRepository`、`CaseEventRepository`（只增不改）。

> `DATABASE_URL` 为空（私有化单机）时，此 Step 提供文件降级实现（`org/file_customers.py`），
> 接口签名与 repository 一致；SaaS 必配 PostgreSQL。

### 5.2 新增路由

- `src/deadman/web/routes/org_customers.py`：`/api/org/customers` CRUD（技术设计 §6.2）。
- `src/deadman/web/routes/org_cases.py`：`/api/org/cases` CRUD + assign + events + material。

统一依赖：`Depends(require_org_role("case_manager"))`。

### 5.3 案件状态机

```python
CASE_FLOW = {
    "created":      {"to": ["assigned", "in_progress", "cancelled"]},
    "assigned":     {"to": ["in_progress", "cancelled"]},
    "in_progress":  {"to": ["pending_input", "closed"]},
    "pending_input":{"to": ["in_progress", "closed"]},
    "closed":       {"to": ["in_progress"]},     # 重开
    "cancelled":    set(),
}
```
每次状态变更强制写 `case_events`。

### 5.4 验收

- `src/tests/test_org_cases.py`：全流程（建客户→办案→分配→状态流转→材料包→归档）+ 越权 403 + 审计完整。

---

## Step 6 · 机构工作台前端

### 6.1 新增 `web/static/org.html`

复用 `admin.html` 布局/CSS/图表，SPA 侧边导航：仪表盘 / 客户 / 案件 / 知识库 / 成员 / 审计 / 设置。

### 6.2 新增 `web/routes/org_pages.py`

```python
router = APIRouter()
@router.get("/org")        # 返回 org.html
@router.get("/api/org/dashboard")   # 客户数/进行中/待办/到期（聚合查询）
@router.get("/api/org/kb")          # 平台公共库 + 机构私有库合并视图
@router.post("/api/org/kb/{doc_id}")# 机构私有知识 CRUD
```

### 6.3 挂载

**改 `src/deadman/web/app.py`**（`app.include_router` 区域新增 3 行）：
`org_pages` / `org_customers` / `org_cases`。

### 6.4 验收

- 登录 → `/org` 工作台可用；单机构 30 分钟无培训完成「建客户→办案→材料包」。

---

## Step 7 · 授权码 + 数据导出

### 7.1 授权码

**新增 `src/deadman/billing/license.py`**：

```python
def sign_license(secret, payload) -> str       # HMAC-SHA256, base64
def verify_license(secret, token) -> dict|None # 校验签名+有效期
def license_status() -> dict                   # 试用/正式/过期（读 DEADMAN_LICENSE_KEY 或 /app/license.lic）
```

启动 hook（`web/app.py` lifespan）：`single` 模式无码进入 30 天试用，过期机构只读。

### 7.2 数据导出

**新增 `src/deadman/web/routes/org_export.py`**：
- `GET /api/org/audit-logs?filters` + `/export`（CSV/JSON）。
- `POST /api/org/export`：全量（客户/案件/审计/知识库），异步打包 zip，进度查询。

### 7.3 验收

- `src/tests/test_org_export.py`：导出内容仅含本机构数据；审计含 actor/action/detail。

---

## Step 8 · 清理与收敛

### 8.1 双 web server 收敛

- `pyproject.toml` 移除 `deadman-web-server-legacy` 入口；`web/server.py` 标记废弃（M6 后删除）。
- 统一入口 `uvicorn deadman.web.app:app`。

### 8.2 平台层冻结

- 新建顶层目录 `legacy/`（或 `deadman_legacy/`），将 `mcp_server`(client)、`a2a`、`debate`、`reflexion`、`marketplace`、`plugins`、`selfcheck`、`sandbox`(执行)、`evaluation/ragas` 移入；`pyproject` 移除对应 extra。
- `README/CHANGELOG/PLATFORMS.md` 对外叙事改为 To B。

### 8.3 版本与打包修复（沿用设计文档 §10）

- Dockerfile：`ARG DEADMAN_VERSION` / `LABEL license` / 默认 `CMD web-server`，全部由 `pyproject` 单源注入。
- 打包：rules/knowledge/agents/skills 纳入 `package-data`，保证非 editable 安装可运行。

### 8.4 验收

- `pip install .`（非 editable）后 `uvicorn deadman.web.app:app` 可运行。
- 全量测试 + ruff + mypy 通过。

---

## 实施顺序与工作量估算

| Step | 内容 | 预计 | 依赖 |
|---|---|---|---|
| 1 | 骨架与配置 | 0.5 天 | — |
| 2 | 机构域模型（文件版） | 1.5 天 | 1 |
| 3 | JWT 扩展 + 切换 | 1 天 | 2 |
| 4 | 租户中间件 + store 接线 + 加密迁移 | 2–3 天 | 1,3 |
| 5 | 客户/案件 DB + 路由 | 3–4 天 | 2,4 |
| 6 | 工作台前端 | 3–4 天 | 5 |
| 7 | 授权码 + 导出 | 2 天 | 5 |
| 8 | 清理收敛 | 1–2 天 | 全 |
| 合计 | | **约 14–18 人日** | |

> Step 4 与 Step 5 可部分并行（不同开发人）。

## 风险与对策

| 风险 | 对策 |
|---|---|
| ending_note/vault 加密迁移丢数据 | 迁移 CLI 只读旧数据→写新，逐条校验后删除；保留备份目录 7 天 |
| single/multi 两套路径漂移 | 唯一出口 `resolve_tenant_path()`，测试矩阵跑两种 TENANT_MODE |
| DB 与文件双轨不一致 | 客户/案件/审计只进 DB；业务密文留文件；store 层单入口 |
| `require_admin(strict)` 现存 bug 影响既有 admin 路由 | Step 3 一并改为双层角色，补充回归测试 |
| 越权回归 | `test_tenant_isolation.py` 进 CI，作为 P0 门禁 |

## 提交拆分建议（保持可回滚）

```text
feat(org): tenant mode 骨架与配置            # Step 1
feat(org): 机构/成员/邀请 文件域模型           # Step 2
feat(org): JWT tenant/org_role 扩展与切换     # Step 3
feat(org): 租户中间件 + 业务 store 接线        # Step 4
feat(org): 客户/案件 DB + 路由 + 审计          # Step 5
feat(org): 机构工作台前端                     # Step 6
feat(org): 授权码与数据导出                   # Step 7
chore(cleanup): 双入口收敛与平台层冻结         # Step 8
```