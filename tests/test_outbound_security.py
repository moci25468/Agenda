from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

import httpx

from app.browser_login import (
    _candidate_url_allowed,
    _cookie_from_context,
    _headers_for_config,
    pick_best,
)
from app.netguard import UnsafeURLError, _SafeNetworkBackend, assert_safe_outbound_url
from app.providers.httpjson import _cookie_updates, _redirect_headers
from app.providers.zhengfang import _check_zhengfang_request


class OutboundSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_local_and_metadata_literals(self):
        for url in (
            "http://127.0.0.1/",
            "http://localhost/",
            "http://0.0.0.0/",
            "http://169.254.169.254/",
            "http://[::1]/",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeURLError):
                assert_safe_outbound_url(url)

    async def test_rejects_dns_rebinding(self):
        class Loop:
            async def getaddrinfo(self, *args, **kwargs):
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                         "", ("127.0.0.1", 443))]

        backend = _SafeNetworkBackend()
        with patch("app.netguard.asyncio.get_running_loop", return_value=Loop()):
            with self.assertRaises(UnsafeURLError):
                await backend.connect_tcp("rebind.example", 443)

    async def test_zhengfang_redirect_strips_cookie(self):
        request = httpx.Request(
            "GET", "https://evil.example/", headers={"Cookie": "sid=secret"})
        await _check_zhengfang_request(
            request, ("https", "portal.qut.edu.cn", 443))
        self.assertNotIn("cookie", request.headers)


class CredentialIsolationTests(unittest.TestCase):
    def test_redirect_strips_custom_credentials_cross_origin(self):
        headers = {"X-Token": "secret", "Accept": "application/json"}
        redirected = _redirect_headers(
            headers, "https://school.example/", "https://evil.example/", "GET")
        self.assertNotIn("X-Token", redirected)
        self.assertEqual(redirected["Accept"], "application/json")

    def test_cookie_domain_must_belong_to_response_host(self):
        response = httpx.Response(200, headers=[
            ("set-cookie", "sid=bad; Domain=evil.example; Path=/"),
            ("set-cookie", "safe=yes; Domain=.qut.edu.cn; Path=/"),
        ])
        self.assertEqual(
            _cookie_updates(response, "portal.qut.edu.cn"), {"safe": "yes"})

    def test_browser_rejects_external_candidates(self):
        candidate = {
            "url": "https://evil.example/xskb?next=qut.edu.cn",
            "kind": "courses",
            "status": 200,
            "mapping": {"name": "name", "day": "day"},
            "count": 20,
        }
        self.assertFalse(_candidate_url_allowed(candidate["url"]))
        self.assertIsNone(pick_best([candidate]))
        with self.assertRaises(ValueError):
            _headers_for_config(candidate)

    def test_browser_cookie_selection_is_host_scoped(self):
        cookies = [
            {"domain": ".qut.edu.cn", "name": "root", "value": "a"},
            {"domain": "portal.qut.edu.cn", "name": "portal", "value": "b"},
            {"domain": "other.qut.edu.cn", "name": "other", "value": "c"},
            {"domain": "evil.example", "name": "evil", "value": "d"},
        ]
        self.assertEqual(
            _cookie_from_context(cookies, "portal.qut.edu.cn"),
            "root=a; portal=b")


if __name__ == "__main__":
    unittest.main()
