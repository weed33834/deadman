#!/usr/bin/env bash
# 一键启动 deadman Web 服务（本地开发）。
# 用法：
#   ./scripts/run-web.sh              # 默认 0.0.0.0:3000
#   PORT=8080 ./scripts/run-web.sh    # 自定义端口
set -euo pipefail

cd "$(dirname "$0")/.."

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

PORT="${PORT:-3000}"
HOST="${HOST:-0.0.0.0}"
DATA_ROOT="${DEADMAN_DATA_ROOT:-$HOME/.deadman}"
mkdir -p "$DATA_ROOT"

echo "==> 数据根目录: $DATA_ROOT"
echo "==> 启动 http://$HOST:$PORT"
export DEADMAN_DATA_ROOT="$DATA_ROOT"
exec "$PY" -m uvicorn deadman.web.app:app --host "$HOST" --port "$PORT" "$@"
