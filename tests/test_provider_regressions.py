from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import config, db
from app.models import Course
from app.providers import httpjson
from app.providers.zhengfang import ZhengfangProvider
from app.user_context import current_user_id


class HttpProviderRequestTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, portal: dict, handler):
        with patch.object(httpjson, "SafeAsyncHTTPTransport",
                          return_value=httpx.MockTransport(handler)):
            return await httpjson.HttpJsonProvider({"provider": {"portal": portal}}).fetch_raw()

    async def test_get_uses_real_httpx_request_signature(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"courses": []})

        result = await self.fetch({"url": "https://school.example/courses?week=3",
                                   "headers": {"Accept": "application/json"}}, respond)
        self.assertEqual(result, {"courses": []})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].url.params["week"], "3")
        self.assertEqual(requests[0].headers["Accept"], "application/json")

    async def test_json_post_sends_configured_body(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True})

        result = await self.fetch({"url": "https://school.example/courses", "method": "POST",
                                   "body": {"week": 3}}, respond)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(json.loads(requests[0].content), {"week": 3})

    async def test_form_post_redirects_as_get_without_cross_origin_credentials(self):
        requests = []

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"Location": "https://other.example/courses"})
            return httpx.Response(200, json={"ok": True})

        await self.fetch({
            "url": "https://school.example/courses", "method": "POST",
            "headers": {"Content-Type": "application/x-www-form-urlencoded",
                        "Cookie": "sid=synthetic", "X-Token": "synthetic"},
            "body": "week=3&term=fall",
        }, respond)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(requests[0].content, b"week=3&term=fall")
        self.assertEqual(requests[1].method, "GET")
        self.assertEqual(str(requests[1].url), "https://other.example/courses")
        self.assertEqual(requests[1].content, b"")
        for name in ("Cookie", "X-Token", "Content-Type"):
            self.assertNotIn(name, requests[1].headers)

    async def test_same_origin_307_preserves_post_and_body(self):
        requests = []

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(307, headers={"Location": "/updated-courses"})
            return httpx.Response(200, json={"ok": True})

        await self.fetch({"url": "https://school.example/courses", "method": "POST",
                          "headers": {"X-Token": "synthetic"}, "body": {"week": 3}}, respond)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].method, "POST")
        self.assertEqual(str(requests[1].url), "https://school.example/updated-courses")
        self.assertEqual(json.loads(requests[1].content), {"week": 3})
        self.assertEqual(requests[1].headers["X-Token"], "synthetic")


class ZhengfangExamSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.enterContext(patch.object(db, "DATA_DIR", root))
        self.enterContext(patch.object(db, "DB_PATH", root / "campus.db"))
        db.init_db()
        user_id = db.create_user("exam-regression", "synthetic-hash")
        token = current_user_id.set(user_id)
        self.addCleanup(current_user_id.reset, token)
        config.save_config({"override_uid_version": 2, "provider": {
            "primary": "zhengfang", "type": "zhengfang", "zhengfang": {
                "base": "https://school.example/jwglxt", "username": "synthetic-student",
                "password": "synthetic-password", "year": "2026", "term": "3",
            },
        }})
        self.old_exams = [{"name": "old-semester-exam", "date": "2026-06-10"}]
        db.save_exam_snapshot(self.old_exams, "zhengfang")

    async def save_source(self, *, exam_error: bool):
        from app import main

        @asynccontextmanager
        async def session(_provider):
            yield object()

        exams = AsyncMock(side_effect=ValueError("exam endpoint unavailable")) if exam_error else AsyncMock(return_value=[])
        with (patch.object(ZhengfangProvider, "_session", session),
              patch.object(ZhengfangProvider, "fetch_courses_with_term_fix", AsyncMock(
                  return_value=([Course(name="new-semester-course")], None))),
              patch.object(ZhengfangProvider, "_fetch_exams", exams)):
            return await main.save_zhengfang(main.ZhengfangSaveRequest(
                base="https://school.example/jwglxt", username="synthetic-student",
                year="2026", term="3"))

    async def test_successful_empty_exam_response_clears_old_snapshot(self):
        result = await self.save_source(exam_error=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exam_count"], 0)
        self.assertEqual(result["exam_error"], "")
        self.assertEqual(db.get_exam_snapshot()["exams"], [])
        self.assertEqual(db.get_snapshot()["courses"][0]["name"], "new-semester-course")

    async def test_failed_exam_response_preserves_old_snapshot(self):
        result = await self.save_source(exam_error=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exam_error"], "exam endpoint unavailable")
        self.assertEqual(db.get_exam_snapshot()["exams"], self.old_exams)
        self.assertEqual(db.get_snapshot()["courses"][0]["name"], "new-semester-course")


if __name__ == "__main__":
    unittest.main()
