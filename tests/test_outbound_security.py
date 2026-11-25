from __future__ import annotations

import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpcore
import httpx

from app.browser_login import (
    _candidate_url_allowed,
    _cookie_from_context,
    _headers_for_config,
    pick_best,
)
from app.netguard import (SafeAsyncHTTPTransport, UnsafeURLError, _SafeNetworkBackend,
                          assert_safe_outbound_url)
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


class TrackedCoreStream:
    def __init__(self, content: bytes, *, error: Exception | None = None):
        self.content = content
        self.error = error
        self.closed = False

    async def __aiter__(self):
        if self.error is not None:
            raise self.error
        yield self.content

    async def aclose(self):
        self.closed = True


class OutboundTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_httpcore_response_becomes_httpx_response_and_releases_stream(self):
        stream = TrackedCoreStream(b'{"ok":true}')
        core_response = httpcore.Response(
            200, headers=[(b"Content-Type", b"application/json")], content=stream,
            extensions={"http_version": b"HTTP/2"})
        pool = AsyncMock()
        pool.handle_async_request.return_value = core_response
        with patch("app.netguard.httpcore.AsyncConnectionPool", return_value=pool):
            async with httpx.AsyncClient(transport=SafeAsyncHTTPTransport(), trust_env=False) as client:
                response = await client.post("https://school.example/login", data={"name": "test"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(response.http_version, "HTTP/2")
        self.assertTrue(stream.closed)
        request = pool.handle_async_request.call_args.args[0]
        self.assertIsInstance(request, httpcore.Request)
        self.assertEqual(request.method, b"POST")
        self.assertEqual(b"".join([part async for part in request.stream]), b"name=test")
        pool.aclose.assert_awaited_once()

    async def test_read_failure_also_releases_core_response(self):
        stream = TrackedCoreStream(b"", error=httpcore.ReadError("synthetic read failure"))
        pool = AsyncMock()
        pool.handle_async_request.return_value = httpcore.Response(200, content=stream)
        with patch("app.netguard.httpcore.AsyncConnectionPool", return_value=pool):
            async with httpx.AsyncClient(transport=SafeAsyncHTTPTransport(), trust_env=False) as client:
                with self.assertRaises(httpcore.ReadError):
                    await client.get("https://school.example/login")
        self.assertTrue(stream.closed)

    async def test_httpx_redirect_follows_core_response_and_keeps_session_cookie(self):
        streams = [TrackedCoreStream(b""), TrackedCoreStream(b"ready")]
        pool = AsyncMock()
        pool.handle_async_request.side_effect = [
            httpcore.Response(302, headers=[
                (b"Location", b"/menu"), (b"Set-Cookie", b"sid=synthetic; Path=/")],
                content=streams[0]),
            httpcore.Response(200, content=streams[1]),
        ]
        with patch("app.netguard.httpcore.AsyncConnectionPool", return_value=pool):
            async with httpx.AsyncClient(transport=SafeAsyncHTTPTransport(), trust_env=False,
                                         follow_redirects=True) as client:
                response = await client.get("https://school.example/login")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ready")
        self.assertEqual(str(response.url), "https://school.example/menu")
        second = pool.handle_async_request.call_args_list[1].args[0]
        self.assertIn((b"Cookie", b"sid=synthetic"), second.headers)
        self.assertTrue(all(stream.closed for stream in streams))


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
