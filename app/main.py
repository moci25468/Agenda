"""Agenda 知序 — FastAPI 入口。版本号由 app.version（git tag）动态提供。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
import unicodedata
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, browser_login, cache, db, notifications, room_cache
from .config import (
    DEFAULT_CONFIG, MASK, demo_allowed, global_config, load_config, masked_config, save_config,
)
from .user_context import current_user_id, get_current_user, set_current_user
from .netguard import UnsafeURLError, assert_safe_outbound_url
from .models import (
    Course, Event, Exam, Score, apply_course_overrides, apply_exam_overrides,
    course_uid, current_week, exam_uid, parse_weeks, score_uid,
    week_dates, DEFAULT_SECTION_TIMES, DEFAULT_SECTION_ENDS,
)
from .providers import get_provider
from .providers.zhengfang import ZhengfangProvider, default_term, zhengfang_configured
from .push import push_all, send_selected, wecom_input_error
from .scheduler import (
    WEEKDAY_CN, apply_snapshot, courses_on, free_sections, next_run_time,
    reschedule, run_sync, section_time, shutdown_scheduler, start_scheduler,
    sync_slot_hhmm,
)
from . import weather
from .weather import hours_map
from .version import get_version, get_full_version
from . import ical

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 注册串行化：避免「count_users()==0 判定首个管理员」在并发下产生两个 admin，
# 也避免「先查重再插入」的竞态（单进程足够；多 worker 由 UNIQUE 约束兜底）。


async def _warm_weather():
    """启动时先暖一遍天气缓存：第一次抓取可能要十几秒，别让首个请求去等。"""
    try:
        await hours_map(load_config())
    except Exception:  # noqa: BLE001 - 预热失败不影响启动
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await cache.start()
    cfg = load_config()
    start_scheduler(cfg)
    # 首次启动无课表时静默同步一次（不推送），保证打开页面即有数据
    if db.get_snapshot() is None:
        asyncio.create_task(run_sync(push=False))
    asyncio.create_task(_warm_weather())
    room_task = asyncio.create_task(room_cache.run()) if room_cache.enabled() else None
    try:
        yield
    finally:
        if room_task is not None:
            room_task.cancel()
            with suppress(asyncio.CancelledError):
                await room_task
        shutdown_scheduler()
        await cache.close()


app = FastAPI(title="Agenda 知序", version=get_version(), lifespan=lifespan)

# App 化时常见的 WebView / 本地来源
ALLOWED_ORIGIN_RE = re.compile(
    r"^(tauri://localhost|capacitor://localhost|app://[^/]+|"
    r"http://(localhost|127\.0\.0\.1)(:\d+)?)$",
    re.I,
)
PUBLIC_API_PATHS = {
    "/api/health", "/api/auth/status", "/api/auth/register", "/api/auth/login",
    # 日历订阅端点：webcal:// 拉取时日历 App 不携带 Cookie，靠 URL 里的私有 token 鉴权
    "/api/export/calendar.ics",
}
MAX_API_BODY_BYTES = 2 * 1024 * 1024
_CORS_HEADER_NAMES = {
    b"access-control-allow-origin", b"access-control-allow-credentials",
    b"access-control-allow-headers", b"access-control-allow-methods", b"vary",
}


def _same_origin(origin: str, host: str) -> bool:
    try:
        return urlsplit(origin).netloc.lower() == host.lower()
    except ValueError:
        return False


def _cookie_token(cookie_header: str) -> str:
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == auth.SESSION_COOKIE:
            return v
    return ""


def _apply_cors(response, origin: str, allowed: bool):
    if not allowed:
        return
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Local-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Vary"] = "Origin"


def _subscribe_user_by_token(token: str) -> tuple[int, dict] | None:
    """用日历订阅私有 token 定位用户，返回 (user_id, cfg)。

    订阅 token 存于该用户 user_config 的 calendar_subscribe_token 字段，
    永不过期、可随时重新生成。找不到或未配置时返回 None。
    """
    if not token:
        return None
    from . import db as _db
    for uid in _db.list_user_ids(active_only=True):
        cfg = _db.get_user_config(uid)
        if cfg.get("calendar_subscribe_token") == token:
            return uid, cfg
    return None


class AccessGuard:
    """纯 ASGI 中间件：会话鉴权 + 用户上下文 + 可选 CORS。

    所有 /api/*（白名单除外）必须有有效会话，不再用 Origin 判断身份；
    security.enabled 开启时，跨源调用额外要求本地 token。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        origin = headers.get("origin", "")
        if path.startswith("/api/") and scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
            try:
                declared_size = int(headers.get("content-length", "0"))
            except ValueError:
                declared_size = 0
            if declared_size > MAX_API_BODY_BYTES:
                resp = JSONResponse({"detail": "请求内容过大"}, status_code=413)
                await resp(scope, receive, send)
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    break
                body.extend(message.get("body", b""))
                if len(body) > MAX_API_BODY_BYTES:
                    resp = JSONResponse({"detail": "请求内容过大"}, status_code=413)
                    await resp(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            replayed = False

            async def buffered_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return {"type": "http.disconnect"}

            receive = buffered_receive
        user = db.get_session_user(_cookie_token(headers.get("cookie", "")))
        ctx = set_current_user(user["user_id"] if user else None)
        try:
            sec = load_config().get("security", {}) or {}
            allowed = bool(origin and (ALLOWED_ORIGIN_RE.match(origin)
                                       or origin in (sec.get("allowed_origins") or [])))
            if scope.get("method") == "OPTIONS" and origin:
                resp = Response(status_code=204)
                _apply_cors(resp, origin, allowed)
                await resp(scope, receive, send)
                return
            is_public = (not path.startswith("/api/")) or path in PUBLIC_API_PATHS
            if (scope.get("method") not in ("GET", "HEAD", "OPTIONS") and origin
                    and not _same_origin(origin, headers.get("host", "")) and not allowed):
                resp = JSONResponse({"detail": "请求来源不受信任"}, status_code=403)
                _apply_cors(resp, origin, False)
                await resp(scope, receive, send)
                return
            if not is_public and user is None:
                resp = JSONResponse({"detail": "请先登录"}, status_code=401)
                _apply_cors(resp, origin, allowed)
                await resp(scope, receive, send)
                return
            if (not is_public and sec.get("enabled") and sec.get("token") and origin
                    and not _same_origin(origin, headers.get("host", ""))):
                supplied = ""
                authorization = headers.get("authorization", "")
                if authorization.lower().startswith("bearer "):
                    supplied = authorization[7:].strip()
                supplied = supplied or headers.get("x-local-token", "")
                if supplied != sec["token"]:
                    resp = JSONResponse({"detail": "跨源调用需要有效的本地 token"}, status_code=401)
                    _apply_cors(resp, origin, allowed)
                    await resp(scope, receive, send)
                    return
            if allowed:
                async def send_cors(message):
                    if message["type"] == "http.response.start":
                        hs = [(k, v) for k, v in message.get("headers", [])
                              if k.lower() not in _CORS_HEADER_NAMES]
                        hs.extend([
                            (b"access-control-allow-origin", origin.encode("latin-1")),
                            (b"access-control-allow-credentials", b"true"),
                            (b"access-control-allow-headers",
                             b"Content-Type, Authorization, X-Local-Token"),
                            (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                            (b"vary", b"Origin"),
                        ])
                        message["headers"] = hs
                    await send(message)
                await self.app(scope, receive, send_cors)
            else:
                await self.app(scope, receive, send)
        finally:
            current_user_id.reset(ctx)


app.add_middleware(AccessGuard)


class CachePolicy:
    """页面外壳与静态资源一律 no-cache：每次使用前凭 ETag 回源校验（未变则 304）。

    缺少 Cache-Control 时浏览器按启发式新鲜度（距 Last-Modified 的 10%）直接用缓存、
    不回源校验，发布后旧 app.js 会与新 index.html 配对运行并整页报错（登录失效事故根因）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path in ("/", "/index.html", "/manifest.webmanifest")
                or path.startswith("/static/")):
            return await self.app(scope, receive, send)

        async def send_no_cache(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message["headers"]
                           if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-cache"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_no_cache)


app.add_middleware(CachePolicy)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.webmanifest")
async def pwa_manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/sw.js")
async def pwa_service_worker():
    # 根路径作用域：Service Worker 需放在站点根并显式声明允许的作用域
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/api/status")
async def status():
    cfg = load_config()
    today = date.today()
    week = current_week(cfg.get("semester_start", ""),
                        today, cfg.get("total_weeks", 30))
    snap = db.get_snapshot()
    all_courses = apply_course_overrides(
        [Course.from_dict(d) for d in snap["courses"]] if snap else [],
        cfg.get("course_overrides"))
    exam_snap = db.get_exam_snapshot()
    all_exams = apply_exam_overrides(
        [Exam.from_dict(d) for d in exam_snap["exams"]] if exam_snap else [],
        cfg.get("exam_overrides"))
    upcoming_exams = sorted(
        (e for e in all_exams if not e.is_finished()),
        key=lambda e: (e.date or "9999-12-31", e.start or "23:59"))
    from .config import resolve_course_source, portal_configured, zhengfang_configured
    resolved = resolve_course_source(cfg)
    portal_ok = portal_configured(cfg)
    zf_ok = zhengfang_configured(cfg)
    push_cfg = cfg.get("push", {})
    upcoming_event_count = sum(
        1 for d in db.list_events() if not Event.from_dict(d).is_finished())
    return {
        "school": cfg.get("school", "青岛理工大学"),
        "today": today.isoformat(),
        "weekday": WEEKDAY_CN[today.isoweekday() - 1],
        "week": week,
        "semester_start": cfg.get("semester_start"),
        "total_weeks": cfg.get("total_weeks", 30),
        "provider": resolved,
        "provider_type": resolved,
        "primary": cfg.get("provider", {}).get("primary", "auto"),
        "portal_configured": portal_ok,
        "zhengfang_configured": zf_ok,
        "has_source": bool(portal_ok or zf_ok),
        "is_demo": demo_allowed() and resolved == "mock",
        "course_count": len(all_courses),
        "exam_count": len(all_exams) if exam_snap else 0,
        "upcoming_exam_count": len(upcoming_exams),
        "upcoming_event_count": upcoming_event_count,
        "next_exam": upcoming_exams[0].to_dict() if upcoming_exams else None,
        "exam_fetched_at": exam_snap["fetched_at"] if exam_snap else None,
        "last_sync": snap["fetched_at"] if snap else None,
        "sync_time": sync_slot_hhmm(),
        "next_sync": next_run_time(),
        "push_channels": [c for c, on in (
            ("bark", push_cfg.get("bark_enabled")),
            ("wecom", push_cfg.get("wecom_enabled")),
        ) if on],
    }


@app.get("/api/timetable")
async def timetable(week: int | None = None):
    from .config import portal_configured
    cfg = load_config()
    today = date.today()
    cur_week = current_week(cfg.get("semester_start", ""),
                            today, cfg.get("total_weeks", 30))
    week = week or max(cur_week, 1)
    snap = db.get_snapshot()
    courses = apply_course_overrides(
        [Course.from_dict(d) for d in snap["courses"]] if snap else [],
        cfg.get("course_overrides"))
    in_week = [c for c in courses if c.in_week(week)]
    out = []
    for c in sorted(in_week, key=lambda x: (x.day, x.start)):
        d = c.to_dict()
        d["start_time"] = section_time(cfg, c.start)
        d["end_time"] = section_time(cfg, c.end, end=True)
        out.append(d)
    dates = week_dates(cfg.get("semester_start", ""), week)
    today_s = today.isoformat()
    today_events = [d | {"display_time": Event.from_dict(d).display_time()}
                    for d in db.list_events(date_from=today_s, date_to=today_s)]
    tomorrow = today + timedelta(days=1)
    tomorrow_s = tomorrow.isoformat()
    tomorrow_events = [d | {"display_time": Event.from_dict(d).display_time()}
                       for d in db.list_events(date_from=tomorrow_s, date_to=tomorrow_s)]
    # 考试也是日程的一部分：今日/明日时间线直接带上，考完前一直显示
    exam_snap = db.get_exam_snapshot()
    exam_items = apply_exam_overrides(
        [Exam.from_dict(d) for d in exam_snap["exams"]] if exam_snap else [],
        _exam_overrides(cfg))

    def exams_on_day(day_s: str) -> list[dict]:
        picked = sorted((e for e in exam_items if e.date == day_s),
                        key=lambda e: (e.start or "23:59", e.name))
        return [e.to_dict() | {"finished": e.is_finished(),
                               "display_time": e.display_time()} for e in picked]

    return {
        "week": week,
        "current_week": cur_week,
        "dates": [d.isoformat() for d in dates],
        "today": today.isoformat(),
        "tomorrow": tomorrow_s,
        "is_demo": (snap or {}).get("source") == "mock",
        "has_source": bool(portal_configured(cfg) or zhengfang_configured(cfg)),
        "fetched_at": (snap or {}).get("fetched_at"),
        "section_times": {str(i): section_time(cfg, i) for i in range(1, 13)},
        "section_ends": {str(i): section_time(cfg, i, end=True) for i in range(1, 13)},
        "courses": out,
        "today_courses": [c.to_dict() | {
            "start_time": section_time(cfg, c.start), "end_time": section_time(cfg, c.end, end=True),
        } for c in courses_on(cfg, courses, today)],
        "today_events": today_events,
        "today_exams": exams_on_day(today_s),
        "tomorrow_courses": [c.to_dict() | {
            "start_time": section_time(cfg, c.start), "end_time": section_time(cfg, c.end, end=True),
        } for c in courses_on(cfg, courses, tomorrow)],
        "tomorrow_events": tomorrow_events,
        "tomorrow_exams": exams_on_day(tomorrow_s),
    }


@app.get("/api/weather")
async def get_weather():
    """今明两天的逐小时天气，供课表卡片展示；接口不可用时返回空表。"""
    cfg = load_config()
    w = cfg.get("weather") or {}
    place = str(w.get("place") or "青岛黄岛区").strip() or "青岛黄岛区"
    if w.get("enabled") is False:
        return {"ok": True, "enabled": False, "place": place, "hours": {}}
    return {"ok": True, "enabled": True, "place": place, "hours": await hours_map(cfg)}


@app.get("/api/export/calendar.ics")
async def export_calendar(request: Request):
    """导出整学期课表为 iCalendar（.ics），同时支持日历订阅（webcal:// 自动同步）。

    - 带 ?token=<订阅 token>：定位到具体用户（订阅拉取无 Cookie），供系统日历订阅；
    - 不带 token：走当前登录会话（下载场景），仍按 Cookie 鉴权。
    """
    token = request.query_params.get("token", "").strip()
    sub = _subscribe_user_by_token(token) if token else None
    if token and not sub:
        raise HTTPException(404, "订阅链接无效或已失效，请重新生成订阅链接")

    if sub:
        user_id, raw_cfg = sub
        from .config import DEFAULT_CONFIG, _deep_merge, normalize_provider
        cfg = json.loads(json.dumps(_deep_merge(DEFAULT_CONFIG, raw_cfg)))
        cfg["provider"] = normalize_provider(cfg.get("provider", {}))
        courses = ical.courses_for_export(cfg, user_id=user_id)
        exams = ical.exams_for_export(cfg, user_id=user_id)
    else:
        # 无 token：必须已登录（下载场景）。端点虽在公开白名单（供 webcal 无 Cookie 拉取），
        # 但绝不能回落到全局模板/默认用户，否则会泄露他人课表。
        if get_current_user() is None:
            raise HTTPException(401, "请先登录，或使用有效的订阅链接")
        cfg = load_config()
        courses = ical.courses_for_export(cfg)
        exams = ical.exams_for_export(cfg)
    if not courses and not exams:
        raise HTTPException(404, "课表尚未同步，请先在「课表来源」页同步数据")
    # 逐节附加上课时天气（复用 weather 30 分钟缓存，失败静默降级为无天气）
    try:
        weather_map = await weather.lesson_weather_map(cfg)
    except Exception:  # noqa: BLE001 - 天气不可用不影响日历导出
        weather_map = {}
    body = ical.build_ical(cfg, courses, exams, weather_map=weather_map)
    school = (cfg.get("school", "") or "课程表").replace("/", "-")
    from urllib.parse import quote
    filename = f"{school}-课表.ics"
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"course-schedule.ics\"; "
                f"filename*=UTF-8''{quote(filename)}",
            # 让系统日历识别为可订阅的日历源
            "Cache-Control": "no-cache",
        },
    )


@app.get("/api/export/subscribe")
async def get_subscribe_info(request: Request):
    """返回当前用户的日历订阅信息：订阅 token 与可用的订阅 URL。

    token 不存在时自动生成并落库（绑定当前用户，永不过期）。
    前端据此拼出 webcal:// 与 Google 日历订阅链接。
    """
    cfg = load_config()
    token = (cfg.get("calendar_subscribe_token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        save_config({"calendar_subscribe_token": token})

    # 用请求里的 host 拼绝对地址，兼容本地/线上
    host = request.headers.get("host", "")
    scheme = "https" if not host.startswith(("127.0.0.1", "localhost")) else "http"
    base = f"{scheme}://{host}"
    ics_path = f"/api/export/calendar.ics?token={token}"
    return {
        "token": token,
        "webcal": f"webcal://{host}{ics_path}",
        "https": f"{base}{ics_path}",
        "google": f"https://calendar.google.com/calendar/r?cid=webcal://{host}{ics_path}",
    }


@app.post("/api/export/subscribe/regenerate")
async def regenerate_subscribe_token():
    """重新生成订阅 token（防止链接泄露后他人继续订阅）。旧 token 立即失效。"""
    save_config({"calendar_subscribe_token": secrets.token_urlsafe(32)})
    return {"ok": True}


def _course_overrides(cfg: dict) -> dict:
    ov = cfg.get("course_overrides") or {}
    return {
        "hidden": list(dict.fromkeys(ov.get("hidden") or [])),
        "edited": dict(ov.get("edited") or {}),
        "added": [dict(x) for x in (ov.get("added") or []) if isinstance(x, dict)],
    }


def _course_form(data: dict) -> dict:
    """校验并规范化手动课表表单。"""
    name = str(data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "课程名称不能为空")
    if len(name) > 100:
        raise HTTPException(400, "课程名称太长（最多 100 字）")
    try:
        day = int(data.get("day") or 1)
        start = int(data.get("start") or 1)
        end = int(data.get("end") or start)
    except (TypeError, ValueError):
        raise HTTPException(400, "星期和节次需要填数字")
    if not 1 <= day <= 7:
        raise HTTPException(400, "星期需在 1-7 之间")
    if not 1 <= start <= end <= 12:
        raise HTTPException(400, "节次需在 1-12 之间，且结束不早于开始")
    note = str(data.get("note") or "").strip()
    raw_weeks = data.get("weeks", "")
    if isinstance(raw_weeks, (list, tuple)):
        if len(raw_weeks) > 60:
            raise HTTPException(400, "周次列表过长")
        try:
            weeks = sorted({int(w) for w in raw_weeks if int(w) > 0})
        except (TypeError, ValueError):
            raise HTTPException(400, "周次格式不正确")
        weeks = [w for w in weeks if w <= 60]
    else:
        text = str(raw_weeks or "").strip()
        if len(text) > 500:
            raise HTTPException(400, "周次格式过长")
        try:
            weeks, derived = parse_weeks(text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if text and not weeks:
            raise HTTPException(400, "周次无法识别，请用 1-16、1-15(单)、3,5,7 这类写法")
        if not note:
            note = derived
    teacher = str(data.get("teacher") or "").strip()
    room = str(data.get("room") or "").strip()
    campus = str(data.get("campus") or "").strip()
    note = str(data.get("note") or "").strip()
    if (len(teacher) > 80 or len(room) > 100 or len(campus) > 100
            or len(note) > 500):
        raise HTTPException(400, "教师、地点或备注过长")
    return {
        "name": name, "day": day, "start": start, "end": end,
        "teacher": teacher, "room": room,
        "weeks": weeks,
        "campus": campus,
        "note": note,
    }


def _save_course_overrides(overrides: dict) -> dict:
    if (len(overrides["added"]) > 500 or len(overrides["edited"]) > 2000
            or len(overrides["hidden"]) > 2000):
        raise HTTPException(400, "手动课表调整数量超过上限")
    save_config({"course_overrides": overrides})
    return {
        "ok": True,
        "hidden_count": len(overrides["hidden"]),
        "edited_count": len(overrides["edited"]),
        "added_count": len(overrides["added"]),
        "has_overrides": bool(overrides["hidden"] or overrides["edited"]
                              or overrides["added"]),
    }


@app.get("/api/courses")
async def list_courses():
    """全部生效课程（同步结果 + 手动调整），供编辑课表使用。"""
    cfg = load_config()
    snap = db.get_snapshot()
    raw = [Course.from_dict(d) for d in snap["courses"]] if snap else []
    overrides = _course_overrides(cfg)
    courses = apply_course_overrides(raw, overrides)
    manual_ids = {str(x.get("source_id") or x.get("uid") or "")
                  for x in overrides["added"]}
    out = []
    for c in courses:
        d = c.to_dict()
        d["uid"] = course_uid(c)
        d["manual"] = d["uid"] in manual_ids
        d["start_time"] = section_time(cfg, c.start)
        d["end_time"] = section_time(cfg, c.end, end=True)
        out.append(d)
    out.sort(key=lambda d: (d["day"], d["start"], d["name"]))
    return {
        "courses": out,
        "count": len(out),
        "source": (snap or {}).get("source"),
        "is_demo": (snap or {}).get("source") == "mock",
        "fetched_at": (snap or {}).get("fetched_at"),
        "hidden_count": len(overrides["hidden"]),
        "edited_count": len(overrides["edited"]),
        "added_count": len(overrides["added"]),
        "has_overrides": bool(overrides["hidden"] or overrides["edited"]
                              or overrides["added"]),
    }


class CourseEditRequest(BaseModel):
    action: str                 # add | update | delete | restore | restore_hidden | reset
    uid: str | None = None
    course: dict | None = None


@app.post("/api/courses")
async def edit_course(req: CourseEditRequest):
    """手动增删改课表：改动单独存进配置，下次同步照常更新原始快照。"""
    cfg = load_config()
    overrides = _course_overrides(cfg)
    action = (req.action or "").strip()
    if action == "reset":
        overrides = {"hidden": [], "edited": {}, "added": []}
    elif action == "restore_hidden":
        overrides["hidden"] = []
    elif action == "add":
        data = _course_form(req.course or {})
        sid = f"manual:{uuid.uuid4().hex[:12]}"
        data.update({"source_id": sid, "uid": sid})
        overrides["added"].append(data)
    elif action in ("update", "delete", "restore"):
        uid = (req.uid or "").strip()
        if not uid:
            raise HTTPException(400, "缺少课程标识")
        if len(uid) > 100:
            raise HTTPException(400, "课程标识过长")
        target = next((item for item in overrides["added"]
                       if uid in (item.get("uid"), item.get("source_id"))), None)
        if action == "delete":
            if target is not None:
                overrides["added"].remove(target)
            else:
                overrides["edited"].pop(uid, None)
                if uid not in overrides["hidden"]:
                    overrides["hidden"].append(uid)
        elif action == "restore":
            overrides["hidden"] = [x for x in overrides["hidden"] if x != uid]
            overrides["edited"].pop(uid, None)
        else:
            # 以当前生效课程为底，未提交的字段保持原值
            snap = db.get_snapshot()
            raw = [Course.from_dict(d) for d in snap["courses"]] if snap else []
            current = next((c for c in apply_course_overrides(raw, overrides)
                            if course_uid(c) == uid), None)
            base = current.to_dict() if current is not None else {}
            data = _course_form({**base, **(req.course or {})})
            if target is not None:
                sid = target.get("source_id") or uid
                target.clear()
                target.update(data, source_id=sid, uid=sid)
            else:
                data.update({"source_id": "", "uid": uid})
                overrides["edited"][uid] = data
    else:
        raise HTTPException(400, "未知操作")
    return _save_course_overrides(overrides)


@app.get("/api/search")
async def search_schedule(q: str = ""):
    """跨课程 / 个人日程 / 考试搜索，附所在周次供前端跳转。"""
    cfg = load_config()
    query = (q or "").strip().lower()
    if not query:
        return {"results": []}
    semester_start = cfg.get("semester_start") or ""

    def week_of(day_s: str) -> int:
        try:
            base = date.fromisoformat(semester_start)
            d = date.fromisoformat(day_s or "")
        except ValueError:
            return 0
        return (d - base).days // 7 + 1

    cur_week = current_week(semester_start, date.today(), cfg.get("total_weeks", 30))
    results = []
    snap = db.get_snapshot()
    courses = apply_course_overrides(
        [Course.from_dict(d) for d in snap["courses"]] if snap else [],
        cfg.get("course_overrides"))
    for c in sorted(courses, key=lambda x: (x.day, x.start)):
        if query not in f"{c.name} {c.teacher} {c.room} {c.campus}".lower():
            continue
        jump = cur_week if c.in_week(cur_week) else (min(c.weeks) if c.weeks else cur_week)
        results.append({
            "kind": "course", "title": c.name,
            "meta": f"{WEEKDAY_CN[c.day - 1]} 第 {c.start}–{c.end} 节"
                    + (f" · 第 {min(c.weeks)}–{max(c.weeks)} 周" if c.weeks else ""),
            "sub": " · ".join(filter(None, (c.teacher, c.room, c.campus))),
            "week": jump, "date": "",
        })
    for d in db.list_events():
        if query not in f"{d.get('title', '')} {d.get('location', '')} {d.get('note', '')}".lower():
            continue
        # db.list_events() 返回的是 "start"/"end"（数据库列名才是 start_time/end_time）
        when = d.get("start") or ""
        if d.get("end") and when:
            when = f"{when}–{d['end']}"
        results.append({
            "kind": "event", "title": d.get("title", ""),
            "meta": d.get("date", ""),
            "sub": " · ".join(filter(None, (when or "全天", d.get("location") or ""))),
            "week": week_of(d.get("date", "")), "date": d.get("date", ""),
        })
    exam_snap = db.get_exam_snapshot()
    exams = apply_exam_overrides(
        [Exam.from_dict(x) for x in exam_snap["exams"]],
        cfg.get("exam_overrides")) if exam_snap else []
    for e in sorted(exams, key=lambda x: x.date or "9999"):
        if query not in f"{e.name} {e.room} {e.campus}".lower():
            continue
        results.append({
            "kind": "exam", "title": e.name, "meta": e.date,
            "sub": " · ".join(filter(None, (e.start, e.room))),
            "week": week_of(e.date), "date": e.date,
        })
    return {"results": results[:40], "count": len(results)}


@app.get("/api/events")
async def list_events():
    """个人日程：全部按日期升序，带 finished / display_time 展示字段。"""
    today = date.today()
    out = []
    for d in db.list_events():
        e = Event.from_dict(d)
        out.append(d | {"finished": e.is_finished(), "display_time": e.display_time()})
    return {"events": out, "count": len(out), "today": today.isoformat()}


class EventEditRequest(BaseModel):
    action: str                 # add | update | delete
    id: str | None = None
    event: dict | None = None


def _event_form(data: dict) -> dict:
    """校验并规范化个人日程表单。时间可留空（全天），日期必填。"""
    title = str(data.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "日程标题不能为空")
    if len(title) > 100:
        raise HTTPException(400, "标题太长（最多 100 字）")
    date_s = str(data.get("date") or "").strip()
    try:
        date.fromisoformat(date_s)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    start = str(data.get("start") or "").strip()
    end = str(data.get("end") or "").strip()
    for t in (start, end):
        if t and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise HTTPException(400, "时间格式应为 HH:MM（24 小时制）")
    if end and not start:
        raise HTTPException(400, "填结束时间前请先填开始时间")
    if start and end and end < start:
        raise HTTPException(400, "结束时间不能早于开始时间")
    location = str(data.get("location") or "").strip()
    note = str(data.get("note") or "").strip()
    if len(location) > 100 or len(note) > 500:
        raise HTTPException(400, "地点或备注太长")
    return {"title": title, "date": date_s, "start": start, "end": end,
            "location": location, "note": note}


@app.post("/api/events")
async def edit_event(req: EventEditRequest):
    action = (req.action or "").strip()
    if action == "add":
        data = _event_form(req.event or {})
        return {"ok": True, "event": db.add_event(data)}
    if action in ("update", "delete"):
        event_id = (req.id or "").strip()
        if not event_id:
            raise HTTPException(400, "缺少日程标识")
        if len(event_id) > 100:
            raise HTTPException(400, "日程标识过长")
        current = db.get_event(event_id)
        if current is None:
            raise HTTPException(404, "日程不存在")
        if action == "delete":
            db.delete_event(event_id)
            return {"ok": True}
        data = _event_form({**current, **(req.event or {})})
        return {"ok": True, "event": db.update_event(event_id, data)}
    raise HTTPException(400, "未知操作")


# ---------- 空教室查询（正方教务，短时缓存） ----------

ROOM_META_CACHE_TTL = 6 * 3600
ROOM_FREE_CACHE_TTL = 60


def _rooms_cache_identity(cfg: dict) -> str:
    zf = ((cfg.get("provider") or {}).get("zhengfang") or {})
    identity = "|".join(str(zf.get(k) or "")
                         for k in ("base", "username", "password", "cookie"))
    return f"u{get_current_user() or 0}:{cache.fingerprint(identity)}"


def _rooms_provider(cfg: dict) -> ZhengfangProvider:
    if not zhengfang_configured(cfg):
        raise HTTPException(400, "空教室查询需要正方教务：请先在「课表来源」填写学号密码")
    return ZhengfangProvider(cfg)


@app.get("/api/rooms/meta")
async def rooms_meta(campus: str = ""):
    """校区/教学楼/节次时刻表 + 学期周次范围（日期换算用应用自己的开学日期）。

    未指定 campus 时使用「课表来源」里设置的默认校区；默认校区失效则退回教务第一校区。
    """
    from .providers.base import AuthExpiredError
    cfg = load_config()
    rcfg = cfg.get("rooms") or {}
    default_id = str(rcfg.get("default_campus") or "").strip()
    default_name = str(rcfg.get("default_campus_name") or "").strip()
    semester_start = cfg.get("semester_start", "")
    provider = ZhengfangProvider(cfg)
    requested_campus = campus.strip() or default_id
    shared = room_cache.eligible(provider, semester_start)

    async def get_meta(campus_id: str) -> dict:
        if shared:
            snapshot = await room_cache.get_meta(provider, campus_id)
            if snapshot is not None:
                return snapshot
            # 用户保存的默认校区已被撤下时，可以从共享校区列表确认并退回第一校区。
            if not campus.strip() and campus_id == default_id and campus_id:
                first = await room_cache.get_meta(provider, "")
                if first is not None and all(str(c.get("id")) != campus_id
                                             for c in first["campuses"]):
                    return first
        personal_provider = _rooms_provider(cfg)
        meta_key = (
            f"rooms:meta:v1:{_rooms_cache_identity(cfg)}:{provider.year}:{provider.term}:"
            f"{cache.fingerprint(provider.base)}:{provider.username}:{campus_id}"
        )
        meta = await cache.get(meta_key)
        if not isinstance(meta, dict):
            meta = await personal_provider.fetch_room_meta(campus_id)
            if isinstance(meta, dict) and meta.get("campuses"):
                await cache.set(meta_key, meta, ROOM_META_CACHE_TTL)
        return meta

    try:
        meta = await get_meta(requested_campus)
        ids = {str(c.get("id")) for c in meta.get("campuses") or []}
        if default_id and default_id not in ids:
            # 教务里已经没有这个校区（换了学期或校区代码），别让下拉框空着
            default_id, default_name = "", ""
            if not campus.strip():
                meta = await get_meta("")
    except HTTPException:
        raise
    except AuthExpiredError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"空教室查询失败: {e}")
    today = date.today()
    default_building = _room_default_building(rcfg, meta.get("campus"))
    if not any(str(b.get("id")) == default_building for b in meta.get("buildings") or []):
        default_building = ""
    # 进程内缓存返回原对象，用户专属字段只写入当前响应。
    return {
        **meta,
        "semester_start": semester_start,
        "current_week": current_week(semester_start, today, 40),
        "today": today.isoformat(),
        "today_weekday": today.isoweekday(),
        "default_campus": default_id,
        "default_campus_name": default_name,
        "default_building": default_building,
    }


def _room_default_building(rooms_cfg: dict, campus: str) -> str:
    if campus and campus == str(rooms_cfg.get("default_campus") or "").strip():
        return str(rooms_cfg.get("default_building") or "").strip()
    return ""


@app.get("/api/rooms/free")
async def rooms_free(date_s: str = "", start: int = 1, end: int = 2,
                     campus: str = "", building: str | None = None):
    """某天某节次范围的空教室。日期 → 教学周/星期按开学日期换算。"""
    from .providers.base import AuthExpiredError
    date_s = (date_s or date.today().isoformat()).strip()
    try:
        d = date.fromisoformat(date_s)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    if not (1 <= start <= end <= 12):
        raise HTTPException(400, "节次范围无效")
    cfg = load_config()
    # 未指定校区时用「课表来源」里的默认校区，免得每次查教室都重选
    rcfg = cfg.get("rooms") or {}
    campus = campus.strip() or str(rcfg.get("default_campus") or "").strip()
    semester_start = cfg.get("semester_start", "")
    week = current_week(semester_start, d, 40)
    if week < 1:
        raise HTTPException(400, "该日期还没开学，查不了空教室")
    if week > 30:
        raise HTTPException(400, "该日期超出本学期范围")
    building = _room_default_building(rcfg, campus) if building is None else building.strip()
    response = {"date": date_s, "week": week, "day": d.isoweekday(),
                "start": start, "end": end}
    provider = ZhengfangProvider(cfg)
    if campus and (start, end) in room_cache.SLOTS and room_cache.eligible(provider, semester_start):
        snapshot = await room_cache.get_rooms(provider, d, campus, start, end, building)
        if snapshot is not None:
            rooms = snapshot["data"]
            return {**response, "rooms": rooms, "count": len(rooms),
                    "cache_source": "shared_week", "updated_at": snapshot["updated_at"]}

    shared_key = room_cache.on_demand_key(provider, semester_start, d, campus, building, start, end)
    if shared_key:
        snapshot = await cache.get(shared_key)
        if isinstance(snapshot, dict) and isinstance(snapshot.get("data"), list):
            rooms = snapshot["data"]
            return {**response, "rooms": rooms, "count": len(rooms),
                    "cache_source": "shared_on_demand", "updated_at": snapshot.get("updated_at")}

    provider = _rooms_provider(cfg)
    free_key = (
        f"rooms:free:v1:{_rooms_cache_identity(cfg)}:{provider.year}:{provider.term}:"
        f"{cache.fingerprint(provider.base)}:{provider.username}:{week}:{d.isoweekday()}:"
        f"{start}:{end}:{campus}:{building}"
    )
    share_write = shared_key is not None and room_cache.on_demand_write_eligible(d)
    rooms = None if share_write else await cache.get(free_key)
    try:
        if not isinstance(rooms, list):
            if share_write:
                try:
                    snapshot = await room_cache.fetch_on_demand(
                        shared_key, get_current_user(),
                        lambda: provider.fetch_free_rooms(
                            week, d.isoweekday(), start, end, campus, building, strict=True))
                except room_cache.RoomQueryBusy as e:
                    raise HTTPException(429, str(e))
                rooms = snapshot["data"]
                return {**response, "rooms": rooms, "count": len(rooms),
                        "cache_source": "shared_on_demand",
                        "updated_at": snapshot["updated_at"]}
            else:
                rooms = await provider.fetch_free_rooms(
                    week, d.isoweekday(), start, end, campus, building)
            if isinstance(rooms, list):
                await cache.set(free_key, rooms, ROOM_FREE_CACHE_TTL)
    except AuthExpiredError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"空教室查询失败: {e}")
    return {**response, "rooms": rooms, "count": len(rooms)}


@app.get("/api/rooms/my-free")
async def rooms_my_free(date_s: str = ""):
    """某天没课的节次区间（按课表算好），供前端一键查「我没课的时候」。

    spans 为 null 表示课表尚未同步；为空列表表示全天有课。不依赖正方凭证。
    """
    date_s = (date_s or date.today().isoformat()).strip()
    try:
        d = date.fromisoformat(date_s)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    spans = free_sections(load_config(), d)
    return {"date": date_s,
            "spans": None if spans is None
            else [{"start": a, "end": b} for a, b in spans]}


@app.get("/api/exams")
async def exams():
    """考试安排：未结束的按时间升序在前，已结束的置后（含手动调整）。"""
    from .config import portal_configured
    cfg = load_config()
    p = cfg.get("provider", {})
    ptype = p.get("type", "mock")
    # 演示源不算「能出考试」：没配真实来源就引导去设置，而不是提示「点同步拉取」。
    supported = ptype == "zhengfang" or zhengfang_configured(cfg) or (
        demo_allowed() and ptype == "mock")
    has_source = bool(zhengfang_configured(cfg) or portal_configured(cfg))
    overrides = _exam_overrides(cfg)
    snap = db.get_exam_snapshot()
    items = apply_exam_overrides(
        [Exam.from_dict(d) for d in snap["exams"]] if snap else [], overrides)
    items.sort(key=lambda e: (e.is_finished(), e.date or "9999-12-31",
                              e.start or "23:59", e.name))
    manual_ids = {str(x.get("source_id") or x.get("uid") or "")
                  for x in overrides["added"]}
    out = []
    for e in items:
        d = e.to_dict()
        d["uid"] = exam_uid(e)
        d["manual"] = d["uid"] in manual_ids
        d["finished"] = e.is_finished()
        d["display_time"] = e.display_time()
        out.append(d)
    upcoming = [d for d in out if not d["finished"]]
    finished = [d for d in out if d["finished"]]
    return {
        "exams": out,
        "upcoming": upcoming,
        "finished": finished,
        "today": date.today().isoformat(),
        "provider": ptype,
        "source": (snap or {}).get("source"),
        "fetched_at": (snap or {}).get("fetched_at"),
        "is_demo": demo_allowed() and (snap or {}).get("source") == "mock",
        "supported": supported,
        "has_source": has_source,
        "hidden_count": len(overrides["hidden"]),
        "edited_count": len(overrides["edited"]),
        "added_count": len(overrides["added"]),
        "has_overrides": bool(overrides["hidden"] or overrides["edited"]
                              or overrides["added"]),
    }


def _score_term_key(year, term) -> str:
    return f"{str(year or '')}|{str(term or '')}"


def _score_term_meta(year, term, count: int = 0, current: bool = False) -> dict:
    """成绩学期下拉项：短标签用于标题，完整标签用于提示/分组。"""
    year, term = str(year or ""), str(term or "")
    name = {"3": "第一学期", "12": "第二学期"}.get(
        term, f"第 {term} 学期" if term else "学期")
    try:
        short = f"{int(year)}-{int(year) + 1} {name}"
    except (TypeError, ValueError):
        short = name
    return {
        "key": _score_term_key(year, term),
        "year": year,
        "term": term,
        "label": short,
        "full_label": Score(name="", year=year, term=term).term_label(),
        "count": count,
        "is_current": current,
    }


def _score_term_sort(meta: dict) -> tuple:
    try:
        year = int(meta.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    order = {"3": 1, "12": 2}.get(str(meta.get("term") or ""), 9)
    return year, order


def _score_term_options(items: list[dict], current: dict) -> list[dict]:
    """当前学期即使还没出成绩也要出现在下拉框里，否则用户无法确认「本学期」。"""
    counts: dict[str, int] = {}
    for row in items:
        key = _score_term_key(row.get("year"), row.get("term"))
        counts[key] = counts.get(key, 0) + 1
    metas = {current["key"]: dict(current)}
    for key in counts:
        if key not in metas:
            year, term = key.split("|", 1)
            metas[key] = _score_term_meta(year, term)
    for key, meta in metas.items():
        meta["count"] = counts.get(key, 0)
        meta["is_current"] = key == current["key"]
    return sorted(metas.values(), key=_score_term_sort, reverse=True)


@app.get("/api/grades")
async def grades():
    """考试成绩（只读）：从正方教务同步，按学期分组展示。"""
    cfg = load_config()
    p = cfg.get("provider", {})
    ptype = p.get("type", "mock")
    # 演示源不提供成绩：只有正方（配了凭证）才算「能出成绩」
    from .config import portal_configured
    supported = ptype == "zhengfang" or zhengfang_configured(cfg)
    has_source = bool(zhengfang_configured(cfg) or portal_configured(cfg))
    cur_year, cur_term = default_term()
    current_term = _score_term_meta(cur_year, cur_term, current=True)
    # 最近一次成绩同步失败原因。同步日志按当前用户隔离、按时间倒序：
    # 若比失败更新的日志里已有「N 门成绩」，说明接口已恢复，不再提示错误。
    score_error = ""
    for row in (db.get_logs(limit=30).get("sync") or []):
        msg = str(row.get("message") or "")
        if "门成绩" in msg:
            break
        if "成绩拉取失败" in msg:
            score_error = msg.split("成绩拉取失败", 1)[1].strip("（ ）:：")
            break
    snap = db.get_score_snapshot()
    # 兼容旧版本遗留的演示快照：一律不展示，等同「还没拉到真实成绩」
    demo_snap = bool(snap) and snap.get("source") == "mock"
    if not snap or demo_snap:
        return {
            "scores": [], "groups": [], "source": None, "fetched_at": None,
            "provider": ptype, "is_demo": demo_allowed() and (demo_snap or ptype == "mock"),
            "supported": supported, "has_source": has_source,
            "current_term": current_term, "terms": [current_term],
            "stats": {"count": 0, "avg": None, "max": None}, "error": score_error,
        }
    items = []
    for d in snap["scores"]:
        s = Score.from_dict(d)
        row = s.to_dict()
        row["uid"] = score_uid(s)
        row["term_label"] = s.term_label()
        items.append(row)
    # 按学年+学期倒序分组（新学期在前），组内按课程名排序
    terms = sorted({(r.get("year", ""), r.get("term", "")) for r in items},
                   key=lambda t: _score_term_sort({"year": t[0], "term": t[1]}),
                   reverse=True)
    groups = []
    for year, term in terms:
        sub = [r for r in items if r.get("year") == year and r.get("term") == term]
        sub.sort(key=lambda r: r.get("name", ""))
        group_nums = []
        for r in sub:
            try:
                v = float(r.get("score", ""))
            except (TypeError, ValueError):
                continue
            if 0 <= v <= 150:
                group_nums.append(v)
        groups.append({
            "year": year, "term": term,
            "label": sub[0]["term_label"] if sub else "",
            "scores": sub,
            "avg": round(sum(group_nums) / len(group_nums), 1) if group_nums else None,
        })
    # 数值成绩的简单统计（等级制如「优秀」不计入）
    nums = []
    for r in items:
        try:
            v = float(r.get("score", ""))
        except (TypeError, ValueError):
            continue
        if 0 <= v <= 150:
            nums.append(v)
    stats = {
        "count": len(items),
        "avg": round(sum(nums) / len(nums), 1) if nums else None,
        "max": max(nums) if nums else None,
    }
    return {
        "scores": items,
        "groups": groups,
        "today": date.today().isoformat(),
        "provider": ptype,
        "source": (snap or {}).get("source"),
        "fetched_at": (snap or {}).get("fetched_at"),
        "is_demo": demo_allowed() and (snap or {}).get("source") == "mock",
        "supported": supported,
        "has_source": has_source,
        "current_term": current_term,
        "terms": _score_term_options(items, current_term),
        "stats": stats,
        "error": score_error,
    }


def _exam_overrides(cfg: dict) -> dict:
    ov = cfg.get("exam_overrides") or {}
    return {
        "hidden": list(dict.fromkeys(ov.get("hidden") or [])),
        "edited": dict(ov.get("edited") or {}),
        "added": [dict(x) for x in (ov.get("added") or []) if isinstance(x, dict)],
    }


def _exam_form(data: dict) -> dict:
    """校验并规范化手动考试表单。"""
    name = str(data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "考试名称不能为空")
    if len(name) > 100:
        raise HTTPException(400, "考试名称太长（最多 100 字）")
    date_s = str(data.get("date") or "").strip()
    try:
        date.fromisoformat(date_s)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    start = str(data.get("start") or "").strip()
    end = str(data.get("end") or "").strip()
    for t in (start, end):
        if t and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise HTTPException(400, "时间格式应为 HH:MM（24 小时制）")
    if end and not start:
        raise HTTPException(400, "填结束时间前请先填开始时间")
    if start and end and end < start:
        raise HTTPException(400, "结束时间不能早于开始时间")
    room = str(data.get("room") or "").strip()
    seat = str(data.get("seat") or "").strip()
    if len(room) > 100 or len(seat) > 50:
        raise HTTPException(400, "考场或座位信息太长")
    note = str(data.get("note") or "").strip()
    campus = str(data.get("campus") or "").strip()
    if len(note) > 500:
        raise HTTPException(400, "备注太长")
    if len(campus) > 100:
        raise HTTPException(400, "校区信息太长")
    return {"name": name, "date": date_s, "start": start, "end": end,
            "room": room, "seat": seat,
            "campus": campus, "note": note}


def _save_exam_overrides(overrides: dict) -> dict:
    if (len(overrides["added"]) > 500 or len(overrides["edited"]) > 2000
            or len(overrides["hidden"]) > 2000):
        raise HTTPException(400, "手动考试调整数量超过上限")
    save_config({"exam_overrides": overrides})
    return {
        "ok": True,
        "hidden_count": len(overrides["hidden"]),
        "edited_count": len(overrides["edited"]),
        "added_count": len(overrides["added"]),
        "has_overrides": bool(overrides["hidden"] or overrides["edited"]
                              or overrides["added"]),
    }


class ExamEditRequest(BaseModel):
    action: str                 # add | update | delete | restore | restore_hidden | reset
    uid: str | None = None
    exam: dict | None = None


@app.post("/api/exams")
async def edit_exam(req: ExamEditRequest):
    """手动增删改考试：改动单独存进配置，下次同步照常更新原始快照。"""
    cfg = load_config()
    overrides = _exam_overrides(cfg)
    action = (req.action or "").strip()
    if action == "reset":
        overrides = {"hidden": [], "edited": {}, "added": []}
    elif action == "restore_hidden":
        overrides["hidden"] = []
    elif action == "add":
        data = _exam_form(req.exam or {})
        sid = f"manual:{uuid.uuid4().hex[:12]}"
        data.update({"source_id": sid, "uid": sid, "time": ""})
        overrides["added"].append(data)
    elif action in ("update", "delete", "restore"):
        uid = (req.uid or "").strip()
        if not uid:
            raise HTTPException(400, "缺少考试标识")
        if len(uid) > 100:
            raise HTTPException(400, "考试标识过长")
        target = next((item for item in overrides["added"]
                       if uid in (item.get("uid"), item.get("source_id"))), None)
        if action == "delete":
            if target is not None:
                overrides["added"].remove(target)
            else:
                overrides["edited"].pop(uid, None)
                if uid not in overrides["hidden"]:
                    overrides["hidden"].append(uid)
        elif action == "restore":
            overrides["hidden"] = [x for x in overrides["hidden"] if x != uid]
            overrides["edited"].pop(uid, None)
        else:
            # 以当前生效考试为底，未提交的字段保持原值
            snap = db.get_exam_snapshot()
            raw = [Exam.from_dict(d) for d in snap["exams"]] if snap else []
            current = next((e for e in apply_exam_overrides(raw, overrides)
                            if exam_uid(e) == uid), None)
            base = current.to_dict() if current is not None else {}
            data = _exam_form({**base, **(req.exam or {})})
            if target is not None:
                sid = target.get("source_id") or uid
                target.clear()
                target.update(data, source_id=sid, uid=sid, time="")
            else:
                data.update({"source_id": "", "uid": uid})
                overrides["edited"][uid] = data
    else:
        raise HTTPException(400, "未知操作")
    return _save_exam_overrides(overrides)


@app.post("/api/sync")
async def sync_now():
    result = await run_sync(push=True)
    return result


class IngestRequest(BaseModel):
    payload: Any
    list_path: str | None = None
    mapping: dict | None = None
    push: bool = False
    source: str = "ingest"


@app.post("/api/ingest")
async def ingest(req: IngestRequest):
    """外部登录容器提交课表 JSON（App WebView / 浏览器扩展 / 系统浏览器脚本）。

    登录态留在容器里，只把数据传进来；解析、变更检测、快照与推送全部复用现有链路。
    不猜得出数组路径时可显式传 list_path / mapping。
    """
    from .curl_import import analyze_payload
    from .providers.httpjson import courses_from_payload

    if req.mapping is not None:
        if not isinstance(req.mapping, dict) or len(req.mapping) > 20:
            raise HTTPException(400, "mapping 必须是字段数不超过 20 的对象")
        for key, value in req.mapping.items():
            if not isinstance(key, str) or not isinstance(value, str) or len(value) > 200:
                raise HTTPException(400, "mapping 的字段名和路径必须是短字符串")
    source = _req_str(req.source or "ingest", "source", 40)
    analysis = analyze_payload(req.payload)
    if analysis["kind"] == "events":
        return {"ok": False,
                "message": "收到的是「按日期的日程事件流」，现有课表模型只表达周期性课程，需要事件适配器；请把这份结构发给助手。",
                "analysis": analysis}
    list_path = req.list_path if req.list_path is not None else analysis["suggested_path"]
    list_path = _req_str(list_path or "", "list_path", 200)
    mapping = req.mapping if req.mapping is not None else analysis["mapping"]
    try:
        courses = courses_from_payload(req.payload, list_path, mapping)
    except ValueError as e:
        return {"ok": False, "message": str(e), "analysis": analysis,
                "list_path": list_path, "mapping": mapping}
    if len(courses) > 2000:
        return {"ok": False, "message": "单次最多导入 2000 门课程", "analysis": analysis}
    result = await apply_snapshot(load_config(), courses, source, req.push)
    result.update(
        {"analysis": analysis, "list_path": list_path, "mapping": mapping})
    return result


@app.get("/api/logs")
async def logs(limit: int = 50):
    # 负数会让 SQLite 的 LIMIT -1 变成「不限」，必须夹紧
    return db.get_logs(max(1, min(limit, 500)))


_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _req_int(v, name, lo, hi):
    if isinstance(v, bool):
        raise HTTPException(400, f"{name} 必须是整数")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} 必须是整数")
    if n < lo or n > hi:
        raise HTTPException(400, f"{name} 需在 {lo}–{hi} 之间")
    return n


def _req_float(v, name, lo, hi):
    if isinstance(v, bool):
        raise HTTPException(400, f"{name} 必须是数字")
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} 必须是数字")
    if n < lo or n > hi:
        raise HTTPException(400, f"{name} 需在 {lo}–{hi} 之间")
    return n


def _req_bool(v, name):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in (
        "true", "1", "yes", "on", "false", "0", "no", "off"):
        return v.strip().lower() in ("true", "1", "yes", "on")
    raise HTTPException(400, f"{name} 必须是布尔值")


def _req_str(v, name, max_len=4000):
    if not isinstance(v, str):
        raise HTTPException(400, f"{name} 必须是字符串")
    if len(v) > max_len:
        raise HTTPException(400, f"{name} 过长（上限 {max_len}）")
    return v


def _req_hhmm(v, name):
    s = _req_str(v, name, 5)
    if not _HHMM_RE.match(s):
        raise HTTPException(400, f"{name} 需为 HH:MM 格式")
    return s


def _validate_settings(cfg: dict) -> dict:
    """收敛 /api/settings 入参类型，避免字符串塞进数值字段后在下游 500。

    只校验请求里实际出现的键（保存是合并语义），返回深拷贝。
    """
    if not isinstance(cfg, dict):
        raise HTTPException(400, "配置必须是 JSON 对象")
    out = json.loads(json.dumps(cfg))

    if "total_weeks" in out:
        out["total_weeks"] = _req_int(out["total_weeks"], "total_weeks", 1, 60)
    if "sync_time" in out:
        out["sync_time"] = _req_hhmm(out["sync_time"], "sync_time")
    if out.get("semester_start"):
        if not _YMD_RE.match(_req_str(out["semester_start"], "semester_start", 10)):
            raise HTTPException(400, "semester_start 需为 YYYY-MM-DD 格式")
    for key in ("push_next_day", "alert_on_fail"):
        if key in out:
            out[key] = _req_bool(out[key], key)

    if "rooms" in out:
        rooms_cfg = out["rooms"]
        if not isinstance(rooms_cfg, dict):
            raise HTTPException(400, "rooms 必须是 JSON 对象")
        for key in ("default_campus", "default_campus_name", "default_building"):
            if key in rooms_cfg:
                rooms_cfg[key] = _req_str(rooms_cfg[key], f"rooms.{key}", 120).strip()
        if "default_campus" in rooms_cfg and not rooms_cfg["default_campus"]:
            rooms_cfg["default_building"] = ""

    push = out.get("push")
    if isinstance(push, dict):
        for key in ("bark_enabled", "wecom_enabled", "reminder_enabled",
                    "exam_review_enabled", "daily_courses", "daily_events", "daily_weather"):
            if key in push:
                push[key] = _req_bool(push[key], f"push.{key}")
        if "reminder_advance" in push:
            push["reminder_advance"] = _req_int(
                push["reminder_advance"], "push.reminder_advance", 0, 180)
        if "exam_review_days" in push:
            push["exam_review_days"] = _req_int(
                push["exam_review_days"], "push.exam_review_days", 0, 90)
        if "daily_time" in push:
            push["daily_time"] = _req_hhmm(push["daily_time"], "push.daily_time")
        if push.get("bark_server") and push["bark_server"] != MASK:
            bs = _req_str(push["bark_server"], "push.bark_server", 500)
            try:
                assert_safe_outbound_url(bs)
            except UnsafeURLError as e:
                raise HTTPException(400, f"Bark 服务器地址不安全：{e}")

    weather_cfg = out.get("weather")
    if isinstance(weather_cfg, dict):
        if "enabled" in weather_cfg:
            weather_cfg["enabled"] = _req_bool(weather_cfg["enabled"], "weather.enabled")
        if "lat" in weather_cfg:
            weather_cfg["lat"] = _req_float(weather_cfg["lat"], "weather.lat", -90, 90)
        if "lon" in weather_cfg:
            weather_cfg["lon"] = _req_float(weather_cfg["lon"], "weather.lon", -180, 180)

    for key in ("section_times", "section_ends"):
        sec = out.get(key)
        if isinstance(sec, dict):
            clean = {}
            for k, v in sec.items():
                if v in (None, ""):
                    continue
                clean[str(k)] = _req_hhmm(str(v), f"{key}.{k}")
            out[key] = clean
        elif sec not in (None, {}):
            raise HTTPException(400, f"{key} 必须是「节次→时间」的对象")

    prov = out.get("provider")
    if isinstance(prov, dict):
        if "primary" in prov and prov["primary"] not in ("auto", "portal", "zhengfang"):
            raise HTTPException(400, "provider.primary 只能是 auto/portal/zhengfang")
        portal = prov.get("portal")
        if isinstance(portal, dict):
            if "method" in portal:
                m = str(portal["method"]).upper()
                if m not in ("GET", "POST"):
                    raise HTTPException(400, "provider.portal.method 只能是 GET/POST")
                portal["method"] = m
            url = portal.get("url")
            if url and url != MASK:
                try:
                    assert_safe_outbound_url(str(url))
                except UnsafeURLError as e:
                    raise HTTPException(400, f"课表接口地址不安全：{e}")

    return out


@app.get("/api/settings")
async def get_settings():
    return masked_config(load_config())


@app.post("/api/settings")
async def post_settings(cfg: dict):
    if "chaoxing" in cfg:
        raise HTTPException(400, "请通过学习通专用接口管理绑定和设置")
    cfg = _validate_settings(cfg)
    hook = str(((cfg or {}).get("push") or {}).get("wecom_webhook") or "")
    if hook and hook != MASK:
        bad = wecom_input_error(hook)
        if bad:
            raise HTTPException(400, bad)
    try:
        saved = save_config(cfg)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    reschedule(saved)
    return masked_config(saved)


async def _chaoxing_operation(operation, *args):
    try:
        return await operation(get_current_user(), *args)
    except notifications.BusyError as exc:
        raise HTTPException(409, str(exc)) from None
    except notifications.OperationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@app.get("/api/chaoxing/status")
def get_chaoxing_status():
    return notifications.status(get_current_user())


@app.post("/api/chaoxing/login")
async def post_chaoxing_login(request: Request):
    # 手动校验，避免默认验证错误把账号密码原样放进响应。
    try:
        payload = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(400, "请求必须为 JSON 对象") from None
    required = {"username", "password"}
    allowed = required | {"captcha"}
    if not isinstance(payload, dict) or not (required <= set(payload) <= allowed):
        raise HTTPException(400, "请提供学习通账号、密码，可选验证码")
    username = _req_str(payload["username"], "账号", 128).strip()
    password = _req_str(payload["password"], "密码", 256)
    captcha = _req_str(payload.get("captcha", ""), "验证码", 32).strip()
    if not username or not password.strip():
        raise HTTPException(400, "请填写学习通账号和密码")
    try:
        username.encode("utf-8")
        password.encode("utf-8")
    except UnicodeError:
        raise HTTPException(400, "学习通账号或密码包含无效字符") from None
    return await _chaoxing_operation(notifications.login, username, password, captcha)


async def _sms_payload(request: Request, field: str) -> tuple[str, str]:
    try:
        payload = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(400, "请求必须为 JSON 对象") from None
    if not isinstance(payload, dict) or set(payload) != {"username", field}:
        raise HTTPException(400, "短信登录参数不完整或包含未知字段")
    username, value = payload["username"], payload[field]
    if not isinstance(username, str) or not re.fullmatch(r"1[0-9]{10}", username):
        raise HTTPException(400, "请输入 11 位手机号")
    if not isinstance(value, str):
        raise HTTPException(400, "验证码参数必须是字符串")
    if field == "code":
        valid = re.fullmatch(r"[0-9]{6}", value)
    else:
        valid = 1 <= len(value) <= 4096 and value.strip() == value and bool(value.strip())
    if not valid:
        raise HTTPException(400, "请输入 6 位短信验证码" if field == "code" else "请先完成人工验证")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise HTTPException(400, "验证码参数包含无效字符") from None
    return username, value


@app.post("/api/chaoxing/sms/send")
async def post_chaoxing_sms_send(request: Request):
    username, validate = await _sms_payload(request, "validate")
    return await _chaoxing_operation(notifications.send_sms, username, validate)


@app.post("/api/chaoxing/sms/login")
async def post_chaoxing_sms_login(request: Request):
    username, code = await _sms_payload(request, "code")
    return await _chaoxing_operation(notifications.login_sms, username, code)


@app.post("/api/chaoxing/disconnect")
async def post_chaoxing_disconnect():
    return await _chaoxing_operation(notifications.disconnect)


@app.post("/api/chaoxing/options")
async def post_chaoxing_options(options: dict):
    if options.keys() - {"enabled", "push_enabled"}:
        raise HTTPException(400, "仅支持 enabled 和 push_enabled")
    if any(type(v) is not bool for v in options.values()):
        raise HTTPException(400, "学习通开关必须是布尔值")
    return await _chaoxing_operation(notifications.set_options, options)


@app.get("/api/notifications")
def get_notifications(kind: Literal["all", "homework", "notice", "expired"] = "all",
                      unread: Literal["true", "false"] = "false",
                      limit: str = Query("100", pattern=r"^[1-9][0-9]{0,2}$")):
    count = int(limit)
    if count > 500:
        raise HTTPException(400, "limit 需在 1–500 之间")
    return notifications.list_items(get_current_user(), kind, unread == "true", count)


@app.post("/api/notifications/homework-status")
def post_homework_status(payload: dict):
    if payload.keys() != {"id", "status"}:
        raise HTTPException(400, "仅支持且必须提供 id 和 status")
    notification_id, status = payload["id"], payload["status"]
    if type(notification_id) is not int or not 1 <= notification_id <= 2**63 - 1:
        raise HTTPException(400, "id 必须是有效作业 ID")
    if not isinstance(status, str) or status not in {"completed", "pending", ""}:
        raise HTTPException(400, "status 必须是 completed、pending 或空字符串")
    try:
        return db.set_homework_status(get_current_user(), notification_id, status)
    except LookupError:
        raise HTTPException(404, "作业不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/notifications/read")
def post_notifications_read(payload: dict):
    if payload.keys() - {"ids", "all"}:
        raise HTTPException(400, "仅支持 ids 和 all")
    all_items = payload.get("all", False)
    ids = payload.get("ids", [])
    if type(all_items) is not bool:
        raise HTTPException(400, "all 必须是布尔值")
    if (not isinstance(ids, list) or len(ids) > 500
            or any(type(i) is not int or not 1 <= i <= 2**63 - 1 for i in ids)):
        raise HTTPException(400, "ids 必须是最多含 500 个有效通知 ID 的数组")
    updated = db.mark_notifications_read(get_current_user(), ids, all_items)
    return {"ok": True, "updated": updated}


@app.post("/api/notifications/sync")
async def post_notifications_sync():
    return await _chaoxing_operation(notifications.sync)


class ZfTestRequest(BaseModel):
    provider: dict | None = None


@app.post("/api/provider/test-zf")
async def test_zhengfang(req: ZfTestRequest):
    """用当前表单里的正方凭证试登录并各拉一次数据，不保存配置、不写快照。"""
    cfg = load_config()
    prov_cfg = dict(cfg.get("provider") or {})
    if req.provider:
        for key in ("zf_base", "zf_username", "zf_password",
                    "zf_cookie", "zf_year", "zf_term"):
            value = req.provider.get(key)
            # 空值与前端回传的掩码都表示「沿用已保存的值」
            if value in (None, "", MASK):
                continue
            prov_cfg[key] = value
    cfg = dict(cfg, provider=prov_cfg)
    try:
        provider = ZhengfangProvider(cfg)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    try:
        async with provider._session() as client:
            courses = await provider._fetch_courses(client)
            exams: list = []
            exam_error = ""
            try:
                exams = await provider._fetch_exams(client)
            except Exception as e:  # noqa: BLE001
                exam_error = str(e)
        message = f"登录成功：{len(courses)} 门课程"
        message += f"，{len(exams)} 场考试" if not exam_error else "；考试接口失败"
        return {"ok": True, "message": message, "course_count": len(courses),
                "exam_count": len(exams), "exam_error": exam_error}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}


class PushTest(BaseModel):
    pass


class CurlImport(BaseModel):
    curl: str


class PayloadAnalyze(BaseModel):
    text: str


@app.post("/api/provider/import-curl")
async def import_curl(req: CurlImport):
    """粘贴浏览器 Copy as cURL → 自动配置接口 + 猜测字段映射 + 试拉取。"""
    import json as _json

    from .curl_import import analyze_payload, diagnose_url, guess_mapping, parse_curl
    from .providers.base import AuthExpiredError

    try:
        parsed = parse_curl(req.curl)
    except ValueError as e:
        raise HTTPException(400, str(e))
    cfg = load_config()
    p = cfg["provider"]
    portal = dict(p.get("portal") or {})
    portal["url"] = parsed["url"]
    portal["method"] = parsed["method"]
    portal["headers"] = parsed["headers"]
    portal["body"] = parsed["body"]
    p["portal"] = portal
    host = (urlsplit(parsed["url"]).hostname or "").lower()
    p["type"] = "portal" if (host == "qut.edu.cn" or host.endswith(".qut.edu.cn")) else "httpjson"
    p["primary"] = "auto"
    warnings = diagnose_url(parsed["url"])
    lower_headers = {str(k).lower() for k in parsed["headers"]}
    if not lower_headers & {"cookie", "authorization", "x-token", "x-access-token", "token"}:
        warnings.append(
            "cURL 里没检测到 Cookie 或 Authorization，门户接口通常需要登录态；请确认复制的是登录后的请求。")
    result = {"ok": False, "url": parsed["url"], "method": parsed["method"],
              "list_path": "", "mapping": {}, "course_count": 0, "sample": None,
              "raw_preview": "", "error": None, "analysis": None, "warnings": warnings}
    try:
        kind = "httpjson" if cfg["provider"].get("type") == "httpjson" else "portal"
        provider = get_provider(cfg, kind)
        payload = await provider.fetch_raw()
    except AuthExpiredError as e:
        result["error"] = str(e)
        return result
    except Exception as e:  # noqa: BLE001
        result["error"] = f"试拉取失败：{e}"
        return result
    path, mapping = guess_mapping(payload)
    portal["list_path"] = path
    portal["mapping"] = mapping
    save_config({"provider": {"portal": portal, "type": p["type"], "primary": "auto"}})
    result.update({"list_path": path, "mapping": mapping,
                   "raw_preview": _json.dumps(payload, ensure_ascii=False)[:4000],
                   "analysis": analyze_payload(payload)})
    try:
        courses = await get_provider(load_config()).fetch()
        result.update({"ok": True, "course_count": len(courses),
                       "sample": courses[0].to_dict() if courses else None})
    except ValueError as e:
        result["error"] = f"接口已连通，但字段映射需调整：{e}"
    return result


@app.post("/api/provider/analyze")
async def analyze_response(req: PayloadAnalyze):
    """离线分析浏览器 Response 面板复制的 JSON：不需登录态、不写配置。"""
    from .curl_import import analyze_payload, parse_payload_text

    try:
        payload = parse_payload_text(req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return analyze_payload(payload)


@app.post("/api/provider/browser-login/start")
async def browser_login_start():
    """打开本机 Chrome 官方登录窗口，登录后自动发现课表接口并写回配置。"""
    return browser_login.start()


@app.get("/api/provider/browser-login/status")
async def browser_login_status():
    return browser_login.get_status()


@app.post("/api/provider/browser-login/stop")
async def browser_login_stop():
    return browser_login.stop()


@app.post("/api/provider/browser-login/force")
async def browser_login_force():
    """人工确认已登录，跳过页面识别直接开始抓取接口。"""
    return browser_login.force_collect()


@app.post("/api/provider/preview")
async def provider_preview():
    """用当前配置试拉取，返回原始响应预览与解析结果（不保存）。"""
    import json as _json

    from .curl_import import analyze_payload, diagnose_url, guess_mapping

    cfg = load_config()
    try:
        kind = "httpjson" if cfg["provider"].get("type") == "httpjson" else "portal"
        provider = get_provider(cfg, kind)
        payload = await provider.fetch_raw()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    path, mapping = guess_mapping(payload)
    try:
        courses = await provider.fetch()
        parsed = {"count": len(
            courses), "sample": courses[0].to_dict() if courses else None}
    except ValueError as e:
        parsed = {"count": 0, "error": str(e)}
    return {"raw_preview": _json.dumps(payload, ensure_ascii=False)[:4000],
            "current_list_path": cfg["provider"].get("portal", {}).get("list_path", ""),
            "current_mapping": cfg["provider"].get("portal", {}).get("mapping", {}),
            "guessed_list_path": path, "guessed_mapping": mapping, "parsed": parsed,
            "analysis": analyze_payload(payload),
            "warnings": diagnose_url(cfg["provider"].get("portal", {}).get("url", ""))}


class PushTestRequest(BaseModel):
    channel: str = ""


@app.get("/api/push/log")
async def push_log(limit: int = 10):
    return {"logs": db.list_push_logs(max(1, min(limit, 200)))}


@app.post("/api/push/test")
async def push_test(req: PushTestRequest | None = None):
    cfg = load_config()
    channel = (req.channel if req else "").strip()
    title = "测试推送"
    body = "Agenda 推送通道测试成功。\n每日定时同步后会推送课表变更与次日课程、日程安排。"
    if channel:
        results = await send_selected(cfg, title, body, [channel])
    else:
        results = await push_all(cfg, title, body)
    return {"results": results}


@app.post("/api/security/token")
async def generate_local_token():
    import secrets
    return {"token": secrets.token_urlsafe(24)}


# ---------- 多用户：注册 / 登录 / 会话 / 邀请码 ----------

def _public_user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"],
            "display_name": u.get("display_name") or u["username"],
            "student_id": u.get("student_id", ""), "role": u["role"],
            "created_at": u.get("created_at", ""),
            "last_login_at": u.get("last_login_at", "")}


def _session_cookie(resp: JSONResponse, request: Request, token: str):
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL_SECONDS,
                    httponly=True, samesite="lax", path="/",
                    secure=request.url.scheme == "https")


async def _sync_after_register(uid: int) -> None:
    """注册选填了正方凭证时，在新用户上下文里后台同步一次课表。

    注册接口本身没有会话上下文（current_user_id 为 None），必须显式 set，
    否则 load_config / 快照会落到全局配置或 uid=1，造成串号。
    """
    ctx = set_current_user(uid)
    try:
        await run_sync(push=False)
    except Exception:  # noqa: BLE001  # 同步失败只记日志，不影响已创建的账号
        import logging
        logging.getLogger(__name__).exception("注册后首次同步失败 uid=%s", uid)
    finally:
        current_user_id.reset(ctx)


class RegisterRequest(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)
    display_name: str = Field(default="", max_length=40)
    student_id: str = Field(default="", max_length=32)
    zhengfang_password: str = Field(default="", max_length=256)
    invite: str = Field(default="", max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)


@app.get("/api/auth/status")
async def auth_status():
    uid = get_current_user()
    user = db.get_user_by_id(uid) if uid else None
    total = db.count_users()
    return {"authenticated": bool(user), "user": _public_user(user) if user else None,
            "needs_setup": total == 0, "invite_required": total > 0}


@app.post("/api/auth/register")
async def auth_register(req: RegisterRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not auth.consume_rate_limit(f"register:{client_ip}"):
        raise HTTPException(429, "注册尝试过于频繁，请一分钟后再试")
    err = auth.validate_credentials(req.username, req.password)
    if err:
        raise HTTPException(400, err)
    username = req.username.strip()
    invite_code = _normalize_invite_code(req.invite)
    err = db.registration_error(username, invite_code)
    if err:
        raise HTTPException(400, err)
    hashed = await run_in_threadpool(auth.hash_password, req.password)
    try:
        uid, _ = db.register_user_atomic(
            username, hashed, (req.display_name or "").strip()[:40],
            (req.student_id or "").strip()[:32], invite_code)
    except sqlite3.IntegrityError:
        raise HTTPException(400, "用户名已存在")
    except ValueError as e:
        raise HTTPException(400, str(e))
    template = global_config()
    clean = json.loads(json.dumps(DEFAULT_CONFIG))
    for key in ("school", "semester_start", "total_weeks", "section_times",
                "section_ends", "weather", "rooms"):
        clean[key] = template.get(key, clean.get(key))
    clean["security"] = {"enabled": False, "token": "", "allowed_origins": []}
    clean["chaoxing"] = dict(DEFAULT_CONFIG["chaoxing"])

    # 注册选填正方凭据后先使用正方；auto 允许后续接入门户时立即优先门户。
    # 只在两项都填了时才配置，避免「有密码没学号」产生半成品凭证。
    sid = (req.student_id or "").strip()
    zf_pwd = (req.zhengfang_password or "").strip()
    has_zf = bool(sid and zf_pwd)
    if has_zf:
        provider = dict(clean.get("provider") or {})
        zf = dict(provider.get("zhengfang") or {})
        zf["username"] = sid
        zf["password"] = zf_pwd
        zf["cookie"] = ""  # 账号密码模式，清掉可能残留的 Cookie
        zf["base"] = zf.get("base") or "https://jxgl.qut.edu.cn/jwglxt"
        zf["year"], zf["term"] = default_term()
        provider["zhengfang"] = zf
        if provider.get("type") == "mock":
            provider["type"] = "zhengfang"
        provider["primary"] = "auto"
        clean["provider"] = provider
    db.save_user_config(uid, clean)

    token = auth.new_token()
    if not db.checked_create_session(uid, token, auth.SESSION_TTL_SECONDS, hashed):
        raise HTTPException(401, "账号状态已变更，请重新注册")
    if has_zf and not os.environ.get("AGENDA_SKIP_REGISTER_SYNC"):
        # 只在注册成功后才拉数据：用新用户上下文后台同步一次，不阻塞注册响应。
        # 失败由 run_sync 自己记录日志，不影响账号已创建这一结果。
        # 验证脚本置 AGENDA_SKIP_REGISTER_SYNC=1，避免测试触发真实正方网络请求。
        asyncio.create_task(_sync_after_register(uid))
    resp = JSONResponse({"ok": True, "user": _public_user(db.get_user_by_id(uid))})
    _session_cookie(resp, request, token)
    return resp


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    username = (req.username or "").strip()
    account_key = f"login:{client_ip}:{username.lower()}"
    ip_key = f"login-ip:{client_ip}"
    attempt = auth.reserve_login_attempt(account_key, ip_key)
    if attempt is None:
        raise HTTPException(429, "尝试过于频繁，请一分钟后再试")
    success = False
    try:
        user = db.get_user_by_username(username)
        stored_hash = user["password_hash"] if user else ""
        pw_ok = await run_in_threadpool(auth.verify_password, req.password, stored_hash)
        if not user or not user["is_active"] or not pw_ok:
            raise HTTPException(400, "用户名或密码错误")
        token = auth.new_token()
        if not db.checked_create_session(user["id"], token, auth.SESSION_TTL_SECONDS,
                                         user["password_hash"]):
            raise HTTPException(400, "账号状态或密码已变更，请重新登录")
        db.set_last_login(user["id"])
        resp = JSONResponse({"ok": True, "user": _public_user(user)})
        _session_cookie(resp, request, token)
        success = True
        return resp
    finally:
        auth.complete_login_attempt(attempt, success=success)


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    db.delete_session(_cookie_token(request.headers.get("cookie", "")))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


class DeleteAccountRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/delete-account")
async def auth_delete_account(req: DeleteAccountRequest, request: Request):
    """注销当前账号，需要同时核对用户名和密码，防止误操作或会话被盗用。"""
    uid = get_current_user()
    me = db.get_user_by_id(uid or 0)
    if not me:
        raise HTTPException(401, "请先登录")
    if (req.username or "").strip() != me["username"]:
        raise HTTPException(400, "用户名与当前账号不一致")
    if not await run_in_threadpool(auth.verify_password, req.password or "", me["password_hash"]):
        raise HTTPException(400, "密码错误")
    promoted = ""
    if me["role"] == "admin":
        # 管理员注销后把权限交给最早注册的其他活跃账号，避免没人能生成邀请码
        others = [u for u in db.list_users() if u["id"] != uid and u["is_active"]]
        if others:
            db.set_user_role(others[0]["id"], "admin")
            promoted = others[0]["username"]
    await run_in_threadpool(db.delete_user, uid)
    resp = JSONResponse({"ok": True, "promoted": promoted})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


def _require_admin() -> dict:
    me = db.get_user_by_id(get_current_user() or 0)
    if not me or me["role"] != "admin":
        raise HTTPException(403, "仅管理员可用")
    return me


@app.get("/api/auth/users")
async def auth_users(q: str = "", sort: str = "id"):
    _require_admin()
    from .config import portal_configured, zhengfang_configured
    from .push import channel_ready, CHANNELS, CHANNEL_NAMES
    users = db.list_users(q, sort)
    # 附带每个用户「配了哪些数据源」，供管理列表在用户名旁上色显示，
    # 不返回任何凭据内容，只给布尔状态。
    for u in users:
        cfg = db.get_user_config(u["id"])
        u["portal_configured"] = portal_configured(cfg)
        u["zhengfang_configured"] = zhengfang_configured(cfg)
        # 推送渠道配置状态：只返回是否已配置、是否启用，不返回具体凭据。
        push = cfg.get("push") or {}
        push_status = {}
        for ch in CHANNELS:
            ready, _ = channel_ready(cfg, ch)
            push_status[ch] = {
                "configured": ready,
                "enabled": push.get(f"{ch}_enabled") is True,
                "name": CHANNEL_NAMES.get(ch, ch)
            }
        u["push_status"] = push_status
    return {"users": users}


@app.get("/api/auth/invites")
async def auth_invites():
    _require_admin()
    invites = db.list_invites()
    # 附上使用人用户名，方便管理员看清谁用了哪个码
    id2name = {u["id"]: u["username"] for u in db.list_users()}
    for inv in invites:
        inv["used_by_name"] = id2name.get(inv.get("used_by"), "")
        inv["used_by_names"] = [id2name.get(u["user_id"], f"#{u['user_id']}")
                                for u in db.list_invite_uses(inv["code"])]
    return {"invites": invites}


MAX_INVITE_USES = 999
MAX_INVITE_BATCH = 50
INVITE_CODE_MIN, INVITE_CODE_MAX = 4, 32
class InviteCreateRequest(BaseModel):
    max_uses: int = 1  # 一个码最多可被多少人用来注册
    count: int = 1     # 一次生成几个（批量用）
    code: str = ""     # 自定义邀请码，留空则随机生成


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _normalize_invite_code(raw: str) -> str:
    """邀请码归一化：NFKC（全角转半角、统一兼容字符）+ 去首尾空白。

    中文可以正常保留；`２３０１` 这类全角数字会被转成 `2301`，
    避免「看起来一样但码不同」。存库与比对都用归一化后的值。
    """
    return unicodedata.normalize("NFKC", raw or "").strip()


def _custom_invite_code(raw: str) -> str:
    """校验并返回自定义邀请码；留空返回空串（表示随机生成）。"""
    code = _normalize_invite_code(raw)
    if not code:
        return ""
    length = len(code)  # 按字符数算，一个汉字算一个字
    if not (INVITE_CODE_MIN <= length <= INVITE_CODE_MAX):
        raise HTTPException(
            400, f"邀请码长度需在 {INVITE_CODE_MIN}-{INVITE_CODE_MAX} 个字符之间")
    bad = sorted({ch for ch in code if not (ch.isalnum() or ch in "_-")})
    if bad:
        raise HTTPException(
            400, "邀请码只能用中文/字母/数字/下划线（_）/短横线（-），"
                 f"不能有空格、标点或表情：{' '.join(bad)}")
    if db.invite_exists(code):
        raise HTTPException(400, "这个邀请码已经被用过了，换一个吧")
    return code


@app.post("/api/auth/invites")
async def auth_new_invite(req: InviteCreateRequest | None = None):
    me = _require_admin()
    max_uses = _clamp_int(req.max_uses if req else 1, 1, MAX_INVITE_USES, 1)
    count = _clamp_int(req.count if req else 1, 1, MAX_INVITE_BATCH, 1)
    custom = _custom_invite_code(req.code if req else "")
    if custom and count > 1:
        raise HTTPException(400, "自定义邀请码一次只能生成一个")
    codes = []
    for _ in range(count):
        code = custom or auth.new_invite()
        try:
            db.create_invite(code, me["id"], auth.INVITE_TTL_SECONDS, max_uses)
        except sqlite3.IntegrityError:
            raise HTTPException(400, "这个邀请码已经被用过了，换一个吧")
        codes.append(code)
    db.add_admin_log(me["id"], me["username"], "create_invite", codes[0],
                     ("自定义 · " if custom else "") +
                     f"可复用 {max_uses} 次" + (f" × {count} 个" if count > 1 else ""))
    return {"ok": True, "code": codes[0], "codes": codes,
            "max_uses": max_uses, "custom": bool(custom)}


@app.post("/api/auth/invites/batch")
async def auth_new_invites_batch(req: InviteCreateRequest | None = None):
    """批量生成邀请码，返回多个（批量只能随机生成）。"""
    me = _require_admin()
    count = _clamp_int(req.count if req else 5, 1, MAX_INVITE_BATCH, 5)
    max_uses = _clamp_int(req.max_uses if req else 1, 1, MAX_INVITE_USES, 1)
    if req and (req.code or "").strip():
        raise HTTPException(400, "批量生成不支持自定义邀请码，请单独生成")
    codes = []
    for _ in range(count):
        code = auth.new_invite()
        db.create_invite(code, me["id"], auth.INVITE_TTL_SECONDS, max_uses)
        codes.append(code)
    db.add_admin_log(me["id"], me["username"], "create_invite_batch",
                     f"{count} 个 · 每个可复用 {max_uses} 次", ", ".join(codes))
    return {"ok": True, "codes": codes, "max_uses": max_uses}


@app.delete("/api/auth/invites/{code}")
async def auth_delete_invite(code: str):
    me = _require_admin()
    db.delete_invite(code)
    db.add_admin_log(me["id"], me["username"], "delete_invite", code)
    return {"ok": True}


# ---------- 用户管理（仅管理员） ----------

class AdminUserAction(BaseModel):
    role: str | None = None
    active: bool | None = None


@app.post("/api/auth/users/{user_id}/role")
async def auth_set_role(user_id: int, req: AdminUserAction):
    me = _require_admin()
    if req.role not in ("admin", "user"):
        raise HTTPException(400, "角色只能是 admin 或 user")
    if user_id == me["id"] and req.role != "admin":
        raise HTTPException(400, "不能取消自己的管理员身份")
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    db.set_user_role(user_id, req.role)
    db.add_admin_log(me["id"], me["username"], "set_role", target["username"], req.role)
    return {"ok": True, "user_id": user_id, "role": req.role}


@app.post("/api/auth/users/{user_id}/active")
async def auth_set_active(user_id: int, req: AdminUserAction):
    me = _require_admin()
    if req.active is None:
        raise HTTPException(400, "缺少 active 参数")
    if user_id == me["id"] and req.active is False:
        raise HTTPException(400, "不能停用自己")
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    db.set_user_active(user_id, bool(req.active))
    if req.active is False:
        db.delete_user_sessions(user_id)  # 停用即踢下线
    db.add_admin_log(me["id"], me["username"], "set_active", target["username"],
                     "启用" if req.active else "停用")
    return {"ok": True, "user_id": user_id, "active": bool(req.active)}


@app.delete("/api/auth/users/{user_id}")
async def auth_delete_user(user_id: int):
    me = _require_admin()
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if user_id == me["id"]:
        raise HTTPException(400, "不能删除自己，请先转让管理员或注册新管理员")
    db.delete_user(user_id)
    db.add_admin_log(me["id"], me["username"], "delete_user", target["username"])
    return {"ok": True, "user_id": user_id}


class AdminEditUserRequest(BaseModel):
    display_name: str = ""
    student_id: str = ""


@app.post("/api/auth/users/{user_id}/info")
async def auth_edit_user_info(user_id: int, req: AdminEditUserRequest):
    me = _require_admin()
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    display_name = (req.display_name or "").strip()[:40]
    student_id = (req.student_id or "").strip()[:32]
    db.set_user_info(user_id, display_name, student_id)
    db.add_admin_log(me["id"], me["username"], "edit_user", target["username"],
                     f"昵称={display_name}, 学号={student_id}")
    return {"ok": True, "user_id": user_id}


class BatchUserAction(BaseModel):
    ids: list[int]
    active: bool


@app.post("/api/auth/users/batch-active")
async def auth_batch_active(req: BatchUserAction):
    me = _require_admin()
    ids = [i for i in req.ids if i != me["id"]]  # 保护自己
    done = 0
    for uid in ids:
        if db.get_user_by_id(uid):
            db.set_user_active(uid, req.active)
            if not req.active:
                db.delete_user_sessions(uid)
            done += 1
    db.add_admin_log(me["id"], me["username"], "batch_active",
                     f"{done} 个用户", "启用" if req.active else "停用")
    return {"ok": True, "count": done}


class ResetPasswordRequest(BaseModel):
    new_password: str


@app.post("/api/auth/users/{user_id}/reset-password")
async def auth_reset_password(user_id: int, req: ResetPasswordRequest):
    me = _require_admin()
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    err = auth.validate_credentials(target["username"], req.new_password)
    if err:
        raise HTTPException(400, err)
    db.set_user_password(user_id, await run_in_threadpool(auth.hash_password, req.new_password))
    db.delete_user_sessions(user_id)  # 旧会话失效，强制重新登录
    db.add_admin_log(me["id"], me["username"], "reset_password", target["username"])
    return {"ok": True, "user_id": user_id}


# ---------- 管理员：仪表盘 / 数据源总览 / 审计日志 ----------

@app.get("/api/auth/admin-stats")
async def admin_stats():
    _require_admin()
    from datetime import datetime, timedelta
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    users = db.list_users()
    return {
        "total": len(users),
        "active": sum(1 for u in users if u.get("is_active")),
        "inactive": sum(1 for u in users if not u.get("is_active")),
        "admin": sum(1 for u in users if u.get("role") == "admin"),
        "new_week": sum(1 for u in users if (u.get("created_at") or "") >= week_ago),
    }


@app.get("/api/auth/users/source-status")
async def users_source_status():
    """每个用户当前课表来源与最后同步时间，供管理员总览。"""
    _require_admin()
    from .config import portal_configured, zhengfang_configured, resolve_course_source
    rows = []
    for u in db.list_users():
        cfg = db.get_user_config(u["id"])
        try:
            resolved = resolve_course_source(cfg)
        except Exception:  # noqa: BLE001
            resolved = "auto"
        p_cfg = portal_configured(cfg)
        z_cfg = zhengfang_configured(cfg)
        snap = None
        with db.get_conn() as conn:
            r = conn.execute(
                "SELECT source, fetched_at FROM snapshot WHERE user_id = ?",
                (u["id"],)).fetchone()
            if r:
                snap = {"source": r["source"], "fetched_at": r["fetched_at"]}
        rows.append({
            "id": u["id"], "username": u["username"],
            "display_name": u.get("display_name") or u["username"],
            "portal_configured": p_cfg, "zhengfang_configured": z_cfg,
            "resolved": resolved,
            "snapshot_source": (snap or {}).get("source"),
            "fetched_at": (snap or {}).get("fetched_at"),
        })
    return {"users": rows}


@app.get("/api/auth/admin-log")
async def admin_log(limit: int = 100):
    _require_admin()
    return {"logs": db.list_admin_log(max(1, min(limit, 500)))}


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/auth/me/password")
async def auth_change_password(req: ChangePasswordRequest, request: Request):
    """用户自助修改自己的密码（需验证旧密码）。当前浏览器会话保持登录。"""
    uid = get_current_user()
    me = db.get_user_by_id(uid or 0)
    if not me:
        raise HTTPException(401, "请先登录")
    if not await run_in_threadpool(auth.verify_password, req.old_password, me["password_hash"]):
        raise HTTPException(400, "原密码错误")
    err = auth.validate_credentials(me["username"], req.new_password)
    if err:
        raise HTTPException(400, err)
    db.set_user_password(uid, await run_in_threadpool(auth.hash_password, req.new_password))
    # 仅让其他设备 / 会话失效，当前浏览器不掉线
    cur = _cookie_token(request.headers.get("cookie", ""))
    db.delete_user_sessions(uid, except_token=cur)
    return {"ok": True, "message": "密码已修改，其他设备已退出登录"}


# ---------- 数据源状态与正方账号登录 ----------

class PrimaryRequest(BaseModel):
    primary: str


@app.post("/api/provider/primary")
async def set_provider_primary(req: PrimaryRequest):
    """手动指定课表主数据源：auto（门户优先）/ zhengfang / portal，并立即按新源同步一次。"""
    from .config import portal_configured, resolve_course_source, zhengfang_configured

    if req.primary not in ("auto", "zhengfang", "portal"):
        raise HTTPException(400, "primary 只能是 auto / zhengfang / portal")
    cfg = load_config()
    if req.primary == "zhengfang" and not zhengfang_configured(cfg):
        raise HTTPException(400, "还没配置正方教务，请先填学号密码")
    if req.primary == "portal" and not portal_configured(cfg):
        raise HTTPException(400, "还没配置门户，请先扫码登录或导入 cURL")
    save_config({"provider": {"primary": req.primary}})
    result = await run_sync(push=False)
    return {"ok": bool(result.get("ok")), "primary": req.primary,
            "resolved": resolve_course_source(load_config()),
            "message": result.get("message", "")}


@app.get("/api/provider/sources")
async def provider_sources():
    """返回两套数据源的配置状态，供「课表来源」页展示与操作。"""
    from .config import portal_configured, zhengfang_configured, resolve_course_source
    cfg = load_config()
    snap = db.get_snapshot()
    portal_url = cfg["provider"].get("portal", {}).get("url") or ""
    try:
        parts = urlsplit(portal_url)
        portal_origin = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], "", "", ""))
    except ValueError:
        portal_origin = ""
    return {
        "primary": cfg["provider"].get("primary", "auto"),
        "resolved": resolve_course_source(cfg),
        "portal": {
            "configured": portal_configured(cfg),
            "url": portal_origin[:80],
            "has_login": bool(cfg["provider"].get("portal", {}).get("headers")),
        },
        "zhengfang": {
            "configured": zhengfang_configured(cfg),
            "base": cfg["provider"].get("zhengfang", {}).get("base", ""),
            "username": cfg["provider"].get("zhengfang", {}).get("username", ""),
            "year": cfg["provider"].get("zhengfang", {}).get("year", "2026"),
            "term": str(cfg["provider"].get("zhengfang", {}).get("term", "3")),
            "has_login": bool(cfg["provider"].get("zhengfang", {}).get("password")
                             or cfg["provider"].get("zhengfang", {}).get("cookie")),
        },
        "snapshot_source": (snap or {}).get("source"),
        "snapshot_fetched_at": (snap or {}).get("fetched_at"),
    }


class ZhengfangSaveRequest(BaseModel):
    base: str = ""
    username: str = ""
    password: str = ""
    year: str = ""
    term: str = ""
    test: bool = True


@app.post("/api/provider/zhengfang")
async def save_zhengfang(req: ZhengfangSaveRequest):
    """保存正方账号（学号+密码），可立即测试登录并拉一次课表，不破坏门户配置。

    密码框留空表示沿用已保存的密码（页面不回显密码）。学年/学期只有测试成功才
    写入配置，选错了学期不会把配置改坏；所选学期教务里没排课时自动切到当前学期。
    """
    from .providers.zhengfang import (
        ZhengfangProvider, TermEmptyError, default_term, normalize_year, term_label)

    cfg = load_config()
    p = cfg["provider"]
    zf = dict(p.get("zhengfang") or {})
    if not req.password and not zf.get("password") and not zf.get("cookie"):
        return {"ok": False,
                "message": "请填写教务密码（密码框留空只表示沿用已保存的密码）"}
    zf["base"] = (req.base or "https://jxgl.qut.edu.cn/jwglxt").rstrip("/")
    zf["username"] = req.username.strip()
    if req.password:
        zf["password"] = req.password
        zf["cookie"] = ""  # 账号密码模式：清空 Cookie，避免混用
    p["zhengfang"] = zf
    # 配了正方凭证就不再是演示数据：type 从 mock 切到 zhengfang，避免演示兜底抢回
    if p.get("type") == "mock":
        p["type"] = "zhengfang"
    p["primary"] = p.get("primary") or "auto"
    save_config({"provider": {"zhengfang": zf, "type": p["type"], "primary": p["primary"]}})

    if not req.test:
        zf["year"] = normalize_year(req.year) or default_term()[0]
        zf["term"] = str(req.term or default_term()[1])
        save_config({"provider": {"zhengfang": zf}})
        return {"ok": True, "message": "已保存正方账号（未测试）",
                "year": zf["year"], "term": zf["term"]}

    test_cfg = json.loads(json.dumps(cfg))
    test_zf = test_cfg["provider"]["zhengfang"]
    test_zf["year"] = normalize_year(req.year) or default_term()[0]
    test_zf["term"] = str(req.term or default_term()[1])
    try:
        provider = ZhengfangProvider(test_cfg)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    try:
        async with provider._session() as client:
            courses, switched = await provider.fetch_courses_with_term_fix(client)
            exams: list = []
            exam_error = ""
            try:
                exams = await provider._fetch_exams(client)
            except Exception as e:  # noqa: BLE001
                exam_error = str(e)
    except TermEmptyError as e:
        message = str(e)
        detected = provider.last_detected_term
        if detected and detected != (provider.year, provider.term):
            message += f"；教务当前学期是 {term_label(*detected)}，请改选这个学期"
        return {"ok": False, "message": message}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}

    # 测试期间账号可能已被另一个请求修改，不能落入旧账号的课表与考试。
    latest_zf = load_config()["provider"].get("zhengfang", {})
    if any(latest_zf.get(key) != zf.get(key)
           for key in ("base", "username", "password", "cookie")):
        return {"ok": False, "message": "测试期间正方账号配置已变更，请重新测试"}
    # 只回写测试得到的学期，避免网络等待期间覆盖其他设置或订阅令牌。
    zf["year"], zf["term"] = provider.year, provider.term
    save_config({"provider": {"zhengfang": {"year": provider.year, "term": provider.term}}})
    cfg = load_config()
    p = cfg["provider"]
    label = term_label(provider.year, provider.term)
    message = (f"已自动切换到教务当前学期（{label}）：" if switched else f"{label}：")
    message += f"{len(courses)} 门课程"
    message += f"，{len(exams)} 场考试" if not exam_error else "；考试接口失败"
    from .config import resolve_course_source
    resolved = resolve_course_source(load_config())
    if resolved == "zhengfang":
        # 课表主源就是正方：直接写入这次拉到的数据，页面无需等下一次定时同步
        try:
            await apply_snapshot(cfg, courses, "zhengfang", False,
                                 exams=None if exam_error else exams,
                                 exam_source="zhengfang" if not exam_error else None)
        except Exception as e:  # noqa: BLE001
            message += f"；课表已保存但写入失败：{e}"
        message += "，课表已切换为正方教务"
    else:
        # 课表主源是门户：不用正方的课表覆盖门户那份（门户含临时调课），
        # 只触发一次同步把正方的考试数据带进来。
        message += "；课表仍以门户为准，正方用于考试与空教室"
        try:
            await run_sync(push=False)
        except Exception:  # noqa: BLE001
            pass
    snap = db.get_snapshot()
    return {"ok": True, "message": message,
            "course_count": len(courses),
            "exam_count": len(exams), "exam_error": exam_error,
            "overwritten": resolved == "zhengfang" and (snap or {}).get("source") == "portal",
            "primary": p.get("primary") or "auto",
            "resolved": resolved,
            "year": provider.year, "term": provider.term,
            "switched": bool(switched)}


@app.get("/api/health")
async def health():
    return {"ok": True, **get_full_version()}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
