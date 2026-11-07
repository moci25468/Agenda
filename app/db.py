"""SQLite 存储：账号 / 会话 / 邀请码，以及按用户隔离的课表快照与日志。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from .config import DATA_DIR
from .user_context import current_user_id

DB_PATH = DATA_DIR / "campus.db"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _uid() -> int:
    """没有用户上下文时（启动、离线脚本）回落到 1，兼容单用户数据。"""
    return current_user_id.get() or 1


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshot (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exam_snapshot (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            ts TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            changes TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS push_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            ts TEXT NOT NULL,
            channel TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            student_id TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invites (
            code TEXT PRIMARY KEY,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_by INTEGER,
            used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS user_config (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL
        );
        """)
        # 旧版本迁移：snapshot 只有 id=1 一行；日志表没有 user_id
        cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshot)")}
        if "user_id" not in cols and "data" in cols:
            conn.execute("ALTER TABLE snapshot RENAME TO snapshot_legacy")
            conn.execute("""CREATE TABLE snapshot (
                user_id INTEGER PRIMARY KEY, data TEXT NOT NULL,
                source TEXT NOT NULL, fetched_at TEXT NOT NULL)""")
            conn.execute("""INSERT OR IGNORE INTO snapshot (user_id, data, source, fetched_at)
                            SELECT 1, data, source, fetched_at FROM snapshot_legacy""")
            conn.execute("DROP TABLE snapshot_legacy")
        for table in ("sync_log", "push_log"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if cols and "user_id" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dt(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


# ---------- 课表快照与日志（按当前用户） ----------

def get_snapshot() -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM snapshot WHERE user_id = ?", (_uid(),)).fetchone()
    if not row:
        return None
    return {"courses": json.loads(row["data"]), "source": row["source"], "fetched_at": row["fetched_at"]}


def save_snapshot(courses: list[dict], source: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO snapshot (user_id, data, source, fetched_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 data=excluded.data, source=excluded.source, fetched_at=excluded.fetched_at""",
            (_uid(), json.dumps(courses, ensure_ascii=False), source, now_str()),
        )


def get_exam_snapshot() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM exam_snapshot WHERE user_id = ?", (_uid(),)).fetchone()
    if not row:
        return None
    return {"exams": json.loads(row["data"]), "source": row["source"],
            "fetched_at": row["fetched_at"]}


def save_exam_snapshot(exams: list[dict], source: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO exam_snapshot (user_id, data, source, fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 data=excluded.data, source=excluded.source, fetched_at=excluded.fetched_at""",
            (_uid(), json.dumps(exams, ensure_ascii=False), source, now_str()),
        )


def add_sync_log(status: str, message: str, changes: dict | None = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sync_log (user_id, ts, status, message, changes) VALUES (?, ?, ?, ?, ?)",
            (_uid(), now_str(), status, message, json.dumps(changes or {}, ensure_ascii=False)),
        )


def add_push_log(channel: str, title: str, body: str, status: str, error: str = ""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO push_log (user_id, ts, channel, title, body, status, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (_uid(), now_str(), channel, title, body, status, error),
        )


def get_logs(limit: int = 50) -> dict:
    with get_conn() as conn:
        sync = [dict(r) for r in conn.execute(
            "SELECT * FROM sync_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (_uid(), limit)).fetchall()]
        push = [dict(r) for r in conn.execute(
            "SELECT * FROM push_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (_uid(), limit)).fetchall()]
    for s in sync:
        try:
            s["changes"] = json.loads(s["changes"])
        except json.JSONDecodeError:
            s["changes"] = {}
    return {"sync": sync, "push": push}


# ---------- 用户配置（按用户存的完整 config dict） ----------

def get_user_config(user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT data FROM user_config WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except json.JSONDecodeError:
        return {}


def save_user_config(user_id: int, cfg: dict):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_config (user_id, data) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET data=excluded.data""",
            (user_id, json.dumps(cfg, ensure_ascii=False)),
        )


# ---------- 用户 ----------

def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_user_ids(active_only: bool = True) -> list[int]:
    with get_conn() as conn:
        sql = "SELECT id FROM users" + (" WHERE is_active = 1" if active_only else "") + " ORDER BY id"
        return [r[0] for r in conn.execute(sql).fetchall()]


def list_users() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, username, display_name, student_id, role, is_active, created_at FROM users ORDER BY id"
        ).fetchall()]


def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def create_user(username: str, password_hash: str, display_name: str = "",
                student_id: str = "", role: str = "user") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO users (username, password_hash, display_name, student_id, role, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (username, password_hash, display_name, student_id, role, now_str()),
        )
        return cur.lastrowid


def set_user_active(user_id: int, active: bool):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))


# ---------- 会话 ----------

def create_session(user_id: int, token: str, ttl: int) -> str:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_str(),))
        conn.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                     (token, user_id, now_str(), _dt(ttl)))
    return token


def get_session_user(token: str) -> dict | None:
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """SELECT s.user_id, u.username, u.display_name, u.student_id, u.role, u.is_active
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, now_str()),
        ).fetchone()
    if not row or not row["is_active"]:
        return None
    return dict(row)


def delete_session(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---------- 邀请码 ----------

def create_invite(code: str, created_by: int, ttl: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO invites (code, created_by, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (code, created_by, now_str(), _dt(ttl)),
        )


def list_invites() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()]


def get_valid_invite(code: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM invites WHERE code = ? AND used_by IS NULL AND expires_at > ?",
            (code, now_str()),
        ).fetchone()
    return dict(row) if row else None


def consume_invite(code: str, user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "UPDATE invites SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
            (user_id, now_str(), code),
        ).rowcount


def delete_invite(code: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM invites WHERE code = ?", (code,))
