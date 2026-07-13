#!/usr/bin/env bash
# 同步脚本 - 把当前工作同步到 GitHub + GitCode 两个仓库
# 用法: ./sync.sh "commit message"
# 密钥已存在 .git/config 的 remote URL 中，不在此脚本中

set -e

MSG="${1:-update: sync to both remotes}"

cd "$(dirname "$0")"

# 检查是否有变更
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  echo "无变更，跳过同步"
  exit 0
fi

echo "=== 1. add + commit ==="
git add -A
git commit -m "$MSG" || { echo "commit 失败（可能无变更）"; exit 1; }

echo "=== 2. push to GitHub (origin) ==="
git push origin main

echo "=== 3. push to GitCode (gitcode) ==="
git push gitcode main

echo "=== 同步完成 ==="
echo "GitHub: https://github.com/MS33834/legacy-aftercare"
echo "GitCode: https://gitcode.com/badhope/legacy-aftercare"
