"""学习通离线回归：运行 python -m unittest discover -s tests -p test_chaoxing.py。"""
from __future__ import annotations

import asyncio
import base64
import copy
import html
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, quote
import uuid

# 必须先隔离再导入 app；若已导入到其他目录则立即失败，不能碰真实凭据。
_DATA = tempfile.TemporaryDirectory(prefix="agenda-chaoxing-tests-")
_PREVIOUS_DATA_DIR = os.environ.get("CAMPUS_DATA_DIR")
os.environ["CAMPUS_DATA_DIR"] = _DATA.name

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app import auth, config, db, notifications
from app.main import app
from app.providers import chaoxing
from app.providers.base import AuthExpiredError
from app.user_context import current_user_id

if config.DATA_DIR.resolve() != Path(_DATA.name).resolve():
    raise RuntimeError("app 已使用其他数据目录导入，请单独运行本测试模块")
if db.DB_PATH.resolve().parent != Path(_DATA.name).resolve():
    raise RuntimeError("测试数据库未隔离，拒绝继续")

_ASYNC_CLIENT = httpx.AsyncClient
_LOGIN_KEY = b"u2oh6Vu^HWe4_AES"
_COURSE = {"courseid": "301", "clazzid": "402", "cpi": "503", "name": "软件测试"}
_PROFILE = '<script>var uid="701"; var fid="801";</script><span aria-label="账号：测试同学"></span>'
_COURSES = '''<ul class="course-list"><li>
<a href="/visit/stucoursemiddle?courseid=301&amp;clazzid=402&amp;cpi=503">进入</a>
<span class="course-name" title="软件测试">缩略名称</span></li></ul>'''
_MIDDLE = '''<input id="workEnc" value="work-ticket"><input name="enc" value="student-ticket">
<input id="openc" value="open-ticket">'''


def _decrypt_login(value: str) -> str:
    decryptor = Cipher(algorithms.AES(_LOGIN_KEY), modes.CBC(_LOGIN_KEY)).decryptor()
    padded = decryptor.update(base64.b64decode(value, validate=True)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def _work_page(enc="first", timestamp="11", work_id="601", status="未提交", url=None):
    if url is None:
        url = f"https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew?workId={work_id}&enc={enc}&t={timestamp}"
    return ('<ul id="workList"><li data="' + html.escape(quote(url, safe=""), quote=True)
            + '"><span class="overHidden2">第一章练习</span><span class="status">'
            + status + '</span></li></ul>')


def _cookies(value="synthetic-session"):
    return [{"name": "session", "value": value, "domain": ".chaoxing.com",
             "path": "/", "secure": True, "domain_specified": True, "expires": None}]


def _item(**updates):
    value = {"source_id": "homework:301:402:601", "kind": "homework", "title": "第一章练习",
             "body": "", "course": "软件测试", "status": "pending", "due_at": "2026-12-01T18:30:00+08:00",
             "published_at": "", "url": "https://mooc1.chaoxing.com/work?workId=601&enc=old&t=1"}
    value.update(updates)
    return value


def setUpModule():
    # 所有网络请求必须由 MockTransport/ASGITransport 接管，禁止漏网请求。
    global _NETWORK_GUARD
    _NETWORK_GUARD = patch("socket.socket.connect", side_effect=AssertionError("测试禁止真实网络连接"))
    _NETWORK_GUARD.start()
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO sqlite_sequence(name, seq) SELECT 'users', 1000 "
                     "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='users')")
        conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq, 1000) WHERE name='users'")


def tearDownModule():
    _NETWORK_GUARD.stop()
    _DATA.cleanup()
    if _PREVIOUS_DATA_DIR is None:
        os.environ.pop("CAMPUS_DATA_DIR", None)
    else:
        os.environ["CAMPUS_DATA_DIR"] = _PREVIOUS_DATA_DIR


