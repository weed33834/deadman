#!/usr/bin/env bash
# 同步脚本 - 把当前工作同步到三个仓库（GitHub + GitCode + Gitee）
# 用法: ./sync.sh "commit message"
# 三仓均为各自平台主仓库（非镜像），平等维护
# 凭据存在 .git/config 的 remote URL 中，未配置凭据的 remote 会推送失败并跳过

set -e

MSG="${1:-update: sync to all remotes}"

cd "$(dirname "$0")"

# 检查是否有变更
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  echo "无变更，跳过同步"
  exit 0
fi

echo "=== 1. add + commit ==="
git add -A
git commit -m "$MSG" || { echo "commit 失败（可能无变更）"; exit 1; }

# 推送到所有已配置的 remote（任一失败不影响其他）
FAILED=0
for remote in origin gitcode gitee; do
  if git remote get-url "$remote" >/dev/null 2>&1; then
    echo "=== push to $remote ==="
    git push "$remote" main || { echo "[警告] 推送 $remote 失败，已跳过"; FAILED=1; }
  fi
done

echo "=== 同步完成 ==="
echo "GitHub:  https://github.com/bad-hope/deadman"
echo "GitCode: https://gitcode.com/badhope/deadman"
echo "Gitee:   https://gitee.com/badhope/deadman"

exit $FAILED
