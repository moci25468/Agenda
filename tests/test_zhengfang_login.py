from __future__ import annotations

import base64
import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx
import httpcore
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.providers import zhengfang


def login_page(*, encrypted: bool = False, duplicate_password: bool = False,
               extra_fields: str = "") -> str:
    hidden_password = ('<input type="password" name="mm" id="hidMm" '
                       'style="display:none" autocomplete="off">') if duplicate_password else ""
    return (
        f'<input type="hidden" name="mmsfjm" id="mmsfjm" value= {int(encrypted)}>'
        '<form class="form-horizontal" action="/jwglxt/xtgl/login_slogin.html" method="post">'
        '<input type="hidden" id="csrftoken" name="csrftoken" value="csrf-synthetic">'
        '<input type="hidden" id="language" name="language" value="zh_CN">'
        '<input type="hidden" name="ydType" id="ydType" value="">'
        '<input type="text" name="yhm" id="yhm" value="">'
        f'{hidden_password}'
        '<input type="text" name="mm" id="mm" value="">'
        f'{extra_fields}</form>'
    )


class ZhengfangLoginTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, base: str = "https://school.example/jwglxt"):
        return zhengfang.ZhengfangProvider({"provider": {"zhengfang": {
            "base": base, "username": "synthetic-student",
            "password": "synthetic-password", "year": "2026", "term": "3"}}})

    async def login(self, respond, *, base: str = "https://school.example/jwglxt"):
        with patch.object(zhengfang, "SafeAsyncHTTPTransport",
                          return_value=httpx.MockTransport(respond)):
            async with self.provider(base)._session():
                pass

    async def test_rsa_login_preserves_cookies_and_follows_root_relative_redirect(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        public_key = private_key.public_key().public_numbers()

        def encode_int(number):
            return base64.b64encode(number.to_bytes((number.bit_length() + 7) // 8, "big")).decode()

        requests = []

        def respond(request):
            requests.append(request)
            path = request.url.path
            self.assertEqual(request.url.host, "school.example")
            if path == "/jwglxt/xtgl/login_slogin.html" and request.method == "GET":
                return httpx.Response(200, headers={"Set-Cookie": "JSESSIONID=synthetic; Path=/jwglxt"},
                    text=login_page(encrypted=True, duplicate_password=True))
            self.assertIn("JSESSIONID=synthetic", request.headers.get("Cookie", ""))
            if path == "/jwglxt/xtgl/login_getPublicKey.html":
                return httpx.Response(200, json={"modulus": encode_int(public_key.n),
                                                 "exponent": encode_int(public_key.e)})
            if path == "/jwglxt/xtgl/login_logoutAccount.html":
                self.assertEqual(request.method, "POST")
                self.assertEqual(parse_qs(request.content.decode(), keep_blank_values=True),
                                 {"csrfTokenLogout": [""]})
                return httpx.Response(200, text="success")
            if path == "/jwglxt/xtgl/login_slogin.html" and request.method == "POST":
                self.assertEqual(request.headers["Origin"], "https://school.example")
                form = parse_qs(request.content.decode(), keep_blank_values=True)
                self.assertEqual(form["csrftoken"], ["csrf-synthetic"])
                self.assertEqual(form["yhm"], ["synthetic-student"])
                self.assertEqual(form["language"], ["zh_CN"])
                self.assertEqual(form["ydType"], [""])
                self.assertEqual(len(form["mm"]), 2)
                self.assertEqual(form["mm"][0], form["mm"][1])
                self.assertNotIn("hidMm", form)
                self.assertEqual(requests[-2].url.path, "/jwglxt/xtgl/login_logoutAccount.html")
                clear_password = private_key.decrypt(base64.b64decode(form["mm"][0]), padding.PKCS1v15())
                self.assertEqual(clear_password, b"synthetic-password")
                return httpx.Response(302, headers={"Location": "/jwglxt/xtgl/index_initMenu.html"})
            if path == "/jwglxt/xtgl/index_initMenu.html":
                return httpx.Response(200, text="index_initMenu")
            if path == "/jwglxt/kbcx/xskbcx_cxXsKb.html":
                return httpx.Response(200, json={"kbList": [
                    {"kcmc": "数学", "xqj": "1", "jcs": "1-2", "zcd": "1-16周"}]})
            self.fail(f"Unexpected request: {request.method} {request.url}")

        with patch.object(zhengfang, "SafeAsyncHTTPTransport", return_value=httpx.MockTransport(respond)):
            courses = await self.provider().fetch()
        self.assertEqual([course.name for course in courses], ["数学"])
        self.assertEqual(len(requests), 6)

    async def test_relative_login_redirect_is_resolved_from_login_response_url(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.method == "POST":
                self.assertEqual(request.headers["Origin"], "https://school.example:8443")
                form = parse_qs(request.content.decode(), keep_blank_values=True)
                self.assertEqual(form["mm"], ["synthetic-password"])
                return httpx.Response(302, headers={"Location": "index_initMenu.html"})
            if request.url.path.endswith("login_slogin.html"):
                return httpx.Response(200, text=login_page())
            self.assertEqual(str(request.url), "https://school.example:8443/jwglxt/xtgl/index_initMenu.html")
            return httpx.Response(200, text="index_initMenu")

        await self.login(respond, base="https://school.example:8443/jwglxt")
        self.assertEqual(len(requests), 3)

    async def test_core_transport_keeps_rotated_cookies_through_two_login_redirects(self):
        requests = []
        test = self

        class Pool:
            async def handle_async_request(self, request):
                path = request.url.target.decode().split("?", 1)[0]
                headers = httpx.Headers(request.headers)
                method = request.method.decode()
                requests.append((method, path))
                cookie = headers.get("cookie", "")
                if len(requests) == 1:
                    return httpcore.Response(200, headers=[
                        (b"Set-Cookie", b"JSESSIONID=before; Path=/jwglxt"),
                        (b"Set-Cookie", b"route=synthetic; Path=/")],
                        content=login_page(duplicate_password=True).encode())
                test.assertIn("route=synthetic", cookie)
                if path.endswith("login_logoutAccount.html"):
                    test.assertIn("JSESSIONID=before", cookie)
                    return httpcore.Response(200, headers=[
                        (b"Set-Cookie", b"JSESSIONID=after; Path=/jwglxt")], content=b"null")
                test.assertIn("JSESSIONID=after", cookie)
                test.assertNotIn("JSESSIONID=before", cookie)
                if method == "POST":
                    body = b"".join([chunk async for chunk in request.stream])
                    test.assertEqual(parse_qs(body.decode())["mm"],
                                     ["synthetic-password", "synthetic-password"])
                    return httpcore.Response(302, headers=[
                        (b"Location", b"/jwglxt/xtgl/login_slogin.html"),
                        (b"Set-Cookie", b"AUTH=established; Path=/jwglxt")])
                test.assertIn("AUTH=established", cookie)
                if path.endswith("login_slogin.html"):
                    return httpcore.Response(302, headers=[
                        (b"Location", b"/jwglxt/xtgl/index_initMenu.html")])
                test.assertTrue(path.endswith("index_initMenu.html"))
                return httpcore.Response(200, content=b"index_initMenu")

            async def aclose(self):
                pass

        with patch("app.netguard.httpcore.AsyncConnectionPool", return_value=Pool()):
            async with self.provider()._session():
                pass
        self.assertEqual(requests, [
            ("GET", "/jwglxt/xtgl/login_slogin.html"),
            ("POST", "/jwglxt/xtgl/login_logoutAccount.html"),
            ("POST", "/jwglxt/xtgl/login_slogin.html"),
            ("GET", "/jwglxt/xtgl/login_slogin.html"),
            ("GET", "/jwglxt/xtgl/index_initMenu.html"),
        ])

    async def test_same_host_http_login_redirects_keep_https_and_secure_cookies(self):
        requests = []
        submitted = False

        def respond(request):
            nonlocal submitted
            requests.append(request)
            self.assertEqual(request.url.scheme, "https")
            self.assertEqual(request.url.host, "school.example")
            if len(requests) == 1:
                return httpx.Response(200, headers={
                    "Set-Cookie": "JSESSIONID=synthetic; Path=/jwglxt; Secure"},
                    text=login_page(duplicate_password=True))
            self.assertIn("JSESSIONID=synthetic", request.headers.get("cookie", ""))
            if request.url.path.endswith("login_logoutAccount.html"):
                return httpx.Response(200, text="null")
            if request.method == "POST" and request.url.path.endswith("login_slogin.html"):
                submitted = True
                return httpx.Response(302, headers={
                    "Location": "http://school.example/jwglxt/xtgl/login_slogin.html?target=menu",
                    "Set-Cookie": "AUTH=established; Path=/jwglxt; Secure"})
            self.assertTrue(submitted)
            self.assertIn("AUTH=established", request.headers.get("cookie", ""))
            if request.url.path.endswith("login_slogin.html"):
                self.assertEqual(request.url.query, b"target=menu")
                return httpx.Response(302, headers={
                    "Location": "http://school.example:80/jwglxt/xtgl/index_initMenu.html?gnmkdm=index"})
            if request.url.path.endswith("index_initMenu.html"):
                self.assertEqual(request.url.query, b"gnmkdm=index")
                self.assertIn(request.url.port, (None, 443))
                return httpx.Response(200, text="index_initMenu")
            if request.url.path.endswith("xskbcx_cxXsKb.html"):
                return httpx.Response(200, json={"kbList": [
                    {"kcmc": "数学", "xqj": "1", "jcs": "1-2", "zcd": "1-16周"}]})
            self.fail(f"Unexpected request: {request.method} {request.url.path}")

        with patch.object(zhengfang, "SafeAsyncHTTPTransport", return_value=httpx.MockTransport(respond)):
            courses = await self.provider().fetch()
        self.assertEqual([course.name for course in courses], ["数学"])
        self.assertEqual(len(requests), 6)

    async def test_cross_host_http_redirect_is_not_upgraded_and_receives_no_cookie(self):
        requests = []

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, headers={
                    "Set-Cookie": "shared=synthetic; Domain=school.example; Path=/"},
                    text=login_page())
            if request.method == "POST":
                self.assertIn("shared=synthetic", request.headers.get("cookie", ""))
                return httpx.Response(302, headers={
                    "Location": "http://other.school.example/jwglxt/xtgl/login_slogin.html"})
            self.assertEqual(request.url.scheme, "http")
            self.assertEqual(request.url.host, "other.school.example")
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, text=login_page())

        with self.assertRaises(ValueError):
            await self.login(respond, base="https://login.school.example/jwglxt")
        self.assertEqual(len(requests), 3)

    async def test_response_hook_only_upgrades_redirects_from_the_expected_standard_https_origin(self):
        cases = (
            ("https://school.example/jwglxt/xtgl/login_slogin.html",
             "http://school.example:8080/jwglxt/xtgl/index_initMenu.html", ("https", "school.example", 443)),
            ("https://school.example:8443/jwglxt/xtgl/login_slogin.html",
             "http://school.example/jwglxt/xtgl/index_initMenu.html", ("https", "school.example", 8443)),
            ("https://other.example/jwglxt/xtgl/login_slogin.html",
             "http://school.example/jwglxt/xtgl/index_initMenu.html", ("https", "school.example", 443)),
            ("http://school.example/jwglxt/xtgl/login_slogin.html",
             "http://school.example/jwglxt/xtgl/index_initMenu.html", ("http", "school.example", 80)),
        )
        for source, target, expected_origin in cases:
            with self.subTest(source=source, target=target):
                response = httpx.Response(302, headers={"Location": target},
                                          request=httpx.Request("POST", source))
                await zhengfang._check_zhengfang_response(response, expected_origin)
                self.assertEqual(response.headers["location"], target)

    async def test_hidden_fields_and_successful_controls_survive_login_submission(self):
        requests = []
        extra = (
            '<input type="hidden" name="csrfTokenLogout" value="logout-synthetic">'
            '<input type="hidden" name="clientType" value="student">'
            '<input type="checkbox" name="remember" value="yes" checked>'
            '<input type="checkbox" name="unchecked" value="no">'
            '<input type="radio" name="mode" value="student" checked>'
            '<input type="radio" name="mode" value="teacher">'
            '<input name="disabledField" value="omit" disabled>'
            '<input type="text" value="unnamed input">'
        )

        def respond(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, text=login_page(extra_fields=extra))
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            if request.url.path.endswith("login_logoutAccount.html"):
                self.assertEqual(form, {"csrfTokenLogout": ["logout-synthetic"]})
                return httpx.Response(200, text="success")
            self.assertEqual(requests[-2].url.path, "/jwglxt/xtgl/login_logoutAccount.html")
            self.assertEqual(form["csrfTokenLogout"], ["logout-synthetic"])
            self.assertEqual(form["clientType"], ["student"])
            self.assertEqual(form["remember"], ["yes"])
            self.assertEqual(form["mode"], ["student"])
            self.assertNotIn("unchecked", form)
            self.assertNotIn("disabledField", form)
            return httpx.Response(200, text="index_initMenu")

        await self.login(respond)
        self.assertEqual(len(requests), 3)

    async def test_missing_legacy_logout_endpoint_does_not_prevent_login(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, text=login_page(duplicate_password=True))
            if request.url.path.endswith("login_logoutAccount.html"):
                return httpx.Response(404)
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            self.assertEqual(form["mm"], ["synthetic-password", "synthetic-password"])
            return httpx.Response(200, text="index_initMenu")

        await self.login(respond)
        self.assertEqual(len(requests), 3)

    async def test_legacy_distinct_password_field_uses_the_same_encrypted_value(self):
        html = login_page(encrypted=True, extra_fields='<input name="hidMm" value="">')

        def respond(request):
            if request.method == "GET":
                if request.url.path.endswith("login_getPublicKey.html"):
                    return httpx.Response(200, json={"modulus": "synthetic", "exponent": "key"})
                return httpx.Response(200, text=html)
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            self.assertEqual(form["mm"], ["encrypted-synthetic-password"])
            self.assertEqual(form["hidMm"], ["encrypted-synthetic-password"])
            return httpx.Response(200, text="index_initMenu")

        with patch.object(zhengfang, "rsa_encrypt", return_value="encrypted-synthetic-password"):
            await self.login(respond)

    async def test_logout_server_error_stops_before_password_submission(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, text=login_page(duplicate_password=True))
            self.assertTrue(request.url.path.endswith("login_logoutAccount.html"))
            return httpx.Response(503)

        with self.assertRaises(httpx.HTTPStatusError) as caught:
            await self.login(respond)
        self.assertEqual(caught.exception.response.status_code, 503)
        self.assertEqual(len(requests), 2)

    async def test_login_http_errors_are_not_reported_as_wrong_password(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                def respond(request):
                    if request.method == "GET":
                        return httpx.Response(200, text=login_page())
                    return httpx.Response(status, text="Service unavailable")

                with self.assertRaises(httpx.HTTPStatusError) as caught:
                    await self.login(respond)
                self.assertEqual(caught.exception.response.status_code, status)
                self.assertNotIn("密码错误", str(caught.exception))

    async def test_unknown_success_status_page_does_not_claim_password_is_wrong(self):
        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, text=login_page())
            return httpx.Response(200, text="<html><h1>系统维护中</h1></html>")

        with self.assertRaises(ValueError) as caught:
            await self.login(respond)
        self.assertNotIsInstance(caught.exception, zhengfang.AuthExpiredError)
        self.assertNotIn("账号或密码错误", str(caught.exception))

    async def test_returned_login_page_without_school_error_is_a_protocol_error(self):
        for redirected in (False, True):
            with self.subTest(redirected=redirected):
                def respond(request):
                    if request.method == "GET" or not redirected:
                        return httpx.Response(200, text=login_page())
                    return httpx.Response(302, headers={"Location": "/jwglxt/xtgl/login_slogin.html"})

                with self.assertRaises(ValueError) as caught:
                    await self.login(respond)
                self.assertNotIsInstance(caught.exception, zhengfang.AuthExpiredError)
                self.assertNotIn("账号或密码错误", str(caught.exception))

    async def test_explicit_school_login_error_is_preserved(self):
        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, text=login_page())
            return httpx.Response(200, text='<p id="tips">用户名或密码不正确</p>')

        with self.assertRaisesRegex(zhengfang.AuthExpiredError, "用户名或密码不正确"):
            await self.login(respond)

    async def test_required_captcha_has_its_own_error(self):
        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, text=login_page())
            return httpx.Response(200, text='<form><input name="yzm"></form>')

        with self.assertRaisesRegex(zhengfang.AuthExpiredError, "验证码"):
            await self.login(respond)


if __name__ == "__main__":
    unittest.main()
