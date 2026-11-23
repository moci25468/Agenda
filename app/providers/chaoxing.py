"""学习通只读客户端：短时密码登录，会话按域保存，查询作业和通知。

协议入口参考超星 Web 端及 xuexitong-cli 的协议说明；不提交作业、签到或已读到上游。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from time import monotonic as budget_clock
from datetime import datetime
from http.cookiejar import Cookie
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .base import AuthExpiredError
from ..netguard import assert_safe_outbound_url

TZ = ZoneInfo("Asia/Shanghai")
BASE = "https://i.chaoxing.com/base"
LOGIN = "https://passport2.chaoxing.com/fanyalogin"
COURSES = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
MIDDLE = "https://mooc1.chaoxing.com/visit/stucoursemiddle"
WORKS = "https://mooc1.chaoxing.com/mooc2/work/list"
NOTICES = "https://notice.chaoxing.com/pc/notice/getNoticeList"
NOTICE_HOME = "https://notice.chaoxing.com/pc/notice/myNotice"


class ChaoxingError(RuntimeError):
    """可向用户展示的已脱敏错误。"""


class SmsLoginError(ChaoxingError):
    """仅暴露固定阶段分类，不回显上游正文、手机号或验证码。"""
    MESSAGES = {
        "transport": "学习通登录请求未完成，暂时无法确认结果，请稍后重试",
        "response": "学习通登录响应异常，暂时无法确认结果，请稍后重试",
        "verification": "学习通要求额外安全验证，请在官方页面完成验证",
        "code": "学习通提示短信验证码错误或已失效，请核对最新短信",
        "rejected": "学习通未接受本次登录，原因暂未确认，请稍后重试",
        "profile": "学习通已接受验证码，但账号身份确认暂未完成，请稍后重试",
    }

    def __init__(self, reason: str):
        self.reason = reason if reason in self.MESSAGES else "rejected"
        super().__init__(self.MESSAGES[self.reason] + f" [SMS_LOGIN_{self.reason.upper()}]")


class SmsSendError(ChaoxingError):
    """短信阶段和原因仅使用固定分类，不携带上游响应或凭据。"""
    MESSAGES = {
        "prepare": "无法建立学习通短信会话，请稍后重试",
        "quota_check": "学习通发送限额检查失败，请稍后重试",
        "quota": "学习通提示短信发送次数受限，请稍后再试",
        "transport": "学习通短信接口连接失败或拒绝访问，请稍后重试",
        "response": "学习通短信接口返回异常，未能确认发送成功",
        "verification": "学习通未接受本次安全验证，短信未发送；请重新获取并完成人工验证",
        "phone": "学习通未接受该手机号，请在官方登录页核对绑定号码",
        "rejected": "学习通短信接口拒绝发送，原因暂未识别；可先在官方登录页验证短信是否可达",
    }

    def __init__(self, reason: str):
        self.reason = reason if reason in self.MESSAGES else "rejected"
        super().__init__(self.MESSAGES[self.reason] + f" [SMS_{self.reason.upper()}]")


def safe_url(value: str, base: str = BASE) -> str:
    """只接受超星 HTTPS 地址，逐跳校验防重定向泄漏会话。"""
    try:
        url = urljoin(base, value)
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if (p.scheme != "https" or p.username or p.password
                or p.port not in (None, 443)
                or not (host == "chaoxing.com" or host.endswith(".chaoxing.com"))):
            raise ValueError
        return assert_safe_outbound_url(url, allow_http=False)
    except (ValueError, TypeError):
        raise ChaoxingError("学习通返回了不支持的页面地址") from None


def encrypt_login(value: str) -> str:
    key = b"u2oh6Vu^HWe4_AES"
    padder = padding.PKCS7(128).padder()
    data = padder.update(value.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")


def plain(value: str) -> str:
    soup = BeautifulSoup(str(value or ""), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def parse_courses(page: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    found = {}
    for link in soup.select("[href], [data], [onclick]"):
        text = " ".join(str(link.get(k, "")) for k in ("href", "data", "onclick"))
        match = re.search(r"stucoursemiddle\?([^\s\"'<>]+)", text)
        if not match:
            continue
        qs = parse_qs(match.group(1))
        values = [qs.get(k, [""])[0] for k in ("courseid", "clazzid", "cpi")]
        if not all(re.fullmatch(r"\d+", v) for v in values):
            continue
        # 名称常在同一课程卡中，位于链接后方。
        card = link.find_parent("li") or link.parent or link
        name_node = (card if any("course-name" in str(c) for c in card.get("class", []))
                     else card.select_one(".course-name, [class*='course-name']"))
        name = ((name_node.get("title") or name_node.get_text(" ", strip=True))
                if name_node else link.get("title") or link.get_text(" ", strip=True))
        found.setdefault(tuple(values), dict(zip(("courseid", "clazzid", "cpi"), values), name=name or "未命名课程"))
    if not found and not (soup.select_one(".not-data, .no-data, .nullData")
                          or any(s in page for s in ("暂无课程", "还没有课程", "没有相关课程", "没有课程"))):
        raise ChaoxingError("学习通课程页面结构发生变化，未能读取课程")
    return list(found.values())


def parse_homework_start(page: str) -> str:
    text = plain(page)
    match = re.search(r"(?:发布时间|开始时间|作答时间)\s*[:：]?\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2})", text)
    if not match:
        return ""
    try:
        return datetime.strptime(match.group(1).replace("/", "-"), "%Y-%m-%d %H:%M").replace(tzinfo=TZ).isoformat()
    except ValueError:
        return ""


def parse_due(page: str) -> str:
    text = plain(page)
    full = re.search(r"(?:截止时间|结束时间)\s*[:：]?\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2})", text)
    if full:
        try:
            return datetime.strptime(full.group(1).replace("/", "-"), "%Y-%m-%d %H:%M").replace(tzinfo=TZ).isoformat()
        except ValueError:
            return ""
    interval = re.search(r"作答时间\s*[:：]?\s*(?:\d{4}-)?\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}\s*至\s*((?:\d{4}-)?\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})", text)
    if not interval:
        return ""
    raw = interval.group(1)
    now = datetime.now(TZ)
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            value = raw if re.match(r"\d{4}-", raw) else f"{year}-{raw}"
            candidates.append(datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=TZ))
        except ValueError:
            continue
    return min(candidates, key=lambda d: abs((d - now).total_seconds())).isoformat() if candidates else ""


def homework_status(value: str) -> str:
    if any(s in value for s in ("已批阅", "已批改", "已完成")):
        return "graded"
    if any(s in value for s in ("已提交", "待批阅")):
        return "submitted"
    if any(s in value for s in ("已截止", "已结束")):
        return "expired"
    if any(s in value for s in ("未提交", "未交", "未完成", "待提交", "待完成", "进行中", "待做")):
        return "pending"
    return "unknown"


def parse_homework(page: str, course: dict) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    items = []
    for node in soup.select("li[data]"):
        title = node.select_one(".overHidden2, .work-name, [class*='workName']")
        if not title:
            continue
        status = node.select_one(".status")
        raw = unquote(str(node.get("data") or ""))
        url = safe_url(raw, WORKS) if raw else ""
        qs = parse_qs(urlsplit(url).query)
        work_id = (qs.get("workId") or qs.get("workid") or qs.get("id") or [""])[0]
        title_text = title.get_text(" ", strip=True)
        # 动态 enc/t 不参与身份，避免每次同步生成重复通知。
        stable = work_id or hashlib.sha256(title_text.encode()).hexdigest()[:24]
        items.append({"source_id": f"homework:{course['courseid']}:{course['clazzid']}:{stable}",
                      "kind": "homework", "title": title_text, "body": "",
                      "course": course["name"], "status": homework_status(status.get_text(" ", strip=True)) if status else "unknown",
                      "due_at": "", "published_at": "", "url": url})
    if not items and not (soup.select_one(".not-data, .no-data, .nullData")
                          or any(s in page for s in ("暂无作业", "没有作业", "还没有发布", "暂无内容"))):
        raise ChaoxingError("学习通作业页面未能识别，已保留上次同步数据")
    return items


class ChaoxingClient:
    def __init__(self, config: dict):
        self.config = config
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False,
                                        headers={"User-Agent": "Mozilla/5.0", "Referer": BASE})
        self._last_request = 0.0
        self._warnings: list[str] = []
        self._deadline: float | None = None
        self._remaining_requests = 240
        for c in config.get("cookies") or []:
            if not isinstance(c, dict):
                continue
            domain = str(c.get("domain") or "").lower()
            if not (domain.lstrip(".") == "chaoxing.com" or domain.endswith(".chaoxing.com")):
                continue
            cookie = Cookie(0, str(c.get("name") or ""), str(c.get("value") or ""),
                            None, False, domain, bool(c.get("domain_specified", True)), domain.startswith("."),
                            str(c.get("path") or "/"), True, bool(c.get("secure", True)),
                            c.get("expires"), False, None, None, {}, False)
            self.client.cookies.jar.set_cookie(cookie)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    def export_cookies(self) -> list[dict]:
        return [{"name": c.name, "value": c.value, "domain": c.domain,
                 "domain_specified": c.domain_specified, "path": c.path,
                 "secure": c.secure, "expires": c.expires}
                for c in self.client.cookies.jar
                if c.domain.lstrip(".") == "chaoxing.com" or c.domain.endswith(".chaoxing.com")]

    async def _request(self, method: str, url: str, *, login: bool = False, **kwargs) -> httpx.Response:
        url = safe_url(url)
        for _ in range(6):
            remaining = self._deadline - budget_clock() if self._deadline else 20.0
            if remaining <= 0.5 or self._remaining_requests <= 0:
                raise ChaoxingError("本轮学习通查询已达时间或请求预算，保留已读取内容")
            self._remaining_requests -= 1
            wait = 0.35 - (asyncio.get_running_loop().time() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = asyncio.get_running_loop().time()
            try:
                response = await asyncio.wait_for(
                    self.client.request(method, url, timeout=min(20.0, remaining), **kwargs),
                    timeout=min(20.0, remaining))
            except (httpx.HTTPError, TimeoutError):
                raise ChaoxingError("暂时无法连接学习通，请稍后重试") from None
            if response.is_redirect:
                target = safe_url(response.headers.get("location", ""), str(response.url))
                if "passport" in (urlsplit(target).hostname or "") and not login:
                    raise AuthExpiredError("学习通登录已失效，请在设置 → 账号重新登录")
                # 登录凭据不经重定向再次提交。
                if login:
                    raise ChaoxingError("学习通登录需要在官方页面完成验证后重试")
                url, method, kwargs = target, "GET", {}
                continue
            if response.status_code in (401, 403):
                raise AuthExpiredError("学习通登录失效或需要验证，请重新登录")
            if response.status_code >= 400:
                raise ChaoxingError("学习通暂时不可用，请稍后重试")
            if not login:
                text = response.text
                if any(x in text for x in ("passport2.chaoxing.com/login", "id=\"loginForm\"", "id='loginForm'")):
                    raise AuthExpiredError("学习通登录已失效，请重新登录")
                if any(x in text for x in ("antispider", "访问过于频繁", "人机验证", "安全验证")):
                    raise ChaoxingError("学习通需要人工验证，请在官方页面完成验证后再重试")
            return response
        raise ChaoxingError("学习通页面跳转异常，请稍后重试")

    async def _profile(self) -> dict:
        response = await self._request("GET", BASE)
        page = response.text
        uid = re.search(r"\b(?:var\s+)?uid\s*[:=]\s*['\"]?(\d+)", page)
        cookie_uid = next((c.value for c in self.client.cookies.jar if c.name == "_uid"), "")
        identity = uid.group(1) if uid else cookie_uid
        has_account = bool(re.search(r"账号[：:]|id=['\"]siteName['\"]|var\s+uid\s*=", page))
        if not identity or not re.fullmatch(r"\d+", identity) or not has_account:
            raise AuthExpiredError("未能确认学习通登录身份，请重新登录")
        name = re.search(r"aria-label=['\"]账号[：:]([^'\"]+)['\"]", page)
        self._base_page = page
        return {"uid": identity, "name": name.group(1) if name else ""}

    async def login(self, username: str, password: str, captcha: str = "") -> dict:
        """兼容密码登录，人工验证交由官方流程处理。"""
        # 新登录不携带任何旧账号 Cookie，避免凭据失败后误绑旧会话。
        self.client.cookies.clear()
        payload = {
            "fid": "-1", "uname": encrypt_login(username), "password": encrypt_login(password),
            "refer": "https%3A%2F%2Fi.chaoxing.com", "t": "true", "forbidotherlogin": "0",
            "validate": captcha, "doubleFactorLogin": "0", "independentId": "0"}
        response = await self._request("POST", LOGIN, login=True, data=payload)
        try:
            result = response.json()
        except ValueError:
            raise ChaoxingError("学习通登录响应异常，请稍后重试") from None
        if (not isinstance(result, dict) or result.get("status") is not True
                or result.get("containTwoFactorLogin")):
            raise ChaoxingError("学习通登录失败，请检查账号密码或完成官方验证")
        return await self._profile()

    async def send_sms(self, username: str, validate: str) -> None:
        login_page = "https://passport2.chaoxing.com/login?loginType=2&newversion=true&fid=-1"
        headers = {"Referer": login_page, "X-Requested-With": "XMLHttpRequest"}
        try:
            await self._request("GET", "https://passport2.chaoxing.com/login", login=True,
                                params={"loginType": "2", "newversion": "true", "fid": "-1"})
        except (ChaoxingError, AuthExpiredError):
            raise SmsSendError("prepare") from None
        try:
            response = await self._request("POST", "https://passport2.chaoxing.com/num/booleanCode",
                                           login=True, params={"key": username, "type": "1"},
                                           headers={**headers, "Origin": "https://passport2.chaoxing.com"})
        except (ChaoxingError, AuthExpiredError):
            raise SmsSendError("quota_check") from None
        if "alert" in response.text.lower():
            raise SmsSendError("quota")
        try:
            response = await self._request("GET", "https://passport2.chaoxing.com/num/phonecode",
                                          login=True, params={"phone": username, "code": "", "type": "1",
                                          "needcode": "false", "countrycode": "86", "validate": validate, "fid": "-1"},
                                          headers=headers)
        except (ChaoxingError, AuthExpiredError):
            raise SmsSendError("transport") from None
        try:
            result = response.json()
        except ValueError:
            raise SmsSendError("response") from None
        if not isinstance(result, dict):
            raise SmsSendError("response")
        if result.get("result") is not True:
            # 只将常见文案归类；不回传原始 msg，防止回显手机号和票据。
            message = str(result.get("msg") or "").lower()
            if any(word in message for word in ("频繁", "上限", "超限", "次数", "限制")):
                reason = "quota"
            elif any(word in message for word in ("验证", "captcha", "validate")):
                reason = "verification"
            elif any(word in message for word in ("手机号", "号码", "手机格式")):
                reason = "phone"
            else:
                reason = "rejected"
            raise SmsSendError(reason)

    async def login_sms(self, username: str, code: str) -> dict:
        # 必须沿用发送短信时的临时 Cookie。
        try:
            response = await self._request("POST", "https://passport2.chaoxing.com/fanyaloginbycode",
                                          login=True, data={"fid": "-1", "uname": username,
                                          "verCode": quote(encrypt_login(code), safe=""),
                                          "refer": "https%3A%2F%2Fi.chaoxing.com", "doubleFactorLogin": "0",
                                          "independentNameId": "0", "countrycode": "86"})
        except (ChaoxingError, AuthExpiredError):
            raise SmsLoginError("transport") from None
        try:
            result = response.json()
        except ValueError:
            raise SmsLoginError("response") from None
        if not isinstance(result, dict):
            raise SmsLoginError("response")
        if result.get("containTwoFactorLogin"):
            raise SmsLoginError("verification")
        if result.get("status") is not True:
            message = str(result.get("msg") or "")
            if any(word in message for word in ("安全验证", "二次验证", "人机验证")):
                reason = "verification"
            elif "验证码" in message and any(word in message for word in ("错误", "不正确", "失效", "过期")):
                reason = "code"
            else:
                reason = "rejected"
            raise SmsLoginError(reason)
        # 仅在明确接受验证码后重试只读身份确认；不自动重放验证码 POST。
        for attempt in range(2):
            try:
                return await self._profile()
            except (ChaoxingError, AuthExpiredError):
                if attempt:
                    raise SmsLoginError("profile") from None
                await asyncio.sleep(0.5)

    async def _homework(self, course: dict) -> list[dict]:
        response = await self._request("GET", MIDDLE, params={
            **{k: course[k] for k in ("courseid", "clazzid", "cpi")}, "ismooc2": "1", "v": "2"})
        soup = BeautifulSoup(response.text, "html.parser")
        def hidden(key):
            node = soup.find("input", id=key) or soup.find("input", attrs={"name": key})
            return str(node.get("value") or "") if node else ""
        enc = hidden("workEnc")
        if not enc:
            raise ChaoxingError("该课程未提供可读取的作业入口")
        response = await self._request("GET", WORKS, params={
            "courseId": course["courseid"], "classId": course["clazzid"], "cpi": course["cpi"],
            "ut": "s", "openc": hidden("openc"), "enc": enc, "stuenc": hidden("enc"),
            "t": time.time_ns() // 1_000_000, "isdisplaytable": "2"})
        if "没有此页面访问权限" in response.text:
            raise ChaoxingError("作业入口权限已变化，请稍后重试")
        items = parse_homework(response.text, course)
        # 只读详情，并保留已读到的作业，即使某份详情暂不可用。
        pending = [i for i in items if i["url"] and i["status"] in ("pending", "unknown")]
        if len(pending) > 30:
            self._warnings.append(f"「{course['name']}」待交作业较多，部分截止时间尚未读取")
        for item in pending[:30]:
            if self._deadline and (budget_clock() >= self._deadline - 1 or self._remaining_requests <= 2):
                self._warnings.append("本轮查询已达预算，部分作业截止时间暂未读取")
                break
            try:
                detail = await self._request("GET", item["url"])
                item["due_at"] = parse_due(detail.text)
                item["published_at"] = parse_homework_start(detail.text)
            except AuthExpiredError:
                raise
            except ChaoxingError:
                self._warnings.append(f"「{course['name']}」部分作业截止时间暂未读取")
        return items

    async def _notices(self) -> list[dict]:
        items, cursor, seen = [], "", set()
        for _ in range(5):
            response = await self._request("POST", NOTICES, data={
                "type": "0", "notice_type": "0", "lastValue": cursor, "sort": "0", "folderUUID": "",
                "kw": "", "startTime": "", "endTime": "", "gKw": "", "gName": "", "year": "",
                "tag": "", "fidsCode": "", "filterSenderPuids": "", "filterTags": ""})
            try:
                payload = response.json()
            except ValueError:
                raise ChaoxingError("学习通通知接口响应异常，已保留上次数据") from None
            if not isinstance(payload, dict) or payload.get("status") not in (True, 1):
                raise ChaoxingError("学习通通知暂时无法读取，请稍后重试")
            data = payload.get("notices")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ChaoxingError("学习通通知数据结构发生变化")
            for n in data["list"]:
                if not isinstance(n, dict):
                    continue
                title, body = plain(n.get("title", "")), plain(n.get("content", ""))
                stamp = str(n.get("completeTime") or n.get("insertTime") or "")
                sender = plain(n.get("createrName", ""))
                key = str(n.get("id") or n.get("noticeId") or n.get("uuid") or "")
                if not key:
                    key = hashlib.sha256(f"{sender}\0{title}\0{stamp}".encode()).hexdigest()
                items.append({"source_id": f"notice:{key}", "kind": "notice", "title": title or "学习通通知",
                              "body": body[:20000], "course": sender, "status": "", "due_at": "",
                              "published_at": stamp, "url": NOTICE_HOME})
            next_cursor = str(data.get("lastGetId") or "")
            if not data["list"] or data.get("lastPage") in (True, 1, "true", "1") or not next_cursor or next_cursor in seen:
                break
            seen.add(next_cursor)
            cursor = next_cursor
        else:
            self._warnings.append("通知较多，本轮读取最近 5 页，更早内容请在学习通查看")
        return items

    async def fetch_items(self) -> dict:
        self._deadline = budget_clock() + 180
        self._remaining_requests = 240
        identity = await self._profile()
        if self.config.get("uid") and identity["uid"] != str(self.config["uid"]):
            raise AuthExpiredError("学习通账号身份发生变化，请重新绑定")
        fid = re.search(r"(?:var\s+)?['\"]?fid['\"]?\s*[:=]\s*['\"]?(\d+)", self._base_page)
        items, warnings, successes = [], [], 0
        # 通知先读，长课程扫描也不阻塞通知中心。
        try:
            items.extend(await self._notices())
            successes += 1
        except AuthExpiredError:
            raise
        except ChaoxingError:
            warnings.append("课程通知未完整读取，保留上次通知数据")
        try:
            page = await self._request("GET", COURSES, params={"courseType": "1", "courseFid": fid.group(1) if fid else "", "courseTeacherid": "0", "courseOrderBy": "0"})
            courses = parse_courses(page.text)
            if len(courses) > 60:
                warnings.append("课程较多，本轮仅查询前 60 门课程")
            for course in courses[:60]:
                if budget_clock() >= self._deadline - 1 or self._remaining_requests <= 2:
                    warnings.append("本轮扫描达到时间或请求上限，未查询课程保留上次数据")
                    break
                try:
                    items.extend(await self._homework(course))
                    successes += 1
                except AuthExpiredError:
                    raise
                except ChaoxingError:
                    warnings.append(f"「{course['name']}」作业未完整读取，保留上次数据")
            if not courses:
                successes += 1
        except AuthExpiredError:
            raise
        except ChaoxingError:
            warnings.append("课程列表未完整读取，保留上次作业数据")
        if not successes:
            raise ChaoxingError("本次未能读取学习通作业和通知，已保留上次数据")
        return {"items": items, "warnings": list(dict.fromkeys([*warnings, *self._warnings]))}
