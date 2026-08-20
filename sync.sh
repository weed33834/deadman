#!/usr/bin/env bash
# 三端同步脚本 - 把当前工作同步到三个平台
# 用法: ./sync.sh "commit message" [tag]
#   - github  : https://github.com/weed33834/deadman    (主仓，唯一事实源)
#   - gitcode : https://gitcode.com/badhope/deadman     (镜像)
#   - gitee   : https://gitee.com/badhope/deadman       (镜像)
# 凭据存于 ~/.git-credentials (credential.helper store)，remote URL 不含 token
# 小版本迭代：每个提交对应一个 0.x 小版本；可选第二个参数作为 tag 一并推送。

set -e

MSG="${1:-update: sync to all remotes}"
TAG="${2:-}"

cd "$(dirname "$0")"

# 检查是否有变更
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  echo "无变更，跳过 commit"
else
  echo "=== 1. add + commit ==="
  git add -A
  git commit -m "$MSG" || { echo "commit 失败（可能无变更）"; }
fi

# 推送到所有已配置的 remote（任一失败不影响其他）
FAILED=0
for remote in github gitcode gitee; do
  if git remote get-url "$remote" >/dev/null 2>&1; then
    echo "=== push to $remote ==="
    git push "$remote" main || { echo "[警告] 推送 $remote 失败，已跳过"; FAILED=1; }
  fi
done

# 可选：推送版本标签
if [ -n "$TAG" ]; then
  echo "=== tag $TAG ==="
  git tag -f "$TAG" >/dev/null 2>&1 || git tag "$TAG"
  for remote in github gitcode gitee; do
    if git remote get-url "$remote" >/dev/null 2>&1; then
      git push "$remote" "$TAG" 2>/dev/null || echo "[警告] 推送 tag $TAG 到 $remote 失败，已跳过"
    fi
  done
fi

echo "=== 同步完成 ==="
echo "GitHub :  https://github.com/weed33834/deadman  (主)"
echo "GitCode:  https://gitcode.com/badhope/deadman"
echo "Gitee  :  https://gitee.com/badhope/deadman"

exit $FAILED
