"""通用 HTTP JSON 课表接口适配器。

适用于学校/学院自建的课表 API（如门户抓包得到的接口）。
配置 URL、请求头、以及响应字段映射即可接入，无需改代码。
支持从浏览器 cURL 一键导入（见 curl_import.py）。
"""
from __future__ import annotations

import json
import re

import httpx

from ..models import Course, parse_weeks
from .base import AuthExpiredError, TimetableProvider

WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}


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
    if isinstance(v, (int, float)):
        return max(1, min(7, int(v)))
    s = str(v or "")
    m = re.search(r"[1-7]", s)
    if m:
        return int(m.group())
    for ch, n in WEEKDAY_MAP.items():
        if ch in s:
            return n
    return 1


def _to_int(v, default: int) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    nums = re.findall(r"\d+", str(v or ""))
    return int(nums[0]) if nums else default


def check_auth_expired(response: httpx.Response):
    """响应被踢回 SSO / 登录页、或接口返回未登录时抛出 AuthExpiredError。"""
    final_url = str(response.url)
    ctype = response.headers.get("content-type", "")
    body_head = response.text[:3000]
    msg = "门户登录已过期（请求被重定向回统一身份认证），请重新登录门户并导入新的 cURL / 更新 Cookie"
    if ("sso." in final_url and "login" in final_url) or \
       ("text/html" in ctype and ("统一身份认证" in body_head or "login" in body_head.lower())):
        raise AuthExpiredError(msg)
    if response.status_code in (401, 403):
        raise AuthExpiredError(msg)
    # 部分门户不跳转，只返回 {"code":401,"msg":"未登录"} 之类的 JSON
    if "json" in ctype or body_head.lstrip().startswith(("{", "[")):
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            code = str(payload.get("code") or payload.get("status") or "")
            text = str(payload.get("msg") or payload.get("message") or "")
            if code in ("401", "403") or any(
                w in text for w in ("未登录", "登录失效", "登录过期", "会话过期", "请重新登录", "unauthorized")
            ):
                raise AuthExpiredError(msg)


def _field_value(item: dict, mapping: dict, field: str, default=None):
    v = _dig(item, mapping.get(field, field))
    return default if v is None else v


def _weeks_value(raw) -> tuple[list[int], str]:
    if isinstance(raw, list):
        return [int(w) for w in raw], ""
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
        self.url = p.get("http_url", "")
        self.method = (p.get("http_method") or "GET").upper()
        self.headers = self._parse_json(p.get("http_headers"), {})
        self.body = self._parse_json(p.get("http_body"), None)
        self.list_path = p.get("http_list_path", "")
        self.mapping = p.get("http_mapping", {})
        if not self.url:
            raise ValueError("未配置课表 API 地址 http_url")

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

    async def fetch_raw(self):
        """请求接口并返回原始 JSON（含登录态检查），供导入预览与字段猜测。"""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as client:
            if self.method == "POST":
                r = await client.post(self.url, headers=self.headers, json=self.body)
            else:
                r = await client.get(self.url, headers=self.headers)
            check_auth_expired(r)
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                raise ValueError("接口未返回 JSON（可能登录态失效或接口地址不对），请重新导入 cURL")

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
                "门户模式需要登录态：请在门户课表页抓包，复制 cURL 到设置中导入（含 Cookie 或 Authorization）")
