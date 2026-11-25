from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import browser_login, scheduler
from app.providers.base import AuthExpiredError, PortalResponseError
from app.user_context import current_user_id


class PortalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = {"provider": {"type": "portal", "primary": "portal", "portal": {
            "url": "https://portal.qut.edu.cn/api/xskb", "headers": {"Cookie": "synthetic"},
        }}}
        self.bundle = ([{"name": "synthetic-course"}], [])

    def provider(self, outcome):
        fetch = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
        return SimpleNamespace(name="portal", fetch_bundle=fetch)

    async def test_success_does_not_launch_browser(self):
        provider = self.provider(self.bundle)
        with (patch.object(scheduler, "get_provider", return_value=provider),
              patch.object(browser_login, "silent_refresh_cookie") as refresh):
            result = await scheduler._fetch_bundle_with_cookie_refresh(self.cfg)
        self.assertEqual(result, (provider, self.bundle))
        refresh.assert_not_called()

    async def test_expired_and_unexpected_page_both_try_recovery_and_retry_current_config(self):
        for error in (AuthExpiredError("expired"), PortalResponseError("unexpected page")):
            with self.subTest(error=type(error).__name__):
                original = self.provider(error)
                recovered = self.provider(self.bundle)
                current_cfg = {**self.cfg, "school": "updated-during-recovery"}
                seen_users = []

                def refresh():
                    seen_users.append(current_user_id.get())
                    return {"ok": True, "refreshed": True, "expired": False}

                token = current_user_id.set(123)
                try:
                    with (patch.object(scheduler, "get_provider", side_effect=[original, recovered]) as factory,
                          patch.object(scheduler, "load_config", return_value=current_cfg),
                          patch.object(scheduler.db, "add_sync_log") as log,
                          patch.object(browser_login, "silent_refresh_cookie", side_effect=refresh)):
                        result = await scheduler._fetch_bundle_with_cookie_refresh(self.cfg)
                finally:
                    current_user_id.reset(token)
                self.assertEqual(result, (recovered, self.bundle))
                self.assertEqual(seen_users, [123])
                self.assertIs(factory.call_args_list[1].args[0], current_cfg)
                original.fetch_bundle.assert_awaited_once()
                recovered.fetch_bundle.assert_awaited_once()
                self.assertEqual(log.call_args.args[0], "ok")

    async def test_temporary_recovery_failures_are_not_reported_as_expired(self):
        for reason in ("busy", "low-memory", "no-profile", "navigation-failed", "login-timeout"):
            with self.subTest(reason=reason):
                provider = self.provider(AuthExpiredError("original-cookie-expired"))
                with (patch.object(scheduler, "get_provider", return_value=provider),
                      patch.object(browser_login, "silent_refresh_cookie", return_value={
                          "ok": False, "refreshed": False, "expired": False, "reason": reason,
                      })):
                    with self.assertRaises(scheduler.PortalRefreshError) as raised:
                        await scheduler._fetch_bundle_with_cookie_refresh(self.cfg)
                self.assertEqual(raised.exception.reason, reason)
                self.assertNotIn("original-cookie-expired", str(raised.exception))

    async def test_only_confirmed_login_page_requires_rescanning(self):
        with (patch.object(scheduler, "get_provider", return_value=self.provider(AuthExpiredError("expired"))),
              patch.object(browser_login, "silent_refresh_cookie", return_value={
                  "ok": True, "refreshed": False, "expired": True, "reason": "redirected-to-login",
              })):
            with self.assertRaises(AuthExpiredError) as raised:
                await scheduler._fetch_bundle_with_cookie_refresh(self.cfg)
        self.assertIn("重新扫码", str(raised.exception))

    async def test_browser_exception_does_not_expose_credentials(self):
        secret = "https://portal.qut.edu.cn/?ticket=synthetic-secret"
        with (patch.object(scheduler, "get_provider", return_value=self.provider(AuthExpiredError("expired"))),
              patch.object(browser_login, "silent_refresh_cookie", side_effect=RuntimeError(secret))):
            with self.assertRaises(scheduler.PortalRefreshError) as raised:
                await scheduler._fetch_bundle_with_cookie_refresh(self.cfg)
        self.assertEqual(raised.exception.reason, "refresh-failed")
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(scheduler.PortalRefreshError(secret).reason, "refresh-failed")

    async def test_cookie_candidate_rejected_by_api_is_not_proof_profile_expired(self):
        original = self.provider(AuthExpiredError("expired"))
        rejected = self.provider(AuthExpiredError("still rejected"))
        with (patch.object(scheduler, "get_provider", side_effect=[original, rejected]),
              patch.object(scheduler, "load_config", return_value=self.cfg),
              patch.object(browser_login, "silent_refresh_cookie", return_value={
                  "ok": True, "refreshed": True, "expired": False,
              })):
            with self.assertRaises(scheduler.PortalRefreshError) as raised:
                await scheduler._fetch_bundle_with_cookie_refresh(self.cfg)
        self.assertEqual(raised.exception.reason, "retry-rejected")

    async def test_network_failure_does_not_launch_browser(self):
        with (patch.object(scheduler, "get_provider", return_value=self.provider(RuntimeError("network"))),
              patch.object(browser_login, "silent_refresh_cookie") as refresh):
            with self.assertRaisesRegex(RuntimeError, "network"):
                await scheduler._fetch_bundle_with_cookie_refresh(self.cfg)
        refresh.assert_not_called()

    async def test_recovery_failure_preserves_snapshot_and_logs_actual_reason(self):
        error = scheduler.PortalRefreshError("low-memory")
        with (patch.object(scheduler, "load_config", return_value=self.cfg),
              patch.object(scheduler, "_fetch_bundle_with_cookie_refresh", AsyncMock(side_effect=error)),
              patch.object(scheduler, "_fallback_bundle", AsyncMock(return_value=None)),
              patch.object(scheduler, "_alert_auth_expired", AsyncMock()) as alert,
              patch.object(scheduler, "apply_snapshot", AsyncMock()) as apply,
              patch.object(scheduler.db, "add_sync_log") as log):
            result = await scheduler._run_sync_inner(push=False)
        self.assertFalse(result["ok"])
        self.assertIn("内存不足", result["message"])
        self.assertIn("内存不足", log.call_args.args[1])
        self.assertIs(alert.call_args.args[2], error)
        apply.assert_not_awaited()


class PortalAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = {"alert_on_fail": True, "push": {"bark_enabled": True}}

    async def test_browser_capacity_issue_has_specific_neutral_notification(self):
        with patch.object(scheduler, "_push_reminder", AsyncMock()) as push:
            await scheduler._alert_auth_expired(
                self.cfg, "portal", scheduler.PortalRefreshError("low-memory"), alert_failure=True)
        self.assertEqual(push.call_args.args[3], "门户自动恢复暂未完成")
        self.assertIn("内存不足", push.call_args.args[4])
        self.assertNotIn("重新扫码", push.call_args.args[4])

    async def test_existing_login_or_recovery_does_not_send_failure_notification(self):
        with patch.object(scheduler, "_push_reminder", AsyncMock()) as push:
            for reason in ("busy", "interactive-login-running", "configuration-changed"):
                await scheduler._alert_auth_expired(
                    self.cfg, "portal", scheduler.PortalRefreshError(reason), alert_failure=True)
        push.assert_not_awaited()

    async def test_incidental_cookie_or_curl_text_does_not_trigger_login_alert(self):
        for error in (ValueError("Cookie cURL response format"), PortalResponseError("unexpected HTML")):
            with self.subTest(error=type(error).__name__):
                with patch.object(scheduler, "_push_reminder", AsyncMock()) as push:
                    await scheduler._alert_auth_expired(self.cfg, "portal", error, alert_failure=True)
                self.assertEqual(push.call_args.args[3], "门户课表同步失败")
                self.assertNotIn("重新扫码", push.call_args.args[4])

    async def test_confirmed_expiry_notifies_user_to_rescan(self):
        with patch.object(scheduler, "_push_reminder", AsyncMock()) as push:
            await scheduler._alert_auth_expired(self.cfg, "portal", AuthExpiredError("login required"))
        self.assertEqual(push.call_args.args[3], "门户登录已过期")
        self.assertIn("已尝试自动恢复", push.call_args.args[4])
        self.assertIn("重新扫码", push.call_args.args[4])


if __name__ == "__main__":
    unittest.main()
