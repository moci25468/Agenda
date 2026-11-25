from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import browser_login, config, db


def provider_config():
    provider = copy.deepcopy(config.DEFAULT_CONFIG["provider"])
    provider["type"] = "portal"
    provider["portal"].update({
        "url": "https://portal.qut.edu.cn/api/xskb",
        "headers": {"Cookie": "sid=old", "Accept": "application/json"},
    })
    return provider


def page_state(url=browser_login.PORTAL_ENTRY, *, ready="complete", login=False):
    return {"url": url, "ready": ready, "login": login}


class PortalURLTests(unittest.TestCase):
    def test_only_actual_portal_routes_are_login_candidates(self):
        self.assertTrue(browser_login._looks_logged_in(browser_login.PORTAL_ENTRY))
        for url in (
            "https://www.qut.edu.cn/",
            "https://portal-other.qut.edu.cn/",
            "https://portal.qut.edu.cn/#/login",
            "https://portal.qut.edu.cn/#/%6cogin",
            "https://portal.qut.edu.cn/login",
            "https://portal.qut.edu.cn/oauth/callback",
            "https://sso.qut.edu.cn/login",
            "http://portal.qut.edu.cn/portal/event",
            "https://portal.qut.edu.cn.attacker.example/",
        ):
            with self.subTest(url=url):
                self.assertFalse(browser_login._looks_logged_in(url))


class SilentRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.events = []
        self.provider = provider_config()
        self.page = MagicMock()
        self.page.evaluate.side_effect = lambda _: page_state()
        self.page.wait_for_timeout.side_effect = self.advance
        self.old_page = MagicMock()
        self.old_page.url = "https://www.qut.edu.cn/"
        self.ctx = MagicMock()
        self.ctx.pages = [self.old_page]
        self.ctx.new_page.return_value = self.page
        self.ctx.cookies.return_value = [
            {"name": "sid", "value": "renewed", "domain": "portal.qut.edu.cn"},
        ]
        self.ctx.close.side_effect = lambda: self.events.append("context-close")
        self.pw = MagicMock()
        self.pw.chromium.launch_persistent_context.return_value = self.ctx
        self.manager = MagicMock()
        self.manager.__enter__.return_value = self.pw
        self.manager.__exit__.side_effect = lambda *_: self.events.append("driver-exit")
        self.factory = MagicMock(return_value=self.manager)
        playwright = types.ModuleType("playwright")
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = self.factory
        playwright.sync_api = sync_api
        self.enterContext(patch.dict(sys.modules, {
            "playwright": playwright, "playwright.sync_api": sync_api,
        }))
        self.enterContext(patch.object(browser_login, "get_current_user", return_value=7))
        self.enterContext(patch.object(db, "get_user_config", side_effect=lambda _: {
            "provider": copy.deepcopy(self.provider),
        }))
        self.enterContext(patch.object(browser_login.time, "monotonic", side_effect=lambda: self.now))
        self.save_cookie = self.enterContext(patch.object(
            browser_login, "_save_portal_cookie", return_value=True))

    def advance(self, milliseconds):
        self.now += milliseconds / 1000

    def refresh(self, save=True):
        return browser_login._silent_refresh_inner(Path("/unused-synthetic-profile"), save)

    def assert_technical_failure(self, result, reason):
        self.assertEqual(result["reason"], reason)
        self.assertFalse(result["expired"])
        self.assertFalse(result["refreshed"])

    def assert_closed_before_driver(self):
        self.assertEqual(self.events, ["context-close", "driver-exit"])

    def test_new_tab_and_cookie_are_used_and_context_closes_before_driver(self):
        result = self.refresh()
        self.assertTrue(result["refreshed"])
        self.assertEqual(result["reason"], "cookie-refreshed")
        self.ctx.new_page.assert_called_once()
        self.old_page.evaluate.assert_not_called()
        self.ctx.cookies.assert_called_with("https://portal.qut.edu.cn/api/xskb")
        self.save_cookie.assert_called_once_with("sid=renewed", self.provider, 7)
        self.assert_closed_before_driver()

    def test_delayed_spa_is_allowed_to_finish(self):
        self.page.evaluate.side_effect = lambda _: page_state(
            ready="loading" if self.now < 8 else "complete")
        result = self.refresh()
        self.assertTrue(result["refreshed"])
        self.assertGreaterEqual(self.now, 10)

    def test_transient_sso_page_does_not_trigger_reauthentication(self):
        self.page.evaluate.side_effect = lambda _: (
            page_state("https://sso.qut.edu.cn/login", login=True)
            if self.now < 6 else page_state())
        result = self.refresh()
        self.assertTrue(result["refreshed"])
        self.assertFalse(result["expired"])
        self.assertGreaterEqual(self.now, 8)

    def test_only_stable_current_login_page_confirms_expiry(self):
        self.page.evaluate.side_effect = lambda _: page_state(
            "https://sso.qut.edu.cn/login", login=True)
        result = self.refresh()
        self.assertTrue(result["expired"])
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["reason"], "redirected-to-login")
        self.assertGreaterEqual(self.now, browser_login.SILENT_WAIT_SECONDS)
        self.ctx.cookies.assert_not_called()
        self.old_page.evaluate.assert_not_called()
        self.assert_closed_before_driver()

    def test_login_url_without_visible_login_ui_does_not_confirm_expiry(self):
        self.page.evaluate.side_effect = lambda _: page_state(
            "https://sso.qut.edu.cn/login", login=False)
        self.assert_technical_failure(self.refresh(), "login-timeout")

    def test_recent_redirect_at_deadline_is_not_confirmed_expired(self):
        self.page.evaluate.side_effect = lambda _: (
            page_state("about:blank", ready="loading") if self.now < 29
            else page_state("https://sso.qut.edu.cn/login", login=True))
        self.assert_technical_failure(self.refresh(), "login-timeout")

    def test_hash_login_route_cannot_reuse_cookies_as_portal_success(self):
        self.page.evaluate.side_effect = lambda _: page_state(
            "https://portal.qut.edu.cn/#/login", login=True)
        result = self.refresh()
        self.assertTrue(result["expired"])
        self.ctx.cookies.assert_not_called()
        self.save_cookie.assert_not_called()

    def test_missing_cookie_is_unconfirmed_failure(self):
        self.ctx.cookies.return_value = []
        self.assert_technical_failure(self.refresh(), "no-cookie")
        self.save_cookie.assert_not_called()

    def test_unchanged_cookie_remains_available_for_api_verification(self):
        self.ctx.cookies.return_value[0]["value"] = "old"
        result = self.refresh()
        self.assertTrue(result["refreshed"])
        self.assertEqual(result["reason"], "cookie-unchanged")

    def test_diagnostic_mode_does_not_claim_config_is_refreshed(self):
        self.assert_technical_failure(self.refresh(save=False), "not-saved")
        self.save_cookie.assert_not_called()

    def test_configuration_change_during_refresh_is_not_overwritten(self):
        self.save_cookie.return_value = False
        self.assert_technical_failure(self.refresh(), "configuration-changed")

    def test_token_only_source_does_not_claim_cookie_refresh(self):
        self.provider["portal"]["headers"] = {"Authorization": "synthetic-token"}
        self.assert_technical_failure(self.refresh(), "no-cookie-header")
        self.factory.assert_not_called()

    def test_unsupported_source_is_rejected_before_opening_browser(self):
        self.provider["portal"]["url"] = "https://school.attacker.example/xskb"
        self.assert_technical_failure(self.refresh(), "unsupported-source")
        self.factory.assert_not_called()

    def test_navigation_exception_is_sanitized_and_context_is_closed(self):
        self.page.goto.side_effect = RuntimeError("https://secret.example/?token=secret")
        result = self.refresh()
        self.assert_technical_failure(result, "navigation-failed")
        self.assertNotIn("secret", repr(result))
        self.assert_closed_before_driver()

    def test_launch_failure_is_sanitized(self):
        self.pw.chromium.launch_persistent_context.side_effect = RuntimeError("secret-profile-path")
        result = self.refresh()
        self.assert_technical_failure(result, "browser-launch-failed")
        self.assertEqual(self.pw.chromium.launch_persistent_context.call_count, 2)
        self.assertNotIn("secret", repr(result))

    def test_chromium_fallback_retains_same_profile(self):
        self.pw.chromium.launch_persistent_context.side_effect = [RuntimeError("no-chrome"), self.ctx]
        self.assertTrue(self.refresh()["refreshed"])
        first, second = self.pw.chromium.launch_persistent_context.call_args_list
        self.assertEqual(first.args, second.args)
        self.assertNotIn("channel", second.kwargs)

    def test_close_error_does_not_leak_details_or_confirm_expiry(self):
        self.ctx.close.side_effect = RuntimeError("secret-profile-path")
        result = self.refresh()
        self.assert_technical_failure(result, "refresh-failed")
        self.assertNotIn("secret", repr(result))

    def test_preflight_unavailable_profile_and_memory_are_unconfirmed(self):
        profile = MagicMock()
        profile.is_dir.return_value = True
        profile.iterdir.return_value = iter([Path("synthetic-state")])
        with (patch.object(browser_login, "_profile_dir", return_value=profile),
              patch.object(browser_login, "_available_mb", return_value=100)):
            self.assert_technical_failure(browser_login.silent_refresh_cookie(), "low-memory")
        profile.iterdir.side_effect = PermissionError("secret-profile-path")
        with patch.object(browser_login, "_profile_dir", return_value=profile):
            result = browser_login.silent_refresh_cookie()
        self.assert_technical_failure(result, "profile-unavailable")
        self.assertNotIn("secret", repr(result))

    def test_missing_optional_browser_dependency_is_not_expiry(self):
        with patch.dict(sys.modules, {"playwright.sync_api": None}):
            self.assert_technical_failure(self.refresh(), "no-playwright")

    def test_no_user_or_profile_is_not_expiry(self):
        with patch.object(browser_login, "get_current_user", return_value=None):
            self.assert_technical_failure(browser_login.silent_refresh_cookie(), "no-user")
        profile = MagicMock()
        profile.is_dir.return_value = False
        with patch.object(browser_login, "_profile_dir", return_value=profile):
            self.assert_technical_failure(browser_login.silent_refresh_cookie(), "no-profile")

    def test_busy_or_interactive_browser_is_not_expiry(self):
        profile = MagicMock()
        profile.is_dir.return_value = True
        profile.iterdir.side_effect = lambda: iter([Path("synthetic-state")])
        active = MagicMock()
        active.is_alive.return_value = True
        lock = threading.Lock()
        with (patch.object(browser_login, "_profile_dir", return_value=profile),
              patch.object(browser_login, "_available_mb", return_value=1000),
              patch.object(browser_login, "_SILENT_LOCK", lock),
              patch.object(browser_login, "_thread", active)):
            self.assert_technical_failure(
                browser_login.silent_refresh_cookie(), "interactive-login-running")
            with patch.object(browser_login, "_thread", None):
                lock.acquire()
                try:
                    self.assert_technical_failure(browser_login.silent_refresh_cookie(), "busy")
                    self.assertFalse(browser_login.start()["ok"])
                finally:
                    lock.release()

    def test_unexpected_failure_is_sanitized_and_releases_global_browser_lock(self):
        profile = MagicMock()
        profile.is_dir.return_value = True
        profile.iterdir.side_effect = lambda: iter([Path("synthetic-state")])
        lock = threading.Lock()
        with (patch.object(browser_login, "_profile_dir", return_value=profile),
              patch.object(browser_login, "_available_mb", return_value=1000),
              patch.object(browser_login, "_SILENT_LOCK", lock),
              patch.object(browser_login, "_thread", None),
              patch.object(browser_login, "_silent_refresh_inner", side_effect=RuntimeError("secret"))):
            result = browser_login.silent_refresh_cookie()
        self.assert_technical_failure(result, "refresh-failed")
        self.assertNotIn("secret", repr(result))
        self.assertFalse(lock.locked())


class CookieCompareAndSwapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.enterContext(patch.object(db, "DATA_DIR", self.root))
        self.enterContext(patch.object(db, "DB_PATH", self.root / "campus.db"))
        db.init_db()
        self.uid = db.create_user("cookie-test", "not-a-real-hash")
        self.provider = provider_config()
        db.save_user_config(self.uid, {"provider": self.provider, "school": "keep-school"})

    def test_saved_cookie_preserves_other_provider_headers_and_config(self):
        self.assertTrue(browser_login._save_portal_cookie("sid=renewed", self.provider, self.uid))
        saved = db.get_user_config(self.uid)
        self.assertEqual(saved["school"], "keep-school")
        self.assertEqual(saved["provider"]["portal"]["headers"], {
            "Cookie": "sid=renewed", "Accept": "application/json",
        })
        self.assertEqual(saved["provider"]["zhengfang"], self.provider["zhengfang"])

    def test_changed_source_or_credentials_rejects_stale_refresh(self):
        for field, value in (("headers", {"Cookie": "new-account"}),
                             ("url", "https://portal.qut.edu.cn/api/new-source")):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.provider)
                changed["portal"][field] = value
                db.save_user_config(self.uid, {"provider": changed})
                self.assertFalse(browser_login._save_portal_cookie(
                    "sid=stale-refresh", self.provider, self.uid))
                self.assertEqual(db.get_user_config(self.uid)["provider"], changed)

    def test_primary_source_change_rejects_stale_refresh(self):
        changed = copy.deepcopy(self.provider)
        changed["primary"] = "zhengfang"
        db.save_user_config(self.uid, {"provider": changed})
        self.assertFalse(browser_login._save_portal_cookie("sid=renewed", self.provider, self.uid))
        self.assertEqual(db.get_user_config(self.uid)["provider"], changed)

    def test_legacy_headers_are_migrated_without_overriding_new_cookie(self):
        legacy = {"type": "portal", "http_url": self.provider["portal"]["url"],
                  "http_headers": json.dumps({"Cookie": "sid=legacy"})}
        db.save_user_config(self.uid, {"provider": legacy})
        self.assertTrue(browser_login._save_portal_cookie("sid=renewed", legacy, self.uid))
        saved = db.get_user_config(self.uid)["provider"]
        self.assertNotIn("http_headers", saved)
        self.assertEqual(config.normalize_provider(saved)["portal"]["headers"]["Cookie"], "sid=renewed")


if __name__ == "__main__":
    unittest.main()
