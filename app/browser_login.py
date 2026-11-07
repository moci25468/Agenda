"""一键登录门户：用本机 Chrome 打开官方登录窗口，自动发现课表接口并写回配置。

设计要点：
- 不接触用户密码。登录在官方页面由用户本人完成，扫码 / 短信 / 验证码 / MFA
  全部由浏览器与学校页面自己处理，本模块只监听登录成功后的 JSON 响应。
- playwright 是可选依赖，通过 channel="chrome" 调用本机已安装的 Chrome，
  不需要另下载 Chromium；浏览器 profile 持久化，多数情况下无需反复登录。
- 只适用于有图形界面的本机环境。VPS / Docker 无桌面时请改用 cURL 导入。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import threading
import time
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import DATA_DIR, load_config, save_config
from .curl_import import collect_arrays, diagnose_url, guess_mapping
from .scheduler import run_sync
from .user_context import get_current_user, set_current_user

PORTAL_ENTRY = "http://portal.qut.edu.cn/portal/event"
PROFILE_DIR = DATA_DIR / "browser_profile"
TIMEOUT_SECONDS = 300
COLLECT_MAX_SECONDS = 25   # 登录后最多等这么久收集接口
SETTLE_SECONDS = 3         # 发现第一个候选后再等这么久收尾

# 统一身份认证相关子域，出现这些主机名说明还没进门户
_AUTH_HOSTS = {"sso", "cas", "auth", "login", "id", "passport"}

_SKIP_HEADERS = {
    "host", "content-length", "accept-encoding", "connection",
    "sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest", "sec-fetch-user",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "upgrade-insecure-requests",
}

_lock = threading.Lock()
_stop_event: threading.Event | None = None
_force_event = threading.Event()   # 用户点「我已登录，立即抓取」时置位
_thread: threading.Thread | None = None
_owner_uid: int | None = None      # 登录窗口归属用户，避免多人串状态
_state: dict = {
    "status": "idle",          # idle/starting/waiting_login/analyzing/done/error/cancelled
    "message": "",
    "started_at": None,
    "candidates": [],          # 脱敏后的候选接口（不含 Cookie 与响应体）
    "result": {},
    "log": [],
    "qr_image": None,          # 无界面模式下回传前端的登录二维码（base64 PNG）
}


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    # 底层异常 / 浏览器日志是多行的，压成单行，避免污染前端提示与状态 JSON
    text = " ".join(str(msg).splitlines())[:300]
    with _lock:
        _state["log"].append(f"[{ts}] {text}")
        _state["log"] = _state["log"][-30:]
        _state["message"] = text


def _set(**kw):
    with _lock:
        _state.update(kw)


def get_status() -> dict:
    with _lock:
        if _owner_uid is not None and get_current_user() != _owner_uid:
            return {"status": "idle", "message": "", "started_at": None,
                    "candidates": [], "result": {}, "log": []}
        return json.loads(json.dumps(_state))


_SENSITIVE_PARAMS = ("token", "ticket", "code",
                     "secret", "key", "session", "sign")

# URL 中出现这些片断，说明是课表数据接口（优先级高于门户的应用 / 资源 / 新闻列表）
COURSE_URL_HINTS = (
    "xskb", "/kb", "kbcx", "kbxx", "timetable", "schedule", "courselist",
    "/course", "kblist", "skb",
)


def _mask_url(url: str) -> str:
    """回显给前端时遮掉 URL 里的票据类参数，真实地址仍写在配置里。"""
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        q = [(k, "***" if any(s in k.lower() for s in _SENSITIVE_PARAMS) else v)
             for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(q, safe="*"), parts.fragment))
    except Exception:  # noqa: BLE001
        return url


def _parse_body(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    s = str(raw)
    if "=" in s and "{" not in s:
        return dict(parse_qsl(s))
    return s


def build_candidate(url: str, method: str, status: int, headers: dict,
                    post_data, payload) -> dict | None:
    """把一个 JSON 响应整理成候选数据接口；既不像课表也不像日程则返回 None。"""
    arrays = collect_arrays(payload)
    if not arrays:
        return None
    course = [a for a in arrays if a["course_like"]]
    event = [a for a in arrays if a["event_like"]]
    if not (course or event):
        return None
    path, mapping = guess_mapping(payload)
    best = (course or event)[0]
    chosen = path or best["path"]
    kind = "events" if event and not course else "courses"
    return {
        "url": url,
        "method": (method or "GET").upper(),
        "status": status,
        "headers": headers or {},
        "post_data": post_data,
        "list_path": "" if chosen == "(根数组)" else chosen,
        "mapping": mapping,
        "kind": kind,
        "count": best["count"],
        "keys": best["keys"],
        "score": (2000 if kind == "courses" else 1000) + best["count"],
        "preview": json.dumps(payload, ensure_ascii=False)[:1200],
    }


def pick_best(candidates: list[dict]) -> dict | None:
    """只挑「周课表」型接口；日程事件接口需要单独适配器，不写进配置。

    优先选 URL 带课表语义（xskb / kbcx / timetable …）且字段映射完整的候选：
    门户的「应用 / 资源」列表同样是大数组，不能只按条数多少挑。
    """
    usable = [c for c in candidates if c.get("kind") == "courses"
              and c.get("status") == 200 and c.get("url", "").startswith(("http://", "https://"))]
    if not usable:
        return None
    qualified = []
    for c in usable:
        m = c.get("mapping") or {}
        if not m.get("name"):
            continue
        if not (m.get("day") or m.get("start")):
            continue
        qualified.append(c)
    pool = qualified or usable
    kb_like = [c for c in pool if any(
        h in c["url"].lower() for h in COURSE_URL_HINTS)]
    if kb_like:
        pool = kb_like
    by_url: dict[str, dict] = {}
    for c in pool:
        by_url[c["url"]] = c  # 同 URL 取最后一次响应

    def quality(c: dict):
        m = c.get("mapping") or {}
        fields = sum(1 for f in ("name", "day", "start", "end", "weeks", "room", "teacher")
                     if m.get(f))
        return (fields, c.get("count", 0))

    return sorted(by_url.values(), key=lambda c: quality(c), reverse=True)[0]


def _looks_logged_in(url: str) -> bool:
    """判断某个页面 URL 是否已经进入门户（而不是还停在统一身份认证）。"""
    try:
        parts = urlsplit(url or "")
    except Exception:  # noqa: BLE001
        return False
    host = (parts.hostname or "").lower()
    if not host.endswith("qut.edu.cn"):
        return False
    if not host.startswith("portal") and host.split(".")[0] in _AUTH_HOSTS:
        return False
    path = parts.path.lower()
    if "/sso/" in path or "login" in path or "oauth" in path:
        return False
    return True


def _page_urls(ctx) -> list[str]:
    """取各标签页的真实地址。

    必须走一次 API 调用（evaluate）才能真正驱动 playwright 的同步事件泵：
    只读 page.url 拿到的是上一次同步时的缓存值，SPA 跳转后不会更新。
    """
    urls: list[str] = []
    for pg in ctx.pages:
        url = ""
        try:
            url = pg.evaluate("() => location.href")
        except Exception:  # noqa: BLE001
            try:
                url = pg.url
            except Exception:  # noqa: BLE001
                url = ""
        if url:
            urls.append(str(url))
    return urls


def _pump(ctx) -> None:
    """主动泵一次事件循环，让 response 回调有机会执行。"""
    try:
        ctx.cookies()
    except Exception:  # noqa: BLE001
        pass


def _canvas_dark_ratio(el) -> float:
    """canvas 里深色像素占比；用来排除还没画出来的空白二维码画布。"""
    try:
        return float(el.evaluate("""c => {
            const ctx = c.getContext && c.getContext('2d');
            if (!ctx || !c.width || !c.height) return -1;
            const d = ctx.getImageData(0, 0, c.width, c.height).data;
            let dark = 0;
            for (let i = 0; i < d.length; i += 4) {
                if (d[i] < 100 && d[i + 1] < 100 && d[i + 2] < 100) dark++;
            }
            return dark / (d.length / 4);
        }"""))
    except Exception:  # noqa: BLE001
        return -1


def _grab_qr(page, timeout: float = 6.0) -> str | None:
    """截登录页二维码，返回 base64 PNG。

    只接受真正像二维码的可见元素（约 100–360px、正方形、canvas 有深色像素）；
    不再退化成整页截图——那会把还没加载完的登录页当成二维码显示。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "sso.qut.edu.cn" not in (page.url or ""):
            return None
        best = None
        best_score = -1
        fallback = []
        try:
            handles = page.query_selector_all("canvas, img")
        except Exception:  # noqa: BLE001
            handles = []
        for el in handles:
            try:
                box = el.bounding_box()
                if not box:
                    continue
                w, h = box["width"], box["height"]
                if w < 90 or h < 90 or w > 360 or h > 360:
                    continue
                tag = el.evaluate("e => e.tagName")
                if tag == "CANVAS":
                    ratio = _canvas_dark_ratio(el)
                    if ratio < 0.02 or ratio > 0.8:
                        continue
                    score = 10
                else:
                    alt = (el.get_attribute("alt") or "").lower()
                    src = (el.get_attribute("src") or "").lower()
                    hinted = any(x in alt + src for x in (
                        "qr", "scan", "二维码", "code"))
                    if hinted:
                        score = 6
                    else:
                        fallback.append(el)
                        continue
                if abs(w - h) > max(w, h) * 0.25:
                    continue
                if score > best_score:
                    best, best_score = el, score
            except Exception:  # noqa: BLE001
                continue
        # 有些学校页面二维码是没有 alt/src 提示的 <img>，画面里只有它一个候选
        if best is None and len(fallback) == 1:
            best = fallback[0]
        if best is not None:
            try:
                shot = best.screenshot()
            except Exception:  # noqa: BLE001
                shot = None
            if shot and len(shot) > 300:
                return base64.b64encode(shot).decode("ascii")
        time.sleep(0.5)
    return None


