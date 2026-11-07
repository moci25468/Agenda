"""版本号唯一来源：从 git 读取（tag / commit / 提交时间）。

优先取最近的 tag 作为版本号（形如 v1.0.0 或 1.0.0），
再附上短 commit hash 与提交时间，便于线上定位当前运行的是哪次构建。
容器内若无 .git 目录（部署时只 cp 了 app/static），回落到默认值。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_VERSION = "1.1.5"


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _strip_v(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def get_version() -> str:
    """返回语义化版本号（不含 v 前缀），如 1.0.3。"""
    tag = _git("describe", "--tags", "--abbrev=0")
    if tag:
        return _strip_v(tag)
    return FALLBACK_VERSION


def get_full_version() -> dict:
    """返回完整版本信息，供 /api/health 展示。"""
    tag = _git("describe", "--tags", "--abbrev=0")
    commit = _git("rev-parse", "--short", "HEAD")
    date = _git("show", "-s", "--format=%ci", "HEAD")
    return {
        "version": _strip_v(tag) if tag else FALLBACK_VERSION,
        "tag": tag or "",
        "commit": commit,
        "committed_at": date,
    }
