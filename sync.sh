#!/usr/bin/env bash
# 三端同步脚本 - 把当前工作同步到三个平台
# 用法: ./sync.sh "commit message"
# 三端平等维护（github 为主仓库，自动化工作流在此生效）
#   - github  : https://github.com/weed33834/deadman    (主)
#   - gitcode : https://gitcode.com/badhope/deadman     (辅)
#   - gitee   : https://gitee.com/badhope/deadman       (辅)
# 凭据存于 ~/.git-credentials (credential.helper store)，remote URL 不含 token

set -e

MSG="${1:-update: sync to all remotes}"

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

echo "=== 同步完成 ==="
echo "GitHub :  https://github.com/weed33834/deadman  (主)"
echo "GitCode:  https://gitcode.com/badhope/deadman"
echo "Gitee  :  https://gitee.com/badhope/deadman"

exit $FAILED