class ParserTests(unittest.TestCase):
    def test_homework_start_uses_explicit_year_not_deadline(self):
        self.assertEqual(chaoxing.parse_homework_start("作答时间：2026-09-01 08:00 至 2026-12-01 18:30"),
                         "2026-09-01T08:00:00+08:00")
        self.assertEqual(chaoxing.parse_homework_start("发布时间：2026/09/02 09:30"),
                         "2026-09-02T09:30:00+08:00")
        self.assertEqual(chaoxing.parse_homework_start("截止时间：2026-12-01 18:30"), "")
        self.assertEqual(chaoxing.parse_homework_start("作答时间：09-01 08:00 至 12-01 18:30"), "")


    def test_login_aes_uses_16_byte_key_and_roundtrips_utf8_and_padding(self):
        self.assertEqual(len(_LOGIN_KEY), 16)
        for value in ("", "13800138000", "synthetic-password", "中文密码𠮷", "a" * 16, "a" * 47):
            with self.subTest(length=len(value)):
                ciphertext = chaoxing.encrypt_login(value)
                self.assertEqual(len(base64.b64decode(ciphertext)) % 16, 0)
                self.assertEqual(_decrypt_login(ciphertext), value)

    def test_course_cards_deduplicate_and_reject_non_numeric_ids(self):
        duplicate = '<a onclick="open(\'/visit/stucoursemiddle?courseid=301&amp;clazzid=402&amp;cpi=503\')">重复</a>'
        invalid = '<a href="/visit/stucoursemiddle?courseid=nope&amp;clazzid=402&amp;cpi=503">非法</a>'
        self.assertEqual(chaoxing.parse_courses(_COURSES + duplicate + invalid), [_COURSE])

    def test_empty_pages_are_distinct_from_unrecognized_or_login_html(self):
        self.assertEqual(chaoxing.parse_courses('<div class="no-data">暂无课程</div>'), [])
        self.assertEqual(chaoxing.parse_homework('<div id="workList">暂无作业</div>', _COURSE), [])
        for page in ('<form id="loginForm">登录</form>', '<html>维护中</html>'):
            for parser, args in ((chaoxing.parse_courses, ()), (chaoxing.parse_homework, (_COURSE,))):
                with self.subTest(parser=parser.__name__, page=page):
                    with self.assertRaises(chaoxing.ChaoxingError):
                        parser(page, *args)

    def test_container_or_page_title_alone_does_not_prove_empty(self):
        for page in ('<ul class="course-list"><li>课程结构已改变</li></ul>', '<div id="courseList"></div>'):
            with self.subTest(page=page), self.assertRaises(chaoxing.ChaoxingError):
                chaoxing.parse_courses(page)
        for page in ('<title>作业列表</title><li data="x">新字段</li>', '<ul id="workList"></ul>'):
            with self.subTest(page=page), self.assertRaises(chaoxing.ChaoxingError):
                chaoxing.parse_homework(page, _COURSE)

    def test_homework_statuses_and_encoded_url(self):
        statuses = {"未提交": "pending", "待完成": "pending", "进行中": "pending", "已提交": "submitted",
                    "待批阅": "submitted", "已批阅": "graded", "已完成": "graded", "已截止": "expired",
                    "已结束": "expired", "未知状态": "unknown"}
        for label, expected in statuses.items():
            with self.subTest(status=label):
                item = chaoxing.parse_homework(_work_page(status=label), _COURSE)[0]
                self.assertEqual(item["status"], expected)
                self.assertEqual(item["title"], "第一章练习")
                self.assertEqual(item["source_id"], "homework:301:402:601")
                self.assertIn("workId=601&enc=first&t=11", item["url"])

    def test_dynamic_enc_and_timestamp_do_not_change_homework_identity(self):
        for work_id in ("601", ""):
            with self.subTest(work_id=work_id):
                first = chaoxing.parse_homework(_work_page(work_id=work_id), _COURSE)[0]
                second = chaoxing.parse_homework(_work_page("second", "22", work_id), _COURSE)[0]
                self.assertEqual(first["source_id"], second["source_id"])
                self.assertNotEqual(first["url"], second["url"])

    def test_due_date_and_html_sanitization(self):
        self.assertEqual(chaoxing.parse_due("<b>截止时间：</b>2026/12/01 18:30"), "2026-12-01T18:30:00+08:00")
        self.assertEqual(chaoxing.parse_due("截止时间：2026-02-30 18:30"), "")
        self.assertEqual(chaoxing.parse_due("尚未公布截止时间"), "")
        self.assertEqual(chaoxing.plain("<style>隐藏</style><script>恶意</script><b>正文</b>"), "正文")

    def test_malicious_urls_are_rejected_including_homework_links(self):
        bad_urls = ("http://mooc1.chaoxing.com/work", "https://chaoxing.com.attacker.example/work",
                    "https://attacker.example/chaoxing.com", "https://evilchaoxing.com/work",
                    "//attacker.example/work", "javascript:alert(1)", "file:///etc/passwd",
                    "https://127.0.0.1/work", "https://169.254.169.254/latest/meta-data/",
                    "https://user:secret@mooc1.chaoxing.com/work", "https://mooc1.chaoxing.com:444/work")
        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(chaoxing.ChaoxingError):
                    chaoxing.safe_url(url)
                with self.assertRaises(chaoxing.ChaoxingError):
                    chaoxing.parse_homework(_work_page(url=url), _COURSE)
        self.assertEqual(chaoxing.safe_url("/base"), chaoxing.BASE)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, handler, settings=None):
        holder = {}

        def respond(request):
            # 仅跳过真实节流等待，认证、解析和重定向仍走原实现。
            holder["provider"]._last_request = 0
            return handler(request)

        def factory(**kwargs):
            return _ASYNC_CLIENT(transport=httpx.MockTransport(respond), **kwargs)

        with patch.object(chaoxing.httpx, "AsyncClient", side_effect=factory):
            client = chaoxing.ChaoxingClient(settings or {})
        holder["provider"] = client
        self.addAsyncCleanup(client.client.aclose)
        return client

    async def test_full_login_profile_courses_homework_detail_and_notices_chain(self):
        requests = []
        payloads = []
        username, password = "13800138000", "synthetic-password-中文"

        def handler(request):
            requests.append(request)
            if str(request.url) == chaoxing.LOGIN:
                self.assertEqual(request.method, "POST")
                self.assertNotIn("cookie", request.headers)
                payload = parse_qs(request.content.decode())
                payloads.append(payload)
                self.assertEqual(_decrypt_login(payload["uname"][0]), username)
                self.assertEqual(_decrypt_login(payload["password"][0]), password)
                self.assertNotIn(username, request.content.decode())
                self.assertNotIn(password, request.content.decode())
                return httpx.Response(200, json={"status": True}, headers={
                    "set-cookie": "session=mock-login-ticket; Domain=.chaoxing.com; Path=/; Secure"})
            self.assertIn("session=mock-login-ticket", request.headers.get("cookie", ""))
            if request.url.path == "/base":
                return httpx.Response(200, text=_PROFILE)
            if request.url.path == "/mooc2-ans/visit/courselistdata":
                self.assertEqual(request.url.params["courseFid"], "801")
                return httpx.Response(200, text=_COURSES)
            if request.url.path == "/visit/stucoursemiddle":
                for key in ("courseid", "clazzid", "cpi"):
                    self.assertEqual(request.url.params[key], _COURSE[key])
                return httpx.Response(200, text=_MIDDLE)
            if request.url.path == "/mooc2/work/list":
                expected = {"courseId": "301", "classId": "402", "cpi": "503", "ut": "s",
                            "enc": "work-ticket", "stuenc": "student-ticket", "openc": "open-ticket"}
                for key, value in expected.items():
                    self.assertEqual(request.url.params[key], value)
                self.assertRegex(request.url.params["t"], r"^\d{13,}$")
                return httpx.Response(200, text=_work_page())
            if request.url.path == "/mooc-ans/work/doHomeWorkNew":
                self.assertEqual(request.url.params["workId"], "601")
                return httpx.Response(200, text="截止时间：2026-12-01 18:30")
            if str(request.url) == chaoxing.NOTICES:
                self.assertEqual(request.method, "POST")
                self.assertEqual(parse_qs(request.content.decode())["type"], ["0"])
                return httpx.Response(200, json={"status": True, "notices": {"list": [{
                    "id": "901", "title": "<b>课程通知</b>", "content": "<script>隐藏</script>请预习",
                    "createrName": "测试教师", "insertTime": "2026-11-23 12:00"}], "lastPage": True}})
            self.fail(f"未预期的模拟请求：{request.method} {request.url.path}")

        client = await self.make_client(handler, {"cookies": _cookies("old-account-ticket")})
        account = await client.login(username, password)
        self.assertEqual(account, {"uid": "701", "name": "测试同学"})
        result = await client.fetch_items()
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len(result["items"]), 2)
        work = next(i for i in result["items"] if i["kind"] == "homework")
        notice = next(i for i in result["items"] if i["kind"] == "notice")
        self.assertEqual(work["due_at"], "2026-12-01T18:30:00+08:00")
        self.assertEqual(notice["source_id"], "notice:901")
        self.assertEqual((notice["title"], notice["body"]), ("课程通知", "请预习"))
        self.assertEqual(len(requests), 8)
        self.assertEqual(len(payloads), 1)
        self.assertEqual({c["value"] for c in client.export_cookies()}, {"mock-login-ticket"})

    async def test_sms_requires_true_results_and_rejects_two_factor(self):
        for payload in ({"status": True, "containTwoFactorLogin": True}, {"status": 1}, [], {"status": False}):
            client = await self.make_client(lambda request: httpx.Response(200, json=payload))
            with self.assertRaises(chaoxing.ChaoxingError):
                await client.login_sms("13800138000", "123456")
        for result in ({"result": False}, {"result": "true"}, {"result": 1}, []):
            def handler(request):
                return httpx.Response(200, json=result) if request.url.path == "/num/phonecode" else httpx.Response(200, text="false")
            client = await self.make_client(handler)
            with self.assertRaises(chaoxing.ChaoxingError):
                await client.send_sms("13800138000", "human-token")
        seen = []
        def limited(request):
            seen.append(request.url.path)
            return httpx.Response(200, text="alert('sensitive-upstream')")
        client = await self.make_client(limited)
        with self.assertRaises(chaoxing.ChaoxingError) as error:
            await client.send_sms("13800138000", "human-token")
        self.assertNotIn("sensitive-upstream", str(error.exception))
        self.assertEqual(seen, ["/login", "/num/booleanCode"])

    async def test_sms_profile_retry_keeps_updated_cookie_without_resubmitting_code(self):
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/fanyaloginbycode":
                return httpx.Response(200, json={"status": True}, headers={
                    "set-cookie": "session=accepted; Domain=.chaoxing.com; Path=/; Secure"})
            self.assertEqual(request.url.path, "/base")
            if paths.count("/base") == 1:
                return httpx.Response(200, text="session preparing", headers={
                    "set-cookie": "session=ready; Domain=.chaoxing.com; Path=/; Secure"})
            self.assertIn("session=ready", request.headers.get("cookie", ""))
            return httpx.Response(200, text=_PROFILE)
        client = await self.make_client(handler)
        account = await client.login_sms("13800138000", "123456")
        self.assertEqual(account["uid"], "701")
        self.assertEqual(paths, ["/fanyaloginbycode", "/base", "/base"])

    async def test_sms_profile_retry_is_bounded_and_classified(self):
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return (httpx.Response(200, json={"status": True})
                    if request.url.path == "/fanyaloginbycode" else httpx.Response(503))
        client = await self.make_client(handler)
        with self.assertRaises(chaoxing.SmsLoginError) as caught:
            await client.login_sms("13800138000", "123456")
        self.assertEqual(caught.exception.reason, "profile")
        self.assertEqual(paths, ["/fanyaloginbycode", "/base", "/base"])

    async def test_sms_rejection_does_not_retry_or_echo_upstream(self):
        cases = [({"status": False, "msg": "验证码错误 private-sensitive"}, "code"),
                 ({"status": False, "msg": "未知 private-sensitive"}, "rejected"),
                 ({"status": True, "containTwoFactorLogin": True}, "verification"),
                 ([], "response")]
        for payload, reason in cases:
            seen = []
            def handler(request):
                seen.append(request.url.path)
                return httpx.Response(200, json=payload)
            client = await self.make_client(handler)
            with self.assertRaises(chaoxing.SmsLoginError) as caught:
                await client.login_sms("13800138000", "123456")
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("private-sensitive", str(caught.exception))
            self.assertEqual(seen, ["/fanyaloginbycode"])

    async def test_expired_internal_budget_preserves_collected_notice(self):
        clock = [0.0]
        def handler(request):
            if request.url.path == "/base":
                return httpx.Response(200, text=_PROFILE)
            if request.url.path == "/pc/notice/getNoticeList":
                clock[0] = 181.0
                return httpx.Response(200, json={"status": True, "notices": {"list": [{"id": "one", "title": "已读到通知"}], "lastPage": True}})
            self.fail("预算耗尽后不应再发起请求")
        client = await self.make_client(handler, {"uid": "701"})
        with patch.object(chaoxing, "budget_clock", side_effect=lambda: clock[0]):
            result = await client.fetch_items()
        self.assertEqual(result["items"][0]["source_id"], "notice:one")
        self.assertTrue(result["warnings"])

    async def test_login_failure_is_generic_and_never_reuses_old_account(self):
        seen = []

        def handler(request):
            seen.append(request)
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, json={"status": False, "msg": "sensitive-account-password-ticket"})

        client = await self.make_client(handler, {"cookies": _cookies("old-account")})
        with self.assertRaises(chaoxing.ChaoxingError) as error:
            await client.login("synthetic-user", "synthetic-password")
        self.assertNotIn("sensitive-account-password-ticket", str(error.exception))
        self.assertEqual(len(seen), 1)
        self.assertEqual(client.export_cookies(), [])

    async def test_login_pages_and_expired_statuses_are_not_empty_successes(self):
        for code, body in ((200, '<form id="loginForm">登录</form>'),
                           (200, '<a href="https://passport2.chaoxing.com/login">登录</a>'),
                           (401, ""), (403, "")):
            with self.subTest(status=code, body=body):
                client = await self.make_client(lambda request: httpx.Response(code, text=body), {"uid": "701"})
                with self.assertRaises(AuthExpiredError):
                    await client.fetch_items()

    async def test_login_html_at_each_fetch_stage_aborts_instead_of_succeeding_empty(self):
        stages = ("/mooc2-ans/visit/courselistdata", "/visit/stucoursemiddle", "/mooc2/work/list",
                  "/mooc-ans/work/doHomeWorkNew", "/pc/notice/getNoticeList")
        for stage in stages:
            with self.subTest(stage=stage):
                def handler(request):
                    if request.url.path == stage:
                        return httpx.Response(200, text='<form id="loginForm">登录</form>')
                    pages = {"/base": _PROFILE, "/mooc2-ans/visit/courselistdata": _COURSES,
                             "/visit/stucoursemiddle": _MIDDLE, "/mooc2/work/list": _work_page(),
                             "/mooc-ans/work/doHomeWorkNew": "截止时间：2026-12-01 18:30"}
                    if request.url.path in pages:
                        return httpx.Response(200, text=pages[request.url.path])
                    if request.url.path == "/pc/notice/getNoticeList":
                        return httpx.Response(200, json={"status": True, "notices": {"list": [], "lastPage": True}})
                    self.fail(f"未预期请求：{request.url.path}")

                client = await self.make_client(handler, {"uid": "701"})
                with self.assertRaises(AuthExpiredError):
                    await client.fetch_items()

    async def test_expired_redirect_stops_before_sending_cookies_to_passport(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "https://passport2.chaoxing.com/login"})

        client = await self.make_client(handler, {"cookies": _cookies("private-ticket")})
        with self.assertRaises(AuthExpiredError):
            await client._request("GET", chaoxing.BASE)
        self.assertEqual(len(seen), 1)

    async def test_identity_change_stops_before_fetching_courses_or_notices(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text=_PROFILE)

        client = await self.make_client(handler, {"uid": "different-account"})
        with self.assertRaises(AuthExpiredError):
            await client.fetch_items()
        self.assertEqual(len(seen), 1)

    async def test_external_redirect_is_rejected_before_any_cookie_can_leave(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "https://attacker.example/collect"})

        client = await self.make_client(handler, {"cookies": _cookies("private-ticket")})
        with self.assertRaises(chaoxing.ChaoxingError):
            await client._request("GET", chaoxing.BASE)
        self.assertEqual([r.url.host for r in seen], ["i.chaoxing.com"])
        self.assertIn("private-ticket", seen[0].headers.get("cookie", ""))

    async def test_allowed_cross_host_redirect_preserves_cookie_domain_scope(self):
        seen = []

        def handler(request):
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(302, headers={"location": "https://notice.chaoxing.com/target"})
            return httpx.Response(200, text="ok")

        scoped = _cookies("host-only-ticket")
        scoped[0].update(domain="i.chaoxing.com", domain_specified=False)
        client = await self.make_client(handler, {"cookies": scoped})
        await client._request("GET", chaoxing.BASE)
        self.assertIn("host-only-ticket", seen[0].headers.get("cookie", ""))
        self.assertNotIn("cookie", seen[1].headers)

    async def test_login_redirect_never_reposts_credentials(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(307, headers={"location": "https://i.chaoxing.com/base"})

        client = await self.make_client(handler)
        with self.assertRaises(chaoxing.ChaoxingError):
            await client.login("synthetic-user", "synthetic-password")
        self.assertEqual(len(seen), 1)

    async def test_notice_pagination_stops_on_repeated_cursor_and_cleans_html(self):
        cursors = []

        def handler(request):
            cursors.append(parse_qs(request.content.decode(), keep_blank_values=True)["lastValue"][0])
            return httpx.Response(200, json={"status": 1, "notices": {"list": [{
                "noticeId": str(len(cursors)), "title": "<b>通知</b>", "content": "正文<style>隐藏</style>"}],
                "lastGetId": "same-cursor", "lastPage": False}})

        client = await self.make_client(handler)
        items = await client._notices()
        self.assertEqual(cursors, ["", "same-cursor"])
        self.assertEqual([item["source_id"] for item in items], ["notice:1", "notice:2"])
        self.assertEqual(items[0]["body"], "正文")

    async def test_notice_page_limit_preserves_collected_items_and_reports_partial_scan(self):
        cursors = []

        def handler(request):
            if request.url.path == "/base":
                return httpx.Response(200, text=_PROFILE)
            if str(request.url) == chaoxing.NOTICES:
                cursors.append(parse_qs(request.content.decode(), keep_blank_values=True)["lastValue"][0])
                page = len(cursors)
                return httpx.Response(200, json={"status": True, "notices": {
                    "list": [{"id": str(page), "title": f"第 {page} 页通知"}],
                    "lastGetId": f"cursor-{page}", "lastPage": False}})
            if request.url.path == "/mooc2-ans/visit/courselistdata":
                return httpx.Response(200, text='<div class="no-data">暂无课程</div>')
            self.fail(f"未预期请求：{request.url.path}")

        client = await self.make_client(handler, {"uid": "701"})
        result = await client.fetch_items()
        self.assertEqual(cursors, ["", "cursor-1", "cursor-2", "cursor-3", "cursor-4"])
        self.assertEqual([item["source_id"] for item in result["items"]],
                         [f"notice:{page}" for page in range(1, 6)])
        self.assertTrue(any("5 页" in warning for warning in result["warnings"]))

    async def test_untrusted_persisted_cookies_are_not_imported_or_exported(self):
        client = await self.make_client(lambda request: httpx.Response(200, text="ok"), {"cookies": [
            *_cookies("trusted-ticket"), {"name": "foreign", "value": "foreign-ticket", "domain": ".attacker.example"},
            {"name": "lookalike", "value": "lookalike-ticket", "domain": "evilchaoxing.com"}, None]})
        self.assertEqual({cookie["value"] for cookie in client.export_cookies()}, {"trusted-ticket"})

    async def test_notice_malformed_responses_never_mean_no_notices(self):
        payloads = ([], {"status": False}, {"status": True, "notices": []},
                    {"status": True, "notices": {"list": {}}})
        for payload in payloads:
            with self.subTest(payload=payload):
                client = await self.make_client(lambda request: httpx.Response(200, json=payload))
                with self.assertRaises(chaoxing.ChaoxingError):
                    await client._notices()
        client = await self.make_client(lambda request: httpx.Response(200, text="not-json"))
        with self.assertRaises(chaoxing.ChaoxingError):
            await client._notices()

    async def test_network_failure_does_not_expose_sensitive_request_url(self):
        def handler(request):
            raise httpx.ConnectError("sensitive-ticket-from-upstream", request=request)

        client = await self.make_client(handler)
        with self.assertRaises(chaoxing.ChaoxingError) as error:
            await client._request("GET", chaoxing.BASE + "?secret=sensitive-ticket")
        self.assertNotIn("sensitive-ticket", str(error.exception))

    async def test_partial_course_failure_reports_warning_and_preserves_other_items(self):
        def handler(request):
            if request.url.path == "/base":
                return httpx.Response(200, text=_PROFILE)
            if request.url.path == "/mooc2-ans/visit/courselistdata":
                return httpx.Response(200, text=_COURSES)
            if request.url.path == "/visit/stucoursemiddle":
                return httpx.Response(200, text="课程入口结构已变化")
            if str(request.url) == chaoxing.NOTICES:
                return httpx.Response(200, json={"status": True, "notices": {"list": [], "lastPage": True}})
            self.fail(f"未预期请求：{request.url.path}")

        client = await self.make_client(handler)
        result = await client.fetch_items()
        self.assertEqual(result["items"], [])
        self.assertTrue(any("软件测试" in warning for warning in result["warnings"]))


class _DatabaseMixin:
    def setUp(self):
        super().setUp()
        self.users = []
        self.context = current_user_id.set(None)
        self.addCleanup(current_user_id.reset, self.context)
        self.addCleanup(self.cleanup_users)

    def cleanup_users(self):
        for user_id in self.users:
            db.delete_user(user_id)

    def new_user(self):
        user_id = db.create_user("cx-test-" + uuid.uuid4().hex[:20], auth.hash_password("local-test-password"))
        self.assertGreater(user_id, 1000)
        self.users.append(user_id)
        self.set_term(user_id, "2026-08-31")
        return user_id

    def set_term(self, user_id, start):
        cfg = db.get_user_config(user_id)
        cfg["semester_start"] = start
        db.save_user_config(user_id, cfg)

    def bind(self, user_id, uid="701", value="synthetic-session"):
        self.assertTrue(db.set_chaoxing_binding(user_id, {"username": "synthetic-account", "uid": uid,
                                                       "name": "测试同学", "cookies": _cookies(value)}))

    def items(self, user_id, **options):
        return notifications.list_items(user_id, options.get("kind", "all"), options.get("unread", False), 100)

    def fake_client(self, *, items=None, error=None, uid="701", cookie_value="synthetic-session", warnings=None):
        fetch = AsyncMock(return_value={"items": copy.deepcopy(items or []), "warnings": warnings or []}, side_effect=error)
        login = AsyncMock(return_value={"uid": uid, "name": "测试同学"})
        exported_cookies = _cookies(cookie_value)

        class FakeChaoxingClient:
            def __init__(self, settings):
                self.config = copy.deepcopy(settings)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            fetch_items = fetch

            async def login(self, username, password):
                return await login(username, password)

            def export_cookies(self):
                return copy.deepcopy(exported_cookies)

        return FakeChaoxingClient, fetch, login

    async def sync_items(self, user_id, items, **kwargs):
        fake, _, _ = self.fake_client(items=items, **kwargs)
        with patch.object(chaoxing, "ChaoxingClient", fake):
            return await notifications.sync(user_id)


class DomainTests(_DatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.push = AsyncMock(return_value=[{"channel": "bark", "ok": True}])
        patcher = patch.object(notifications, "push_all", self.push)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = self.new_user()
        self.bind(self.user)
        cfg = db.get_user_config(self.user)
        cfg["push"] = {"bark_enabled": True, "bark_key": "synthetic-bark-key", "wecom_enabled": False}
        db.save_user_config(self.user, cfg)
        await notifications.set_options(self.user, {"push_enabled": True})

    def outbox(self, user_id=None):
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM notification_outbox WHERE user_id=? ORDER BY id",
                (self.user if user_id is None else user_id,))]

    def expire_outbox(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE notification_outbox SET next_attempt=? WHERE user_id=? AND status='pending'",
                         ("2000-01-01T00:00:00+08:00", self.user))

    async def test_first_sync_and_unchanged_second_sync_do_not_push(self):
        first = await self.sync_items(self.user, [_item()])
        self.assertEqual({k: first[k] for k in ("ok", "count", "new_count")}, {"ok": True, "count": 1, "new_count": 1})
        notification = self.items(self.user)["items"][0]
        db.mark_notifications_read(self.user, [notification["id"]], False)
        second = await self.sync_items(self.user, [_item()])
        self.assertEqual({k: second[k] for k in ("ok", "count", "new_count")}, {"ok": True, "count": 1, "new_count": 0})
        self.assertTrue(self.items(self.user)["items"][0]["is_read"])
        self.push.assert_not_awaited()

    async def test_partial_first_scans_defer_baseline_until_complete_without_pushing_history(self):
        warning = "课程列表未完整读取，保留上次作业数据"
        first = await self.sync_items(self.user, [_item()], warnings=[warning])
        self.assertTrue(first["ok"])
        self.assertFalse(notifications.status(self.user)["baseline_complete"])
        original = self.items(self.user)["items"][0]
        db.mark_notifications_read(self.user, [original["id"]], False)
        history = _item(source_id="homework:301:402:602", title="恢复的历史作业")
        await self.sync_items(self.user, [_item(), history], warnings=[warning])
        self.assertFalse(notifications.status(self.user)["baseline_complete"])
        recovered = await self.sync_items(self.user, [_item(), history, _item(
            source_id="homework:301:402:603", title="补齐的历史作业")])
        self.assertEqual(recovered["new_count"], 1)
        self.assertTrue(notifications.status(self.user)["baseline_complete"])
        self.assertTrue(next(item for item in self.items(self.user)["items"]
                             if item["id"] == original["id"])["is_read"])
        self.assertEqual(self.outbox(), [])
        self.push.assert_not_awaited()

        fresh = _item(source_id="homework:301:402:604", title="基线后的新增作业")
        result = await self.sync_items(self.user, [fresh], warnings=[warning])
        self.assertEqual(result["new_count"], 1)
        self.assertTrue(notifications.status(self.user)["baseline_complete"])
        self.push.assert_awaited_once()
        self.assertIn(fresh["title"], self.push.await_args.args[2])
        self.assertNotIn(history["title"], self.push.await_args.args[2])
        self.assertEqual([row["status"] for row in self.outbox()], ["sent"])

    async def test_partial_warning_is_independent_of_error_and_clears_on_complete_scan(self):
        await self.sync_items(self.user, [_item()])
        fake, _, _ = self.fake_client(error=chaoxing.ChaoxingError("private-upstream-detail"))
        with patch.object(chaoxing, "ChaoxingClient", fake):
            await notifications.sync(self.user)
        self.assertEqual(notifications.status(self.user)["state"], "error")

        warning = "课程通知未完整读取，保留上次通知数据"
        result = await self.sync_items(self.user, [_item()], warnings=[warning])
        self.assertTrue(result["ok"])
        for state in (notifications.status(self.user), self.items(self.user)):
            self.assertEqual(state["state"], "partial")
            self.assertEqual(state["error"], "")
            self.assertEqual(state["warning"], warning)
        meta = db.get_chaoxing_status_data(self.user)[1]
        self.assertEqual(meta["failures"], 0)
        self.assertTrue(meta["baseline_complete"])
        await self.sync_items(self.user, [_item()])
        self.assertEqual(notifications.status(self.user)["state"], "ready")
        self.assertEqual(self.items(self.user)["warning"], "")
        self.push.assert_not_awaited()

    async def test_failed_push_is_persisted_and_scheduled_unchanged_scan_retries(self):
        await self.sync_items(self.user, [_item()])
        self.push.return_value = [{"channel": "bark", "ok": False, "error": "private-webhook-ticket"}]
        result = await self.sync_items(self.user, [_item(title="第一章练习（更新）")])
        self.assertTrue(result["ok"])
        pending = self.outbox()[0]
        self.assertEqual((pending["status"], pending["attempt_count"]), ("pending", 1))
        self.assertEqual(json.loads(pending["payload"])["status"], "pending")
        self.assertEqual(json.loads(pending["payload"])["title"], "第一章练习（更新）")
        self.assertEqual(json.loads(pending["delivered_channels"]), [])
        self.assertNotIn("private-webhook-ticket", json.dumps(pending))
        self.assertNotIn("private-webhook-ticket", json.dumps(result))
        self.assertEqual(notifications.status(self.user)["state"], "partial")
        self.assertEqual(notifications.status(self.user)["error"], "")

        fake, fetch, _ = self.fake_client(items=[_item(title="第一章练习（更新）")])
        with patch.object(chaoxing, "ChaoxingClient", fake):
            self.assertIsNone(await notifications.sync(self.user, scheduled=True))
        fetch.assert_not_awaited()
        self.push.assert_awaited_once()
        self.expire_outbox()
        with db.get_conn() as conn:
            conn.execute("UPDATE chaoxing_sync SET last_attempt=? WHERE user_id=?",
                         ("2000-01-01T00:00:00+08:00", self.user))
        self.push.return_value = [{"channel": "bark", "ok": True}]
        with patch.object(chaoxing, "ChaoxingClient", fake):
            result = await notifications.sync(self.user, scheduled=True)
        fetch.assert_awaited_once()
        self.assertEqual(result["new_count"], 0)
        sent = self.outbox()[0]
        self.assertEqual((sent["id"], sent["status"], sent["attempt_count"]),
                         (pending["id"], "sent", 2))
        self.assertEqual(json.loads(sent["delivered_channels"]), ["bark"])
        self.assertEqual(self.push.await_count, 2)
        self.assertEqual(notifications.status(self.user)["warning"], "")
        self.assertEqual(notifications.status(self.user)["state"], "ready")
        self.assertIsNone(current_user_id.get())

    async def test_scheduled_retry_skips_delivered_channel_even_without_new_fetch(self):
        cfg = db.get_user_config(self.user)
        cfg["push"].update(wecom_enabled=True, wecom_webhook="synthetic-webhook")
        db.save_user_config(self.user, cfg)
        await self.sync_items(self.user, [_item()])
        self.push.return_value = [{"channel": "bark", "ok": True}, {"channel": "wecom", "ok": False}]
        await self.sync_items(self.user, [_item(title="第一章练习（更新）")])
        pending = self.outbox()[0]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(json.loads(pending["delivered_channels"]), ["bark"])
        self.expire_outbox()
        self.push.return_value = [{"channel": "wecom", "ok": True}]
        fake, fetch, _ = self.fake_client()
        with patch.object(chaoxing, "ChaoxingClient", fake):
            self.assertIsNone(await notifications.sync(self.user, scheduled=True))
            self.assertIsNone(await notifications.sync(self.user, scheduled=True))
        fetch.assert_not_awaited()
        self.assertEqual(self.push.await_count, 2)
        first_cfg = self.push.await_args_list[0].args[0]["push"]
        retry_cfg = self.push.await_args_list[1].args[0]["push"]
        self.assertTrue(first_cfg["bark_enabled"])
        self.assertTrue(first_cfg["wecom_enabled"])
        self.assertFalse(retry_cfg["bark_enabled"])
        self.assertTrue(retry_cfg["wecom_enabled"])
        sent = self.outbox()[0]
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(json.loads(sent["delivered_channels"]), ["bark", "wecom"])
        self.assertEqual(notifications.status(self.user)["warning"], "")

    async def test_disabling_push_or_sync_clears_pending_without_replaying_disabled_changes(self):
        await self.sync_items(self.user, [_item()])
        for option in ("push_enabled", "enabled"):
            with self.subTest(option=option):
                self.push.reset_mock()
                self.push.return_value = [{"channel": "bark", "ok": False}]
                pending_item = _item(title=f"关闭 {option} 前的更新")
                await self.sync_items(self.user, [pending_item])
                self.assertEqual(self.outbox()[0]["status"], "pending")
                await notifications.set_options(self.user, {option: False})
                self.assertEqual(self.outbox(), [])
                self.assertEqual(notifications.status(self.user)["warning"], "")
                self.push.reset_mock()
                disabled_item = _item(title=f"关闭 {option} 期间的更新")
                await self.sync_items(self.user, [disabled_item])
                self.assertEqual(self.items(self.user)["items"][0]["title"], disabled_item["title"])
                self.assertEqual(self.outbox(), [])
                self.push.assert_not_awaited()
                await notifications.set_options(self.user, {option: True})
                await self.sync_items(self.user, [disabled_item])
                self.assertEqual(self.outbox(), [])
                self.push.assert_not_awaited()
                self.push.return_value = [{"channel": "bark", "ok": True}]
                await self.sync_items(self.user, [_item(title=f"重新开启 {option} 后的更新")])
                self.push.assert_awaited_once()
                self.assertEqual(self.outbox()[0]["status"], "sent")

    async def test_missing_homework_detail_preserves_due_date_and_read_state(self):
        await self.sync_items(self.user, [_item()])
        original = self.items(self.user)["items"][0]
        db.mark_notifications_read(self.user, [original["id"]], False)
        for missing in ("", None):
            with self.subTest(due_at=missing):
                result = await self.sync_items(self.user, [_item(due_at=missing, status="unknown")],
                                               warnings=["部分作业截止时间暂未读取"])
                updated = self.items(self.user)["items"][0]
                self.assertEqual(result["new_count"], 0)
                self.assertEqual(updated["id"], original["id"])
                self.assertEqual(updated["due_at"], original["due_at"])
                self.assertEqual(updated["status"], "pending")
                self.assertTrue(updated["is_read"])
        self.push.assert_not_awaited()
        self.assertEqual(self.outbox(), [])
        await self.sync_items(self.user, [_item(due_at="", status="submitted")])
        self.assertEqual(self.items(self.user)["items"], [])
        with db.get_conn() as conn:
            stored = conn.execute("SELECT data, is_read FROM notifications WHERE id=?", (original["id"],)).fetchone()
        self.assertEqual(json.loads(stored["data"])["due_at"], original["due_at"])
        self.assertEqual(json.loads(stored["data"])["status"], "submitted")
        self.assertTrue(stored["is_read"])
        self.push.assert_not_awaited()

    async def test_notice_content_update_reuses_identity_resets_read_and_pushes_once(self):
        notice = _item(kind="notice", source_id="notice:901", title="课程通知", body="原始正文",
                       status="", due_at="")
        await self.sync_items(self.user, [notice])
        original = self.items(self.user)["items"][0]
        db.mark_notifications_read(self.user, [original["id"]], False)
        updated_notice = dict(notice, body="更新后的正文")
        result = await self.sync_items(self.user, [updated_notice])
        self.assertEqual(result["new_count"], 0)
        rows = self.items(self.user)["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], original["id"])
        self.assertEqual(rows[0]["body"], updated_notice["body"])
        self.assertFalse(rows[0]["is_read"])
        self.push.assert_awaited_once()
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(json.loads(self.outbox()[0]["payload"])["body"], updated_notice["body"])
        db.mark_notifications_read(self.user, [original["id"]], False)
        await self.sync_items(self.user, [updated_notice, updated_notice])
        self.assertTrue(self.items(self.user)["items"][0]["is_read"])
        self.push.assert_awaited_once()
        self.assertEqual(len(self.outbox()), 1)

    async def test_homework_change_resets_read_and_pushes_once_with_user_config(self):
        await self.sync_items(self.user, [_item()])
        original = self.items(self.user)["items"][0]
        db.mark_notifications_read(self.user, [original["id"]], False)
        result = await self.sync_items(self.user, [_item(title="第一章练习（更新）")])
        updated = self.items(self.user)["items"][0]
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(updated["id"], original["id"])
        self.assertFalse(updated["is_read"])
        self.assertEqual(updated["status"], "pending")
        self.assertEqual(updated["title"], "第一章练习（更新）")
        self.assertEqual(self.items(self.user)["pending_count"], 1)
        self.push.assert_awaited_once()
        self.assertEqual(self.push.await_args.args[0]["chaoxing"]["uid"], "701")
        self.assertIn("第一章练习", self.push.await_args.args[2])
        self.assertIsNone(current_user_id.get())

    async def test_dynamic_url_refresh_preserves_read_and_does_not_push(self):
        await self.sync_items(self.user, [_item()])
        original = self.items(self.user)["items"][0]
        db.mark_notifications_read(self.user, [original["id"]], False)
        url = "https://mooc1.chaoxing.com/work?workId=601&enc=new&t=2"
        result = await self.sync_items(self.user, [_item(url=url)])
        updated = self.items(self.user)["items"][0]
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(updated["url"], url)
        self.assertTrue(updated["is_read"])
        self.push.assert_not_awaited()

    async def test_duplicate_source_items_in_one_scan_keep_last_version(self):
        result = await self.sync_items(self.user, [_item(), _item(title="第一章练习（更新）")])
        self.assertEqual({k: result[k] for k in ("ok", "count", "new_count")}, {"ok": True, "count": 1, "new_count": 1})
        self.assertEqual([item["title"] for item in self.items(self.user)["items"]], ["第一章练习（更新）"])
        self.assertEqual(self.items(self.user)["items"][0]["status"], "pending")

    async def test_homework_order_uses_deadline_before_limit_and_keeps_notices(self):
        items = [
            _item(source_id="near", due_at="2026-11-25T09:00:00+08:00"),
            _item(source_id="far", due_at="2026-11-27T09:00:00+08:00"),
            _item(source_id="middle", due_at="2026-11-25T03:00:00+00:00"),
            _item(source_id="unknown", due_at="", published_at="2026-09-01"),
            _item(source_id="invalid", due_at="invalid-date", published_at="2026-09-01"),
            _item(kind="notice", source_id="notice:1", due_at="", status=""),
            _item(kind="notice", source_id="notice:2", due_at="", status=""),
        ]
        # 标题仅用于核对返回顺序；source_id 不在公开列表字段中。
        items = [dict(item, title=item["source_id"]) for item in items]
        await self.sync_items(self.user, items)
        for unread in (False, True):
            result = db.list_notifications(self.user, "all", unread, 100)
            self.assertEqual([i["title"] for i in result["items"]], ["near", "middle", "far", "invalid", "unknown", "notice:2", "notice:1"])
            limited = db.list_notifications(self.user, "homework", unread, 2)
            self.assertEqual([i["title"] for i in limited["items"]], ["near", "middle"])
        notice_only = db.list_notifications(self.user, "notice", False, 100)
        self.assertEqual([i["title"] for i in notice_only["items"]], ["notice:2", "notice:1"])

    async def test_term_start_filters_dates_not_course_names_and_preserves_notices(self):
        await self.sync_items(self.user, [
            _item(source_id="current", title="本学期", course="课表外网课", published_at="2026-08-31T00:00:00+08:00"),
            _item(source_id="past", title="旧作业", published_at="2026-08-30T23:59:59+08:00"),
            _item(source_id="unknown", due_at="", published_at=""),
            _item(kind="notice", source_id="notice", title="课程通知", published_at="2025-01-01", status=""),
        ])
        result = db.list_notifications(self.user, "all", False, 100)
        self.assertEqual([i["title"] for i in result["items"]], ["本学期", "课程通知"])
        self.assertEqual(result["pending_count"], 1)
        self.set_term(self.user, "2027-02-22")
        result = db.list_notifications(self.user, "all", False, 1)
        self.assertEqual([i["title"] for i in result["items"]], ["课程通知"])
        self.assertEqual((result["pending_count"], result["unread_count"]), (0, 1))

    async def test_new_term_works_after_start_changes_and_keeps_known_start(self):
        await self.sync_items(self.user, [_item(published_at="2026-09-01", due_at="2027-03-01T18:00:00+08:00")])
        self.set_term(self.user, "2027-02-22")
        await self.sync_items(self.user, [_item(published_at="", due_at="2027-03-01T18:00:00+08:00"),
                                          _item(source_id="spring", title="春季作业", course="任何课程", published_at="2027-02-22", due_at="")])
        result = self.items(self.user)
        self.assertEqual([i["title"] for i in result["items"]], ["春季作业"])
        self.assertEqual(result["pending_count"], 1)
        self.assertTrue(db._homework_in_term({"published_at": "2026-08-30T16:00:00+00:00"}, db._dated("2026-08-31")))

    async def test_term_date_fallback_and_user_isolation(self):
        await self.sync_items(self.user, [_item()])
        other = self.new_user()
        self.set_term(other, "2027-02-22")
        self.assertEqual(self.items(self.user)["pending_count"], 1)
        self.set_term(self.user, "")
        self.assertEqual(self.items(self.user)["items"], [])
        with db.get_conn() as conn:
            conn.execute("INSERT INTO snapshot(user_id,data,source,fetched_at) VALUES (?,?,?,?)",
                         (self.user, json.dumps([{"name": "课表课程", "date": "2026-09-02"}]), "portal", db.now_str()))
        self.assertEqual(self.items(self.user)["pending_count"], 1)
        self.set_term(self.user, "2027-02-22")
        self.assertEqual(self.items(self.user)["pending_count"], 0)

    async def test_term_change_cancels_pending_homework_pushes(self):
        await self.sync_items(self.user, [_item()])
        self.push.return_value = [{"channel": "bark", "ok": False}]
        await self.sync_items(self.user, [_item(title="更新")])
        self.assertTrue(self.outbox())
        cx, _ = db.get_chaoxing_status_data(self.user)
        db.get_due_notification_outbox(self.user, cx, "2026-12-01T12:00:00+08:00")
        self.assertTrue(any(r["kind"] == "homework_due" for r in self.outbox()))
        self.set_term(self.user, "2027-02-22")
        self.assertEqual(db.get_due_notification_outbox(self.user, cx, "2026-12-01T13:00:00+08:00"), [])
        self.assertEqual([r for r in self.outbox() if r["status"] == "pending"], [])

    async def test_my_homework_uses_completion_status_not_read_status(self):
        await self.sync_items(self.user, [
            _item(source_id="pending", title="未完成作业"),
            _item(source_id="unknown", status="unknown"),
            _item(kind="notice", source_id="notice", status="", due_at=""),
            _item(source_id="submitted", status="submitted"),
        ])
        db.mark_notifications_read(self.user, [], True)
        for unread in (False, True):
            result = db.list_notifications(self.user, "homework", unread, 100)
            self.assertEqual([item["title"] for item in result["items"]], ["未完成作业"])
            self.assertTrue(result["items"][0]["is_read"])
            self.assertEqual(result["pending_count"], 1)
        await self.sync_items(self.user, [_item(source_id="pending", status="submitted")])
        self.assertEqual(db.list_notifications(self.user, "homework", False, 100)["items"], [])

    async def test_closed_homework_not_collected_but_notices_and_unknown_remain(self):
        closed = [_item(source_id=f"closed:{status}", status=status) for status in ("submitted", "graded", "expired")]
        notice = _item(kind="notice", source_id="notice:keep", status="", due_at="")
        unknown = _item(source_id="unknown:keep", status="unknown")
        result = await self.sync_items(self.user, [*closed, notice, unknown, _item()])
        self.assertEqual(result["new_count"], 3)
        visible = self.items(self.user)
        self.assertEqual(len(visible["items"]), 3)
        self.assertEqual((visible["unread_count"], visible["pending_count"]), (3, 1))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=?", (self.user,)).fetchone()[0], 4)
        self.assertEqual([i["status"] for i in self.items(self.user, kind="expired")["items"]], ["expired"])
        self.push.assert_not_awaited()

    async def test_local_expired_status_roundtrip_and_sync_preservation(self):
        await self.sync_items(self.user, [_item(status="expired")])
        item = self.items(self.user, kind="expired")["items"][0]
        self.assertTrue(item["can_manage"])
        self.assertTrue(item["is_expired"])
        db.set_homework_status(self.user, item["id"], "pending")
        mine = self.items(self.user, kind="homework")
        self.assertEqual(mine["pending_count"], 1)
        self.assertEqual(mine["items"][0]["status"], "expired")
        self.assertEqual(mine["items"][0]["local_status"], "pending")
        db.set_homework_status(self.user, item["id"], "completed")
        self.assertEqual(self.items(self.user)["pending_count"], 0)
        self.assertEqual(self.items(self.user)["items"], [])
        await self.sync_items(self.user, [_item(status="expired", title="上游更新")])
        managed = self.items(self.user, kind="expired")["items"][0]
        self.assertEqual(managed["local_status"], "completed")
        self.assertEqual(managed["title"], "上游更新")
        db.set_homework_status(self.user, item["id"], "")
        self.assertEqual(self.items(self.user, kind="homework")["items"], [])
        self.assertEqual(self.items(self.user, kind="expired")["items"][0]["local_status"], "")
        self.push.assert_not_awaited()

    async def test_elapsed_pending_management_scope_isolation_and_push_cancel(self):
        await self.sync_items(self.user, [_item(due_at="2026-09-01T23:59:00+08:00")])
        item = self.items(self.user, kind="expired")["items"][0]
        self.assertEqual(item["status"], "pending")
        other = self.new_user()
        with self.assertRaises(LookupError):
            db.set_homework_status(other, item["id"], "completed")
        with db.get_conn() as conn:
            for kind in ("homework", "homework_due"):
                conn.execute("INSERT INTO notification_outbox(user_id,kind,source_id,payload,next_attempt) VALUES (?,?,?,?,?)",
                             (self.user, kind, _item()["source_id"], json.dumps(_item()), "2099-01-01"))
        db.set_homework_status(self.user, item["id"], "completed")
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.items(self.user)["pending_count"], 0)
        db.set_homework_status(self.user, item["id"], "pending")
        cx, _ = db.get_chaoxing_status_data(self.user)
        self.assertEqual(db.get_due_notification_outbox(self.user, cx, "2026-11-23T23:00:00+08:00"), [])
        self.set_term(self.user, "2027-02-22")
        self.assertEqual(self.items(self.user, kind="expired")["items"], [])
        self.assertEqual(self.items(self.user, kind="homework")["items"], [])

    async def test_management_rejects_notices_future_and_completed_platform(self):
        await self.sync_items(self.user, [_item(), _item(kind="notice", source_id="notice", status="")])
        for item in self.items(self.user)["items"]:
            with self.assertRaises(ValueError):
                db.set_homework_status(self.user, item["id"], "completed")
        await self.sync_items(self.user, [_item(status="expired")])
        item = self.items(self.user, kind="expired")["items"][0]
        db.set_homework_status(self.user, item["id"], "pending")
        await self.sync_items(self.user, [_item(status="submitted")])
        self.assertEqual(self.items(self.user, kind="homework")["items"], [])
        with self.assertRaises(ValueError):
            db.set_homework_status(self.user, item["id"], "pending")
        db.set_homework_status(self.user, item["id"], "")

    async def test_completion_silently_cancels_updates_and_deadline_reminders(self):
        await self.sync_items(self.user, [_item()])
        self.push.return_value = [{"channel": "bark", "ok": False}]
        await self.sync_items(self.user, [_item(title="待完成更新")])
        original = self.items(self.user)["items"][0]
        db.mark_notifications_read(self.user, [original["id"]], False)
        payload = dict(_item(), event="homework_due")
        with db.get_conn() as conn:
            for key, status in (("due-pending", "pending"), ("due-sent", "sent")):
                conn.execute("INSERT INTO notification_outbox(user_id,kind,source_id,payload,status,next_attempt) VALUES (?,'homework_due',?,?,?,?)", (self.user, key, json.dumps(payload), status, "2099-01-01T00:00:00+08:00"))
        self.push.reset_mock()
        result = await self.sync_items(self.user, [_item(status="submitted")], warnings=["另一课程暂不可用"])
        self.assertEqual(result["new_count"], 0)
        visible = self.items(self.user)
        self.assertEqual(visible["items"], [])
        self.assertEqual((visible["unread_count"], visible["pending_count"]), (0, 0))
        self.assertEqual([(r["source_id"], r["status"]) for r in self.outbox()], [("due-sent", "sent")])
        with db.get_conn() as conn:
            row = conn.execute("SELECT data,is_read FROM notifications WHERE id=?", (original["id"],)).fetchone()
        self.assertEqual(json.loads(row["data"])["status"], "submitted")
        self.assertTrue(row["is_read"])
        self.push.assert_not_awaited()

    async def test_legacy_closed_items_hidden_and_queued_updates_cancelled(self):
        await self.sync_items(self.user, [_item()])
        self.push.return_value = [{"channel": "bark", "ok": False}]
        await self.sync_items(self.user, [_item(title="待发旧更新")])
        with db.get_conn() as conn:
            conn.execute("UPDATE notifications SET data=json_set(data,'$.status','graded') WHERE user_id=?", (self.user,))
        for kind in ("all", "homework"):
            for unread in (False, True):
                visible = db.list_notifications(self.user, kind, unread, 100)
                self.assertEqual(visible["items"], [])
                self.assertEqual(visible["unread_count"], 0)
        cx, _ = db.get_chaoxing_status_data(self.user)
        self.assertEqual(db.get_due_notification_outbox(self.user, cx, "2099-01-01T00:00:00+08:00"), [])
        self.assertEqual(self.outbox(), [])

    async def test_users_cannot_see_or_mark_each_others_items(self):
        other = self.new_user()
        self.bind(other, uid="702", value="other-session")
        await self.sync_items(self.user, [_item(title="用户甲作业")])
        await self.sync_items(other, [_item(title="用户乙作业")], uid="702", cookie_value="other-session")
        mine = self.items(self.user)["items"][0]
        theirs = self.items(other)["items"][0]
        self.assertNotEqual(mine["id"], theirs["id"])
        self.assertEqual(db.mark_notifications_read(self.user, [theirs["id"]], False), 0)
        self.assertFalse(self.items(other)["items"][0]["is_read"])
        self.assertEqual(self.items(self.user)["items"][0]["title"], "用户甲作业")
        self.assertEqual(self.items(other)["items"][0]["title"], "用户乙作业")
        await notifications.disconnect(self.user)
        self.assertEqual(len(self.items(other)["items"]), 1)

    async def test_disconnect_clears_binding_notifications_and_sync_metadata(self):
        await self.sync_items(self.user, [_item()])
        self.push.return_value = [{"channel": "bark", "ok": False}]
        await self.sync_items(self.user, [_item(title="第一章练习（更新）")])
        self.assertEqual(self.outbox()[0]["status"], "pending")
        result = await notifications.disconnect(self.user)
        self.assertEqual(result["state"], "unconfigured")
        cx, meta = db.get_chaoxing_status_data(self.user)
        self.assertEqual(cx["cookies"], [])
        self.assertEqual(cx["uid"], "")
        self.assertEqual(meta, {})
        self.assertEqual(self.items(self.user)["items"], [])
        self.assertEqual(self.outbox(), [])

    async def test_relogin_same_upstream_uid_preserves_items_and_read_state(self):
        await self.sync_items(self.user, [_item()])
        self.push.return_value = [{"channel": "bark", "ok": False}]
        await self.sync_items(self.user, [_item(title="第一章练习（更新）")])
        pending = self.outbox()
        self.assertEqual(pending[0]["status"], "pending")
        self.push.reset_mock()
        original = self.items(self.user)["items"][0]
        db.mark_notifications_read(self.user, [original["id"]], False)
        fake, _, login = self.fake_client(cookie_value="fresh-session")
        with patch.object(chaoxing, "ChaoxingClient", fake):
            result = await notifications.login(self.user, "new-login-name", "ephemeral-password")
        login.assert_awaited_once_with("new-login-name", "ephemeral-password")
        self.assertEqual(result["state"], "partial")
        self.assertTrue(result["warning"])
        self.assertTrue(result["push_enabled"])
        self.assertEqual(self.items(self.user)["items"][0]["id"], original["id"])
        self.assertTrue(self.items(self.user)["items"][0]["is_read"])
        self.assertEqual(self.outbox(), pending)
        self.assertTrue(result["baseline_complete"])
        await self.sync_items(self.user, [_item(title="第一章练习（更新）")], cookie_value="fresh-session")
        self.push.assert_not_awaited()
        self.assertEqual(self.outbox(), pending)
        self.expire_outbox()
        self.push.return_value = [{"channel": "bark", "ok": True}]
        await self.sync_items(self.user, [_item(title="第一章练习（更新）")], cookie_value="fresh-session")
        self.push.assert_awaited_once()
        self.assertEqual(self.outbox()[0]["status"], "sent")
        self.assertTrue(self.items(self.user)["items"][0]["is_read"])
        self.assertNotIn("ephemeral-password", json.dumps(db.get_user_config(self.user)))

    async def test_rebinding_different_upstream_uid_clears_old_data(self):
        await self.sync_items(self.user, [_item()])
        self.push.return_value = [{"channel": "bark", "ok": False}]
        await self.sync_items(self.user, [_item(title="第一章练习（更新）")])
        self.assertEqual(self.outbox()[0]["status"], "pending")
        fake, _, _ = self.fake_client(uid="999", cookie_value="different-account-session")
        with patch.object(chaoxing, "ChaoxingClient", fake):
            await notifications.login(self.user, "different-account", "ephemeral-password")
        self.assertEqual(self.items(self.user)["items"], [])
        self.assertEqual(db.get_chaoxing_status_data(self.user)[1], {})
        self.assertEqual(self.outbox(), [])
        self.push.reset_mock()
        await self.sync_items(self.user, [_item(title="新账号历史作业")], uid="999",
                              cookie_value="different-account-session")
        self.push.assert_not_awaited()
        self.assertEqual(self.outbox(), [])

    async def test_account_deletion_clears_pending_outbox_without_affecting_other_user(self):
        other = self.new_user()
        self.bind(other, uid="702", value="other-session")
        other_cfg = db.get_user_config(other)
        other_cfg["push"] = {"bark_enabled": True}
        db.save_user_config(other, other_cfg)
        await notifications.set_options(other, {"push_enabled": True})
        for user_id, uid, cookie in ((self.user, "701", "synthetic-session"),
                                     (other, "702", "other-session")):
            await self.sync_items(user_id, [_item()], uid=uid, cookie_value=cookie)
            self.push.return_value = [{"channel": "bark", "ok": False}]
            await self.sync_items(user_id, [_item(title="第一章练习（更新）")], uid=uid, cookie_value=cookie)
            self.assertEqual(self.outbox(user_id)[0]["status"], "pending")
        other_outbox = self.outbox(other)
        other_items = self.items(other)
        db.delete_user(self.user)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.items(self.user)["items"], [])
        self.assertEqual(db.get_chaoxing_status_data(self.user), ({}, {}))
        self.assertEqual(self.outbox(other), other_outbox)
        self.assertEqual(self.items(other), other_items)

    async def test_failed_login_never_saves_or_discards_existing_binding(self):
        await self.sync_items(self.user, [_item()])
        before = copy.deepcopy(db.get_user_config(self.user))
        before_items = self.items(self.user)
        fake, _, login = self.fake_client()
        login.side_effect = chaoxing.ChaoxingError("upstream-secret-password-ticket")
        with patch.object(chaoxing, "ChaoxingClient", fake):
            with self.assertRaises(notifications.OperationError) as error:
                await notifications.login(self.user, "synthetic-user", "ephemeral-password")
        self.assertNotIn("upstream-secret", str(error.exception))
        self.assertEqual(db.get_user_config(self.user), before)
        self.assertEqual(self.items(self.user), before_items)

    async def test_failed_initial_login_leaves_user_unconfigured(self):
        user = self.new_user()
        before = db.get_user_config(user)
        fake, _, login = self.fake_client()
        login.side_effect = chaoxing.ChaoxingError("sensitive-upstream-message")
        with patch.object(chaoxing, "ChaoxingClient", fake):
            with self.assertRaises(notifications.OperationError):
                await notifications.login(user, "synthetic-account", "ephemeral-password")
        self.assertEqual(db.get_user_config(user), before)
        self.assertEqual(notifications.status(user)["state"], "unconfigured")
        self.assertNotIn(user, notifications._busy_users)

    async def test_partial_scan_keeps_existing_items_not_returned_by_upstream(self):
        await self.sync_items(self.user, [_item()])
        old = self.items(self.user)["items"][0]
        db.mark_notifications_read(self.user, [old["id"]], False)
        result = await self.sync_items(self.user, [], warnings=["课程列表未完整读取，保留上次作业数据"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["warnings"])
        self.assertEqual(self.items(self.user)["items"][0]["id"], old["id"])
        self.assertTrue(self.items(self.user)["items"][0]["is_read"])
        self.push.assert_not_awaited()

    async def test_manual_sync_refreshes_cookies_and_preserves_disabled_options(self):
        await notifications.set_options(self.user, {"enabled": False, "push_enabled": False})
        result = await self.sync_items(self.user, [_item()], cookie_value="rotated-session")
        self.assertTrue(result["ok"])
        cx, _ = db.get_chaoxing_status_data(self.user)
        self.assertEqual(cx["cookies"], _cookies("rotated-session"))
        self.assertFalse(cx["enabled"])
        self.assertFalse(cx["push_enabled"])
        self.assertEqual(notifications.status(self.user)["state"], "disabled")
        self.push.assert_not_awaited()

    async def test_auth_expiry_retains_old_items_and_blocks_scheduled_retries(self):
        await self.sync_items(self.user, [_item()])
        before = self.items(self.user)["items"]
        fake, fetch, _ = self.fake_client(error=AuthExpiredError("sensitive-upstream-response"))
        with patch.object(chaoxing, "ChaoxingClient", fake):
            result = await notifications.sync(self.user)
            self.assertIsNone(await notifications.sync(self.user, scheduled=True))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], notifications.AUTH_ERROR)
        self.assertEqual(self.items(self.user)["items"], before)
        self.assertEqual(notifications.status(self.user)["state"], "auth_expired")
        fetch.assert_awaited_once()
        self.push.assert_not_awaited()

    async def test_transient_failure_keeps_old_items_and_hides_exception_details(self):
        await self.sync_items(self.user, [_item()])
        before = self.items(self.user)["items"]
        for exception in (chaoxing.ChaoxingError("secret-ticket"), RuntimeError("secret-password")):
            with self.subTest(error_type=type(exception).__name__):
                fake, _, _ = self.fake_client(error=exception)
                with patch.object(chaoxing, "ChaoxingClient", fake):
                    result = await notifications.sync(self.user)
                self.assertEqual(result["error"], notifications.SYNC_ERROR)
                self.assertEqual(self.items(self.user)["items"], before)
                self.assertEqual(notifications.status(self.user)["state"], "error")
        self.assertIsNone(current_user_id.get())

    async def test_busy_user_rejects_parallel_operations_while_other_user_works(self):
        other = self.new_user()
        entered, release = asyncio.Event(), asyncio.Event()
        fake, fetch, _ = self.fake_client()

        async def block_fetch():
            entered.set()
            await release.wait()
            return {"items": [_item()], "warnings": []}

        fetch.side_effect = block_fetch
        with patch.object(chaoxing, "ChaoxingClient", fake):
            task = asyncio.create_task(notifications.sync(self.user))
            try:
                await asyncio.wait_for(entered.wait(), timeout=3)
                for operation in (notifications.sync(self.user), notifications.disconnect(self.user),
                                  notifications.login(self.user, "synthetic", "password"),
                                  notifications.set_options(self.user, {"enabled": False})):
                    with self.assertRaises(notifications.BusyError):
                        await operation
                result = await notifications.disconnect(other)
                self.assertEqual(result["state"], "unconfigured")
            finally:
                release.set()
                await asyncio.wait_for(task, timeout=3)
        self.assertNotIn(self.user, notifications._busy_users)

    async def test_stale_session_cannot_write_items_or_errors_after_rebinding(self):
        old, _ = db.get_chaoxing_status_data(self.user)
        self.bind(self.user, uid="702", value="new-account")
        self.assertFalse(db.start_chaoxing_attempt(self.user, old, "2026-11-23T12:00:00+08:00"))
        self.assertIsNone(db.save_chaoxing_notifications(self.user, old, [_item()], _cookies(), "2026-11-23T12:00:00+08:00"))
        db.record_chaoxing_failure(self.user, old, "旧会话错误", True)
        self.assertEqual(self.items(self.user)["items"], [])
        self.assertEqual(notifications.status(self.user)["state"], "ready")

    async def test_homework_due_reminders_enqueue_once_per_deadline_and_cancel_on_status_change(self):
        # 完成首次基线，不生成历史提醒
        await self.sync_items(self.user, [_item(due_at="2026-11-25T10:00:00+08:00")])
        self.assertEqual(len([r for r in self.outbox() if r["kind"] == "homework_due"]), 0)
        # 距截止 23 小时，自动刷新生成提醒
        fake, _, _ = self.fake_client()
        with patch.object(chaoxing, "ChaoxingClient", fake):
            await notifications.sync(self.user, scheduled=True)
        with db.get_conn() as conn:
            db._refresh_homework_reminders(conn, self.user, "2026-11-24T11:30:00+08:00")
        reminders = [row for row in self.outbox() if row["kind"] == "homework_due"]
        self.assertEqual(len(reminders), 1)
        payload = json.loads(reminders[0]["payload"])
        self.assertEqual(payload["title"], "第一章练习")
        self.assertEqual(payload["event"], "homework_due")
        self.assertIn("2026-11-25T10:00:00", payload["due_at"])
        source_id = reminders[0]["source_id"]
        # 同一截止时间重新刷新不会重复生成
        with db.get_conn() as conn:
            db._refresh_homework_reminders(conn, self.user, "2026-11-24T12:00:00+08:00")
        self.assertEqual(len([r for r in self.outbox() if r["kind"] == "homework_due"]), 1)
        # 作业已提交，取消待发提醒
        await self.sync_items(self.user, [_item(status="submitted", due_at="2026-11-25T10:00:00+08:00")])
        with db.get_conn() as conn:
            db._refresh_homework_reminders(conn, self.user, "2026-11-24T13:00:00+08:00")
        self.assertEqual(len([r for r in self.outbox() if r["kind"] == "homework_due" and r["status"] == "pending"]), 0)
        # 截止时间变更后按新时间重新生成
        await self.sync_items(self.user, [_item(status="pending", due_at="2026-11-25T18:00:00+08:00")])
        with db.get_conn() as conn:
            db._refresh_homework_reminders(conn, self.user, "2026-11-24T19:00:00+08:00")
        new_reminders = [r for r in self.outbox() if r["kind"] == "homework_due" and r["status"] == "pending"]
        self.assertEqual(len(new_reminders), 1)
        self.assertNotEqual(new_reminders[0]["source_id"], source_id)
        self.assertIn("2026-11-25T18:00:00", json.loads(new_reminders[0]["payload"])["due_at"])

    async def test_due_reminders_separate_batch_from_content_updates(self):
        await self.sync_items(self.user, [_item(due_at="2026-11-25T14:00:00+08:00")])
        # 内容更新生成一条 outbox
        result = await self.sync_items(self.user, [_item(title="更新后的作业", due_at="2026-11-25T14:00:00+08:00")])
        self.assertTrue(result["ok"])
        content_updates = [r for r in self.outbox() if r["kind"] == "homework"]
        self.assertEqual(len(content_updates), 1)
        self.assertEqual(content_updates[0]["status"], "sent")
        # 截止提醒单独排队，待交作业且距24小时内才生成
        with db.get_conn() as conn:
            db._refresh_homework_reminders(conn, self.user, "2026-11-24T15:00:00+08:00")
        reminders = [r for r in self.outbox() if r["kind"] == "homework_due"]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["status"], "pending")
        # 分批发送时优先提醒
        rows = db.get_due_notification_outbox(self.user, {"uid": "701", "cookies": _cookies()}, "2026-11-24T15:00:00+08:00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "homework_due")


class ApiTests(_DatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Each test owns an isolated temporary SMS state, including phone-wide cooldown.
        notifications._sms_sessions.clear()
        notifications._sms_sent_at.clear()
        self.user = self.new_user()
        db.save_user_config(self.user, {"override_uid_version": 2})
        token = auth.new_token()
        db.create_session(self.user, token, 3600)
        self.client = _ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="https://agenda.test",
                                    cookies={auth.SESSION_COOKIE: token})
        self.addAsyncCleanup(self.client.aclose)
        # ASGITransport 不启动 lifespan，避免调度器、天气预热和其他出站请求。

    async def test_all_chaoxing_endpoints_require_real_auth_session(self):
        async with _ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="https://agenda.test") as client:
            routes = (("GET", "/api/chaoxing/status"), ("POST", "/api/chaoxing/login"),
                      ("POST", "/api/chaoxing/sms/send"), ("POST", "/api/chaoxing/sms/login"),
                      ("POST", "/api/chaoxing/disconnect"), ("POST", "/api/chaoxing/options"),
                      ("GET", "/api/notifications"), ("POST", "/api/notifications/read"),
                      ("POST", "/api/notifications/sync"))
            for method, url in routes:
                with self.subTest(method=method, url=url):
                    response = await client.request(method, url, json={})
                    self.assertEqual(response.status_code, 401)

    async def test_sms_validation_does_not_echo_inputs(self):
        for route, field in (("send", "validate"), ("login", "code")):
            for value in (None, 123456, [], "", "sensitive-value" * 500):
                response = await self.client.post(f"/api/chaoxing/sms/{route}",
                                                  json={"username": "13800138000", field: value})
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("sensitive-value", response.text)
            for phone in (12345678901, "1380013800", "１３８００１３８０００", "13800138000 "):
                response = await self.client.post(f"/api/chaoxing/sms/{route}",
                                                  json={"username": phone, field: "123456"})
                self.assertEqual(response.status_code, 400)

    async def test_sms_failure_reasons_are_classified_without_sensitive_details(self):
        cases = [
            ({"result": False, "msg": "验证失败 private-token 13800138000"}, "verification"),
            ({"result": False, "msg": "发送次数达到上限 private-token"}, "quota"),
            ({"result": False, "msg": "手机号格式错误 private-token"}, "phone"),
            ({"result": False, "msg": "private-token 13800138000"}, "rejected"),
            ([], "response"),
        ]
        for payload, reason in cases:
            with self.subTest(reason=reason):
                with notifications._sms_guard:
                    notifications._sms_sent_at.clear()
                def handler(request):
                    if request.url.path == "/login":
                        return httpx.Response(200, text="登录")
                    self.assertEqual(request.headers["referer"], "https://passport2.chaoxing.com/login?loginType=2&newversion=true&fid=-1")
                    self.assertEqual(request.headers["x-requested-with"], "XMLHttpRequest")
                    if request.url.path == "/num/booleanCode":
                        return httpx.Response(200, text="false")
                    return httpx.Response(200, json=payload)
                def factory(**kwargs):
                    return _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
                with patch.object(chaoxing.httpx, "AsyncClient", side_effect=factory), self.assertLogs("app.notifications", level="WARNING") as logs:
                    response = await self.client.post("/api/chaoxing/sms/send", json={"username": "13800138000", "validate": "private-token"})
                self.assertEqual(response.status_code, 429 if reason == "quota" else 400)
                self.assertIn("SMS_" + reason.upper(), response.json()["detail"])
                combined = response.text + str(logs.output)
                self.assertNotIn("private-token", combined)
                self.assertNotIn("13800138000", combined)
                self.assertNotIn((self.user, "13800138000"), notifications._sms_sessions)

    async def test_sms_preparation_failure_does_not_call_send(self):
        seen = []
        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(503, text="private-sensitive")
        def factory(**kwargs):
            return _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(chaoxing.httpx, "AsyncClient", side_effect=factory):
            response = await self.client.post("/api/chaoxing/sms/send", json={"username": "13800138000", "validate": "private-token"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("SMS_PREPARE", response.text)
        self.assertEqual(seen, ["/login"])
        self.assertNotIn("private-sensitive", response.text)

    async def test_sms_cookie_flow_limits_isolation_and_binding(self):
        phone = "13800138000"
        seen = []
        fail_login = [True]
        def handler(request):
            seen.append(request)
            path = request.url.path
            if path == "/login":
                self.assertEqual(dict(request.url.params), {"loginType": "2", "newversion": "true", "fid": "-1"})
                self.assertNotIn("cookie", request.headers)
                return httpx.Response(200, text="登录", headers={"set-cookie": "sms=initial; Domain=.chaoxing.com; Path=/; Secure"})
            self.assertIn("sms=", request.headers.get("cookie", ""))
            if path == "/num/booleanCode":
                self.assertEqual(request.method, "POST")
                self.assertEqual(dict(request.url.params), {"key": phone, "type": "1"})
                return httpx.Response(200, text="false")
            if path == "/num/phonecode":
                self.assertEqual(dict(request.url.params), {"phone": phone, "code": "", "type": "1", "needcode": "false", "countrycode": "86", "validate": "human-token", "fid": "-1"})
                return httpx.Response(200, json={"result": True})
            if path == "/fanyaloginbycode":
                data = parse_qs(request.content.decode())
                self.assertEqual(data, {"fid": ["-1"], "uname": [phone], "verCode": [quote(chaoxing.encrypt_login("123456"), safe="")], "refer": ["https%3A%2F%2Fi.chaoxing.com"], "doubleFactorLogin": ["0"], "independentNameId": ["0"], "countrycode": ["86"]})
                if fail_login[0]:
                    return httpx.Response(200, json={"status": False, "msg": "sensitive-upstream"}, headers={"set-cookie": "sms=updated; Domain=.chaoxing.com; Path=/; Secure"})
                self.assertIn("sms=updated", request.headers["cookie"])
                return httpx.Response(200, json={"status": True})
            if path == "/base":
                return httpx.Response(200, text=_PROFILE)
            self.fail("非预期短信请求")
        def factory(**kwargs):
            return _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
        self.bind(self.user)
        before = copy.deepcopy(db.get_user_config(self.user))
        with patch.object(chaoxing.httpx, "AsyncClient", side_effect=factory):
            response = await self.client.post("/api/chaoxing/sms/send", json={"username": phone, "validate": "human-token"})
            self.assertEqual(response.json(), {"sent": True, "retry_after": 60})
            again = await self.client.post("/api/chaoxing/sms/send", json={"username": phone, "validate": "human-token"})
            self.assertEqual(again.status_code, 429)
            for user_id, number in ((self.user, "13900139000"), (self.new_user(), phone)):
                with self.assertRaises(notifications.OperationError) as limited:
                    await notifications.send_sms(user_id, number, "human-token")
                self.assertEqual(limited.exception.status_code, 429)
            self.assertEqual(len(seen), 3)
            with self.assertRaises(notifications.OperationError):
                await notifications.login_sms(self.new_user(), phone, "123456")
            with self.assertRaises(notifications.OperationError):
                await notifications.login_sms(self.user, "13900139000", "123456")
            response = await self.client.post("/api/chaoxing/sms/login", json={"username": phone, "code": "123456"})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("sensitive-upstream", response.text)
            self.assertEqual(db.get_user_config(self.user), before)
            fail_login[0] = False
            response = await self.client.post("/api/chaoxing/sms/login", json={"username": phone, "code": "123456"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["configured"])
            self.assertNotIn((self.user, phone), notifications._sms_sessions)

    async def test_sms_inflight_send_is_locked_and_disconnect_cleans_session(self):
        phone = "13800138000"
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_send(*args):
            started.set()
            await release.wait()
        with patch.object(chaoxing.ChaoxingClient, "send_sms", side_effect=slow_send) as send:
            task = asyncio.create_task(notifications.send_sms(self.user, phone, "human-token"))
            await started.wait()
            try:
                self.assertIn((self.user, phone), notifications._sms_sent_at)
                response = await self.client.post("/api/chaoxing/sms/send", json={"username": phone, "validate": "human-token"})
                self.assertEqual(response.status_code, 409)
            finally:
                release.set()
                await task
            self.assertEqual(send.call_count, 1)
        self.assertIn((self.user, phone), notifications._sms_sessions)
        response = await self.client.post("/api/chaoxing/disconnect")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn((self.user, phone), notifications._sms_sessions)

    async def test_sms_login_api_preserves_safe_error_categories_and_binding(self):
        phone = "13800138000"
        self.bind(self.user)
        before = copy.deepcopy(db.get_user_config(self.user))
        for reason in chaoxing.SmsLoginError.MESSAGES:
            notifications._sms_sessions[(self.user, phone)] = {
                "cookies": _cookies(), "expires": notifications.monotonic() + 600, "attempts": 0}
            with patch.object(chaoxing.ChaoxingClient, "login_sms", side_effect=chaoxing.SmsLoginError(reason)):
                response = await self.client.post("/api/chaoxing/sms/login", json={"username": phone, "code": "123456"})
            self.assertEqual(response.status_code, 502 if reason in ("transport", "response", "profile") else 400)
            self.assertIn(f"SMS_LOGIN_{reason.upper()}", response.text)
            self.assertNotIn(phone, response.text)
            self.assertNotIn("123456", response.text)
            self.assertEqual(db.get_user_config(self.user), before)

    async def test_sms_attempt_expiry_busy_and_exception_sanitization(self):
        phone = "13800138000"
        key = (self.user, phone)
        notifications._sms_sessions[key] = {"cookies": _cookies(), "expires": notifications.monotonic() + 600, "attempts": 0}
        with patch.object(chaoxing.ChaoxingClient, "login_sms", side_effect=RuntimeError("private-sensitive")) as login:
            for _ in range(5):
                response = await self.client.post("/api/chaoxing/sms/login", json={"username": phone, "code": "123456"})
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("private-sensitive", response.text)
            response = await self.client.post("/api/chaoxing/sms/login", json={"username": phone, "code": "123456"})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(login.call_count, 5)
            notifications._sms_sessions[key]["expires"] = 0
            response = await self.client.post("/api/chaoxing/sms/login", json={"username": phone, "code": "123456"})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(key, notifications._sms_sessions)
        with notifications._user_lock(self.user):
            response = await self.client.post("/api/chaoxing/sms/send", json={"username": phone, "validate": "human-token"})
            self.assertEqual(response.status_code, 409)
        with patch.object(chaoxing.ChaoxingClient, "send_sms", side_effect=RuntimeError("private-sensitive")) as send:
            response = await self.client.post("/api/chaoxing/sms/send", json={"username": phone, "validate": "human-token"})
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("private-sensitive", response.text)
            response = await self.client.post("/api/chaoxing/sms/send", json={"username": phone, "validate": "human-token"})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(send.call_count, 1)

    async def test_login_rejects_invalid_input_without_echoing_credentials(self):
        secret = "do-not-echo-password"
        payloads = ([], {}, {"username": "test"}, {"username": "", "password": secret},
                    {"username": 123, "password": secret}, {"username": "test", "password": [secret]},
                    {"username": "test", "password": secret, "cookies": []},
                    {"username": "x" * 129, "password": secret},
                    {"username": "test", "password": secret * 30},
                    {"username": "test", "password": "   "})
        with patch.object(notifications, "login", new_callable=AsyncMock) as login:
            for payload in payloads:
                with self.subTest(payload_type=type(payload).__name__):
                    response = await self.client.post("/api/chaoxing/login", json=payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn(secret, response.text)
            malformed = await self.client.post("/api/chaoxing/login", content="{bad-json", headers={"content-type": "application/json"})
            self.assertEqual(malformed.status_code, 400)
            login.assert_not_awaited()

    async def test_failed_login_api_is_sanitized_and_preserves_binding(self):
        self.bind(self.user)
        before = copy.deepcopy(db.get_user_config(self.user))
        fake, _, login = self.fake_client()
        login.side_effect = chaoxing.ChaoxingError("private-upstream-cookie-password")
        with patch.object(chaoxing, "ChaoxingClient", fake):
            response = await self.client.post("/api/chaoxing/login", json={
                "username": "synthetic-account", "password": "ephemeral-password"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("private-upstream", response.text)
        self.assertNotIn("ephemeral-password", response.text)
        self.assertEqual(db.get_user_config(self.user), before)

    async def test_settings_cannot_write_chaoxing_and_cookies_are_masked(self):
        self.bind(self.user, value="must-never-return-cookie")
        before = db.get_user_config(self.user)
        response = await self.client.post("/api/settings", json={"chaoxing": {"uid": "999", "cookies": _cookies("injected")}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(db.get_user_config(self.user), before)
        settings = await self.client.get("/api/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["chaoxing"]["cookies"], config.MASK)
        for url in ("/api/chaoxing/status", "/api/notifications"):
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("must-never-return-cookie", response.text)
            self.assertNotIn('"cookies"', response.text)
        self.assertNotIn("must-never-return-cookie", settings.text)

    async def test_homework_status_api_validation_and_user_isolation(self):
        self.bind(self.user)
        cx, _ = db.get_chaoxing_status_data(self.user)
        db.save_chaoxing_notifications(self.user, cx, [_item(status="expired")], cx["cookies"], "2026-11-23T23:00:00+08:00")
        response = await self.client.get("/api/notifications?kind=expired")
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        for payload in ({}, {"id": True, "status": "completed"}, {"id": 0, "status": "pending"},
                        {"id": item["id"], "status": []}, {"id": item["id"], "status": "submitted"},
                        {"id": item["id"], "status": "completed", "user_id": self.user}):
            result = await self.client.post("/api/notifications/homework-status", json=payload)
            self.assertEqual(result.status_code, 400)
        for status in ("completed", "pending", ""):
            result = await self.client.post("/api/notifications/homework-status", json={"id": item["id"], "status": status})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["local_status"], status)
        other = self.new_user()
        self.bind(other, uid="702", value="other-session")
        other_cx, _ = db.get_chaoxing_status_data(other)
        db.save_chaoxing_notifications(other, other_cx, [_item(status="expired")], other_cx["cookies"], "2026-11-23T23:00:00+08:00")
        theirs = db.list_notifications(other, "expired", False, 100)["items"][0]
        result = await self.client.post("/api/notifications/homework-status", json={"id": theirs["id"], "status": "completed"})
        self.assertEqual(result.status_code, 404)

    async def test_options_validate_boolean_types_and_only_allow_public_switches(self):
        self.bind(self.user)
        before = copy.deepcopy(db.get_user_config(self.user))
        for options in ({"enabled": "false"}, {"push_enabled": 1}, {"cookies": []}, {"uid": "other"}, {"poll_minutes": 1}):
            with self.subTest(options=options):
                response = await self.client.post("/api/chaoxing/options", json=options)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(db.get_user_config(self.user), before)
        response = await self.client.post("/api/chaoxing/options", json={"enabled": False, "push_enabled": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "disabled")
        self.assertTrue(response.json()["push_enabled"])

    async def test_notification_filters_and_read_ids_reject_invalid_input(self):
        for query in ("kind=invalid", "unread=1", "limit=0", "limit=-1", "limit=501", "limit=abc", "limit=9999"):
            with self.subTest(query=query):
                response = await self.client.get("/api/notifications?" + query)
                self.assertIn(response.status_code, (400, 422))
        for payload in ({"ids": [True]}, {"ids": [0]}, {"ids": [-1]}, {"ids": [2**63]},
                        {"ids": ["1"]}, {"ids": "1"}, {"ids": [1] * 501}, {"all": "true"}, {"unknown": True}):
            with self.subTest(payload=payload):
                response = await self.client.post("/api/notifications/read", json=payload)
                self.assertEqual(response.status_code, 400)

    async def test_real_session_login_sync_read_and_disconnect_flow(self):
        fake, fetch, login = self.fake_client(items=[_item()])
        with patch.object(chaoxing, "ChaoxingClient", fake):
            response = await self.client.post("/api/chaoxing/login", json={"username": "synthetic-account", "password": "ephemeral-password"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["state"], "ready")
            self.assertNotIn("synthetic-session", response.text)
            response = await self.client.post("/api/notifications/sync")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
        login.assert_awaited_once_with("synthetic-account", "ephemeral-password")
        fetch.assert_awaited_once()
        response = await self.client.get("/api/notifications?kind=homework&unread=true&limit=1")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual((data["unread_count"], data["pending_count"]), (1, 1))
        response = await self.client.post("/api/notifications/read", json={"ids": [data["items"][0]["id"]]})
        self.assertEqual(response.json(), {"ok": True, "updated": 1})
        response = await self.client.get("/api/notifications?unread=true")
        self.assertEqual(response.json()["items"], [])
        response = await self.client.post("/api/chaoxing/disconnect")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["configured"])
        self.assertNotIn("ephemeral-password", json.dumps(db.get_user_config(self.user)))

    async def test_api_marks_only_current_users_notifications_and_busy_returns_409(self):
        other = self.new_user()
        self.bind(self.user)
        self.bind(other, uid="702")
        for user_id in (self.user, other):
            cx, _ = db.get_chaoxing_status_data(user_id)
            db.save_chaoxing_notifications(user_id, cx, [_item()], cx["cookies"], "2026-11-23T12:00:00+08:00")
        other_id = self.items(other)["items"][0]["id"]
        response = await self.client.post("/api/notifications/read", json={"ids": [other_id]})
        self.assertEqual(response.json()["updated"], 0)
        response = await self.client.post("/api/notifications/read", json={"all": True})
        self.assertEqual(response.json()["updated"], 1)
        self.assertFalse(self.items(other)["items"][0]["is_read"])
        with notifications._user_lock(self.user):
            response = await self.client.post("/api/chaoxing/disconnect")
        self.assertEqual(response.status_code, 409)
        self.assertIn("正在处理", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
