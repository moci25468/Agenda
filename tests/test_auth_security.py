from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import auth, config


class PasswordSecurityTests(unittest.TestCase):
    def test_password_length_is_bounded(self):
        self.assertIsNotNone(auth.validate_credentials("alice", "a" * 257))
        with self.assertRaises(ValueError):
            auth.hash_password("a" * 257)
        self.assertFalse(auth.verify_password("a" * 257, "anything"))

    def test_legacy_password_hash_still_verifies(self):
        salt = b"legacy-salt"
        digest = hashlib.pbkdf2_hmac(
            "sha256", b"old-password", salt, 200_000).hex()
        stored = f"pbkdf2_sha256$200000${salt.hex()}${digest}"
        self.assertTrue(auth.verify_password("old-password", stored))
        self.assertFalse(auth.verify_password("wrong", stored))

    def test_success_clears_only_its_rate_limit_keys(self):
        auth._attempts.clear()
        try:
            for key in ("account:a", "account:b"):
                for _ in range(5):
                    self.assertTrue(auth.rate_limit_allowed(key))
                    auth.record_auth_failure(key)
            auth.clear_rate_limit("account:a")
            self.assertTrue(auth.rate_limit_allowed("account:a"))
            self.assertFalse(auth.rate_limit_allowed("account:b"))
        finally:
            auth._attempts.clear()


class ConfigSecurityTests(unittest.TestCase):
    def test_global_patch_is_atomic_and_preserves_masked_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            config_path = data_dir / "config.json"
            config_path.write_text(json.dumps({
                "school": "青岛理工大学",
                "security": {"token": "keep-me"},
            }), encoding="utf-8")
            with (patch.object(config, "DATA_DIR", data_dir),
                  patch.object(config, "CONFIG_PATH", config_path)):
                from app.user_context import current_user_id
                context_token = current_user_id.set(None)
                try:
                    saved = config.save_config({
                        "school": "新学校",
                        "security": {"token": config.MASK},
                    })
                    self.assertEqual(saved["security"]["token"], "keep-me")
                    self.assertEqual(config.global_config()["school"], "新学校")
                    self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(list(data_dir.glob(".config-*.tmp")), [])
                finally:
                    current_user_id.reset(context_token)


class RegistrationSecurityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request():
        return Request({
            "type": "http", "method": "POST", "path": "/api/auth/register",
            "headers": [], "client": ("127.0.0.1", 1234),
            "scheme": "http", "server": ("test", 80),
        })

    async def test_first_user_does_not_inherit_credentials_or_overrides(self):
        from app import main

        template = json.loads(json.dumps(config.DEFAULT_CONFIG))
        template["school"] = "共享学校"
        template["provider"]["portal"]["headers"] = '{"Authorization":"secret"}'
        template["provider"]["zhengfang"]["password"] = "old-password"
        template["push"]["bark_key"] = "old-key"
        template["calendar_subscribe_token"] = "old-token"
        template["course_overrides"]["added"] = [{"name": "旧课程"}]
        saved = {}

        def capture(uid, cfg):
            saved.update(cfg)

        user = {"id": 7, "username": "alice", "role": "admin", "is_active": 1,
                "display_name": "", "student_id": "", "created_at": "",
                "last_login_at": ""}
        with (patch.object(main.auth, "consume_rate_limit", return_value=True),
              patch.object(main.auth, "hash_password", return_value="new-hash"),
              patch.object(main.db, "registration_error", return_value=None),
              patch.object(main.db, "register_user_atomic", return_value=(7, True)),
              patch.object(main.db, "save_user_config", side_effect=capture),
              patch.object(main.db, "get_user_by_id", return_value=user),
              patch.object(main.db, "checked_create_session", return_value=True),
              patch.object(main, "global_config", return_value=template),
              patch.object(main, "run_in_threadpool", AsyncMock(
                  side_effect=lambda fn, *args: fn(*args))),
              patch.dict(main.os.environ, {"AGENDA_SKIP_REGISTER_SYNC": "1"})):
            response = await main.auth_register(
                main.RegisterRequest(username="alice", password="new-password"),
                self.request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved["school"], "共享学校")
        self.assertEqual(saved["provider"]["portal"]["headers"], "")
        self.assertEqual(saved["provider"]["zhengfang"]["password"], "")
        self.assertEqual(saved["push"]["bark_key"], "")
        self.assertEqual(saved["calendar_subscribe_token"], "")
        self.assertEqual(saved["course_overrides"]["added"], [])

    async def test_invalid_invite_is_rejected_before_password_hashing(self):
        from app import main

        with (patch.object(main.auth, "consume_rate_limit", return_value=True),
              patch.object(main.db, "registration_error", return_value="邀请码无效"),
              patch.object(main, "run_in_threadpool", AsyncMock()) as hashing):
            with self.assertRaises(HTTPException) as raised:
                await main.auth_register(
                    main.RegisterRequest(username="alice", password="new-password",
                                         invite="invalid"), self.request())
        self.assertEqual(raised.exception.status_code, 400)
        hashing.assert_not_awaited()


class CsrfSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_origin_post_is_rejected_without_optional_token(self):
        from app.main import AccessGuard

        called = False

        async def downstream(scope, receive, send):
            nonlocal called
            called = True
            await JSONResponse({"ok": True})(scope, receive, send)

        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http", "method": "POST", "path": "/api/events",
            "headers": [(b"origin", b"https://evil.example"),
                        (b"host", b"agenda.example")],
        }
        with (patch("app.main.db.get_session_user", return_value=None),
              patch("app.main.load_config", return_value={"security": {}})):
            await AccessGuard(downstream)(scope, receive, send)
        self.assertFalse(called)
        self.assertEqual(messages[0]["status"], 403)


    async def test_oversized_api_body_is_rejected_before_auth_lookup(self):
        from app.main import AccessGuard

        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.request", "body": b"x" * 32, "more_body": False}

        scope = {
            "type": "http", "method": "POST", "path": "/api/ingest",
            "headers": [(b"content-length", b"32")],
        }
        with (patch("app.main.db.get_session_user") as session_lookup,
              patch("app.main.MAX_API_BODY_BYTES", 16)):
            await AccessGuard(lambda scope, receive, send: None)(scope, receive, send)
        self.assertEqual(messages[0]["status"], 413)
        session_lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
