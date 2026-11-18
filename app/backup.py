"""每日备份：把 SQLite 主库与全局配置快照到 data/backups/，只保留最近 7 天。"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import CONFIG_PATH, DATA_DIR

BACKUP_DIR = DATA_DIR / "backups"
KEEP_DAYS = 7


def _prune(days: int = KEEP_DAYS) -> None:
    cutoff = datetime.now().timestamp() - days * 86400
    for path in BACKUP_DIR.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def backup_now(now: datetime | None = None) -> Path:
    """用 SQLite 在线备份生成当天快照（WAL 模式下也安全），返回数据库备份路径。"""
    from .db import DB_PATH

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(BACKUP_DIR, 0o700)
    except OSError:
        pass
    now_dt = now or datetime.now()
    stamp = now_dt.strftime("%Y%m%d")
    target = BACKUP_DIR / f"campus-{stamp}.db"
    # 同一天多次备份（如启动补备份 + 定时任务）时不覆盖已有备份，追加时分秒
    if target.exists():
        target = BACKUP_DIR / f"campus-{stamp}-{now_dt.strftime('%H%M%S')}.db"
    tmp = target.with_name(target.name + ".part")
    src = sqlite3.connect(DB_PATH, timeout=10)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            # 校验副本完整性，避免把损坏的库当作可用备份
            check = dst.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise RuntimeError(f"备份完整性校验失败：{check}")
        finally:
            dst.close()
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    finally:
        src.close()
    tmp.replace(target)
    os.chmod(target, 0o600)
    if CONFIG_PATH.exists():
        cfg_target = BACKUP_DIR / f"config-{stamp}.json"
        if cfg_target.exists():
            cfg_target = BACKUP_DIR / f"config-{stamp}-{now_dt.strftime('%H%M%S')}.json"
        shutil.copy2(CONFIG_PATH, cfg_target)
        os.chmod(cfg_target, 0o600)
    _prune()
    return target
