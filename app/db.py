"""SQLite 存储：账号 / 会话 / 邀请码，以及按用户隔离的课表快照与日志。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import DATA_DIR, DEFAULT_CONFIG, demo_allowed
from .user_context import current_user_id

DB_PATH = DATA_DIR / "campus.db"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # 主库含密码哈希、明文 provider 凭据与明文 session token，必须仅属主可读；
    # WAL/SHM 可能由 SQLite 另行创建，一并收紧（失败不阻断，仅尽力而为）。
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(str(DB_PATH) + suffix, 0o600)
        except OSError:
            pass
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


_user_locks: dict[int | None, threading.Lock] = {}
_user_locks_guard = threading.Lock()


def user_lock(user_id: int | None) -> threading.Lock:
    with _user_locks_guard:
        return _user_locks.setdefault(user_id, threading.Lock())


def _user_write_allowed(conn: sqlite3.Connection, user_id: int | None = None) -> bool:
    uid = _uid() if user_id is None else user_id
    if user_id is not None or current_user_id.get() is not None:
        return conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is not None
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
        return True
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None


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
        CREATE TABLE IF NOT EXISTS score_snapshot (
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
            used_at TEXT,
            max_uses INTEGER NOT NULL DEFAULT 1,
            used_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS invite_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            used_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_invite_uses_code ON invite_uses(code);
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
        CREATE TABLE IF NOT EXISTS reminders (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            item_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (user_id, day, item_key)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('homework', 'notice')),
            data TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE (user_id, kind, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_user_order ON notifications(user_id, id DESC);
        CREATE TABLE IF NOT EXISTS chaoxing_sync (
            user_id INTEGER PRIMARY KEY,
            last_sync TEXT,
            last_attempt TEXT,
            error TEXT NOT NULL DEFAULT '',
            warning TEXT NOT NULL DEFAULT '',
            push_warning TEXT NOT NULL DEFAULT '',
            baseline_complete INTEGER NOT NULL DEFAULT 0,
            auth_expired INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt TEXT NOT NULL,
            delivered_channels TEXT NOT NULL DEFAULT '[]',
            UNIQUE (user_id, kind, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
            ON notification_outbox(user_id, status, next_attempt);
        """)
        ncols = {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
        if "local_status" not in ncols:
            conn.execute("ALTER TABLE notifications ADD COLUMN local_status TEXT NOT NULL DEFAULT ''")
        # 已有学习通表按安全默认值迁移，不从 last_sync 推断历史扫描是否完整。
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(chaoxing_sync)")}
        for name, definition in (
            ("warning", "TEXT NOT NULL DEFAULT ''"),
            ("push_warning", "TEXT NOT NULL DEFAULT ''"),
            ("baseline_complete", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in ccols:
                conn.execute(f"ALTER TABLE chaoxing_sync ADD COLUMN {name} {definition}")
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
        # 旧版本迁移：邀请码从「一次性」升级为「可限定复用次数」
        icols = {r[1] for r in conn.execute("PRAGMA table_info(invites)")}
        if icols and "max_uses" not in icols:
            conn.execute("ALTER TABLE invites ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1")
        if icols and "used_count" not in icols:
            conn.execute("ALTER TABLE invites ADD COLUMN used_count INTEGER NOT NULL DEFAULT 0")
            # 已用过的老码按 1 次计入，避免升级后被重复使用
            conn.execute("UPDATE invites SET used_count = 1 WHERE used_by IS NOT NULL")
        # 老码的使用记录补进 invite_uses，方便管理员看到用过的人
        conn.execute(
            """INSERT INTO invite_uses (code, user_id, used_at)
               SELECT i.code, i.used_by, COALESCE(i.used_at, i.created_at)
               FROM invites i
               WHERE i.used_by IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM invite_uses u
                                 WHERE u.code = i.code AND u.user_id = i.used_by)""")
        # 关键索引补在迁移之后：旧库补 user_id 列后再建，避免缺列报错
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_sync_log_user ON sync_log(user_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_push_log_user ON push_log(user_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_reminders_user_sent ON reminders(user_id, sent_at)",
            "CREATE INDEX IF NOT EXISTS idx_invites_expires ON invites(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_invite_uses_user ON invite_uses(user_id)",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                continue


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dt(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


# ---------- 课表快照与日志（按当前用户） ----------

def _no_demo(snap: dict | None) -> dict | None:
    """生产环境不展示演示快照：遗留的 mock 数据一律视为「还没有数据」。"""
    if snap and snap.get("source") == "mock" and not demo_allowed():
        return None
    return snap


def get_snapshot() -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM snapshot WHERE user_id = ?", (_uid(),)).fetchone()
    if not row:
        return None
    return _no_demo({"courses": json.loads(row["data"]), "source": row["source"], "fetched_at": row["fetched_at"]})


def get_snapshot_for(user_id: int) -> dict | None:
    """按指定用户读课表快照（供日历订阅等无 Cookie 上下文的场景）。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM snapshot WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return _no_demo({"courses": json.loads(row["data"]), "source": row["source"], "fetched_at": row["fetched_at"]})


def save_snapshot(courses: list[dict], source: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        uid = _uid()
        if not _user_write_allowed(conn):
            return
        conn.execute(
            """INSERT INTO snapshot (user_id, data, source, fetched_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 data=excluded.data, source=excluded.source, fetched_at=excluded.fetched_at""",
            (uid, json.dumps(courses, ensure_ascii=False), source, now_str()),
        )


def get_exam_snapshot() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM exam_snapshot WHERE user_id = ?", (_uid(),)).fetchone()
    if not row:
        return None
    return _no_demo({"exams": json.loads(row["data"]), "source": row["source"],
                     "fetched_at": row["fetched_at"]})


def get_exam_snapshot_for(user_id: int) -> dict | None:
    """按指定用户读考试快照（供日历订阅等无 Cookie 上下文的场景）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM exam_snapshot WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return _no_demo({"exams": json.loads(row["data"]), "source": row["source"],
                     "fetched_at": row["fetched_at"]})


def save_exam_snapshot(exams: list[dict], source: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        uid = _uid()
        if not _user_write_allowed(conn):
            return
        conn.execute(
            """INSERT INTO exam_snapshot (user_id, data, source, fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 data=excluded.data, source=excluded.source, fetched_at=excluded.fetched_at""",
            (uid, json.dumps(exams, ensure_ascii=False), source, now_str()),
        )


def get_score_snapshot() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM score_snapshot WHERE user_id = ?", (_uid(),)).fetchone()
    if not row:
        return None
    return _no_demo({"scores": json.loads(row["data"]), "source": row["source"],
                     "fetched_at": row["fetched_at"]})


def get_score_snapshot_for(user_id: int) -> dict | None:
    """按指定用户读成绩快照（供无 Cookie 上下文的场景）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM score_snapshot WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return _no_demo({"scores": json.loads(row["data"]), "source": row["source"],
                     "fetched_at": row["fetched_at"]})


def save_score_snapshot(scores: list[dict], source: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        uid = _uid()
        if not _user_write_allowed(conn):
            return
        conn.execute(
            """INSERT INTO score_snapshot (user_id, data, source, fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 data=excluded.data, source=excluded.source, fetched_at=excluded.fetched_at""",
            (uid, json.dumps(scores, ensure_ascii=False), source, now_str()),
        )


def add_sync_log(status: str, message: str, changes: dict | None = None):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _user_write_allowed(conn):
            return
        conn.execute(
            "INSERT INTO sync_log (user_id, ts, status, message, changes) VALUES (?, ?, ?, ?, ?)",
            (_uid(), now_str(), status, message, json.dumps(changes or {}, ensure_ascii=False)),
        )


def add_push_log(channel: str, title: str, body: str, status: str, error: str = ""):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _user_write_allowed(conn):
            return
        conn.execute(
            """INSERT INTO push_log (user_id, ts, channel, title, body, status, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (_uid(), now_str(), channel, title, body, status, error),
        )


def list_push_logs(limit: int = 10) -> list[dict]:
    """最近的推送结果，不含正文，供设置页展示失败原因。"""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, ts, channel, title, status, error FROM push_log "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (_uid(), max(1, min(limit, 50)))).fetchall()]


def reminder_sent(day: str, item_key: str) -> bool:
    """今天这个事项是否已经推过提前提醒（每分钟轮询靠它去重）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE user_id = ? AND day = ? AND item_key = ?",
            (_uid(), day, item_key)).fetchone()
    return row is not None


def prune_reminders(conn: sqlite3.Connection, user_id: int,
                    now: datetime | None = None):
    """只清理已过事项日期、且发送超过七天的记录。

    未来考试的复习提醒需保留到考试结束；补推过往事项的新记录仍保留七天。
    """
    now = now or datetime.now()
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "DELETE FROM reminders WHERE user_id=? AND sent_at<? AND day<?",
        (user_id, cutoff, now.date().isoformat()),
    )


def mark_reminder(day: str, item_key: str):
    """标记已推送，并清理已过期的历史去重记录。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reminders (user_id, day, item_key, sent_at) VALUES (?, ?, ?, ?)",
            (_uid(), day, item_key, now_str()))
        prune_reminders(conn, _uid())


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


def prune_logs(keep_days: int = 30) -> int:
    """清理超过 keep_days 的同步/推送/审计日志，返回删除行数。

    日志表只增不删会无限膨胀（尤其推送通道配错时每分钟一行），由定时任务调用。
    """
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    removed = 0
    with get_conn() as conn:
        for table in ("sync_log", "push_log", "admin_log"):
            try:
                cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                removed += cur.rowcount or 0
            except sqlite3.OperationalError:
                continue
    return removed


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


def restore_provider_config(user_id: int, expected_provider: dict, old_provider: dict) -> bool:
    if user_id is None:
        return False
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT data FROM user_config WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return False
        try:
            cfg = json.loads(row["data"])
        except json.JSONDecodeError:
            return False
        if cfg.get("provider") != expected_provider:
            return False
        cfg["provider"] = old_provider
        conn.execute("UPDATE user_config SET data=? WHERE user_id=?",
                     (json.dumps(cfg, ensure_ascii=False), user_id))
    return True


def save_user_config(user_id: int, cfg: dict):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _user_write_allowed(conn, user_id):
            return
        conn.execute(
            """INSERT INTO user_config (user_id, data) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET data=excluded.data""",
            (user_id, json.dumps(cfg, ensure_ascii=False)),
        )


def patch_user_config(user_id: int, patch: dict) -> dict:
    """在写事务内深合并配置，保留其他请求更新的字段。"""
    def merge(target: dict, changes: dict):
        for key, value in changes.items():
            if (key not in ("course_overrides", "exam_overrides") and value
                    and isinstance(value, dict) and isinstance(target.get(key), dict)):
                merge(target[key], value)
            else:
                target[key] = value

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _user_write_allowed(conn, user_id):
            return {}
        row = conn.execute("SELECT data FROM user_config WHERE user_id = ?", (user_id,)).fetchone()
        try:
            cfg = json.loads(row["data"]) if row else {}
        except json.JSONDecodeError:
            cfg = {}
        provider = cfg.get("provider")
        if isinstance(provider, dict) and any(
                key.startswith(("http_", "zf_")) for key in provider):
            from .config import normalize_provider
            # 先消除旧扁平键，再合入本次补丁，确保新 Cookie 和映射生效。
            cfg["provider"] = normalize_provider(provider)
        merge(cfg, patch)
        conn.execute(
            """INSERT INTO user_config (user_id, data) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET data=excluded.data""",
            (user_id, json.dumps(cfg, ensure_ascii=False)),
        )
    return cfg


def migrate_override_uids(user_id: int) -> None:
    """基于更新前快照迁移旧手动调整键；必须在下一次同步覆盖快照前完成。"""
    import hashlib
    from .models import Course, Exam, course_uid, exam_uid

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT data FROM user_config WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return
        cfg = json.loads(row["data"])
        if cfg.get("override_uid_version") == 2:
            return
        for field, table, cls, uid_fn in (
            ("course_overrides", "snapshot", Course, course_uid),
            ("exam_overrides", "exam_snapshot", Exam, exam_uid),
        ):
            snap = conn.execute(f"SELECT data FROM {table} WHERE user_id = ?", (user_id,)).fetchone()
            mapping = {}
            snapshot_data = json.loads(snap["data"]) if snap else []
            for data in snapshot_data:
                item = cls.from_dict(data)
                if item.source_id.startswith("manual:"):
                    continue
                if cls is Course:
                    parts = [item.source_id, item.name, str(item.day), str(item.start),
                             str(item.end), ",".join(str(w) for w in sorted(item.weeks))]
                else:
                    parts = [item.source_id, item.name, item.date, item.start,
                             item.end, item.room, item.seat]
                legacy = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
                prior_uid = item.uid
                item.uid = ""
                new_uid = uid_fn(item)
                mapping[legacy] = new_uid
                if prior_uid:
                    mapping[prior_uid] = new_uid
                    data["uid"] = new_uid
            if snap:
                conn.execute(f"UPDATE {table} SET data = ? WHERE user_id = ?",
                             (json.dumps(snapshot_data, ensure_ascii=False), user_id))
            ov = cfg.get(field)
            if isinstance(ov, dict):
                ov["hidden"] = list(dict.fromkeys(mapping.get(k, k) for k in ov.get("hidden", [])))
                edited = ov.get("edited", {})
                migrated = {mapping.get(k, k): value for k, value in edited.items()}
                # 已按新标识保存的调整优先，不能被旧键迁移覆盖。
                migrated.update({k: value for k, value in edited.items()
                                 if mapping.get(k, k) == k})
                ov["edited"] = migrated
        cfg["override_uid_version"] = 2
        conn.execute("UPDATE user_config SET data = ? WHERE user_id = ?",
                     (json.dumps(cfg, ensure_ascii=False), user_id))


# ---------- 学习通通知 ----------

_NOTIFICATION_FIELDS = ("title", "body", "course", "status", "due_at", "published_at", "url")


def _chaoxing_config(conn: sqlite3.Connection, user_id: int) -> tuple[dict, dict]:
    row = conn.execute("SELECT data FROM user_config WHERE user_id = ?", (user_id,)).fetchone()
    cfg = json.loads(row["data"]) if row else {}
    cx = cfg.get("chaoxing")
    return cfg, cx if isinstance(cx, dict) else {}


def _save_chaoxing_config(conn: sqlite3.Connection, user_id: int, cfg: dict, cx: dict):
    cfg["chaoxing"] = cx
    conn.execute(
        """INSERT INTO user_config (user_id, data) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET data=excluded.data""",
        (user_id, json.dumps(cfg, ensure_ascii=False)),
    )


def _clear_chaoxing(conn: sqlite3.Connection, user_id: int):
    conn.execute("DELETE FROM notification_outbox WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM notifications WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM chaoxing_sync WHERE user_id = ?", (user_id,))


def set_chaoxing_binding(user_id: int, binding: dict | None) -> bool:
    from .config import DEFAULT_CONFIG

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM users WHERE id = ? AND is_active = 1",
                            (user_id,)).fetchone():
            return False
        cfg, old = _chaoxing_config(conn, user_id)
        cx = dict(DEFAULT_CONFIG["chaoxing"])
        if binding:
            cx.update(enabled=True, poll_minutes=old.get("poll_minutes", cx["poll_minutes"]),
                      push_enabled=old.get("push_enabled") is True, **binding)
        if not binding or str(old.get("uid") or "") != binding["uid"]:
            _clear_chaoxing(conn, user_id)
        else:
            conn.execute(
                """UPDATE chaoxing_sync SET last_attempt=NULL, error='', auth_expired=0, failures=0
                   WHERE user_id = ?""", (user_id,),
            )
        _save_chaoxing_config(conn, user_id, cfg, cx)
    return True


def patch_chaoxing_options(user_id: int, options: dict) -> bool:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM users WHERE id = ? AND is_active = 1",
                            (user_id,)).fetchone():
            return False
        cfg, cx = _chaoxing_config(conn, user_id)
        if not cx.get("uid") or not cx.get("cookies"):
            return False
        cx.update(options)
        if cx.get("push_enabled") is not True or cx.get("enabled") is not True:
            # 已送达的截止提醒保留去重记录，重新打开开关不会再次提醒同一截止时间。
            conn.execute("""DELETE FROM notification_outbox WHERE user_id = ?
                            AND NOT (kind='homework_due' AND status='sent')""", (user_id,))
            conn.execute("UPDATE chaoxing_sync SET push_warning='' WHERE user_id = ?", (user_id,))
        _save_chaoxing_config(conn, user_id, cfg, cx)
    return True


def get_chaoxing_status_data(user_id: int) -> tuple[dict, dict]:
    with get_conn() as conn:
        conn.execute("BEGIN")
        _, cx = _chaoxing_config(conn, user_id)
        row = conn.execute("SELECT * FROM chaoxing_sync WHERE user_id = ?", (user_id,)).fetchone()
    return cx, dict(row) if row else {}


def _current_chaoxing_session(conn: sqlite3.Connection, user_id: int, session: dict):
    if not conn.execute("SELECT 1 FROM users WHERE id = ? AND is_active = 1",
                        (user_id,)).fetchone():
        return None
    cfg, cx = _chaoxing_config(conn, user_id)
    if cx.get("uid") == session["uid"] and cx.get("cookies") == session["cookies"]:
        return cfg, cx
    return None


def start_chaoxing_attempt(user_id: int, session: dict, attempted_at: str) -> bool:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _current_chaoxing_session(conn, user_id, session) is None:
            return False
        conn.execute(
            """INSERT INTO chaoxing_sync (user_id, last_attempt) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_attempt=excluded.last_attempt""",
            (user_id, attempted_at),
        )
    return True


def record_chaoxing_failure(user_id: int, session: dict, error: str, auth_expired: bool):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _current_chaoxing_session(conn, user_id, session) is None:
            return
        conn.execute(
            """UPDATE chaoxing_sync SET error=?, auth_expired=MAX(auth_expired, ?), failures=failures+1
               WHERE user_id = ?""", (error, auth_expired, user_id),
        )


def _dated(value):
    """只接受带年份的真实日期，不用收录时间冒充作业发布时间。"""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("/", "-"))
        tz = ZoneInfo("Asia/Shanghai")
        return stamp.replace(tzinfo=tz) if stamp.tzinfo is None else stamp.astimezone(tz)
    except (ValueError, TypeError):
        return None


def _homework_term_start(conn: sqlite3.Connection, user_id: int):
    cfg, _ = _chaoxing_config(conn, user_id)
    # 与设置页同一默认值；显式空值或无效值才尝试课表绝对日期。
    start = _dated(cfg.get("semester_start", DEFAULT_CONFIG.get("semester_start")))
    if start:
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute("SELECT data, source FROM snapshot WHERE user_id=?", (user_id,)).fetchone()
    if not row or row["source"] == "mock":
        return None
    dates = []
    for course in json.loads(row["data"]):
        if isinstance(course, dict):
            day = _dated(course.get("date"))
            if day:
                dates.append(day)
    # 周次/星期没有绝对日期锚点时，无法推算第一节课，不猜日期。
    return min(dates).replace(hour=0, minute=0, second=0, microsecond=0) if dates else None


def _homework_in_term(item: dict, start) -> bool:
    stamp = _dated(item.get("published_at")) or _dated(item.get("due_at"))
    return bool(start and stamp and stamp >= start)


def _homework_expired(item: dict, now=None) -> bool:
    due = _dated(item.get("due_at"))
    return item.get("status") == "expired" or bool(
        due and due <= (now or datetime.now(ZoneInfo("Asia/Shanghai"))))


def _cancel_homework_pushes(conn, user_id: int, source_id: str):
    conn.execute(
        """DELETE FROM notification_outbox WHERE user_id=? AND status='pending'
           AND ((kind='homework' AND source_id=?) OR
                (kind='homework_due' AND json_extract(payload, '$.source_id')=?))""",
        (user_id, source_id, source_id),
    )


def set_homework_status(user_id: int, notification_id: int, status: str) -> dict:
    if not isinstance(status, str) or status not in {"completed", "pending", ""}:
        raise ValueError("无效的作业本地状态")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM notifications WHERE user_id=? AND id=?",
                           (user_id, notification_id)).fetchone()
        if row is None:
            raise LookupError("通知不存在")
        item = json.loads(row["data"])
        eligible = (_homework_in_term(item, _homework_term_start(conn, user_id))
                    and _homework_expired(item))
        if row["kind"] != "homework" or not (eligible or row["local_status"]):
            raise ValueError("仅可管理本学期已截止作业")
        if status == "pending" and item.get("status") in {"submitted", "graded"}:
            raise ValueError("已提交或已批阅作业不能标记待处理")
        conn.execute("UPDATE notifications SET local_status=? WHERE user_id=? AND id=?",
                     (status, user_id, notification_id))
        if status == "completed":
            _cancel_homework_pushes(conn, user_id, row["source_id"])
    return {"ok": True, "id": notification_id, "local_status": status}


def _homework_scope(conn: sqlite3.Connection, user_id: int):
    start = _homework_term_start(conn, user_id)
    conn.create_function("homework_in_term", 1,
                         lambda data: int(_homework_in_term(json.loads(data), start)))
    return start


def save_chaoxing_notifications(user_id: int, session: dict, items: list[dict],
                                cookies: list[dict], synced_at: str, warning: str = "") -> tuple[bool, list, int] | None:
    changed = []
    new_count = 0
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _current_chaoxing_session(conn, user_id, session)
        if current is None:
            return None
        cfg, cx = current
        sync = conn.execute("SELECT baseline_complete FROM chaoxing_sync WHERE user_id = ?",
                            (user_id,)).fetchone()
        first = not sync or not sync["baseline_complete"]
        term_start = _homework_term_start(conn, user_id)
        for item in items:
            item = dict(item)
            kind, source_id = item["kind"], item["source_id"]
            old = conn.execute(
                "SELECT id, data, local_status FROM notifications WHERE user_id=? AND kind=? AND source_id=?",
                (user_id, kind, source_id),
            ).fetchone()
            if old:
                previous = json.loads(old["data"])
                # 详情暂缺或作业已提交时不清空已知截止时间。
                if kind == "homework":
                    for field in ("due_at", "published_at"):
                        if not item.get(field):
                            item[field] = previous.get(field, "")
                if item.get("status") == "unknown" and previous.get("status"):
                    item["status"] = previous["status"]
            data = json.dumps({key: item.get(key, "") for key in _NOTIFICATION_FIELDS},
                              ensure_ascii=False)
            if kind == "homework" and (
                    item.get("status") in {"submitted", "graded"}
                    or _homework_expired(item, _dated(synced_at))
                    or (old and old["local_status"] == "completed")):
                if old:
                    conn.execute("UPDATE notifications SET data=? WHERE id=? AND user_id=?",
                                 (data, old["id"], user_id))
                elif item.get("status") not in {"submitted", "graded"} and _homework_in_term(item, term_start):
                    conn.execute(
                        """INSERT INTO notifications (user_id, source_id, kind, data, created_at)
                           VALUES (?, ?, ?, ?, ?)""", (user_id, source_id, kind, data, synced_at),
                    )
                _cancel_homework_pushes(conn, user_id, source_id)
                continue
            if old is None:
                conn.execute(
                    """INSERT INTO notifications (user_id, source_id, kind, data, created_at)
                       VALUES (?, ?, ?, ?, ?)""", (user_id, source_id, kind, data, synced_at),
                )
                if kind != "homework" or _homework_in_term(item, term_start):
                    new_count += 1
                    changed.append(item)
            elif old["data"] != data:
                # 学习通详情链接携带动态 enc/t，链接刷新不等于有新通知。
                old_content = json.loads(old["data"])
                new_content = json.loads(data)
                meaningful = any(old_content.get(k, "") != new_content.get(k, "")
                                 for k in _NOTIFICATION_FIELDS if k != "url")
                conn.execute("UPDATE notifications SET data=?, is_read=CASE WHEN ? THEN 0 ELSE is_read END WHERE id=?",
                             (data, meaningful, old["id"]))
                if meaningful and (kind != "homework" or _homework_in_term(item, term_start)):
                    changed.append(item)
        # 首次完整扫描及其之前的部分扫描全部静默；后续 warning 不撤销完整基线。
        if not first and cx.get("push_enabled") is True and cx.get("enabled") is True:
            for item in changed:
                payload = json.dumps(dict(kind=item["kind"], source_id=item["source_id"],
                                          **{key: item.get(key, "") for key in _NOTIFICATION_FIELDS}),
                                     ensure_ascii=False)
                conn.execute(
                    """INSERT INTO notification_outbox (user_id, kind, source_id, payload, next_attempt)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, kind, source_id) DO UPDATE SET
                         payload=excluded.payload, status='pending', attempt_count=0,
                         next_attempt=excluded.next_attempt, delivered_channels='[]'""",
                    (user_id, item["kind"], item["source_id"], payload, synced_at),
                )
        conn.execute(
            """INSERT INTO chaoxing_sync (user_id, last_sync, warning, baseline_complete)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_sync=excluded.last_sync,
                 error='', warning=excluded.warning,
                 baseline_complete=MAX(chaoxing_sync.baseline_complete, excluded.baseline_complete),
                 auth_expired=0, failures=0""", (user_id, synced_at, warning, not warning),
        )
        if cookies and cookies != cx["cookies"]:
            cx["cookies"] = cookies
            _save_chaoxing_config(conn, user_id, cfg, cx)
    return first, changed, new_count


def _refresh_homework_reminders(conn: sqlite3.Connection, user_id: int, now: str):
    """按最新作业状态生成提醒，截止时间是持久去重身份的一部分。"""
    tz = ZoneInfo("Asia/Shanghai")
    current_time = datetime.fromisoformat(now)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=tz)
    current_time = current_time.astimezone(tz)
    eligible = {}
    term_start = _homework_term_start(conn, user_id)
    for row in conn.execute(
        """SELECT source_id, data FROM notifications WHERE user_id=? AND kind='homework'
           AND local_status!='completed'
           AND json_extract(data, '$.status')='pending'""", (user_id,),
    ):
        item = json.loads(row["data"])
        if not _homework_in_term(item, term_start):
            continue
        try:
            due = datetime.fromisoformat(item.get("due_at") or "")
            if due.tzinfo is None:
                due = due.replace(tzinfo=tz)
            due = due.astimezone(tz)
        except (ValueError, TypeError, OverflowError):
            continue
        if not current_time < due <= current_time + timedelta(hours=24):
            continue
        # 统一时区表示，签名链接刷新或等价时区写法均不会重复生成提醒。
        key = json.dumps([row["source_id"], due.isoformat()], ensure_ascii=False)
        item.update(kind="homework", source_id=row["source_id"], event="homework_due",
                    due_at=due.isoformat(timespec="seconds"))
        eligible[key] = json.dumps(item, ensure_ascii=False)

    # 作业提交、已过期或截止时间变更后，取消旧提醒（包括尚未送达的重试）。
    for row in conn.execute(
        "SELECT id, source_id FROM notification_outbox WHERE user_id=? AND kind='homework_due' AND status='pending'",
        (user_id,),
    ).fetchall():
        if row["source_id"] not in eligible:
            conn.execute("DELETE FROM notification_outbox WHERE id=? AND user_id=?", (row["id"], user_id))
    for key, payload in eligible.items():
        conn.execute(
            """INSERT INTO notification_outbox (user_id, kind, source_id, payload, next_attempt)
               VALUES (?, 'homework_due', ?, ?, ?)
               ON CONFLICT(user_id, kind, source_id) DO UPDATE SET payload=excluded.payload
                 WHERE notification_outbox.status='pending'""", (user_id, key, payload, now),
        )


def get_due_notification_outbox(user_id: int, session: dict, now: str) -> list[dict]:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _current_chaoxing_session(conn, user_id, session)
        if current is None:
            return []
        _, cx = current
        _homework_scope(conn, user_id)
        conn.execute("""DELETE FROM notification_outbox WHERE user_id=? AND status='pending'
                        AND kind IN ('homework', 'homework_due') AND NOT homework_in_term(payload)""",
                     (user_id,))
        if cx.get("push_enabled") is not True or cx.get("enabled") is not True:
            conn.execute("""DELETE FROM notification_outbox WHERE user_id=?
                            AND NOT (kind='homework_due' AND status='sent')""", (user_id,))
            conn.execute("UPDATE chaoxing_sync SET push_warning='' WHERE user_id=?", (user_id,))
            return []
        baseline = conn.execute("SELECT baseline_complete, auth_expired FROM chaoxing_sync WHERE user_id=?",
                                (user_id,)).fetchone()
        if not baseline or not baseline["baseline_complete"]:
            return []
        for row in conn.execute(
                "SELECT source_id, data, local_status FROM notifications WHERE user_id=? AND kind='homework'",
                (user_id,)).fetchall():
            item = json.loads(row["data"])
            if (row["local_status"] == "completed" or item.get("status") in {"submitted", "graded"}
                    or _homework_expired(item, _dated(now))):
                _cancel_homework_pushes(conn, user_id, row["source_id"])
        if baseline["auth_expired"]:
            conn.execute("DELETE FROM notification_outbox WHERE user_id=? AND kind='homework_due' AND status='pending'",
                         (user_id,))
        else:
            _refresh_homework_reminders(conn, user_id, now)
        rows = [dict(row) for row in conn.execute(
            """SELECT * FROM notification_outbox WHERE user_id=? AND status='pending'
               AND next_attempt<=? ORDER BY CASE WHEN kind='homework_due' THEN 0 ELSE 1 END,
               next_attempt, id LIMIT 50""", (user_id, now),
        )]
        if not rows:
            # 旧截止提醒取消后，不继续展示已不存在的发送失败提示。
            if not conn.execute("SELECT 1 FROM notification_outbox WHERE user_id=? AND status='pending' LIMIT 1",
                                (user_id,)).fetchone():
                conn.execute("UPDATE chaoxing_sync SET push_warning='' WHERE user_id=?", (user_id,))
            return []
        # 提醒和更新分批发送；同批仅合并已成功通道相同的记录。
        deadline_batch = rows[0]["kind"] == "homework_due"
        rows = [row for row in rows if row["delivered_channels"] == rows[0]["delivered_channels"]
                and (row["kind"] == "homework_due") == deadline_batch]
        if deadline_batch:
            rows = rows[:5]  # 每条截止提醒都完整列出，其余留待下一分钟发送。
        for row in rows:
            row["attempt_count"] += 1
            minutes = min(360, 2 ** min(row["attempt_count"], 9))
            retry_at = (datetime.fromisoformat(now) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE notification_outbox SET attempt_count=?, next_attempt=? WHERE id=? AND user_id=?",
                (row["attempt_count"], retry_at, row["id"], user_id),
            )
    return rows


def finish_notification_outbox(user_id: int, session: dict, rows: list[dict],
                               delivered_channels: list[str], succeeded: bool):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _current_chaoxing_session(conn, user_id, session) is None:
            return
        for row in rows:
            delivered = sorted(set(json.loads(row["delivered_channels"])) | set(delivered_channels))
            conn.execute(
                """UPDATE notification_outbox SET status=?, delivered_channels=?
                   WHERE id=? AND user_id=? AND payload=? AND status='pending'""",
                ("sent" if succeeded else "pending", json.dumps(delivered),
                 row["id"], user_id, row["payload"]),
            )
        pending = conn.execute(
            "SELECT 1 FROM notification_outbox WHERE user_id=? AND status='pending' LIMIT 1",
            (user_id,),
        ).fetchone()
        if not succeeded or not pending:
            conn.execute(
                "UPDATE chaoxing_sync SET push_warning=? WHERE user_id=?",
                ("站内通知已更新，外部推送未全部成功，将自动重试" if not succeeded else "", user_id),
            )


def list_notifications(user_id: int, kind: str, unread: bool, limit: int) -> dict:
    pending = "(local_status='pending' OR json_extract(data, '$.status')='pending')"
    visible = "(kind != 'homework' OR (homework_in_term(data) AND local_status!='completed' AND COALESCE(json_extract(data, '$.status'), '') NOT IN ('submitted', 'graded') AND (json_extract(data, '$.status')!='expired' OR local_status='pending')))"
    managed = "(kind='homework' AND homework_in_term(data) AND homework_expired(data) AND COALESCE(json_extract(data, '$.status'), '') NOT IN ('submitted', 'graded'))"
    constraints = ["user_id = ?", managed if kind == "expired" else visible]
    args: list = [user_id]
    if kind not in {"all", "expired"}:
        constraints.append("kind = ?")
        args.append(kind)
    if kind == "homework":
        # 本地待处理独立于学习通的原始提交状态。
        constraints.append(pending)
    elif unread:
        constraints.append("is_read = 0")
    where = " AND ".join(constraints)
    with get_conn() as conn:
        conn.execute("BEGIN")
        _homework_scope(conn, user_id)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        conn.create_function("homework_expired", 1,
                             lambda data: int(_homework_expired(json.loads(data), now)))
        items = [dict(json.loads(row["data"]), id=row["id"], kind=row["kind"],
                      local_status=row["local_status"],
                      can_manage=row["kind"] == "homework" and bool(
                          _homework_expired(json.loads(row["data"]), now) or row["local_status"]),
                      is_expired=row["kind"] == "homework" and _homework_expired(json.loads(row["data"]), now),
                      is_read=bool(row["is_read"]), created_at=row["created_at"])
                 for row in conn.execute(
                     f"""SELECT id, kind, data, local_status, is_read, created_at FROM notifications
                         WHERE {where}
                         ORDER BY CASE WHEN kind='homework' THEN 0 ELSE 1 END,
                           CASE WHEN kind='homework' AND julianday(json_extract(data,'$.due_at')) IS NULL THEN 1 ELSE 0 END,
                           CASE WHEN kind='homework' THEN julianday(json_extract(data,'$.due_at')) END ASC,
                           id DESC LIMIT ?""", (*args, limit),
                 )]
        counts = conn.execute(
            f"""SELECT SUM(is_read = 0) AS unread_count,
                       SUM(kind = 'homework' AND {pending}) AS pending_count
                FROM notifications WHERE user_id = ? AND {visible}""", (user_id,),
        ).fetchone()
        _, cx = _chaoxing_config(conn, user_id)
        sync = conn.execute("SELECT * FROM chaoxing_sync WHERE user_id = ?", (user_id,)).fetchone()
    return {"items": items, "unread_count": counts["unread_count"] or 0,
            "pending_count": counts["pending_count"] or 0, "config": cx,
            "sync": dict(sync) if sync else {}}


def mark_notifications_read(user_id: int, ids: list[int], all_items: bool) -> int:
    sql = "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0"
    args: list = [user_id]
    if not all_items:
        if not ids:
            return 0
        sql += " AND id IN (" + ",".join("?" for _ in ids) + ")"
        args.extend(ids)
    with get_conn() as conn:
        return conn.execute(sql, args).rowcount


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


def registration_error(username: str, invite_code: str) -> str | None:
    """哈希前预检，减少无效注册触发的高成本密码派生；最终仍以原子注册为准。"""
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            return "用户名已存在"
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            invite = conn.execute(
                """SELECT 1 FROM invites
                   WHERE code = ? AND expires_at > ?
                     AND COALESCE(used_count, 0) < COALESCE(max_uses, 1)""",
                (invite_code, now_str()),
            ).fetchone()
            if not invite:
                return "邀请码无效、已用完或已过期"
    return None


def register_user_atomic(username: str, password_hash: str, display_name: str,
                         student_id: str, invite_code: str) -> tuple[int, bool]:
    """原子判断首位管理员、创建账号并核销邀请码。"""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        first = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        ts = now_str()
        if not first:
            invite = conn.execute(
                """SELECT 1 FROM invites
                   WHERE code = ? AND expires_at > ?
                     AND COALESCE(used_count, 0) < COALESCE(max_uses, 1)""",
                (invite_code, ts),
            ).fetchone()
            if not invite:
                raise ValueError("邀请码无效、已用完或已过期")
        cur = conn.execute(
            """INSERT INTO users (username, password_hash, display_name, student_id, role, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (username, password_hash, display_name, student_id,
             "admin" if first else "user", ts),
        )
        uid = cur.lastrowid
        if not first:
            ts = now_str()
            changed = conn.execute(
                """UPDATE invites
                   SET used_count = COALESCE(used_count, 0) + 1,
                       used_by = COALESCE(used_by, ?),
                       used_at = COALESCE(used_at, ?)
                   WHERE code = ? AND expires_at > ?
                     AND COALESCE(used_count, 0) < COALESCE(max_uses, 1)""",
                (uid, ts, invite_code, ts),
            ).rowcount
            if not changed:
                raise ValueError("邀请码无效、已用完或已过期")
            conn.execute(
                "INSERT INTO invite_uses (code, user_id, used_at) VALUES (?, ?, ?)",
                (invite_code, uid, ts),
            )
    return uid, first


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
    with user_lock(user_id):
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("sessions", "snapshot", "exam_snapshot", "score_snapshot",
                          "sync_log", "push_log", "user_config", "events",
                          "reminders", "notifications", "chaoxing_sync", "notification_outbox"):
                conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            # 邀请码是可复用的，注销账号只收回他这一次的占用，不能把整个码删掉
            conn.execute(
                """UPDATE invites SET used_count = MAX(COALESCE(used_count, 0) - 1, 0)
                   WHERE code IN (SELECT code FROM invite_uses WHERE user_id = ?)""",
                (user_id,))
            conn.execute("DELETE FROM invite_uses WHERE user_id = ?", (user_id,))
            conn.execute("UPDATE invites SET used_by = NULL, used_at = NULL WHERE used_by = ?",
                         (user_id,))
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        import logging
        import shutil
        from .browser_login import _profile_dir
        try:
            shutil.rmtree(_profile_dir(user_id))
        except FileNotFoundError:
            pass
        except OSError:
            logging.getLogger(__name__).warning("注销后清理浏览器 profile 失败 uid=%s", user_id)


# ---------- 会话 ----------

def create_session(user_id: int, token: str, ttl: int) -> str:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_str(),))
        conn.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                     (token, user_id, now_str(), _dt(ttl)))
    return token


def checked_create_session(user_id: int, token: str, ttl: int,
                           expected_password_hash: str) -> bool:
    """仅在账号仍活跃且密码哈希未变时创建会话，避免改密竞态。"""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT is_active, password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row or not row["is_active"] or row["password_hash"] != expected_password_hash:
            return False
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_str(),))
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now_str(), _dt(ttl)),
        )
    return True


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

def create_invite(code: str, created_by: int, ttl: int, max_uses: int = 1):
    """生成邀请码。max_uses 为这个码总共能被几个人用来注册（>=1）。

    自定义码可能与已有码重名，这里让 sqlite 抛 IntegrityError，由调用方转成友好提示。
    """
    max_uses = max(1, int(max_uses or 1))
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO invites
               (code, created_by, created_at, expires_at, max_uses, used_count)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (code, created_by, now_str(), _dt(ttl), max_uses),
        )


def invite_exists(code: str) -> bool:
    """是否已有同名邀请码（不区分大小写，避免 QUT2026 与 qut2026 混淆）。"""
    if not code:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM invites WHERE LOWER(code) = LOWER(?)", (code,)).fetchone()
    return bool(row)


def list_invites() -> list[dict]:
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM invites ORDER BY created_at DESC").fetchall()]
        for r in rows:
            r["max_uses"] = r.get("max_uses") or 1
            r["used_count"] = r.get("used_count") or 0
            r["remaining"] = max(0, r["max_uses"] - r["used_count"])
        return rows


def list_invite_uses(code: str) -> list[dict]:
    """某个邀请码每次被使用的记录（谁、什么时候）。"""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM invite_uses WHERE code = ? ORDER BY used_at", (code,)).fetchall()]


def get_valid_invite(code: str) -> dict | None:
    """码存在、未过期、且还有剩余可用次数时返回。"""
    if not code:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM invites
               WHERE code = ? AND expires_at > ?
                 AND COALESCE(used_count, 0) < COALESCE(max_uses, 1)""",
            (code, now_str()),
        ).fetchone()
    return dict(row) if row else None


def consume_invite(code: str, user_id: int) -> int:
    """核销一次。原子判断剩余次数，成功返回 1，次数用完/过期返回 0。"""
    ts = now_str()
    with get_conn() as conn:
        changed = conn.execute(
            """UPDATE invites
               SET used_count = COALESCE(used_count, 0) + 1,
                   used_by = COALESCE(used_by, ?),
                   used_at = COALESCE(used_at, ?)
               WHERE code = ? AND expires_at > ?
                 AND COALESCE(used_count, 0) < COALESCE(max_uses, 1)""",
            (user_id, ts, code, ts),
        ).rowcount
        if changed:
            conn.execute(
                "INSERT INTO invite_uses (code, user_id, used_at) VALUES (?, ?, ?)",
                (code, user_id, ts))
        return changed


def delete_invite(code: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM invites WHERE code = ?", (code,))
        conn.execute("DELETE FROM invite_uses WHERE code = ?", (code,))


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
