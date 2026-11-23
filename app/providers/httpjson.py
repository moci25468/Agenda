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
from ..netguard import assert_safe_outbound_url, check_outbound_request
from .base import AuthExpiredError, TimetableProvider

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

    def _remember_refreshed_cookies(self, response) -> None:
        """门户网关会在响应里滚动下发新 Cookie（滑动续期）。

        把这部分写回配置，静态快照就能跟着网关一起续命；失败一律静默，
        绝不能因为写配置出错而影响取数。
        """
        if self.name != "portal":
            return
        updates: dict[str, str] = {}
        try:
            for hop in [*response.history, response]:
                for raw in hop.headers.get_list("set-cookie"):
                    name, sep, rest = raw.partition("=")
                    name = name.strip()
                    value = rest.split(";", 1)[0].strip()
                    if name and sep:
                        updates[name] = value
        except Exception:  # noqa: BLE001
            return
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
        """请求接口并返回原始 JSON（含登录态检查），供导入预览与字段猜测。"""
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, trust_env=False,
            event_hooks={"request": [check_outbound_request]},
        ) as client:
            if self.method == "POST":
                if self.content_type == "application/x-www-form-urlencoded":
                    kwargs = ({"data": self.body} if isinstance(self.body, dict)
                              else {"content": self.body or ""})
                elif self.content_type and self.content_type != "application/json" and not self.content_type.endswith("+json"):
                    kwargs = {"content": self.body or ""}
                else:
                    kwargs = {"json": self.body}
                r = await client.post(self.url, headers=self.headers, **kwargs)
            else:
                r = await client.get(self.url, headers=self.headers)
            self._remember_refreshed_cookies(r)
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
                "门户模式需要登录态：请到「课表来源」用一键登录扫码，或粘贴含 Cookie 的 cURL 导入")