def _cookie_from_context(cookies) -> str:
    return "; ".join(
        f"{c['name']}={c['value']}" for c in cookies
        if "qut.edu.cn" in str(c.get("domain", "")) and c.get("name") and c.get("value")
    )


def _headers_for_config(cand: dict, cookie_str: str) -> dict:
    out = {}
    for k, v in (cand.get("headers") or {}).items():
        lk = str(k).lower()
        if lk in _SKIP_HEADERS or str(k).startswith(":"):
            continue
        out[k] = v
    if cookie_str and not any(str(k).lower() == "cookie" for k in out):
        out["Cookie"] = cookie_str
    return out


def apply_candidate(cand: dict, cookie_str: str = "") -> dict:
    """把选中的接口写成 portal 数据源，并立即试同步一次（不推送）。"""
    cfg = load_config()
    p = cfg["provider"]
    p["type"] = "portal"
    p["http_url"] = cand["url"]
    p["http_method"] = cand["method"]
    p["http_headers"] = _headers_for_config(cand, cookie_str)
    p["http_body"] = _parse_body(cand.get("post_data")) or {}
    p["http_list_path"] = cand["list_path"]
    p["http_mapping"] = cand["mapping"]
    save_config(cfg)
    warnings = diagnose_url(cand["url"])
    try:
        res = asyncio.run(run_sync(push=False))
        synced, message = bool(res["ok"]), res["message"]
    except Exception as e:  # noqa: BLE001
        synced, message = False, f"试同步失败：{e}"
    return {
        "ok": synced,
        "url": _mask_url(cand["url"]),
        "method": cand["method"],
        "kind": cand["kind"],
        "list_path": cand["list_path"],
        "mapping": cand["mapping"],
        "warnings": warnings,
        "message": message,
    }


