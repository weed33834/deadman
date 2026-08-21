#!/usr/bin/env bash
# 一键安装 + 初始化 deadman 开发环境。
# 用法：
#   ./scripts/setup.sh                # 创建 venv + 安装全部依赖 + 数据目录
#   DEADMAN_DATA_ROOT=/path ./scripts/setup.sh   # 自定义数据根目录
#
# 设计目标：让「克隆 → 跑起来」一步到位，不踩依赖缺失 / HOME 易失 / 导入失败等坑。
set -euo pipefail

cd "$(dirname "$0")/.."   # 仓库根

PY=python3
command -v $PY >/dev/null 2>&1 || PY=python

echo "==> Python: $($PY --version)"

# 1) 创建数据根目录（默认 ~/.deadman，可用 DEADMAN_DATA_ROOT 覆盖，见 multi_tenant.DATA_ROOT）
DATA_ROOT="${DEADMAN_DATA_ROOT:-$HOME/.deadman}"
mkdir -p "$DATA_ROOT"
echo "==> 数据根目录: $DATA_ROOT"

# 2) 可选：创建 venv（已存在则复用）
if [ -d .venv ] && [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
  echo "==> 复用 .venv"
else
  echo "==> 创建 .venv（跳过请设置 DEADMAN_NO_VENV=1）"
  $PY -m venv .venv 2>/dev/null || true
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi
fi

# 3) 安装本项目。开发环境安装 dev/db/text 三个 extras：
#    - dev : pytest/pytest-asyncio 等测试依赖
#    - db  : sqlalchemy + asyncpg + aiosqlite + alembic（机构客户/案件主数据库）
#    - text: jieba + rank-bm25（textproc 分词/检索）
#    生产环境仅需：pip install -e .
echo "==> pip install -e '.[dev,db,text]' （生产环境仅需 '.'）"
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -e ".[dev,db,text]" -q

echo
echo "==> 完成。启动 Web 服务："
echo "    DEADMAN_DATA_ROOT=$DATA_ROOT $PY -m uvicorn deadman.web.app:app --port 3000"
echo "    或: ./scripts/run-web.sh"
echo
echo "    主数据库(可选): DATABASE_URL=postgresql://user:pw@host/db $PY -m pip install -e '.[db]'"
