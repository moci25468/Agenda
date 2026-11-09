"""Agenda 知序 — FastAPI 入口。版本号由 app.version（git tag）动态提供。"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, auth, browser_login, db
from .config import (
    DEFAULT_CONFIG, MASK, global_config, load_config, masked_config, save_config,
)
from .user_context import current_user_id, get_current_user, set_current_user
from .models import (
    Course, Event, Exam, apply_course_overrides, apply_exam_overrides,
    course_uid, current_week, exam_uid, parse_weeks,
    week_dates, DEFAULT_SECTION_TIMES, DEFAULT_SECTION_ENDS,
)
from .providers import get_provider
from .providers.zhengfang import ZhengfangProvider, zhengfang_configured
from .push import push_all
from .scheduler import (
    WEEKDAY_CN, apply_snapshot, courses_on, next_run_time, reschedule, run_sync,
    section_time, shutdown_scheduler, start_scheduler,
)
from . import weather
from .weather import hours_map
from .version import get_version, get_full_version
from . import ical

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    cfg = load_config()
    start_scheduler(cfg)
    # 首次启动无课表时静默同步一次（不推送），保证打开页面即有数据
    if db.get_snapshot() is None:
        asyncio.create_task(run_sync(push=False))
    yield
    shutdown_scheduler()


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
    for uid in _db.list_user_ids(active_only=False):
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
    upcoming_exams = []
    if exam_snap:
        all_exams = apply_exam_overrides(
            [Exam.from_dict(d) for d in exam_snap["exams"]],
            cfg.get("exam_overrides"))
        for e in all_exams:
            if not e.is_finished():
                upcoming_exams.append(e)
        upcoming_exams.sort(key=lambda e: (e.date or "9999-12-31", e.start or "23:59"))
    provider_type = cfg.get("provider", {}).get("type", "mock")
    primary = cfg.get("provider", {}).get("primary", "auto")
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
        "provider_type": provider_type,
        "primary": primary,
        "portal_configured": portal_ok,
        "zhengfang_configured": zf_ok,
        "has_source": bool(portal_ok or zf_ok),
        "is_demo": resolved == "mock",
        "course_count": len(all_courses),
        "exam_count": len(all_exams) if exam_snap else 0,
        "upcoming_exam_count": len(upcoming_exams),
        "upcoming_event_count": upcoming_event_count,
        "next_exam": upcoming_exams[0].to_dict() if upcoming_exams else None,
        "exam_fetched_at": exam_snap["fetched_at"] if exam_snap else None,
        "last_sync": snap["fetched_at"] if snap else None,
        "sync_time": cfg.get("sync_time", "12:00"),
        "next_sync": next_run_time(),
        "ai_enabled": bool(cfg.get("ai", {}).get("enabled") and cfg.get("ai", {}).get("api_key")),
        "push_channels": [c for c, on in (("bark", push_cfg.get("bark_enabled")),) if on],
    }


@app.get("/api/timetable")
async def timetable(week: int | None = None):
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
        [Exam.from_dict(d) for d in exam_snap["exams"]],
        _exam_overrides(cfg)) if exam_snap else []

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
        cfg["calendar_subscribe_token"] = token
        save_config(cfg)

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
    cfg = load_config()
    cfg["calendar_subscribe_token"] = secrets.token_urlsafe(32)
    save_config(cfg)
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
        try:
            weeks = sorted({int(w) for w in raw_weeks if int(w) > 0})
        except (TypeError, ValueError):
            raise HTTPException(400, "周次格式不正确")
        weeks = [w for w in weeks if w <= 60]
    else:
        text = str(raw_weeks or "").strip()
        weeks, derived = parse_weeks(text)
        if text and not weeks:
            raise HTTPException(400, "周次无法识别，请用 1-16、1-15(单)、3,5,7 这类写法")
        if not note:
            note = derived
    return {
        "name": name, "day": day, "start": start, "end": end,
        "teacher": str(data.get("teacher") or "").strip(),
        "room": str(data.get("room") or "").strip(),
        "weeks": weeks,
        "campus": str(data.get("campus") or "").strip(),
        "note": note,
    }


def _save_course_overrides(cfg: dict, overrides: dict) -> dict:
    save_config(dict(cfg, course_overrides=overrides))
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
    return _save_course_overrides(cfg, overrides)


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
        current = db.get_event(event_id)
        if current is None:
            raise HTTPException(404, "日程不存在")
        if action == "delete":
            db.delete_event(event_id)
            return {"ok": True}
        data = _event_form({**current, **(req.event or {})})
        return {"ok": True, "event": db.update_event(event_id, data)}
    raise HTTPException(400, "未知操作")


# ---------- 空教室查询（正方教务，实时） ----------

def _rooms_provider() -> ZhengfangProvider:
    cfg = load_config()
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
    provider = _rooms_provider()
    try:
        meta = await provider.fetch_room_meta(campus.strip() or default_id)
        ids = {str(c.get("id")) for c in meta.get("campuses") or []}
        if default_id and default_id not in ids:
            # 教务里已经没有这个校区（换了学期或校区代码），别让下拉框空着
            default_id, default_name = "", ""
            if not campus.strip():
                meta = await provider.fetch_room_meta("")
    except AuthExpiredError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"空教室查询失败: {e}")
    today = date.today()
    semester_start = cfg.get("semester_start", "")
    meta.update({
        "semester_start": semester_start,
        "current_week": current_week(semester_start, today, 40),
        "today": today.isoformat(),
        "today_weekday": today.isoweekday(),
        "default_campus": default_id,
        "default_campus_name": default_name,
    })
    return meta


@app.get("/api/rooms/free")
async def rooms_free(date_s: str = "", start: int = 1, end: int = 2,
                     campus: str = "", building: str = ""):
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
    campus = campus.strip() or str((cfg.get("rooms") or {}).get("default_campus") or "").strip()
    week = current_week(cfg.get("semester_start", ""), d, 40)
    if week < 1:
        raise HTTPException(400, "该日期还没开学，查不了空教室")
    if week > 30:
        raise HTTPException(400, "该日期超出本学期范围")
    provider = _rooms_provider()
    try:
        rooms = await provider.fetch_free_rooms(
            week, d.isoweekday(), start, end, campus, building.strip())
    except AuthExpiredError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"空教室查询失败: {e}")
    return {"rooms": rooms, "count": len(rooms), "date": date_s,
            "week": week, "day": d.isoweekday(), "start": start, "end": end}


@app.get("/api/exams")
async def exams():
    """考试安排：未结束的按时间升序在前，已结束的置后（含手动调整）。"""
    cfg = load_config()
    p = cfg.get("provider", {})
    ptype = p.get("type", "mock")
    supported = ptype in ("zhengfang", "mock") or zhengfang_configured(cfg)
    overrides = _exam_overrides(cfg)
    snap = db.get_exam_snapshot()
    if not snap:
        return {
            "exams": [], "upcoming": [], "finished": [],
            "source": None, "fetched_at": None,
            "provider": ptype,
            "is_demo": ptype == "mock",
            "supported": supported,
            "hidden_count": len(overrides["hidden"]),
            "edited_count": len(overrides["edited"]),
            "added_count": len(overrides["added"]),
            "has_overrides": bool(overrides["hidden"] or overrides["edited"]
                                  or overrides["added"]),
        }
    items = apply_exam_overrides(
        [Exam.from_dict(d) for d in snap["exams"]], overrides)
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
        "source": snap["source"],
        "fetched_at": snap["fetched_at"],
        "is_demo": snap["source"] == "mock",
        "supported": supported,
        "hidden_count": len(overrides["hidden"]),
        "edited_count": len(overrides["edited"]),
        "added_count": len(overrides["added"]),
        "has_overrides": bool(overrides["hidden"] or overrides["edited"]
                              or overrides["added"]),
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
    if len(note) > 500:
        raise HTTPException(400, "备注太长")
    return {"name": name, "date": date_s, "start": start, "end": end,
            "room": room, "seat": seat,
            "campus": str(data.get("campus") or "").strip(), "note": note}


def _save_exam_overrides(cfg: dict, overrides: dict) -> dict:
    save_config(dict(cfg, exam_overrides=overrides))
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
    return _save_exam_overrides(cfg, overrides)


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

    analysis = analyze_payload(req.payload)
    if analysis["kind"] == "events":
        return {"ok": False,
                "message": "收到的是「按日期的日程事件流」，现有课表模型只表达周期性课程，需要事件适配器；请把这份结构发给助手。",
                "analysis": analysis}
    list_path = req.list_path if req.list_path is not None else analysis["suggested_path"]
    mapping = req.mapping if req.mapping is not None else analysis["mapping"]
    try:
        courses = courses_from_payload(req.payload, list_path, mapping)
    except ValueError as e:
        return {"ok": False, "message": str(e), "analysis": analysis,
                "list_path": list_path, "mapping": mapping}
    result = await apply_snapshot(load_config(), courses, req.source or "ingest", req.push)
    result.update(
        {"analysis": analysis, "list_path": list_path, "mapping": mapping})
    return result


@app.get("/api/logs")
async def logs(limit: int = 50):
    return db.get_logs(limit)


@app.get("/api/settings")
async def get_settings():
    return masked_config(load_config())


@app.post("/api/settings")
async def post_settings(cfg: dict):
    try:
        saved = save_config(cfg)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    reschedule(saved)
    return masked_config(saved)


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
    p["type"] = "portal" if "qut.edu.cn" in parsed["url"] else "httpjson"
    p["primary"] = p.get("primary") or "auto"
    warnings = diagnose_url(parsed["url"])
    lower_headers = {str(k).lower() for k in parsed["headers"]}
    if not lower_headers & {"cookie", "authorization", "x-token", "x-access-token", "token"}:
        warnings.append(
            "cURL 里没检测到 Cookie 或 Authorization，门户接口通常需要登录态；请确认复制的是登录后的请求。")
    result = {"ok": False, "url": parsed["url"], "method": parsed["method"],
              "list_path": "", "mapping": {}, "course_count": 0, "sample": None,
              "raw_preview": "", "error": None, "analysis": None, "warnings": warnings}
    try:
        provider = get_provider(cfg)
        payload = await provider.fetch_raw()
    except AuthExpiredError as e:
        save_config(cfg)
        result["error"] = str(e)
        return result
    except Exception as e:  # noqa: BLE001
        save_config(cfg)
        result["error"] = f"试拉取失败：{e}"
        return result
    path, mapping = guess_mapping(payload)
    p["portal"]["list_path"] = path
    p["portal"]["mapping"] = mapping
    save_config(cfg)
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
        provider = get_provider(cfg)
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


@app.post("/api/push/test")
async def push_test():
    cfg = load_config()
    results = await push_all(cfg, "测试推送", "Agenda 推送通道测试成功。\n每日定时同步后会推送课表变更与次日课程、日程安排。")
    return {"results": results}


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(400, "消息不能为空")
    try:
        reply = await ai.chat(req.message, req.history)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"AI 请求失败: {e}")
    return {"reply": reply}


@app.post("/api/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    """SSE 流式回复：data: {"delta": "..."}，结束发 {"done": true}，失败发 {"error": "..."}。"""
    if not req.message.strip():
        raise HTTPException(400, "消息不能为空")
    try:
        ai.check_ready()
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def event_gen():
        try:
            async for chunk in ai.chat_stream(req.message, req.history):
                yield "data: " + json.dumps({"delta": chunk}, ensure_ascii=False) + "\n\n"
            yield 'data: {"done": true}\n\n'
        except Exception as e:  # noqa: BLE001
            yield "data: " + json.dumps({"error": f"AI 请求失败: {e}"}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # 让 nginx 不缓冲，逐字下发
    })


@app.get("/api/ai/models")
async def ai_models():
    try:
        return {"models": await ai.list_models()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


# ---------- AI 对话落库（短期记忆）与长期记忆 ----------

class ChatHistoryRequest(BaseModel):
    messages: list[dict] = []


@app.get("/api/chat/history")
async def chat_history():
    """当前用户的对话快照，刷新/换设备后恢复。"""
    return {"messages": db.get_chat_history()}


@app.post("/api/chat/history")
async def save_chat_history(req: ChatHistoryRequest):
    """前端在每轮对话结束、编辑截断后整体回存（客户端为准）。"""
    cleaned = []
    for m in req.messages[:db.CHAT_HISTORY_CAP]:
        role = m.get("role")
        content = str(m.get("content") or "")[:4000]
        if role in ("user", "assistant") and content.strip():
            cleaned.append({"role": role, "content": content})
    db.replace_chat_history(cleaned)
    return {"ok": True, "count": len(cleaned)}


@app.post("/api/chat/clear")
async def clear_chat():
    db.clear_chat_history()
    return {"ok": True}


class MemoryRequest(BaseModel):
    action: str                 # add | delete
    content: str = ""
    id: str | None = None


@app.get("/api/memories")
async def list_memories():
    return {"memories": db.list_memories()}


@app.post("/api/memories")
async def edit_memory(req: MemoryRequest):
    action = (req.action or "").strip()
    if action == "add":
        content = req.content.strip()
        if not content:
            raise HTTPException(400, "记忆内容不能为空")
        if len(content) > 200:
            raise HTTPException(400, "记忆太长（最多 200 字）")
        try:
            return {"ok": True, "memory": db.add_memory(content)}
        except ValueError as e:
            raise HTTPException(400, str(e))
    if action == "delete":
        mem_id = (req.id or "").strip()
        if not mem_id or not db.delete_memory(mem_id):
            raise HTTPException(404, "记忆不存在")
        return {"ok": True}
    raise HTTPException(400, "未知操作")


@app.post("/api/ai/test")
async def ai_test():
    try:
        return await ai.test_connection()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


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


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    student_id: str = ""
    invite: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/api/auth/status")
async def auth_status():
    uid = get_current_user()
    user = db.get_user_by_id(uid) if uid else None
    total = db.count_users()
    return {"authenticated": bool(user), "user": _public_user(user) if user else None,
            "needs_setup": total == 0, "invite_required": total > 0}


@app.post("/api/auth/register")
async def auth_register(req: RegisterRequest, request: Request):
    err = auth.validate_credentials(req.username, req.password)
    if err:
        raise HTTPException(400, err)
    username = req.username.strip()
    if db.get_user_by_username(username):
        raise HTTPException(400, "用户名已存在")
    first = db.count_users() == 0
    code = (req.invite or "").strip()
    if not first and not db.get_valid_invite(code):
        raise HTTPException(400, "邀请码无效或已被使用")
    uid = db.create_user(username, auth.hash_password(req.password),
                         (req.display_name or "").strip()[:40],
                         (req.student_id or "").strip()[:32],
                         "admin" if first else "user")
    if not first:
        db.consume_invite(code, uid)
    template = global_config()
    if first:
        # 第一个账号继承现有的单用户配置与课表（快照 user_id=1 已迁移）
        db.save_user_config(uid, template)
    else:
        clean = json.loads(json.dumps(DEFAULT_CONFIG))
        for key in ("school", "semester_start", "total_weeks", "section_times"):
            clean[key] = template.get(key, clean.get(key))
        clean["security"] = {"enabled": False, "token": "", "allowed_origins": []}
        db.save_user_config(uid, clean)
    token = auth.new_token()
    db.create_session(uid, token, auth.SESSION_TTL_SECONDS)
    resp = JSONResponse({"ok": True, "user": _public_user(db.get_user_by_id(uid))})
    _session_cookie(resp, request, token)
    return resp


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest, request: Request):
    key = request.client.host if request.client else "unknown"
    if not auth.check_rate_limit(key):
        raise HTTPException(429, "尝试过于频繁，请一分钟后再试")
    user = db.get_user_by_username((req.username or "").strip())
    if not user or not user["is_active"] or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(400, "用户名或密码错误")
    auth.clear_rate_limit(key)
    db.set_last_login(user["id"])
    token = auth.new_token()
    db.create_session(user["id"], token, auth.SESSION_TTL_SECONDS)
    resp = JSONResponse({"ok": True, "user": _public_user(user)})
    _session_cookie(resp, request, token)
    return resp


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
    if not auth.verify_password(req.password or "", me["password_hash"]):
        raise HTTPException(400, "密码错误")
    promoted = ""
    if me["role"] == "admin":
        # 管理员注销后把权限交给最早注册的其他活跃账号，避免没人能生成邀请码
        others = [u for u in db.list_users() if u["id"] != uid and u["is_active"]]
        if others:
            db.set_user_role(others[0]["id"], "admin")
            promoted = others[0]["username"]
    db.delete_user(uid)
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
    return {"users": db.list_users(q, sort)}


@app.get("/api/auth/invites")
async def auth_invites():
    _require_admin()
    invites = db.list_invites()
    # 附上使用人用户名，方便管理员看清谁用了哪个码
    id2name = {u["id"]: u["username"] for u in db.list_users()}
    for inv in invites:
        inv["used_by_name"] = id2name.get(inv.get("used_by"), "")
    return {"invites": invites}


@app.post("/api/auth/invites")
async def auth_new_invite():
    me = _require_admin()
    code = auth.new_invite()
    db.create_invite(code, me["id"], auth.INVITE_TTL_SECONDS)
    db.add_admin_log(me["id"], me["username"], "create_invite", code)
    return {"ok": True, "code": code}


@app.post("/api/auth/invites/batch")
async def auth_new_invites_batch():
    """批量生成邀请码，返回多个。"""
    me = _require_admin()
    count = 5
    codes = []
    for _ in range(count):
        code = auth.new_invite()
        db.create_invite(code, me["id"], auth.INVITE_TTL_SECONDS)
        codes.append(code)
    db.add_admin_log(me["id"], me["username"], "create_invite_batch",
                     f"{count} 个", ", ".join(codes))
    return {"ok": True, "codes": codes}


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
    db.set_user_password(user_id, auth.hash_password(req.new_password))
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
    return {"logs": db.list_admin_log(min(limit, 500))}


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
    if not auth.verify_password(req.old_password, me["password_hash"]):
        raise HTTPException(400, "原密码错误")
    err = auth.validate_credentials(me["username"], req.new_password)
    if err:
        raise HTTPException(400, err)
    db.set_user_password(uid, auth.hash_password(req.new_password))
    # 仅让其他设备 / 会话失效，当前浏览器不掉线
    cur = _cookie_token(request.headers.get("cookie", ""))
    db.delete_user_sessions(uid, except_token=cur)
    return {"ok": True, "message": "密码已修改，其他设备已退出登录"}


# ---------- 数据源状态与正方账号登录 ----------

@app.get("/api/provider/sources")
async def provider_sources():
    """返回两套数据源的配置状态，供「课表来源」页展示与操作。"""
    from .config import portal_configured, zhengfang_configured, resolve_course_source
    cfg = load_config()
    snap = db.get_snapshot()
    return {
        "primary": cfg["provider"].get("primary", "auto"),
        "resolved": resolve_course_source(cfg),
        "portal": {
            "configured": portal_configured(cfg),
            "url": (cfg["provider"].get("portal", {}).get("url") or "")[:80],
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
    year: str = "2026"
    term: str = "3"
    test: bool = True


@app.post("/api/provider/zhengfang")
async def save_zhengfang(req: ZhengfangSaveRequest):
    """保存正方账号（学号+密码），可立即测试登录并拉一次课表，不破坏门户配置。

    用户明确：正方登录只用账号密码，不暴露 Cookie 输入框。
    """
    cfg = load_config()
    p = cfg["provider"]
    zf = dict(p.get("zhengfang") or {})
    zf["base"] = (req.base or "https://jxgl.qut.edu.cn/jwglxt").rstrip("/")
    zf["username"] = req.username.strip()
    zf["password"] = req.password
    zf["year"] = req.year.strip() or "2026"
    zf["term"] = str(req.term or "3")
    zf["cookie"] = ""  # 账号密码模式：清空 Cookie，避免混用
    p["zhengfang"] = zf
    p["primary"] = p.get("primary") or "auto"
    save_config(cfg)

    if not req.test:
        return {"ok": True, "message": "已保存正方账号（未测试）"}
    # 立即试登录并拉课表，验证账号密码
    from .providers.zhengfang import ZhengfangProvider
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
        snap = db.get_snapshot()
        return {"ok": True, "message": message, "course_count": len(courses),
                "exam_count": len(exams), "exam_error": exam_error,
                "overwritten": (snap or {}).get("source") == "portal"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}


@app.get("/api/health")
async def health():
    return {"ok": True, **get_full_version()}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
