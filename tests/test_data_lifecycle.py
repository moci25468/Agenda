from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import db, push, room_cache, scheduler
from app.user_context import current_user_id


class DeletionRaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db_patch = patch.object(db, "DATA_DIR", self.root)
        self.path_patch = patch.object(db, "DB_PATH", self.root / "campus.db")
        self.db_patch.start()
        self.path_patch.start()
        db.init_db()
        self.user_id = db.create_user("delete-test", "not-a-real-hash")

    def tearDown(self):
        self.db_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    def test_deleted_user_data_cannot_be_recreated_by_stale_sync(self):
        token = current_user_id.set(self.user_id)
        try:
            db.save_snapshot([{"name": "before"}], "test")
            db.save_user_config(self.user_id, {"school": "before"})
            with patch("app.browser_login._profile_dir", return_value=self.root / "profile"):
                db.delete_user(self.user_id)
            db.save_snapshot([{"name": "after"}], "test")
            db.save_exam_snapshot([], "test")
            db.save_score_snapshot([], "test")
            db.save_user_config(self.user_id, {"school": "after"})
            db.add_sync_log("ok", "after")
            with db.get_conn() as conn:
                for table in ("snapshot", "exam_snapshot", "score_snapshot",
                              "user_config", "sync_log"):
                    self.assertEqual(
                        conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?",
                                     (self.user_id,)).fetchone()[0], 0)
        finally:
            current_user_id.reset(token)

    def test_delete_waits_for_active_sync_lock(self):
        lock = db.user_lock(self.user_id)
        lock.acquire()
        finished = threading.Event()

        def delete():
            with patch("app.browser_login._profile_dir", return_value=self.root / "profile"):
                db.delete_user(self.user_id)
            finished.set()

        worker = threading.Thread(target=delete)
        worker.start()
        try:
            self.assertFalse(finished.wait(0.1))
        finally:
            lock.release()
            worker.join(timeout=5)
        self.assertTrue(finished.is_set())


class PushReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = {"push": {"bark_enabled": True, "bark_key": "synthetic",
                             "wecom_enabled": True, "wecom_webhook": "synthetic"}}

    async def test_successful_channel_is_persisted_before_later_failure(self):
        completed = []

        async def bark(*_args):
            return True, ""

        async def wecom(*_args):
            raise RuntimeError("secret-webhook")

        with (patch.object(push, "send_bark", bark),
              patch.object(push, "send_wecom", wecom),
              patch.object(push.db, "add_push_log")):
            results = await push.push_all(self.cfg, "title", "body", completed.append)
        self.assertEqual([result["channel"] for result in completed], ["bark", "wecom"])
        self.assertTrue(completed[0]["ok"])
        self.assertNotIn("secret-webhook", repr(results))

    async def test_log_failure_does_not_skip_remaining_channel(self):
        async def send(*_args):
            return True, ""

        with (patch.object(push, "send_bark", send),
              patch.object(push, "send_wecom", send),
              patch.object(push.db, "add_push_log", side_effect=RuntimeError("db-down"))):
            results = await push.push_all(self.cfg, "title", "body")
        self.assertEqual([result["channel"] for result in results], ["bark", "wecom"])

    async def test_transport_exception_does_not_expose_credential_url(self):
        class FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                raise RuntimeError("request failed for https://api.day.app/secret-key")

        with patch.object(push.httpx, "AsyncClient", return_value=FailingClient()):
            ok, error = await push.send_bark(self.cfg, "title", "body")
        self.assertFalse(ok)
        self.assertNotIn("secret-key", error)


class ReminderRetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        for name, value in (("DATA_DIR", root), ("DB_PATH", root / "campus.db")):
            patcher = patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        self.user_id = db.create_user("reminder-test", "synthetic-hash")
        token = current_user_id.set(self.user_id)
        self.addCleanup(current_user_id.reset, token)
        self.now = datetime(2026, 11, 1, 9, tzinfo=scheduler.TZ)
        clock = patch.object(db, "datetime", wraps=datetime)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = self.now.replace(tzinfo=None)

    async def assert_review_stays_deduplicated(self, cleanup):
        exam_day = (self.now + timedelta(days=15)).date().isoformat()
        db.save_exam_snapshot([{"name": "数学", "date": exam_day, "start": "09:00"}], "test")
        cfg = {"push": {"bark_enabled": True, "exam_review_days": 15}}
        send = AsyncMock(return_value=[{"channel": "bark", "ok": True}])
        with patch.object(scheduler, "push_all", send):
            await scheduler.push_exam_reviews(cfg, self.now)
            self.assertEqual(send.await_count, 1)
            later = self.now + timedelta(days=8)
            self.clock.now.return_value = later.replace(tzinfo=None)
            cleanup(later)
            await scheduler.push_exam_reviews(cfg, later)
            self.assertEqual(send.await_count, 1)

    async def test_daily_sync_cleanup_preserves_future_exam_deduplication(self):
        await self.assert_review_stays_deduplicated(
            lambda now: db.mark_reminder(now.date().isoformat(), "daily_sync"))

    async def test_failed_reminder_cleanup_preserves_future_exam_deduplication(self):
        def cleanup(now):
            key = "unrelated-course"
            token = scheduler._claim_reminder(now.date().isoformat(), key, now)
            self.assertIsNotNone(token)
            scheduler._finish_reminder(token, key, False, now)

        await self.assert_review_stays_deduplicated(cleanup)

    async def test_cleanup_removes_only_old_past_items_for_current_user(self):
        today = self.now.date().isoformat()
        past = (self.now - timedelta(days=9)).date().isoformat()
        future = (self.now + timedelta(days=60)).date().isoformat()
        old_sent_at = (self.now - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO reminders (user_id, day, item_key, sent_at) VALUES (?, ?, ?, ?)",
                [(self.user_id, day, key, old_sent_at) for day, key in (
                    (past, "old-past"), (today, "old-today"), (future, "old-future"))]
                + [(self.user_id + 1, past, "other-user", old_sent_at)],
            )
        db.mark_reminder(past, "recently-sent-past")
        self.assertFalse(db.reminder_sent(past, "old-past"))
        self.assertTrue(db.reminder_sent(today, "old-today"))
        self.assertTrue(db.reminder_sent(future, "old-future"))
        self.assertTrue(db.reminder_sent(past, "recently-sent-past"))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE user_id=?", (self.user_id + 1,),
            ).fetchone()[0], 1)


class RoomSingleflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        room_cache._on_demand_tasks.clear()
        room_cache._on_demand_user_last.clear()
        room_cache._on_demand_key_last.clear()

    def tearDown(self):
        room_cache._on_demand_tasks.clear()
        room_cache._on_demand_user_last.clear()
        room_cache._on_demand_key_last.clear()

    async def test_concurrent_identical_queries_share_one_upstream_call(self):
        import asyncio
        from app import room_cache

        calls = 0
        release = asyncio.Event()

        async def fetch():
            nonlocal calls
            calls += 1
            await release.wait()
            return [{"id": "room-1"}]

        with (patch.object(room_cache.cache, "get", AsyncMock(return_value=None)),
              patch.object(room_cache.cache, "set", AsyncMock())):
            tasks = [asyncio.create_task(room_cache.fetch_on_demand("same-key", 1, fetch))
                     for _ in range(5)]
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual(calls, 1)
        self.assertTrue(all(result["data"] == [{"id": "room-1"}] for result in results))

    async def test_different_query_is_rate_limited_for_same_user(self):
        from app import room_cache

        async def fetch():
            return []

        with (patch.object(room_cache.cache, "get", AsyncMock(return_value=None)),
              patch.object(room_cache.cache, "set", AsyncMock())):
            await room_cache.fetch_on_demand("key-one", 7, fetch)
            with self.assertRaises(room_cache.RoomQueryBusy):
                await room_cache.fetch_on_demand("key-two", 7, fetch)


if __name__ == "__main__":
    unittest.main()
