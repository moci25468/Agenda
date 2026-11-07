#!/usr/bin/env bash
# 发布脚本：自动版本号 +1 → 提交 → 打 tag → 推送到 GitHub。
#
# 版本号规则：从 git tag 读取最新版本（形如 v1.0.3），+1 补丁号（如 v1.0.4）。
# 用法：
#   ./release.sh                # 自动 bump 补丁号并推送
#   ./release.sh patch          # 同上（显式）
#   ./release.sh minor          # bump 次版本号（1.0.x -> 1.1.0）
#   ./release.sh major          # bump 主版本号（1.x.x -> 2.0.0）
#   ./release.sh -m "说明"      # 自定义提交信息
set -euo pipefail

# 绕过 macOS 的 Xcode license 弹窗（否则 git 命令会卡死）
export GIT_ASKPASS=true

REMOTE="origin"
BRANCH="main"

# 读取当前最新 tag（优先用远程，本地没有就取本地）
latest_tag=$(git describe --tags --abbrev=0 2>/dev/null || true)

if [ -z "$latest_tag" ]; then
  # 没有任何 tag 时，从 remote 拉取
  latest_tag=$(git ls-remote --tags "$REMOTE" 2>/dev/null \
    | sed 's#.*refs/tags/##' | grep -v '\^{}' | sort -V | tail -1 || true)
fi

if [ -z "$latest_tag" ]; then
  echo "未找到任何 tag，将使用基线版本 v1.0.0 并 bump 到 v1.0.1"
  latest_tag="v1.0.0"
fi

# 去掉 v 前缀解析版本
ver="${latest_tag#v}"
IFS='.' read -r major minor patch <<< "$ver"
[ -z "$major" ] && major=0
[ -z "$minor" ] && minor=0
[ -z "$patch" ] && patch=0

bump="${1:-patch}"
msg_opt=""
if [ "$bump" = "-m" ]; then
  msg_opt="$2"
  bump="patch"
fi

case "$bump" in
  major)
    major=$((major + 1)); minor=0; patch=0 ;;
  minor)
    minor=$((minor + 1)); patch=0 ;;
  patch|"")
    patch=$((patch + 1)) ;;
  *)
    echo "未知版本级别：$bump（可选 patch/minor/major）" >&2
    exit 1 ;;
esac

new_ver="v${major}.${minor}.${patch}"
echo "版本号：$latest_tag -> $new_ver"

# 检查工作区是否有改动
if [ -n "$(git status --porcelain)" ]; then
  echo "检测到未提交改动，先提交..."
  git add -A
  if [ -n "${msg_opt:-}" ]; then
    git commit -m "$msg_opt"
  else
    git commit -m "发布版本 $new_ver"
  fi
else
  echo "工作区干净，无改动需要提交（仅打 tag）"
fi

# 打 tag 并推送
git tag -a "$new_ver" -m "Release $new_ver"
git push "$REMOTE" "$BRANCH"
git push "$REMOTE" "$new_ver"

echo "✅ 已推送并打 tag：$new_ver"