def _public_candidates(candidates: list[dict]) -> list[dict]:
    """状态回显时去掉 Cookie / 请求头 / 响应体，避免凭据进入前端与日志。"""
    out = []
    for c in candidates:
        item = {k: v for k, v in c.items() if k not in (
            "headers", "preview", "post_data")}
        item["url"] = _mask_url(item.get("url", ""))
        out.append(item)
    return out


def _run(uid: int):
    global _stop_event
    # 后台线程里绑定发起用户：写配置 / 快照时落到该用户名下
    set_current_user(uid)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _set(status="error")
        _log("缺少依赖 playwright：请在项目目录执行 .venv/bin/pip install playwright")
        return

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE_DIR, 0o700)
    except OSError:
        pass
    candidates: list[dict] = []
    cookie_str = ""
    logged_in = False
    cancelled = False

    _set(status="starting")
    _log("正在打开浏览器…")
    try:
        with sync_playwright() as pw:
            try:
                launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                               "--disable-gpu", "--no-first-run"]
                disp = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
                if disp:
                    # 本机有桌面：优先用已安装的 Chrome 弹出可见窗口（密码/验证码更方便）
                    try:
                        ctx = pw.chromium.launch_persistent_context(
                            str(PROFILE_DIR), channel="chrome", headless=False,
                            args=["--no-first-run", "--no-default-browser-check"],
                            viewport={"width": 1180, "height": 820},
                        )
                    except Exception:
                        ctx = pw.chromium.launch_persistent_context(
                            str(PROFILE_DIR), headless=True, args=launch_args)
                else:
                    # 无桌面（macOS / Windows 本机，或手机浏览器访问本机后端）：
                    # 无界面打开登录页，二维码回传前端供手机扫码。优先用本机 Chrome
                    # 无界面运行（不依赖 playwright 自带浏览器缓存，macOS 上尤其如此）；
                    # 没有 Chrome 时再退回自带 Chromium（Docker 镜像已装 headless shell）。
                    try:
                        ctx = pw.chromium.launch_persistent_context(
                            str(PROFILE_DIR), channel="chrome", headless=True,
                            args=launch_args)
                    except Exception:
                        ctx = pw.chromium.launch_persistent_context(
                            str(PROFILE_DIR), headless=True, args=launch_args)
            except Exception as e:
                brief = str(e).splitlines()[0][:200]
                raise RuntimeError(
                    f"无法启动浏览器（{brief}）。请改用 cURL 导入或在本机桌面环境运行。"
                )

            def on_response(resp):
                try:
                    if "json" not in resp.headers.get("content-type", ""):
                        return
                    if "qut.edu.cn" not in resp.url:
                        return
                    payload = json.loads(resp.text())
                except Exception:  # noqa: BLE001
                    return
                cand = build_candidate(
                    resp.url, resp.request.method, resp.status,
                    dict(resp.request.all_headers()
                         ), resp.request.post_data, payload,
                )
                if cand:
                    candidates.append(cand)

            ctx.on("page", lambda pg: pg.on("response", on_response))
            for pg in ctx.pages:
                pg.on("response", on_response)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _set(status="waiting_login")
            _log("已打开浏览器，请在窗口中完成登录（扫码 / 验证码由学校页面处理）")
            try:
                page.goto(PORTAL_ENTRY,
                          wait_until="domcontentloaded", timeout=60000)
            except Exception as e:  # noqa: BLE001
                _log(f"页面加载提示：{e}；可继续在窗口中手动操作")

            # 无界面模式：把登录页二维码截下来回传前端，供用户用微信扫码
            already_in = any(
                _looks_logged_in(u) for u in _page_urls(ctx))
            qr = None if already_in else _grab_qr(page, timeout=20.0)
            if qr:
                _set(qr_image=qr)
                _log("二维码已生成，请用微信扫描上方二维码")
            elif already_in:
                _log("已沿用上次登录状态，直接进入门户")
            else:
                _log("登录页暂未渲染出二维码，继续等待；登录成功后会自动继续")

            deadline = time.time() + TIMEOUT_SECONDS
            warned = False
            last_urls: list[str] = []
            qr_retry_at = time.time() + 5
            while time.time() < deadline:
                if _stop_event and _stop_event.is_set():
                    cancelled = True
                    break
                pages = ctx.pages
                if not pages:
                    _log("浏览器窗口已关闭")
                    break
                # 注意：必须通过 API 调用取值，否则同步事件循环不推进，页面 URL 永远停在旧值
                urls = _page_urls(ctx)
                if urls and urls != last_urls:
                    last_urls = list(urls)
                    _log("当前页面：" + " | ".join(_mask_url(u) for u in urls[:2]))
                hit = next((u for u in urls if _looks_logged_in(u)), None)
                if hit:
                    logged_in = True
                    _log(f"已进入门户（{_mask_url(hit)}），开始收集课表接口")
                    break
                if _force_event.is_set():
                    logged_in = True
                    _log("已按「我已登录，立即抓取」开始收集接口")
                    break
                if candidates:
                    logged_in = True
                    _log(f"已捕获 {len(candidates)} 个数据接口，判定登录成功")
                    break
                remain = deadline - time.time()
                if remain < 60 and not warned:
                    warned = True
                    _log("还剩不到 60 秒；若已登录但没自动识别，请点「我已登录，立即抓取」")
                # 二维码可能比页面晚渲染，或用户切换了登录方式：持续补抓
                if qr is None and time.time() >= qr_retry_at:
                    qr_retry_at = time.time() + 5
                    fresh = _grab_qr(page, timeout=2.0)
                    if fresh:
                        qr = fresh
                        _set(qr_image=qr)
                        _log("二维码已生成，请用微信扫描上方二维码")
                time.sleep(1)

            if logged_in:
                _set(qr_image=None)
                # 登录后若停在首页 / 工作台，课表 XHR 不会发出：主动打开日程页触发一次
                target = None
                for pg in ctx.pages:
                    try:
                        if _looks_logged_in(pg.evaluate("() => location.href")):
                            target = pg
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if target is None and ctx.pages:
                    target = ctx.pages[0]
                if target is not None and not candidates:
                    _log("正在打开课表 / 日程页以触发数据请求…")
                    try:
                        target.goto(
                            PORTAL_ENTRY, wait_until="domcontentloaded", timeout=45000)
                    except Exception as e:  # noqa: BLE001
                        _log(f"打开课表页提示：{e}")
                _log(f"正在收集课表接口（最多 {COLLECT_MAX_SECONDS} 秒）…")
                collect_deadline = time.time() + COLLECT_MAX_SECONDS
                first_hit = None
                while time.time() < collect_deadline:
                    if _stop_event and _stop_event.is_set():
                        cancelled = True
                        break
                    _pump(ctx)  # 泵事件，保证 response 回调持续执行
                    if first_hit is None and candidates:
                        first_hit = time.time()
                        _log(f"已捕获 {len(candidates)} 个候选接口，继续观察…")
                    if first_hit is not None and time.time() - first_hit >= SETTLE_SECONDS:
                        break
                    time.sleep(0.5)
                _log(f"收集完成，共捕获 {len(candidates)} 个候选接口")
            try:
                cookie_str = _cookie_from_context(ctx.cookies())
            except Exception:  # noqa: BLE001
                pass
            _log("正在关闭登录窗口…")
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        _set(status="error")
        _log(f"浏览器登录失败：{e}")
        return

    _set(status="analyzing", candidates=_public_candidates(candidates))
    _log(f"共发现 {len(candidates)} 个候选数据接口")
    if cancelled or (_stop_event and _stop_event.is_set()):
        _set(status="cancelled")
        _log("已取消，未修改数据源配置")
        return
    best = pick_best(candidates)
    if not best:
        has_events = any(c["kind"] == "events" for c in candidates)
        msg = ("只发现「按日期的日程事件流」接口，现有课表模型无法直接承载一次性调课，"
               "需要单独的事件适配器；请把候选接口信息发给助手。") if has_events else \
            "登录窗口中没捕获到课表接口，请确认已进入课表 / 日程页后重试。"
        _set(status="done", result={"ok": False, "message": msg})
        _log(msg)
        return
    result = apply_candidate(best, cookie_str)
    _set(status="done", result=result)
    _log(result["message"])
    if result.get("warnings"):
        for w in result["warnings"]:
            _log(f"提示：{w}")


