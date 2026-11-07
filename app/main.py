"""青理课表助手 v1.0 — FastAPI 入口。"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, auth, browser_login, db
from .config import (
    DEFAULT_CONFIG, MASK, global_config, load_config, masked_config, save_config,
)
from .user_context import current_user_id, get_current_user, set_current_user
from .models import (
    Course, Exam, apply_course_overrides, course_uid, current_week, parse_weeks,
    week_dates, DEFAULT_SECTION_TIMES, DEFAULT_SECTION_ENDS,
)
from .providers import get_provider
from .providers.zhengfang import ZhengfangProvider, zhengfang_configured
from .push import push_all
from .scheduler import (
    WEEKDAY_CN, apply_snapshot, courses_on, next_run_time, reschedule, run_sync,
    section_time, shutdown_scheduler, start_scheduler,
)

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


app = FastAPI(title="青理课表助手", version="1.0.0", lifespan=lifespan)

# App 化时常见的 WebView / 本地来源
ALLOWED_ORIGIN_RE = re.compile(
    r"^(tauri://localhost|capacitor://localhost|app://[^/]+|"
    r"http://(localhost|127\.0\.0\.1)(:\d+)?)$",
    re.I,
)
PUBLIC_API_PATHS = {
    "/api/health", "/api/auth/status", "/api/auth/register", "/api/auth/login",
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
        for d in exam_snap["exams"]:
            e = Exam.from_dict(d)
            if not e.is_finished():
                upcoming_exams.append(e)
        upcoming_exams.sort(key=lambda e: (e.date or "9999-12-31", e.start or "23:59"))
    provider_type = cfg.get("provider", {}).get("type", "mock")
    push_cfg = cfg.get("push", {})
    return {
        "school": cfg.get("school", "青岛理工大学"),
        "today": today.isoformat(),
        "weekday": WEEKDAY_CN[today.isoweekday() - 1],
        "week": week,
        "semester_start": cfg.get("semester_start"),
        "total_weeks": cfg.get("total_weeks", 30),
        "provider": provider_type,
        "is_demo": provider_type == "mock",
        "course_count": len(all_courses),
        "exam_count": len(exam_snap["exams"]) if exam_snap else 0,
        "upcoming_exam_count": len(upcoming_exams),
        "next_exam": upcoming_exams[0].to_dict() if upcoming_exams else None,
        "exam_fetched_at": exam_snap["fetched_at"] if exam_snap else None,
        "last_sync": snap["fetched_at"] if snap else None,
        "sync_time": cfg.get("sync_time", "12:00"),
        "next_sync": next_run_time(),
        "ai_enabled": bool(cfg.get("ai", {}).get("enabled") and cfg.get("ai", {}).get("api_key")),
        "push_channels": [c for c, on in (("bark", push_cfg.get("bark_enabled")), ("email", push_cfg.get("smtp_enabled"))) if on],
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
    return {
        "week": week,
        "current_week": cur_week,
        "dates": [d.isoformat() for d in dates],
        "today": today.isoformat(),
        "is_demo": (snap or {}).get("source") == "mock",
        "fetched_at": (snap or {}).get("fetched_at"),
        "section_times": {str(i): section_time(cfg, i) for i in range(1, 13)},
        "section_ends": {str(i): section_time(cfg, i, end=True) for i in range(1, 13)},
        "courses": out,
        "today_courses": [c.to_dict() | {
            "start_time": section_time(cfg, c.start), "end_time": section_time(cfg, c.end, end=True),
        } for c in courses_on(cfg, courses, today)],
    }


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


@app.get("/api/exams")
async def exams():
    """考试安排：未结束的按时间升序在前，已结束的置后。"""
    cfg = load_config()
    p = cfg.get("provider", {})
    ptype = p.get("type", "mock")
    supported = ptype in ("zhengfang", "mock") or zhengfang_configured(cfg)
    snap = db.get_exam_snapshot()
    if not snap:
        return {
            "exams": [], "upcoming": [], "finished": [],
            "source": None, "fetched_at": None,
            "provider": ptype,
            "is_demo": ptype == "mock",
            "supported": supported,
        }
    items = [Exam.from_dict(d) for d in snap["exams"]]
    items.sort(key=lambda e: (e.is_finished(), e.date or "9999-12-31",
                              e.start or "23:59", e.name))
    out = []
    for e in items:
        d = e.to_dict()
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
    }


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
    p["type"] = "portal" if "qut.edu.cn" in parsed["url"] else "httpjson"
    p["http_url"] = parsed["url"]
    p["http_method"] = parsed["method"]
    p["http_headers"] = parsed["headers"]
    p["http_body"] = parsed["body"]
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
    p["http_list_path"] = path
    p["http_mapping"] = mapping
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
            "current_list_path": cfg["provider"].get("http_list_path", ""),
            "current_mapping": cfg["provider"].get("http_mapping", {}),
            "guessed_list_path": path, "guessed_mapping": mapping, "parsed": parsed,
            "analysis": analyze_payload(payload),
            "warnings": diagnose_url(cfg["provider"].get("http_url", ""))}


@app.post("/api/push/test")
async def push_test():
    cfg = load_config()
    results = await push_all(cfg, "测试推送", "青理课表助手推送通道测试成功。\n每日 12:00 将自动同步课表并推送变更与次日课表。")
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


@app.get("/api/ai/models")
async def ai_models():
    try:
        return {"models": await ai.list_models()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


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
            "student_id": u.get("student_id", ""), "role": u["role"]}


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


def _require_admin() -> dict:
    me = db.get_user_by_id(get_current_user() or 0)
    if not me or me["role"] != "admin":
        raise HTTPException(403, "仅管理员可用")
    return me


@app.get("/api/auth/users")
async def auth_users():
    _require_admin()
    return {"users": db.list_users()}


@app.get("/api/auth/invites")
async def auth_invites():
    _require_admin()
    return {"invites": db.list_invites()}


@app.post("/api/auth/invites")
async def auth_new_invite():
    me = _require_admin()
    code = auth.new_invite()
    db.create_invite(code, me["id"], auth.INVITE_TTL_SECONDS)
    return {"ok": True, "code": code}


@app.get("/api/health")
async def health():
    return {"ok": True, "version": "1.0.0"}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
