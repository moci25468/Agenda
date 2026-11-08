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
            created_at TEXT NOT NULL,
            last_login_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            admin_name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            ts TEXT NOT NULL
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
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            start_time TEXT NOT NULL DEFAULT '',
            end_time TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_user_date ON events(user_id, date);
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
        # 旧版本迁移：users 表补充 last_login_at 字段
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if ucols and "last_login_at" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT NOT NULL DEFAULT ''")


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


# ---------- 个人日程（按当前用户） ----------

def _event_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "title": row["title"], "date": row["date"],
        "start": row["start_time"], "end": row["end_time"],
        "location": row["location"], "note": row["note"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_events(date_from: str = "", date_to: str = "") -> list[dict]:
    sql = "SELECT * FROM events WHERE user_id = ?"
    args: list = [_uid()]
    if date_from:
        sql += " AND date >= ?"
        args.append(date_from)
    if date_to:
        sql += " AND date <= ?"
        args.append(date_to)
    sql += " ORDER BY date, start_time, created_at"
    with get_conn() as conn:
        return [_event_dict(r) for r in conn.execute(sql, args).fetchall()]


def get_event(event_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ? AND user_id = ?",
                           (event_id, _uid())).fetchone()
    return _event_dict(row) if row else None


def add_event(data: dict) -> dict:
    import uuid
    event_id = uuid.uuid4().hex[:16]
    ts = now_str()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO events (id, user_id, title, date, start_time, end_time,
                                   location, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, _uid(), data["title"], data["date"],
             data.get("start", ""), data.get("end", ""),
             data.get("location", ""), data.get("note", ""), ts, ts),
        )
    return get_event(event_id)


def update_event(event_id: str, data: dict) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE events SET title = ?, date = ?, start_time = ?, end_time = ?,
                                 location = ?, note = ?, updated_at = ?
               WHERE id = ? AND user_id = ?""",
            (data["title"], data["date"], data.get("start", ""), data.get("end", ""),
             data.get("location", ""), data.get("note", ""), now_str(),
             event_id, _uid()),
        )
        if not cur.rowcount:
            return None
    return get_event(event_id)


def delete_event(event_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM events WHERE id = ? AND user_id = ?",
                           (event_id, _uid()))
        return bool(cur.rowcount)


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


def list_users(q: str = "", sort: str = "id") -> list[dict]:
    q = (q or "").strip()
    order = {"id": "id", "created": "created_at", "login": "last_login_at DESC",
             "name": "username"}.get(sort, "id")
    with get_conn() as conn:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                """SELECT id, username, display_name, student_id, role, is_active,
                          created_at, last_login_at FROM users
                   WHERE username LIKE ? OR display_name LIKE ? OR student_id LIKE ?
                   ORDER BY {}""".format(order),
                (like, like, like),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, username, display_name, student_id, role, is_active,
                          created_at, last_login_at FROM users ORDER BY {}""".format(order),
            ).fetchall()
    return [dict(r) for r in rows]


def set_user_info(user_id: int, display_name: str, student_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET display_name = ?, student_id = ? WHERE id = ?",
            (display_name, student_id, user_id),
        )


def set_last_login(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                     (now_str(), user_id))


def count_active_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]


def count_new_users_since(ts: str) -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (ts,)).fetchone()[0]


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


def set_user_role(user_id: int, role: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def set_user_password(user_id: int, password_hash: str):
    """重置密码（管理员操作或用户自助改密）。"""
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (password_hash, user_id))


def delete_user_sessions(user_id: int, except_token: str = ""):
    """踢掉该用户所有已登录会话（改密 / 重置后强制重新登录）。

    except_token 非空时保留该会话（用户自助改密后当前浏览器不掉线）。
    """
    with get_conn() as conn:
        if except_token:
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token != ?",
                (user_id, except_token))
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def delete_user(user_id: int):
    """注销账号：用户行和各表里属于他的数据一起删除，避免留孤儿数据。"""
    with get_conn() as conn:
        for table in ("sessions", "snapshot", "exam_snapshot",
                      "sync_log", "push_log", "user_config", "events"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM invites WHERE used_by = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


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


# ---------- 管理员审计日志 ----------

def add_admin_log(admin_id: int, admin_name: str, action: str,
                  target: str = "", detail: str = ""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO admin_log (admin_id, admin_name, action, target, detail, ts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (admin_id, admin_name, action, target, detail, now_str()),
        )


def list_admin_log(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM admin_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
