"""通用 HTTP JSON 课表接口适配器。

适用于学校/学院自建的课表 API（如门户抓包得到的接口）。
配置 URL、请求头、以及响应字段映射即可接入，无需改代码。
支持从浏览器 cURL 一键导入（见 curl_import.py）。
"""
from __future__ import annotations

import json
import re
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..models import Course, parse_weeks
from ..netguard import SafeAsyncHTTPTransport, assert_safe_outbound_url
from .base import AuthExpiredError, PortalResponseError, TimetableProvider

WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}


def merge_cookie_header(existing: str, updates: dict) -> str:
    """把响应里新下发的 Cookie 合并进现有的 Cookie 请求头（同名覆盖，保持原顺序）。"""
    pairs: dict[str, str] = {}
    order: list[str] = []
    for part in (existing or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k not in pairs:
            order.append(k)
        pairs[k] = v.strip()
    changed = False
    for k, v in updates.items():
        if pairs.get(k) != v:
            if k not in pairs:
                order.append(k)
            pairs[k] = v
            changed = True
    if not changed:
        return existing
    return "; ".join(f"{k}={pairs[k]}" for k in order)


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


_SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "token", "x-token",
    "x-access-token", "x-auth-token", "x-api-key", "api-key",
}


def _redirect_headers(headers: dict, old_url: str, new_url: str, method: str) -> dict:
    out = dict(headers)
    if _origin(old_url) != _origin(new_url):
        out = {k: v for k, v in out.items() if str(k).lower() not in _SENSITIVE_HEADERS}
    if method == "GET":
        out = {k: v for k, v in out.items() if str(k).lower() not in {
            "content-length", "transfer-encoding", "content-type"}}
    return out


def _domain_matches(host: str, domain: str) -> bool:
    domain = domain.lower().lstrip(".").rstrip(".")
    return host == domain or host.endswith("." + domain)


def _cookie_updates(response, expected_host: str) -> dict[str, str]:
    updates: dict[str, str] = {}
    for raw in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        try:
            parsed.load(raw)
        except Exception:
            continue
        for name, morsel in parsed.items():
            domain = morsel["domain"]
            if domain and not _domain_matches(expected_host, domain):
                continue
            updates[name] = morsel.value
    return updates