def start() -> dict:
    global _thread, _stop_event, _owner_uid
    uid = get_current_user()
    if uid is None:
        return {"ok": False, "message": "请先登录后再使用一键登录"}
    with _lock:
        if _thread is not None and _thread.is_alive():
            return {"ok": False, "message": "已有一个登录窗口在进行中，请先等待或取消"}
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return {"ok": False, "message": "缺少可选依赖 playwright，请在项目目录执行：.venv/bin/pip install playwright"}
    _stop_event = threading.Event()
    _force_event.clear()
    _owner_uid = uid
    _set(status="starting", message="", started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         candidates=[], result={}, log=[], qr_image=None)
    _thread = threading.Thread(target=_run, args=(uid,), daemon=True)
    _thread.start()
    return {"ok": True, "message": "已启动，登录页二维码将显示在下方面板，请用微信扫码"}


def force_collect() -> dict:
    """人工确认已登录：跳过页面识别，直接开始收集接口。"""
    if _thread is None or not _thread.is_alive():
        return {"ok": False, "message": "当前没有进行中的登录窗口，请先点「一键登录门户」"}
    if _owner_uid is not None and get_current_user() != _owner_uid:
        return {"ok": False, "message": "当前登录窗口属于其他用户"}
    _force_event.set()
    _log("已收到「立即抓取」指令")
    return {"ok": True, "message": "已开始收集接口"}


def stop() -> dict:
    if _owner_uid is not None and get_current_user() != _owner_uid:
        return {"ok": False, "message": "当前登录窗口属于其他用户"}
    if _stop_event is not None:
        _stop_event.set()
    return {"ok": True, "message": "正在取消…"}
