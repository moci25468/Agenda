from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from app.providers import httpjson
from app.providers.base import AuthExpiredError, PortalResponseError


class PortalAuthClassificationTests(unittest.TestCase):
    def response(self, status=200, *, url="https://portal.qut.edu.cn/api/courses",
                 text="", content_type="text/html", payload=None):
        kwargs = {"json": payload} if payload is not None else {
            "text": text, "headers": {"Content-Type": content_type}}
        return httpx.Response(status, request=httpx.Request("GET", url), **kwargs)

    def test_official_sso_login_url_is_auth_expired(self):
        for url in ("https://sso.qut.edu.cn/sso/login?service=portal",
                    "https://sso.qut.edu.cn/cas/login?service=portal"):
            with self.subTest(url=url), self.assertRaises(AuthExpiredError):
                httpjson.check_auth_expired(self.response(url=url, text="<html></html>"))

    def test_login_title_and_login_form_are_explicit_auth_evidence(self):
        pages = (
            "<html><title>青岛理工大学统一身份认证</title></html>",
            '<html><form action="/login"><input name="username">'
            '<input type="password" name="password"></form></html>',
        )
        for status in (200, 403):
            for page in pages:
                with self.subTest(status=status, page=page), self.assertRaises(AuthExpiredError):
                    httpjson.check_auth_expired(self.response(status, text=page))

    def test_http_401_is_auth_expired_even_without_login_markup(self):
        with self.assertRaises(AuthExpiredError):
            httpjson.check_auth_expired(self.response(401, text="Unauthorized"))

    def test_json_requires_unauthenticated_evidence_not_merely_forbidden(self):
        for payload in ({"code": 401}, {"status": "401"},
                        {"code": 403, "msg": "未登录"},
                        {"status": "error", "message": "会话过期，请重新登录"},
                        {"message": "Unauthorized"}):
            with self.subTest(payload=payload), self.assertRaises(AuthExpiredError):
                httpjson.check_auth_expired(self.response(payload=payload))
        for payload in ({"code": 403, "message": "Forbidden"},
                        {"status": 403, "message": "访问被拒绝"},
                        {"code": 200, "data": {"login": "help"}}):
            with self.subTest(payload=payload):
                httpjson.check_auth_expired(self.response(payload=payload))

    def test_generic_html_login_links_and_auth_mentions_are_not_login_pages(self):
        page = ('<html><title>服务维护</title><p>统一身份认证服务升级</p>'
                '<a href="https://sso.qut.edu.cn/sso/login">login</a></html>')
        for status in (200, 403, 500, 502):
            with self.subTest(status=status):
                httpjson.check_auth_expired(self.response(status, text=page))

    def test_server_error_at_login_endpoint_is_not_auth_expired(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                httpjson.check_auth_expired(self.response(
                    status, url="https://sso.qut.edu.cn/sso/login",
                    text="<title>统一身份认证</title><p>服务维护</p>"))

    def test_sso_word_in_query_does_not_make_an_api_a_login_page(self):
        httpjson.check_auth_expired(self.response(
            url="https://portal.qut.edu.cn/api/courses?return=https://sso.qut.edu.cn/login",
            payload={"courses": []}))


class PortalFetchResponseTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, handler):
        with patch.object(httpjson, "SafeAsyncHTTPTransport",
                          return_value=httpx.MockTransport(handler)):
            provider = httpjson.PortalProvider({"provider": {"portal": {
                "url": "https://portal.qut.edu.cn/api/courses",
                "headers": {"Cookie": "sid=synthetic"},
            }}})
            return await provider.fetch_raw()

    async def test_unexpected_successful_content_has_a_neutral_recoverable_error(self):
        for text, ctype in (
            ('<html><title>维护</title><a href="/login">login</a></html>', "text/html"),
            ("temporarily unavailable", "text/plain"),
            ("{broken-json", "application/json"),
        ):
            with self.subTest(content_type=ctype):
                with self.assertRaises(PortalResponseError) as error:
                    await self.fetch(lambda request: httpx.Response(
                        200, text=text, headers={"Content-Type": ctype}))
                self.assertNotIsInstance(error.exception, AuthExpiredError)
                self.assertNotRegex(str(error.exception), r"Cookie|cURL|登录|过期|失效")

    async def test_ordinary_http_errors_keep_the_http_status_error(self):
        for status in (403, 500, 502, 503):
            with self.subTest(status=status):
                with self.assertRaises(httpx.HTTPStatusError) as error:
                    await self.fetch(lambda request: httpx.Response(
                        status, text='<html><a href="/login">login</a>service unavailable</html>',
                        headers={"Content-Type": "text/html"}))
                self.assertEqual(error.exception.response.status_code, status)

    async def test_server_error_with_login_page_title_keeps_the_http_error(self):
        with self.assertRaises(httpx.HTTPStatusError):
            await self.fetch(lambda request: httpx.Response(
                503, text="<title>统一身份认证</title>",
                headers={"Content-Type": "text/html"}))

    async def test_explicit_login_page_inside_403_is_auth_expired(self):
        with self.assertRaises(AuthExpiredError):
            await self.fetch(lambda request: httpx.Response(
                403, text="<title>青岛理工大学统一身份认证</title>",
                headers={"Content-Type": "text/html"}))

    async def test_official_login_redirect_remains_auth_expired(self):
        requests = []

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={
                    "Location": "https://sso.qut.edu.cn/sso/login?service=portal"})
            return httpx.Response(200, text="<title>青岛理工大学统一身份认证</title>",
                                  headers={"Content-Type": "text/html"})

        with self.assertRaises(AuthExpiredError):
            await self.fetch(respond)
        self.assertEqual(len(requests), 2)
        self.assertNotIn("Cookie", requests[1].headers)

    async def test_successful_json_still_returns_data(self):
        payload = {"courses": [{"name": "数学", "day": 1, "start": 1, "end": 2}]}
        result = await self.fetch(lambda request: httpx.Response(200, json=payload))
        self.assertEqual(result, payload)


if __name__ == "__main__":
    unittest.main()
