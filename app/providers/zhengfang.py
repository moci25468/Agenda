"""正方教务系统（jwglxt）适配器：课表 + 考试。

青岛理工大学教务平台为正方 jwglxt 架构（jxgl.qut.edu.cn/jwglxt）。
登录流程按第三方客户端实测协议实现：

  1. GET  /xtgl/login_slogin.html                 取 csrftoken
  2. GET  /xtgl/login_getPublicKey.html           取 RSA modulus / exponent
  3. POST /xtgl/login_slogin.html                 RSA 加密密码后提交
  4. POST /kbcx/xskbcx_cxXsKb.html?gnmkpath=N2151 课表（kbList）
  5. POST /kwgl/kscx_cxXsksxxIndex.html           考试（items）

也可以跳过账号密码，直接把浏览器登录后的 Cookie 填到设置里。
"""
from __future__ import annotations

import base64
import os
import re
import time
from contextlib import asynccontextmanager

import httpx

from ..models import Course, Exam, parse_weeks
from .base import AuthExpiredError, TimetableProvider

UA = ("Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36")
WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
LOGIN_EXPIRED_MSG = "教务会话已过期或登录失败，请重新登录，或更新设置中的 Cookie"


def zhengfang_configured(cfg: dict) -> bool:
    """是否填了可用于拉课表/考试的正方凭证（Cookie 或账号密码）。"""
    from ..config import zhengfang_configured as _zc
    return _zc(cfg)


def rsa_encrypt(password: str, modulus_b64: str, exponent_b64: str) -> str:
    """正方登录：modulus/exponent 为 Base64，密码按 RSA/ECB/PKCS1v15 后 Base64。

    纯 Python 实现，避免为一次公钥加密引入 cryptography 依赖。
    """
    n = int.from_bytes(base64.b64decode(modulus_b64), "big")
    e = int.from_bytes(base64.b64decode(exponent_b64), "big")
    if n <= 0 or e <= 0:
        raise ValueError("教务公钥无效")
    msg = password.encode("utf-8")
    k = (n.bit_length() + 7) // 8
    if len(msg) > k - 11:
        raise ValueError("密码过长，无法使用教务公钥加密")
    ps_len = k - len(msg) - 3
    ps = bytearray()
    while len(ps) < ps_len:
        ps.extend(b for b in os.urandom(ps_len - len(ps)) if b != 0)
    em = b"\x00\x02" + bytes(ps) + b"\x00" + msg
    cipher = pow(int.from_bytes(em, "big"), e, n)
    return base64.b64encode(cipher.to_bytes(k, "big")).decode("ascii")


