"""每日备份：把 SQLite 主库与全局配置快照到 data/backups/，只保留最近 7 天。"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .config import CONFIG_PATH, DATA_DIR

BACKUP_DIR = DATA_DIR / "backups"
KEEP_DAYS = 7
_backup_lock = threading.Lock()


def _prune(days: int = KEEP_DAYS) -> None:
    cutoff = datetime.now().timestamp() - days * 86400
    for path in BACKUP_DIR.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def backup_now(now: datetime | None = None) -> Path:
    """用 SQLite 在线备份生成快照（WAL 模式下也安全），返回数据库备份路径。"""
    with _backup_lock:
        return _backup_now(now)


def _backup_now(now: datetime | None = None) -> Path:
    from .db import DB_PATH

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(BACKUP_DIR, 0o700)
    except OSError:
        pass
    now_dt = now or datetime.now()
    stamp = now_dt.strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid4().hex
    target = BACKUP_DIR / f"campus-{stamp}.db"
    tmp = target.with_name(target.name + ".part")
    cfg_target = BACKUP_DIR / f"config-{stamp}.json"
    cfg_tmp = cfg_target.with_name(cfg_target.name + ".part")
    try:
        # 预建私有临时文件，且每次调用都使用独立名字。
        tmp.touch(mode=0o600, exist_ok=False)
        src = sqlite3.connect(DB_PATH, timeout=10)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                check = dst.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError(f"备份完整性校验失败：{check}")
            finally:
                dst.close()
        finally:
            src.close()
        if CONFIG_PATH.exists():
            cfg_tmp.touch(mode=0o600, exist_ok=False)
            # 不继承源配置的旧 mtime，否则刚创建的备份可能被保留策略删除。
            shutil.copyfile(CONFIG_PATH, cfg_tmp)
            os.utime(cfg_tmp, None)
            cfg_tmp.replace(cfg_target)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
        cfg_tmp.unlink(missing_ok=True)
    _prune()
    return target