def _dig(obj, path: str):
    """按点路径取 JSON 节点，如 'data.courses'、'time.0'、'time.-1'。"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            if part.lstrip("-").isdigit():
                idx = int(part)
                cur = cur[idx] if -len(cur) <= idx < len(cur) else None
            else:
                return None
        else:
            return None
    return cur


def _to_day(v) -> int:
    if v is None:
        return 1
    s = str(v).strip()
    if re.fullmatch(r"(?:周|星期)?[一二三四五六日天]", s):
        return WEEKDAY_MAP[s[-1]]
    day = _to_int(v, 0)
    if not 1 <= day <= 7:
        raise ValueError("星期必须在 1..7 范围内")
    return day


def _to_int(v, default: int) -> int:
    if v is None:
        return default
    s = str(v).strip()
    m = re.fullmatch(r"(?:星期|周|第)?([+-]?\d{1,3})(?:\.0+)?(?:节)?", s)
    if not m:
        raise ValueError("星期或节次格式不正确")
    return int(m.group(1))


def _is_login_page(response: httpx.Response, body: str) -> bool:
    """只识别登录端点、认证页标题或实际登录表单，不把普通登录链接当作登录页。"""
    parts = urlsplit(str(response.url))
    auth_host = (parts.hostname or "").lower().split(".")[0] in {
        "sso", "cas", "auth", "authserver", "login", "passport",
    }
    if auth_host and re.search(r"/(?:login|signin)(?:[./;]|$)", parts.path, re.I):
        return True
    ctype = response.headers.get("content-type", "").lower()
    if "html" not in ctype and not body.lstrip().startswith("<"):
        return False
    page = BeautifulSoup(body, "html.parser")
    title = page.title.get_text(" ", strip=True) if page.title else ""
    if (re.search(r"统一身份认证|统一认证|单点登录", title)
            and not re.search(r"维护|错误|异常|故障|不可用|失败|error|unavailable|maintenance", title, re.I)):
        return True
    for form in page.find_all("form"):
        if form.find("input", attrs={"type": re.compile(r"^password$", re.I)}) is None:
            continue
        identity = " ".join(str(form.get(key) or "") for key in ("id", "name", "action"))
        if re.search(r"login|signin|sign-in|/cas/|/sso/", identity, re.I):
            return True
    return False


def check_auth_expired(response: httpx.Response):
    """只有明确的登录页、401 或 JSON 未登录证据才确认登录态失效。"""
    msg = "门户登录已过期，请重新登录门户并更新登录信息"
    if response.status_code == 401:
        raise AuthExpiredError(msg)
    # 服务端错误保留原 HTTP 状态，不能因为错误页带认证标题而误报登录过期。
    if response.status_code >= 500:
        return
    ctype = response.headers.get("content-type", "").lower()
    body_head = response.text[:3000]
    if _is_login_page(response, body_head):
        raise AuthExpiredError(msg)
    # 部分门户不跳转，只返回 {"code":401,"msg":"未登录"} 之类的 JSON
    if "json" in ctype or body_head.lstrip().startswith(("{", "[")):
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            code = str(payload.get("code") or payload.get("status") or "")
            text = str(payload.get("msg") or payload.get("message") or "").lower()
            if code == "401" or any(
                w in text for w in ("未登录", "登录失效", "登录过期", "会话过期", "请重新登录", "unauthorized")
            ):
                raise AuthExpiredError(msg)


def _field_value(item: dict, mapping: dict, field: str, default=None):
    v = _dig(item, mapping.get(field, field))
    return default if v is None else v


def _weeks_value(raw) -> tuple[list[int], str]:
    if isinstance(raw, list):
        weeks = [_to_int(w, 0) for w in raw]
        if any(not 1 <= w <= 60 for w in weeks):
            raise ValueError("周次必须在 1..60 范围内")
        return sorted(set(weeks)), ""
    return parse_weeks(str(raw or ""))


def _is_blank(v) -> bool:
    return v is None or str(v).strip() in ("", "-", "无", "None")


def courses_from_payload(payload, list_path: str = "", mapping: dict | None = None) -> list[Course]:
    """把一份课表 JSON 解析成 Course 列表。

    HTTP 数据源与外部注入（App WebView / 浏览器扩展）共用同一套字段映射逻辑。
    教务未排时间 / 地点的记录（星期、节次为空）会被跳过：否则会被兜底成
    「周一第 1-2 节、每周都上」，在周视图里凭空多出几门课。
    """
    mapping = mapping or {}
    items = _dig(payload, list_path) if list_path else payload
    if not isinstance(items, list):
        raise ValueError(f"list_path='{list_path}' 未定位到课程数组，请检查响应结构")
    courses = []
    for it in items:
        if not isinstance(it, dict):
            continue
        raw_day = _field_value(it, mapping, "day", None)
        raw_start = _field_value(it, mapping, "start", None)
        if mapping.get("day") and _is_blank(raw_day):
            continue  # 未排课的占位记录
        if mapping.get("start") and _is_blank(raw_start):
            continue
        weeks, note = _weeks_value(_field_value(it, mapping, "weeks", []))
        start = _to_int(raw_start, 1)
        end = _to_int(_field_value(it, mapping, "end", start + 1), start + 1)
        if not 1 <= start <= end <= 30:
            raise ValueError("节次必须满足 1 <= 开始节次 <= 结束节次 <= 30")
        courses.append(Course(
            name=str(_field_value(it, mapping, "name", "")),
            day=_to_day(_field_value(it, mapping, "day", 1)),
            start=start, end=max(end, start),
            teacher=str(_field_value(it, mapping, "teacher", "") or ""),
            room=str(_field_value(it, mapping, "room", "") or ""),
            weeks=weeks, note=note,
            campus=str(_field_value(it, mapping, "campus", "") or ""),
            source_id=str(_field_value(it, mapping, "source_id", "") or ""),
        ))
    if not courses:
        raise ValueError("解析出 0 门课程，请检查字段映射")
    return courses


class HttpJsonProvider(TimetableProvider):
    name = "httpjson"

    def __init__(self, cfg: dict):
        p = cfg.get("provider", {})
        portal = p.get("portal") or {}
        self.url = (portal.get("url") or p.get("http_url") or "")
        self.method = (portal.get("method") or p.get("http_method") or "GET").upper()
        self.headers = self._parse_json(
            portal.get("headers") if portal.get("headers") is not None
            else p.get("http_headers"), {})
        body = (portal.get("body") if portal.get("body") is not None
                else p.get("http_body"))
        self.content_type = next((str(v).lower().split(";", 1)[0].strip()
                                  for k, v in self.headers.items()
                                  if str(k).lower() == "content-type"), "")
        if isinstance(body, str) and self.content_type and self.content_type != "application/json" and not self.content_type.endswith("+json"):
            self.body = body
        else:
            self.body = self._parse_json(body, None)
        self.list_path = portal.get("list_path") or p.get("http_list_path") or ""
        self.mapping = portal.get("mapping") or p.get("http_mapping") or {}
        if not self.url:
            raise ValueError("未配置课表 API 地址")
        # 防 SSRF：拒绝本机/内网/云元数据地址
        assert_safe_outbound_url(self.url)

    @staticmethod
    def _parse_json(text, default):
        if isinstance(text, (dict, list)):
            return text
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise ValueError(f"headers/body 配置不是合法 JSON: {str(text)[:60]}")

    def _remember_refreshed_cookies(self, responses) -> None:
        """只接收当前门户源同源响应下发的 Cookie。"""
        if self.name != "portal":
            return
        expected_host = (urlsplit(self.url).hostname or "").lower()
        updates: dict[str, str] = {}
        for response in responses:
            if (urlsplit(str(response.url)).hostname or "").lower() != expected_host:
                continue
            updates.update(_cookie_updates(response, expected_host))
        if not updates:
            return
        key = next((k for k in self.headers if str(k).lower() == "cookie"), None)
        if key is None:
            return
        merged = merge_cookie_header(self.headers.get(key) or "", updates)
        if merged == (self.headers.get(key) or ""):
            return
        try:
            from ..config import load_config, save_config
            cfg = load_config()
            portal = cfg.setdefault("provider", {}).setdefault("portal", {})
            headers = self._parse_json(portal.get("headers"), {})
            headers = dict(headers)
            pkey = next((k for k in headers if str(k).lower() == "cookie"), "Cookie")
            headers[pkey] = merge_cookie_header(headers.get(pkey) or "", updates)
            save_config({"provider": {"portal": {"headers": headers}}})
            self.headers[key] = merged
        except Exception:  # noqa: BLE001
            pass

    async def fetch_raw(self):
        """请求接口并返回原始 JSON，重定向时不把凭据带到其他源。"""
        url = self.url
        method = self.method
        headers = dict(self.headers)
        body = self.body
        responses = []
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False,
            transport=SafeAsyncHTTPTransport(),
        ) as client:
            for _ in range(6):
                kwargs = {}
                if method == "POST":
                    if self.content_type == "application/x-www-form-urlencoded":
                        kwargs = ({"data": body} if isinstance(body, dict)
                                  else {"content": body or ""})
                    elif self.content_type and self.content_type != "application/json" and not self.content_type.endswith("+json"):
                        kwargs = {"content": body or ""}
                    else:
                        kwargs = {"json": body}
                r = await client.request(method, url, headers=headers, **kwargs)
                responses.append(r)
                location = r.headers.get("location") if r.is_redirect else ""
                if not location:
                    break
                if r.status_code == 303 or (r.status_code in (301, 302) and method == "POST"):
                    method, body = "GET", None
                url = urljoin(str(r.url), location)
                assert_safe_outbound_url(url)
                headers = _redirect_headers(headers, str(r.url), url, method)
            else:
                raise ValueError("接口重定向次数过多")
            self._remember_refreshed_cookies(responses)
            # 401/403 仍可检查明确认证证据；其他失败先保留原 HTTP 错误。
            if r.status_code not in (401, 403):
                r.raise_for_status()
            check_auth_expired(r)
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                raise PortalResponseError("门户接口未返回有效 JSON，暂时无法读取课表，请稍后重试") from None

    async def fetch(self) -> list[Course]:
        return courses_from_payload(await self.fetch_raw(), self.list_path, self.mapping)


class PortalProvider(HttpJsonProvider):
    """青岛理工大学融合门户（portal.qut.edu.cn，SSO 登录）。

    与通用 HTTP 模式相同的配置面，但强制要求 Cookie（门户无匿名接口），
    并带 SSO 过期自诊断。推荐通过设置页「粘贴 cURL 导入」生成配置。
    """
    name = "portal"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        lower = {str(k).lower(): v for k, v in self.headers.items()}
        has_cookie = bool(lower.get("cookie"))
        has_token = any(lower.get(k) for k in (
            "authorization", "x-token", "x-access-token", "x-auth-token", "token",
        ))
        if not (has_cookie or has_token):
            raise ValueError(
                "门户模式需要登录态：请到「课表来源」用一键登录扫码，或粘贴含 Cookie 的 cURL 导入")
