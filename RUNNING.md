# deadman 开发运行指南（确保克隆即跑通）

> 目标：**克隆 → 安装 → 启动** 一步到位，避免踩依赖缺失 / 数据目录漂移 / 导入失败的坑。
> 平台：Python 3.10+；GitHub 主仓 + GitCode/Gitee 镜像（见 `sync.sh`）。

## 一、安装（含全部开发依赖）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,db,text]"
```

- `dev`：pytest / pytest-asyncio（跑测试必需）
- `db`：sqlalchemy + aiosqlite（机构客户/案件主数据库；未装则自动降级到文件存储，见 §三）
- `text`：jieba + rank-bm25（textproc 分词/检索）

生产环境只需 `pip install -e .`。

## 二、启动

```bash
./scripts/run-web.sh                # 默认 0.0.0.0:3000
PORT=8080 ./scripts/run-web.sh      # 自定义端口
```

数据根目录默认 `~/.deadman`，可覆盖：

```bash
DEADMAN_DATA_ROOT=/stable/data ./scripts/run-web.sh
```

> `DEADMAN_DATA_ROOT` 是**唯一**数据根开关：auth/org/notification/cron/各 store 目录都
> 从它派生（见 `config._data_root` 与 `multi_tenant.DATA_ROOT`）。适合 HOME 易失的
> 沙箱/容器/CI。早前版本各模块各自写死 `~/.deadman`，改 HOME 会导致登录数据"丢失"。

## 三、主数据库（可选）

机构客户/案件默认走文件存储（`org/file_customers.py`）。启用 PostgreSQL：

```bash
DATABASE_URL=postgresql://user:pw@host/db python -m uvicorn deadman.web.app:app --port 3000
```

DB 层为**零侵入降级**：`db_enabled()` 在 sqlalchemy 未装或 DATABASE_URL 为空时返回 False，
机构仓库自动回退文件存储，不会 500（修复历史 `No module named sqlalchemy` 500 问题）。

## 四、测试

```bash
cd src && python -m pytest tests/ -q
```

## 五、踩坑记录（曾导致"克隆跑不通"，均已修复）

| 现象 | 根因 | 修复 |
|---|---|---|
| `ModuleNotFoundError: deadman` | 未 `pip install -e .`，`src` 不在 sys.path | setup.sh 用 editable 安装 |
| `No module named fastapi/uvicorn/...` | 只装核心、未装 extras；环境重置丢包 | `pip install -e ".[dev,db,text]"` |
| 机构客户/案件接口 500 | `get_customer_repo` 无条件 import `db.repositories`→sqlalchemy | `db_enabled()` 容错 + 仓库分发回退文件 |
| 登录后数据"丢失" | 各模块写死 `~/.deadman`，与 `DEADMAN_DATA_ROOT` 分流 | 统一所有 store 目录从 `_data_root()` 派生 |
| 根路径 `HEAD /` 返回 405 | 仅 GET 路由 | 补 `@app.head("/")` |
| 案件推进状态后页面不更新 | 前端 `setStatus` 未调 `renderCases()` | 补刷新 |
| 顶栏显示 user_id | 前端用 `ctx.user_id`；`/api/orgs/me` 无 display_name | 后端补 display_name + 前端回退 |
| 成员日期美式 `8/20/2026` | `toLocaleDateString()` | 统一 `fmtDate` → `YYYY-MM-DD` |
| `pkill -f "uvicorn deadman"` 误杀自身 | 匹配串出现在命令自身命令行 | 用 `fuser -k <port>/tcp` 按端口杀 |

## 六、三端同步

```bash
./sync.sh "feat: xxx" v0.1.x     # 提交 + 打小版本 tag + 推 GitHub/GitCode/Gitee
```
凭据经仓库本地 credential.helper 指向 workspace 内 0600 的 `.git-credentials`（remote URL 不含 token）。
