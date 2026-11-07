#!/usr/bin/env bash
# 发布脚本：自动版本号 +1 → 提交 → 打 tag → 推送到 GitHub。
#
# 版本号规则：从 git tag 读取最新版本（形如 v1.0.3），+1 补丁号（如 v1.0.4）。
# 用法：
#   ./release.sh                # 自动 bump 补丁号并推送
#   ./release.sh patch          # 同上（显式）
#   ./release.sh minor          # bump 次版本号（1.0.x -> 1.1.0）
#   ./release.sh major          # bump 主版本号（1.x.x -> 2.0.0）
#   ./release.sh -m "中文说明具体改动"  # 推荐：提交标题写清更改内容，版本号放在正文
set -euo pipefail

# 绕过 macOS 的 Xcode license 弹窗（否则 git 命令会卡死）
export GIT_ASKPASS=true

REMOTE="origin"
BRANCH="main"

# 始终先读 GitHub 远程标签；远程无标签时才回落到本地标签
latest_tag=$(git ls-remote --tags "$REMOTE" 2>/dev/null \
  | sed 's#.*refs/tags/##' | grep -v '\^{}' | sort -V | tail -1 || true)

if [ -z "$latest_tag" ]; then
  latest_tag=$(git describe --tags --abbrev=0 2>/dev/null || true)
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

bump="patch"
msg_opt=""
if [ "${1:-}" = "-m" ]; then
  msg_opt="${2:-}"
  bump="${3:-patch}"
else
  bump="${1:-patch}"
  if [ "${2:-}" = "-m" ]; then
    msg_opt="${3:-}"
  fi
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

# 提交标题必须说明实际改动，不再自动生成只有版本号的空泛提交。
if [ -z "$msg_opt" ]; then
  echo "本次待提交文件："
  git status --short
  read -r -p "请用中文填写本次具体改动： " msg_opt
fi
if [ -z "$msg_opt" ]; then
  echo "改动说明不能为空。用法：./release.sh -m \"简化登录页并更新介绍文案\"" >&2
  exit 1
fi

# 容器部署通常没有 .git，发布时同步更新运行时的回落版本。
python3 - "$new_ver" <<'PY'
from pathlib import Path
import re
import sys

version = sys.argv[1].removeprefix("v")
path = Path("app/version.py")
source = path.read_text(encoding="utf-8")
updated, count = re.subn(
    r'FALLBACK_VERSION = "[^"]+"',
    f'FALLBACK_VERSION = "{version}"',
    source,
    count=1,
)
if count != 1:
    raise SystemExit("无法在 app/version.py 中更新 FALLBACK_VERSION")
path.write_text(updated, encoding="utf-8")
PY

# 检查工作区是否有改动
if [ -n "$(git status --porcelain)" ]; then
  echo "检测到未提交改动，先提交..."
  git add -A
  git commit -m "$msg_opt" -m "版本：$new_ver"
else
  echo "工作区干净，无改动需要提交（仅打 tag）"
fi

# 打 tag 并推送
git tag -a "$new_ver" -m "$msg_opt" -m "版本：$new_ver"
git push "$REMOTE" "$BRANCH"
git push "$REMOTE" "$new_ver"

echo "✅ 已推送并打 tag：$new_ver"