def _hidden_value(html: str, name: str) -> str:
    """取登录页隐藏域的值；正方模板里 value 可能不带引号（如 value= 1）。"""
    pattern = (r'(?:id|name)=["\']%s["\'][^>]*?value\s*=\s*'
               r'(?:["\']([^"\']*)["\']|([^\s>]+))') % re.escape(name)
    m = re.search(pattern, html, re.S)
    if not m:
        return ""
    return (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()


def _page_message(html: str) -> str:
    """从登录失败页里取提示文字（正方把错误放在 #tips 或 bg_danger 段落里）。"""
    for start_pat in (
        r'id=["\']tips["\'][^>]*>',
        r'class=["\'][^"\']*(?:bg_danger|sl_danger)[^"\']*["\'][^>]*>',
    ):
        m = re.search(start_pat, html, re.S)
        if not m:
            continue
        chunk = html[m.end():m.end() + 600]
        end = re.search(r"</(?:div|p|section|ul|table)>", chunk, re.I)
        if end:
            chunk = chunk[:end.start()]
        text = " ".join(re.sub(r"<[^>]+>", " ", chunk).split())
        text = text.strip(" !·•-—:：")
        if text:
            return text[:120]
    return ""


def _to_int(value, default: int = 1) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    nums = re.findall(r"\d+", str(value or ""))
    return int(nums[0]) if nums else default


def _to_day(value) -> int:
    if isinstance(value, (int, float)):
        return max(1, min(7, int(value)))
    text = str(value or "")
    m = re.search(r"[1-7]", text)
    if m:
        return int(m.group())
    for ch, n in WEEKDAY_MAP.items():
        if ch in text:
            return n
    return 1


class ZhengfangProvider(TimetableProvider):
    name = "zhengfang"
    supports_exams = True

    def __init__(self, cfg: dict):
        p = cfg.get("provider", {})
        zf = p.get("zhengfang") or {}
        if not isinstance(zf, dict):
            zf = {}
        self.base = (zf.get("base") or p.get("zf_base") or "").rstrip("/")
        self.username = zf.get("username") or p.get("zf_username") or ""
        self.password = zf.get("password") or p.get("zf_password") or ""
        self.cookie = (zf.get("cookie") or p.get("zf_cookie") or "").strip()
        if self.cookie.lower().startswith("cookie:"):
            self.cookie = self.cookie[7:].strip()
        self.year = str(zf.get("year") or p.get("zf_year", "") or "")
        self.term = str(zf.get("term") or p.get("zf_term", "3") or "3")
        if not self.base:
            raise ValueError("未配置正方教务地址（设置 → 课表来源 → 正方教务，填入学号密码）")

    @asynccontextmanager
    async def _session(self):
        headers = {"User-Agent": UA}
        if self.cookie:
            headers["Cookie"] = self.cookie
        async with httpx.AsyncClient(
            follow_redirects=True, trust_env=False, headers=headers, timeout=30,
        ) as client:
            if not self.cookie:
                await self._login(client)
            yield client

    async def _login(self, client: httpx.AsyncClient):
        if not self.username or not self.password:
            raise ValueError("未配置学号/密码，也未配置 Cookie")

        login_url = f"{self.base}/xtgl/login_slogin.html"
        r = await client.get(login_url, timeout=20)
        r.raise_for_status()
        html = r.text
        csrf = _hidden_value(html, "csrftoken")
        mmsfjm = _hidden_value(html, "mmsfjm") or "1"

        password_field = self.password
        if mmsfjm != "0":
            r = await client.get(
                f"{self.base}/xtgl/login_getPublicKey.html",
                params={"time": int(time.time() * 1000)}, timeout=20)
            r.raise_for_status()
            try:
                pub = r.json()
            except ValueError:
                raise AuthExpiredError(
                    "教务公钥接口未返回 JSON，可能地址配置错误或接口已变更")
            modulus, exponent = pub.get("modulus"), pub.get("exponent")
            if not (modulus and exponent):
                raise AuthExpiredError(
                    "教务未返回登录加密公钥，请稍后重试或改用 Cookie 模式")
            try:
                password_field = rsa_encrypt(self.password, modulus, exponent)
            except ValueError as e:
                raise ValueError(f"教务登录加密失败：{e}")

        origin = "{0.scheme}://{0.netloc}".format(httpx.URL(self.base))
        r = await client.post(
            f"{login_url}?time={int(time.time() * 1000)}",
            headers={
                "Referer": login_url,
                "Origin": origin,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "csrftoken": csrf,
                "language": "zh_CN",
                "yhm": self.username,
                "mm": password_field,
                "hidMm": "",
                "ydType": "",
                "yzm": "",
            },
            follow_redirects=False, timeout=20,
        )

        location = r.headers.get("location", "")
        final_url = ""
        if 300 <= r.status_code < 400 and location:
            if location.startswith("http"):
                target = location
            elif location.startswith("/"):
                target = f"{origin}{location}"
            else:
                target = f"{self.base}/{location.lstrip('/')}"
            # 正方登录成功时可能先 302 回登录页，再凭已建立的会话跳到主菜单
            try:
                r = await client.get(target, timeout=20)
                final_url = str(r.url)
            except httpx.HTTPError:
                raise AuthExpiredError(f"教务登录后跳转失败（HTTP {r.status_code}），请稍后重试")
            if "login_slogin" in final_url or 'name="yhm"' in r.text:
                detail = _page_message(r.text)
                if not detail and re.search(r'name=["\']yzm["\']', r.text):
                    detail = ("教务要求输入验证码（连续登录失败会触发），"
                              "请稍后重试或改用 Cookie 模式")
                raise AuthExpiredError(f"教务登录失败：{detail or '账号或密码错误'}")
        elif "index_initMenu" in r.text or "initMenu" in r.text:
            pass  # 直接返回了主菜单页
        else:
            detail = _page_message(r.text)
            if not detail and re.search(r'name=["\']yzm["\']', r.text):
                detail = ("教务要求输入验证码（连续登录失败会触发），"
                          "请稍后重试或改用 Cookie 模式")
            if not detail:
                detail = f"账号或密码错误（HTTP {r.status_code}）"
            raise AuthExpiredError(f"教务登录失败：{detail}")

    async def _fetch_courses(self, client: httpx.AsyncClient) -> list[Course]:
        r = await client.post(
            f"{self.base}/kbcx/xskbcx_cxXsKb.html",
            params={"gnmkdm": "N2151"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base}/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N2151",
            },
            data={"xnm": self.year, "xqm": self.term, "kzlx": "ck"},
            timeout=30,
        )
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            raise AuthExpiredError(LOGIN_EXPIRED_MSG)
        kb_list = payload.get("kbList") or []
        if not kb_list:
            raise ValueError(f"课表为空或字段不匹配：响应键为 {list(payload.keys())[:8]}")
        courses = []
        for item in kb_list:
            if not isinstance(item, dict):
                continue
            day = _to_day(item.get("xqj") or item.get("xqjmc") or 1)
            start = _to_int(item.get("jcs") or item.get("jcor") or 1)
            end = start
            nums = [int(n) for n in re.findall(r"\d+", str(item.get("jcs") or ""))]
            if len(nums) >= 2:
                start, end = nums[0], nums[-1]
            weeks, note = parse_weeks(str(item.get("zcd") or ""))
            courses.append(Course(
                name=item.get("kcmc") or "未命名课程",
                day=day, start=start, end=max(end, start),
                teacher=item.get("xm") or item.get("jsxm") or "",
                room=item.get("cdmc") or "",
                weeks=weeks, note=note,
                campus=item.get("cdxqmc") or item.get("xqmc") or "",
                source_id=str(item.get("jxb_id") or item.get("kch_id") or ""),
            ))
        return courses

    async def _fetch_exams(self, client: httpx.AsyncClient) -> list[Exam]:
        r = await client.post(
            f"{self.base}/kwgl/kscx_cxXsksxxIndex.html",
            params={"doType": "query", "gnmkdm": "N358105"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base}/kwgl/kscx_cxXsksxxIndex.html"
                           "?gnmkdm=N358105&layout=default",
            },
            data={
                "xnm": self.year,
                "xqm": self.term,
                "_search": "false",
                "nd": int(time.time() * 1000),
                "queryModel.showCount": "50",
                "queryModel.currentPage": "1",
                "queryModel.sortName": "",
                "queryModel.sortOrder": "asc",
            },
            timeout=30,
        )
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            raise AuthExpiredError(LOGIN_EXPIRED_MSG)
        items = payload.get("items") or payload.get("rows") or []
        exams = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("kcmc") or "").strip()
            raw_time = str(item.get("kssj") or "").strip()
            if not name and not raw_time:
                continue
            exams.append(Exam.from_dict({
                "name": name or "未命名考试",
                "time": raw_time,
                "room": item.get("cdmc") or "",
                "seat": item.get("zwh") or "",
                "campus": item.get("cdxqmc") or item.get("xqmc") or "",
                "note": item.get("ksxz") or "",
                "source_id": str(item.get("id") or f"{name}_{raw_time}"),
            }))
        return exams

    async def fetch(self) -> list[Course]:
        async with self._session() as client:
            return await self._fetch_courses(client)

    async def fetch_exams(self) -> list[Exam]:
        async with self._session() as client:
            return await self._fetch_exams(client)

    async def fetch_bundle(self) -> tuple[list[Course], list[Exam] | None]:
        """一次登录同时拉课表和考试，避免重复登录触发风控。

        考试接口失败不影响课表：返回 None 并记录 last_exam_error，
        由调度器保留上一次考试快照。
        """
        self.last_exam_error = ""
        async with self._session() as client:
            courses = await self._fetch_courses(client)
            try:
                exams = await self._fetch_exams(client)
            except Exception as e:  # noqa: BLE001
                self.last_exam_error = str(e)
                exams = None
            return courses, exams