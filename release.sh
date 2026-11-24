#!/usr/bin/env bash
# 发布脚本：自动版本号 +1 → 提交 → 打 tag → 推送到 GitHub。
#
# 版本号规则：从 git tag 读取最新版本（形如 v1.0.3），+1 补丁号（如 v1.0.4）。
# 用法：
#   ./release.sh patch -m "中文说明具体改动" -- app/example.py static/app.js
#   ./release.sh minor -m "中文说明具体改动" -- README.md docs/功能介绍.md
#   ./release.sh major -m "中文说明具体改动" -- app/example.py
#
# -- 后必须逐个列出本次发布的源码文件，app/version.py 会自动加入。
# 不会提交整个工作区；未显式指定的未跟踪文件或已暂存文件会阻止发布。
set -euo pipefail

# 绕过 macOS 的 Xcode license 弹窗（否则 git 命令会卡死）
export GIT_ASKPASS=true
export GIT_LITERAL_PATHSPECS=1

REMOTE="origin"
BRANCH="main"
selected_paths=()

die() {
  echo "$*" >&2
  exit 1
}

is_sensitive_path() {
  case "$1" in
    data|data/*|*/data/*|.git|.git/*|*/.git/*|.venv|.venv/*|*/.venv/*|venv|venv/*|*/venv/*|__pycache__|__pycache__/*|*/__pycache__/*|backups|backups/*|*/backups/*)
      return 0 ;;
    .env|.env.*|*/.env|*/.env.*|*.env|*.pem|*.key|*.p12|*.pfx|*.crt|*.secret.*|*.db|*.sqlite|*.sqlite3|*.log|*.tmp|*.bak|*.swp|config.local.json|secrets.json|*/config.local.json|*/secrets.json)
      return 0 ;;
    *) return 1 ;;
  esac
}

is_source_path() {
  case "$1" in
    app/*.py|static/*|tests/*.py|docs/*.md|README.md|release.sh|verify.py|Dockerfile|requirements.txt|docker-compose.yml|run.sh|start.command)
      return 0 ;;
    *) return 1 ;;
  esac
}

path_selected() {
  local candidate="$1" item
  [ "${#selected_paths[@]}" -gt 0 ] || return 1
  for item in "${selected_paths[@]}"; do
    [ "$item" = "$candidate" ] && return 0
  done
  return 1
}

add_release_path() {
  local path="$1"
  while [[ "$path" == ./* ]]; do
    path="${path#./}"
  done
  [ -n "$path" ] || die "发布路径不能为空"
  [[ "$path" != /* && "$path" != ../* && "$path" != */../* && "$path" != */.. ]] \
    || die "发布路径必须位于仓库内：$path"
  [[ "$path" != *$'\n'* ]] || die "发布路径不能包含换行：$path"
  [[ "$path" != *"*"* && "$path" != *"?"* && "$path" != *"["* ]] \
    || die "发布路径必须是单个明确文件，不支持通配符：$path"
  is_sensitive_path "$path" && die "拒绝发布敏感或运行时文件：$path"
  is_source_path "$path" || die "不是允许发布的源码路径：$path"
  [ ! -d "$path" ] || die "请逐个指定文件，不支持目录：$path"
  [ ! -L "$path" ] || die "拒绝发布符号链接：$path"
  if [ ! -f "$path" ]; then
    git ls-files --error-unmatch -- "$path" >/dev/null 2>&1 \
      || die "发布文件不存在或未跟踪：$path"
  fi
  path_selected "$path" || selected_paths+=("$path")
}

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
bump_set=0
msg_opt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    patch|minor|major)
      [ "$bump_set" -eq 0 ] || die "版本级别只能指定一次"
      bump="$1"
      bump_set=1
      shift
      ;;
    -m)
      [ "$#" -ge 2 ] || die "-m 后必须提供提交说明"
      [ -z "$msg_opt" ] || die "提交说明只能指定一次"
      msg_opt="$2"
      shift 2
      ;;
    --)
      shift
      [ "$#" -gt 0 ] || die "-- 后必须显式指定本次发布的源码文件"
      for release_path in "$@"; do
        add_release_path "$release_path"
      done
      break
      ;;
    *)
      add_release_path "$1"
      shift
      ;;
  esac
done

[ "${#selected_paths[@]}" -gt 0 ] || die "必须通过 -- 显式指定源码文件，发布脚本不会自动提交整个工作区"
add_release_path "app/version.py"

# 未跟踪文件只能来自明确指定的源码路径，避免把临时文件、凭证或无关文档带进提交。
while IFS= read -r -d '' untracked_path; do
  path_selected "$untracked_path" \
    || die "存在未显式指定的未跟踪文件：$untracked_path；请先移出工作区或用 -- 明确指定"
done < <(git ls-files --others --exclude-standard -z)

# git commit 会包含整个暂存区，因此已有暂存内容也必须全部属于本次发布。
while IFS= read -r -d '' staged_path; do
  path_selected "$staged_path" \
    || die "存在未显式指定的已暂存文件：$staged_path；请先处理暂存区后再发布"
done < <(git diff --cached --name-only -z)

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
  git status --short -- "${selected_paths[@]}"
  read -r -p "请用中文填写本次具体改动： " msg_opt
fi
if [ -z "$msg_opt" ]; then
  echo "改动说明不能为空。用法：./release.sh patch -m \"简化登录页并更新介绍文案\" -- app/example.py" >&2
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

# 只暂存明确列出的源码路径和自动更新的运行时版本，不再整仓暂存。
if [ -n "$(git status --porcelain --untracked-files=all -- "${selected_paths[@]}")" ]; then
  git add -- "${selected_paths[@]}"
  echo "本次将提交文件："
  git diff --cached --name-status -- "${selected_paths[@]}"
  git commit -m "$msg_opt" -m "版本：$new_ver"
else
  echo "本次指定文件无改动（仅打 tag）"
fi

# 打 tag 并推送
git tag -a "$new_ver" -m "$msg_opt" -m "版本：$new_ver"
git push "$REMOTE" "$BRANCH"
git push "$REMOTE" "$new_ver"

echo "已推送并打 tag：$new_ver"
