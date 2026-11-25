"""定时同步：每日指定时间拉取课表 → 变更检测 → Bark 推送。"""
from __future__ import annotations

import asyncio
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db
from .config import load_config, resolve_course_source, demo_allowed
from .models import (Course, Event, Exam, Score, apply_course_overrides, apply_exam_overrides,
                     current_week,
                     diff_courses, diff_scores, event_line, score_uid,
                     DEFAULT_SECTION_TIMES, DEFAULT_SECTION_ENDS)
from .providers import get_provider
from .providers.base import AuthExpiredError, PortalResponseError
from .push import push_all
from .study import review_message
from .user_context import get_current_user
from .weather import weather_line, daily_summary

TZ = ZoneInfo("Asia/Shanghai")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_scheduler: BackgroundScheduler | None = None
JOB_ID = "daily_timetable_sync"
REMINDER_JOB_ID = "class_reminders"
NEXT_DAY_JOB_ID = "next_day_summary"
BACKUP_JOB_ID = "daily_backup"
SYNC_CHECK_JOB_ID = "daily_sync_check"
NOTIFICATION_JOB_ID = "chaoxing_notifications"

# 自动同步窗口：用户不再自设时间，服务器按注册人数把所有人均匀铺进这个窗口，
# 一人一个时段，避免同一分钟集中打门户（也避免续期浏览器排队）。
SYNC_WINDOW_START = (6, 30)
SYNC_WINDOW_SECONDS = 3600   # 目标窗口长度（人数多时按比例压缩间隔）
SYNC_SPACING_MIN = 60        # 相邻用户最小间隔
SYNC_SPACING_MAX = 300       # 人少时也不拉得太开

# 同步串行化：手动同步与定时同步可能同时触发，若并发执行会各自基于同一份旧快照
# 算出重复变更并重复推送。按用户加非阻塞锁，不阻塞其他用户的同步。
# （apply_snapshot 另有一把短锁，保护 diff→保存→写日志这段无 await 的临界区。）
_apply_lock = threading.Lock()
# 失败按用户退避重试；忙碌不消耗尝试次数，仍可在下一轮补跑。
_sync_retries: dict[int, tuple[datetime, int]] = {}

# 普通提醒推送失败时使用 reminders 表的独立元数据键做有限退避，不占用成功去重键。
# 不新增数据库字段，进程重启后仍可借 reminders.sent_at 恢复退避。
REMINDER_RETRY_PREFIX = "__reminder_retry__:"
REMINDER_RETRY_DELAYS = (2, 4, 8, 16, 30, 60)
_reminder_guard = threading.Lock()
_reminder_inflight: set[tuple[int, str, str]] = set()


def _defer_sync_retry(uid: int, now: datetime) -> None:
    prior = _sync_retries.get(uid)
    attempts = (prior[1] if prior and prior[0].date() == now.date() else 0) + 1
    minutes = min(60, 15 * (2 ** min(attempts - 1, 2)))
    _sync_retries[uid] = (now + timedelta(minutes=minutes, seconds=uid % 60), attempts)


def free_spans(occupied: set[int], max_section: int = 12) -> list[tuple[int, int]]:
    """把空闲节次合并成连续区间，如 {1,2,5,6} → [(3,4),(7,12)]。"""
    spans = []
    s = 1
    while s <= max_section:
        if s in occupied:
            s += 1
            continue
        e = s
        while e + 1 <= max_section and e + 1 not in occupied:
            e += 1
        spans.append((s, e))
        s = e + 1
    return spans


def free_sections(cfg: dict, day: date) -> list[tuple[int, int]] | None:
    """算出某天没课的节次区间；课表尚未同步时返回 None（区别于「全天没课」）。"""
    snap = db.get_snapshot()
    courses = apply_course_overrides(
        [Course.from_dict(d) for d in snap["courses"]] if snap else [],
        cfg.get("course_overrides"))
    if not snap and not courses:
        return None
    occupied: set[int] = set()
    for c in courses_on(cfg, courses, day):
        occupied.update(range(c.start, c.end + 1))
    return free_spans(occupied)


def section_time(cfg: dict, section: int, end: bool = False) -> str:
    key = "section_ends" if end else "section_times"
    overrides = cfg.get(key) or {}
    table = DEFAULT_SECTION_ENDS if end else DEFAULT_SECTION_TIMES
    return str(overrides.get(str(section)) or table.get(str(section), ""))


def course_line(cfg: dict, c: Course) -> str:
    t = f"{section_time(cfg, c.start)}-{section_time(cfg, c.end, end=True)}"
    room = f" @{c.room}" if c.room else ""
    teacher = f"（{c.teacher}）" if c.teacher else ""
    note = f" [{c.note}]" if c.note else ""
    return f"{c.start}-{c.end}节 {t} {c.name}{room}{teacher}{note}"


def courses_on(cfg: dict, courses: list[Course], day: date) -> list[Course]:
    start_s = cfg.get("semester_start", "")
    total_weeks = cfg.get("total_weeks", 25)
    try:
        start = date.fromisoformat(start_s)
        total_weeks = int(total_weeks)
    except (ValueError, TypeError):
        # 配置缺失时沿用 current_week 的兼容行为，不把未知学期误判为已结束。
        week = current_week(start_s, day, cfg.get("total_weeks", 25))
    else:
        monday = start - timedelta(days=start.weekday())
        if day < start or total_weeks < 1 or day >= monday + timedelta(weeks=total_weeks):
            return []
        week = current_week(start_s, day, total_weeks)
    dow = day.isoweekday()
    return sorted(
        [c for c in courses if c.day == dow and c.in_week(week)],
        key=lambda c: (c.start, c.name),
    )


def format_changes(cfg: dict, changes: dict) -> str:
    lines = []
    for c in changes.get("added", []):
        lines.append(f"新增：{WEEKDAY_CN[c['day'] - 1]} {course_line(cfg, Course.from_dict(c))}")
    for c in changes.get("removed", []):
        lines.append(f"取消：{WEEKDAY_CN[c['day'] - 1]} {c['name']} {c['start']}-{c['end']}节")
    for ch in changes.get("changed", []):
        a = ch["after"]
        fields = ch.get("fields", {})
        detail = "、".join(
            f"{ {'room': '教室', 'teacher': '教师', 'campus': '校区', 'note': '备注'}.get(k, k)} {v['before'] or '无'}→{v['after'] or '无'}"
            for k, v in fields.items()
        ) or "周次调整"
        lines.append(f"调整：{WEEKDAY_CN[a['day'] - 1]} {a['name']} {a['start']}-{a['end']}节（{detail}）")
    return "\n".join(lines)


def format_score_changes(changes: dict) -> str:
    """把新成绩/成绩更新格式化成推送文本。"""
    lines = []
    for s in changes.get("added", []):
        score = s.get("score") or "成绩待公布"
        term = Score.from_dict(s).term_label()
        extra = " · ".join(x for x in (s.get("course_type"), s.get("credit")) if x)
        lines.append(f"新成绩：{s.get('name') or '未命名课程'} {score}（{term}{'，' + extra if extra else ''}）")
    for ch in changes.get("changed", []):
        a = ch["after"]
        fields = ch.get("fields", {})
        detail = "、".join(
            f"{ {'score': '成绩', 'gpa': '绩点', 'credit': '学分'}.get(k, k)} "
            f"{v['before'] or '—'}→{v['after'] or '—'}"
            for k, v in fields.items()
        )
        lines.append(f"成绩更新：{a.get('name') or '未命名课程'}（{detail}）")
    return "\n".join(lines)


async def apply_snapshot(cfg: dict, courses: list[Course], source: str,
                         push: bool = True, exams: list[Exam] | None = None,
                         exam_source: str | None = None,
                         scores: list[Score] | None = None,
                         score_source: str | None = None) -> dict:
    """把一份课表数据走完整的 diff → 保存 → 推送链路。

    定时同步与外部注入（App WebView / 浏览器扩展）共用，换登录方式不动这段逻辑。
    exams 为 None 时表示当前数据源不提供考试，保留已有考试快照。
    """
    # diff → 保存快照 → 写日志 全程无 await，用短锁串行化，避免并发读到同一份旧快照
    with _apply_lock:
        old = db.get_snapshot()
        if old:
            old_courses = [Course.from_dict(d) for d in old["courses"]]
            changes = diff_courses(old_courses, courses)
        else:
            changes = {"added": [], "removed": [], "changed": [], "has_change": False, "first_sync": True}

        # 成绩 diff 也必须在同一把锁内：避免手动+定时同步基于同一旧成绩快照重复推送。
        # scores 为 None 表示本次未拉到成绩（数据源不支持或抓取失败），保留已有快照。
        score_changes = {"added": [], "changed": [], "has_change": False}
        score_first_sync = False
        if scores is not None:
            old_scores = db.get_score_snapshot()
            # 成绩首同步只看自己的快照，不借用课程的 first_sync：
            # 用户可能早已同步课表，第一次拉成绩时课程快照并不为空，
            # 若复用课程标志会把十几门历史成绩全判为「新增」并刷屏。
            score_first_sync = old_scores is None
            old_list = [Score.from_dict(d) for d in old_scores["scores"]] if old_scores else []
            score_changes = diff_scores(old_list, scores)
            db.save_score_snapshot([s.to_dict() for s in scores], score_source or source)

        db.save_snapshot([c.to_dict() for c in courses], source)
        if exams is not None:
            db.save_exam_snapshot([e.to_dict() for e in exams], exam_source or source)
        summary = f"同步成功：{len(courses)} 门课程" + ("（首次同步）" if changes.get("first_sync") else "")
        if exams is not None:
            summary += f"，{len(exams)} 场考试"
        if scores is not None:
            summary += f"，{len(scores)} 门成绩"
        if changes.get("has_change"):
            summary += f"，检测到变更：新增 {len(changes['added'])} / 取消 {len(changes['removed'])} / 调整 {len(changes['changed'])}"
        if score_changes.get("has_change"):
            summary += f"，成绩新增 {len(score_changes['added'])} / 更新 {len(score_changes['changed'])}"
        log_changes = dict(changes)
        log_changes["scores"] = score_changes
        db.add_sync_log("ok", summary, log_changes)

    pushed = []
    if push:
        today = date.today()
        now = datetime.now(TZ)
        effective = apply_course_overrides(courses, cfg.get("course_overrides"))
        parts = []
        title = "课表已更新"
        has_change = bool(changes.get("has_change"))
        if has_change:
            parts.append("【课表变更】\n" + format_changes(cfg, changes))
        elif changes.get("first_sync"):
            parts.append(f"首次同步完成，共 {len(courses)} 门课程。")
        # 新成绩推送：首次同步只建基线、不刷屏；之后有新成绩或分数更新才推。
        # 成绩推送与课表变更共用一次 push_all，不占用 next_day 去重键。
        if (score_changes.get("has_change") and not score_first_sync
                and cfg.get("push", {}).get("new_score_enabled", True)):
            parts.append("【考试成绩】\n" + format_score_changes(score_changes))
        # 同步后推送：只推「今天剩余课程」（同步时刻之后还没结束的课）与课表变更。
        # 明日课表/日程统一由每晚定时任务（各用户 daily_time，默认 22:00）推送。
        # 不在这里提前推明日课表：2026-11-18 线上实测，中午同步提前推会占用
        # next_day 去重键，导致配置了 22:30 等自定义时间的用户当晚收不到推送；
        # 同时没有推送通道的用户也会被白占去重位。
        if cfg.get("push_next_day", True) and _push_ready(cfg):
            today_courses = courses_on(cfg, effective, today)
            remaining = _courses_after(cfg, today_courses, now)
            if remaining:
                parts.append(
                    f"【今日剩余课程】{WEEKDAY_CN[today.isoweekday() - 1]}\n"
                    + "\n".join(course_line(cfg, c) for c in remaining))
        if parts and _push_ready(cfg):
            pushed = await push_all(cfg, title, "\n\n".join(parts))
    return {"ok": True, "message": summary, "changes": changes, "pushed": pushed}


async def _fallback_bundle(cfg: dict):
    """auto 模式下主数据源失败时，临时改用另一套已配置的数据源。

    只对 primary=auto 生效：用户显式选了正方/门户时不做替换（失败应暴露给他）。
    返回 (provider, courses, exams) 或 None。
    """
    from .config import portal_configured

    if (cfg.get("provider", {}).get("primary") or "auto") != "auto":
        return None
    used = resolve_course_source(cfg)
    if used == "zhengfang" and portal_configured(cfg):
        cand = get_provider(cfg, "portal")
    elif used == "portal":
        # 门户是课表权威来源，正方仅用于补充考试 / 成绩。
        return None
    else:
        return None
    try:
        courses, exams = await cand.fetch_bundle()
    except Exception:  # noqa: BLE001
        return None
    return cand, courses, exams


async def run_sync(push: bool = True) -> dict:
    """按当前数据源拉取一次课表并应用快照（同一用户同时只允许一个同步流程）。

    课表主数据源由 provider.primary 决定（auto = 门户优先，没配门户才用正方）；
    考试始终优先从正方拉取（门户只提供课表），门户存在时也不影响考试同步。
    auto 模式下门户失败保留原课表，避免用正方覆盖门户权威快照。
    """
    from .user_context import get_current_user

    uid = get_current_user()
    lock = db.user_lock(uid)
    if not lock.acquire(blocking=False):
        return {"ok": False, "message": "已有一个同步在进行中，请稍后再试",
                "changes": {}, "pushed": [], "busy": True}
    try:
        if uid is not None:
            user = db.get_user_by_id(uid)
            if not user or not user["is_active"]:
                return {"ok": False, "message": "账号不存在或已停用",
                        "changes": {}, "pushed": []}
        # 生产环境未配置任何真实数据源时不跑同步：不写空快照、不刷「0 门课程」日志。
        # 前端会据 has_source=false 显示「暂无数据 + 去设置配置」引导。
        cfg = load_config()
        if not demo_allowed() and resolve_course_source(cfg) == "mock":
            return {"ok": False, "message": "尚未配置数据源，请到「设置 → 课表来源」接入正方教务或融合门户",
                    "changes": {}, "pushed": [], "no_source": True}
        return await _run_sync_inner(push)
    finally:
        lock.release()


class PortalRefreshError(RuntimeError):
    """自动恢复未完成，不能据此认定保存的学校登录状态已经失效。"""

    _MESSAGES = {
        "no-user": "门户自动恢复缺少用户信息，请重新登录本应用后同步",
        "no-profile": "尚未保存门户浏览器登录状态，请到「课表来源」扫码登录一次",
        "profile-unavailable": "无法读取已保存的门户浏览器状态，请管理员检查数据目录权限",
        "low-memory": "服务器可用内存不足，门户自动恢复暂未启动，稍后将重试",
        "busy": "门户自动恢复正在处理其他同步，稍后将重试",
        "interactive-login-running": "正在扫码登录门户，请完成登录后再同步",
        "no-playwright": "服务器缺少浏览器运行组件，请管理员检查安装",
        "browser-launch-failed": "门户自动恢复无法启动浏览器，请管理员检查运行环境",
        "navigation-failed": "暂时无法打开学校门户，自动恢复尚未完成，稍后将重试",
        "login-timeout": "门户登录跳转尚未完成，自动恢复稍后将重试",
        "no-cookie": "门户尚未提供可用的登录信息，自动恢复稍后将重试",
        "configuration-changed": "门户来源设置已变更，本次自动恢复已停止，请重新同步",
        "no-cookie-header": "当前门户来源未使用 Cookie 登录，无法自动替换登录信息，请检查课表来源",
        "unsupported-source": "当前课表接口无法使用学校门户浏览器状态恢复，请检查课表来源",
        "not-saved": "门户恢复结果尚未保存，请重新同步",
        "retry-rejected": "已尝试恢复门户登录，但课表接口仍未接受登录信息，请检查课表来源或稍后重试",
        "refresh-failed": "门户自动恢复暂未完成，请稍后重试或请管理员检查浏览器运行环境",
    }

    def __init__(self, reason: str):
        # 只记录已知原因；浏览器底层异常可能含 URL 或票据，不回传原始文本。
        if isinstance(reason, str) and reason.startswith("low-memory("):
            reason = "low-memory"
        self.reason = reason if reason in self._MESSAGES else "refresh-failed"
        super().__init__(self._MESSAGES[self.reason])


async def _fetch_bundle_with_cookie_refresh(cfg: dict):
    """门户取数失败且是登录态过期时，用持久 profile 静默续期并重试一次。

    常态不会走到这里：httpjson 每次请求都会顺手接收网关下发的滚动 Cookie，
    只有当会话真的被踢回统一身份认证，才付出一次无头浏览器启动的代价。
    """
    provider = get_provider(cfg)
    try:
        return provider, await provider.fetch_bundle()
    except (AuthExpiredError, PortalResponseError) as original_error:
        if resolve_course_source(cfg) != "portal":
            raise
        from .browser_login import silent_refresh_cookie
        try:
            res = await asyncio.to_thread(silent_refresh_cookie)
        except Exception:  # noqa: BLE001
            raise PortalRefreshError("refresh-failed") from None
        if res.get("expired") is True:
            raise AuthExpiredError(
                "已尝试使用保存的浏览器状态恢复门户，但学校仍要求登录，请到「课表来源」重新扫码"
            ) from original_error
        if not res.get("refreshed"):
            raise PortalRefreshError(res.get("reason", "refresh-failed")) from original_error
        cfg = load_config()
        provider = get_provider(cfg)
        try:
            bundle = await provider.fetch_bundle()
        except (AuthExpiredError, PortalResponseError):
            raise PortalRefreshError("retry-rejected") from None
        db.add_sync_log("ok", "已用保存的浏览器状态恢复门户，并成功重试课表接口")
        return provider, bundle


async def _run_sync_inner(push: bool = True) -> dict:
    cfg = load_config()
    fallback_note = ""
    try:
        provider, (courses, exams) = await _fetch_bundle_with_cookie_refresh(cfg)
    except Exception as e:  # noqa: BLE001
        fallback = await _fallback_bundle(cfg)
        if not fallback:
            msg = f"课表拉取失败：{e}"
            db.add_sync_log("fail", msg)
            # 凭证过期主动告警（按数据源每天去重），避免静默停更
            await _alert_auth_expired(cfg, resolve_course_source(cfg), e,
                                      alert_failure=resolve_course_source(cfg) == "portal")
            return {"ok": False, "message": msg, "changes": {}, "pushed": []}
        provider, courses, exams = fallback
        fallback_note = f"（{resolve_course_source(cfg)} 拉取失败，已临时回退到 {provider.name}：{e}）"
        db.add_sync_log("fail", f"主数据源拉取失败：{e}；已临时回退到 {provider.name}")

    exam_payload = exams if provider.supports_exams else None
    exam_source = provider.name
    exam_error = getattr(provider, "last_exam_error", "") or ""
    if exam_error:
        db.add_sync_log("fail", f"考试拉取失败（课表已正常更新）：{exam_error}")
        await _alert_auth_expired(cfg, "zhengfang", exam_error)
    # 门户 / 通用接口只提供课表；若另填了正方凭证，一次登录同时拉考试和成绩，
    # 避免考试、成绩各登录一次触发正方风控。课表来源不受影响。
    zf_shared = None
    if not provider.supports_exams:
        from .providers.zhengfang import ZhengfangProvider, zhengfang_configured
        if zhengfang_configured(cfg):
            try:
                zf_shared = ZhengfangProvider(cfg)
                exam_payload = await zf_shared.fetch_exams_and_scores()
                exam_source = "zhengfang"
            except Exception as e:  # noqa: BLE001
                exam_error = str(e)
                zf_shared = None
                db.add_sync_log("fail", f"考试拉取失败（课表已正常更新）：{e}")
                await _alert_auth_expired(cfg, "zhengfang", e)

    # 成绩：优先用当前 provider；若它不提供成绩但配了正方凭证，额外从正方拉取。
    # 成绩抓取失败完全隔离（不影响课表/考试），仅记录日志，保留上一次成绩快照。
    from .config import zhengfang_configured as _zf_configured
    score_payload = None
    score_source = None
    score_error = ""
    if getattr(provider, "supports_scores", False):
        try:
            score_payload = await provider.fetch_scores()
            score_source = provider.name
        except Exception as e:  # noqa: BLE001
            score_error = str(e)
    elif zf_shared is not None:
        # 考试抓取时已在同一次登录里拉过成绩，直接复用
        if zf_shared.last_score_error:
            score_error = zf_shared.last_score_error
        else:
            score_payload = zf_shared.last_scores
            score_source = "zhengfang"
    elif _zf_configured(cfg):
        from .providers.zhengfang import ZhengfangProvider
        try:
            score_payload = await ZhengfangProvider(cfg).fetch_scores()
            score_source = "zhengfang"
        except Exception as e:  # noqa: BLE001
            score_error = str(e)
    if score_error:
        db.add_sync_log("fail", f"成绩拉取失败（不影响课表）：{score_error}")

    result = await apply_snapshot(cfg, courses, provider.name, push,
                                  exams=exam_payload, exam_source=exam_source,
                                  scores=score_payload, score_source=score_source)
    if fallback_note:
        result["message"] += fallback_note
        result["fallback"] = True
    if exam_error:
        result["message"] += f"；考试拉取失败：{exam_error}"
    return result


def _parse_hhmm(day_s: str, hhmm: str) -> datetime | None:
    """把 'YYYY-MM-DD' + 'HH:MM' 拼成带时区的 datetime；解析失败返回 None。"""
    try:
        hh, mm = str(hhmm).split(":")[:2]
        return datetime.fromisoformat(f"{day_s}T{int(hh):02d}:{int(mm):02d}:00").replace(tzinfo=TZ)
    except (ValueError, AttributeError):
        return None


def _push_ready(cfg: dict) -> bool:
    """是否至少配置了一个推送通道（Bark / 群机器人）。"""
    p = cfg.get("push", {})
    return bool(p.get("bark_enabled")
                or (p.get("wecom_enabled") and (p.get("wecom_webhook") or "").strip()))


def _reminder_retry_state(user_id: int, day: str, key: str) -> tuple[int, datetime | None]:
    prefix = f"{REMINDER_RETRY_PREFIX}{key}:"
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT item_key, sent_at FROM reminders WHERE user_id=? AND day=? "
            "AND substr(item_key, 1, ?)=?",
            (user_id, day, len(prefix), prefix),
        ).fetchall()
    attempts, latest = 0, None
    for row in rows:
        try:
            attempts = max(attempts, int(row["item_key"][len(prefix):]))
            sent_at = datetime.fromisoformat(row["sent_at"])
        except (ValueError, TypeError):
            continue
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=TZ)
        if latest is None or sent_at > latest:
            latest = sent_at
    return attempts, latest


def _claim_reminder(day: str, key: str, now: datetime) -> tuple[int, str, str] | None:
    user_id = get_current_user() or 1
    token = (user_id, day, key)
    with _reminder_guard:
        if token in _reminder_inflight or db.reminder_sent(day, key):
            return None
        attempts, latest = _reminder_retry_state(user_id, day, key)
        if attempts:
            if attempts >= len(REMINDER_RETRY_DELAYS) or latest is None:
                return None
            delay = REMINDER_RETRY_DELAYS[attempts - 1]
            if now < latest + timedelta(minutes=delay):
                return None
        _reminder_inflight.add(token)
    return token


def _finish_reminder(token: tuple[int, str, str], key: str, ok: bool, now: datetime):
    user_id, day, _ = token
    prefix = f"{REMINDER_RETRY_PREFIX}{key}:"
    with _reminder_guard:
        try:
            if ok:
                db.mark_reminder(day, key)
                with db.get_conn() as conn:
                    conn.execute(
                        "DELETE FROM reminders WHERE user_id=? AND day=? "
                        "AND substr(item_key, 1, ?)=?",
                        (user_id, day, len(prefix), prefix),
                    )
                return
            attempts, _ = _reminder_retry_state(user_id, day, key)
            attempt = min(attempts + 1, len(REMINDER_RETRY_DELAYS))
            retry_key = f"{prefix}{attempt}"
            with db.get_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO reminders (user_id, day, item_key, sent_at) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, day, retry_key, now.isoformat(timespec="seconds")),
                )
                db.prune_reminders(conn, user_id, now.astimezone(TZ))
        finally:
            _reminder_inflight.discard(token)


def _release_reminder(token: tuple[int, str, str]):
    with _reminder_guard:
        _reminder_inflight.discard(token)


async def _push_reminder(cfg: dict, day: str, key: str, title: str,
                         body: str, now: datetime) -> dict | None:
    """推送普通提醒：任一通道成功才标记，全失败时按既有 reminders 表退避重试。"""
    token = _claim_reminder(day, key, now)
    if token is None:
        return None
    try:
        try:
            results = await push_all(cfg, title, body)
        except Exception:  # noqa: BLE001
            # 单次异常不阻断其他提醒；原始异常可能含 webhook，不对外返回。
            results = []
        ok = any(result.get("ok") is True for result in results)
        _finish_reminder(token, key, ok, now)
        return {"title": title, "key": key, "results": results}
    finally:
        _release_reminder(token)


async def _alert_auth_expired(cfg: dict, source: str, err,
                              alert_failure: bool = False) -> list[dict]:
    """登录态过期时主动推一条提醒，按数据源每天去重，修复后不再打扰。

    门户仅在确认学校要求重新登录时发登录告警；自动恢复的技术失败单独提示。
    正方的附加查询保留错误文本，因此只对它兼容明确的登录失败信息。
    """
    if not cfg.get("alert_on_fail", True):
        return []
    if isinstance(err, PortalRefreshError) and err.reason in (
            "busy", "interactive-login-running", "configuration-changed"):
        return []  # 等待已有任务或配置变更完成，不用手机通知打断用户。
    expired = isinstance(err, AuthExpiredError) or (
        source == "zhengfang" and isinstance(err, str) and any(
            word in err for word in ("会话已过期", "登录失败", "未登录", "登录失效")))
    if not (expired or alert_failure) or not _push_ready(cfg):
        return []
    now = datetime.now(TZ)
    day_s = now.date().isoformat()
    key = f"auth_alert:{source}" if expired else f"sync_alert:{source}"
    if not expired:
        if isinstance(err, PortalRefreshError):
            title = "门户自动恢复暂未完成"
            body = f"{err}。\n已保留原有课表。"
        else:
            title = "门户课表同步失败"
            body = "融合门户课表暂时无法更新，已保留原有课表。请检查门户数据源配置或稍后重试。"
    elif source == "zhengfang":
        title = "正方教务登录失效"
        body = ("正方教务登录已失效，考试与空教室暂时无法同步 / 查询。\n"
                "请到「课表来源 → 正方教务」重新填写学号密码或更新 Cookie。")
    else:
        title = "门户登录已过期"
        body = ("已尝试自动恢复门户，学校仍要求重新登录，已保留原有课表。\n"
                "请到「课表来源」重新扫码登录。")
    pushed = await _push_reminder(cfg, day_s, key, title, body, now)
    return [pushed] if pushed else []


def _courses_after(cfg: dict, courses: list[Course], moment: datetime) -> list[Course]:
    """过滤出「moment 时刻之后仍未结束」的课（结束时间 > 当前时刻）。"""
    out = []
    for c in courses:
        end_at = _parse_hhmm(moment.date().isoformat(), section_time(cfg, c.end, end=True))
        if end_at and end_at > moment:
            out.append(c)
    return out


def _hhmm(value, fallback: str) -> str:
    """把配置里的时间规范成 HH:MM；非法值回退默认，避免任务崩溃。"""
    try:
        hh, mm = str(value).split(":")[:2]
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    except (ValueError, AttributeError, TypeError):
        pass
    return fallback


async def push_next_day_summary(now: datetime | None = None) -> list[dict]:
    """每天按用户配置的时间推送「明日安排」，时间与内容均可在设置里调，按天去重。

    与同步推送解耦：同步时若今天还有课只推「今日剩余」，明天的安排交给本任务统一推。
    每分钟轮询一次，配置时间（默认 22:00）至当日结束之间均可补触发，按天去重。
    """
    cfg = load_config()
    if not cfg.get("push_next_day", True):
        return []
    if not _push_ready(cfg):
        return []
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    p = cfg.get("push", {})
    daily_at = _parse_hhmm(now.date().isoformat(), _hhmm(p.get("daily_time"), "22:00"))
    if daily_at is None or now < daily_at:
        return []
    tomorrow = (now + timedelta(days=1)).date()
    tomorrow_s = tomorrow.isoformat()
    dedup_key = "next_day"
    if db.reminder_sent(tomorrow_s, dedup_key):
        return []
    show_courses = p.get("daily_courses", True)
    show_events = p.get("daily_events", True)
    show_weather = p.get("daily_weather", False)
    head = f"{tomorrow.strftime('%m月%d日')} {WEEKDAY_CN[tomorrow.isoweekday() - 1]}"
    parts = []
    if show_courses:
        snap = db.get_snapshot()
        courses = apply_course_overrides(
            [Course.from_dict(d) for d in snap["courses"]] if snap else [],
            cfg.get("course_overrides"))
        if snap or courses:
            next_courses = courses_on(cfg, courses, tomorrow)
            if next_courses:
                parts.append(f"【明日课表】{head}\n"
                             + "\n".join(course_line(cfg, c) for c in next_courses))
            else:
                parts.append(f"【明日课表】{head} 没有课，好好休息。")
    if show_events:
        lines = [event_line(e) for e in
                 (Event.from_dict(d) for d in
                  db.list_events(date_from=tomorrow_s, date_to=tomorrow_s))]
        exam_snap = db.get_exam_snapshot()
        exams = apply_exam_overrides(
            [Exam.from_dict(d) for d in exam_snap["exams"]] if exam_snap else [],
            cfg.get("exam_overrides"))
        for e in exams:
            if e.date == tomorrow_s and not e.is_finished():
                room = f" @{e.room}" if e.room else ""
                lines.append(f"[考试] {e.display_time()} {e.name}{room}")
        if lines:
            parts.append("【明日日程】\n" + "\n".join(lines))
    if show_weather:
        wline = await daily_summary(cfg, tomorrow_s)
        if wline:
            place = str((cfg.get("weather") or {}).get("place") or "青岛黄岛区")
            parts.append(f"【明日天气】{place}：{wline}")
    if not parts:
        return []  # 内容全关或暂无数据时不打扰，也不占用去重标记
    title = "明日课表提醒" if (show_courses and len(parts) == 1) else "明日安排提醒"
    pushed = await _push_reminder(cfg, tomorrow_s, dedup_key, title, "\n\n".join(parts), now)
    return [pushed] if pushed else []


async def run_reminders(now: datetime | None = None) -> list[dict]:
    """每分钟检查一次：课程 / 日程 / 考试开始前 N 分钟各推一次。

    reminders 表按 (用户, 日期, 事项) 去重，推过就不再推；提前量在设置里调。
    上课提醒会附带当堂天气；同一天连续两节（上一节结束+1 = 本节开始）只在
    第一节提醒里带天气。
    """
    cfg = load_config()
    p = cfg.get("push", {})
    if not _push_ready(cfg):
        return []
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    pushed: list[dict] = []
    if p.get("reminder_enabled", True):
        pushed += await _push_upcoming(cfg, p, now)
    pushed += await push_exam_reviews(cfg, now)
    return pushed


async def _push_upcoming(cfg: dict, p: dict, now: datetime) -> list[dict]:
    try:
        advance = int(p.get("reminder_advance") or 15)
    except (TypeError, ValueError):
        advance = 15
    advance = max(1, min(advance, 180))
    day_s = now.date().isoformat()
    window = timedelta(minutes=advance)

    items: list[tuple[str, str, datetime, str]] = []  # (标题, 去重键, 开始时间, 正文)

    snap = db.get_snapshot()
    courses = apply_course_overrides(
        [Course.from_dict(d) for d in snap["courses"]] if snap else [],
        cfg.get("course_overrides"))
    if courses:
        day_courses = courses_on(cfg, courses, now.date())
        for c in day_courses:
            start_at = _parse_hhmm(day_s, section_time(cfg, c.start))
            if not start_at:
                continue
            room = f" @{c.room}" if c.room else ""
            teacher = f"（{c.teacher}）" if c.teacher else ""
            body = (f"{c.start}-{c.end}节 {section_time(cfg, c.start)}"
                    f"-{section_time(cfg, c.end, end=True)} {c.name}{room}{teacher}")
            # 连堂课只在第一节带天气：本节紧接前一节（上一节结束 + 1 = 本节开始）
            adjacent = any(o is not c and o.end + 1 == c.start for o in day_courses)
            in_window = start_at - window <= now < start_at
            if in_window and not adjacent:
                line = await weather_line(cfg, day_s, section_time(cfg, c.start))
                if line:
                    body += "\n" + line
            items.append(("上课提醒", f"course:{c.name}:{c.start}:{c.room}", start_at, body))

    for d in db.list_events(date_from=day_s, date_to=day_s):
        e = Event.from_dict(d)
        if not e.start:
            continue  # 全天日程没有「开始前」的概念
        start_at = _parse_hhmm(day_s, e.start)
        if start_at:
            items.append(("日程提醒", f"event:{e.id}", start_at, event_line(e)))

    exam_snap = db.get_exam_snapshot()
    day_exams = apply_exam_overrides(
        [Exam.from_dict(d) for d in exam_snap["exams"]] if exam_snap else [],
        cfg.get("exam_overrides"))
    if day_exams:
        for e in day_exams:
            if e.date != day_s or not e.start:
                continue
            start_at = _parse_hhmm(day_s, e.start)
            if not start_at:
                continue
            room = f" @{e.room}" if e.room else ""
            seat = f" 座位 {e.seat}" if e.seat else ""
            items.append(("考试提醒", f"exam:{e.name}:{e.start}", start_at, f"{e.name}{room}{seat}"))

    pushed: list[dict] = []
    for title, key, start_at, body in items:
        if not (start_at - window <= now < start_at):
            continue  # 没进提醒窗口，或已经开始了
        remaining = max(1, int((start_at - now).total_seconds() // 60))
        result = await _push_reminder(cfg, day_s, key, title, f"还有 {remaining} 分钟\n{body}", now)
        if result:
            pushed.append(result)
    return pushed


async def push_exam_reviews(cfg: dict, now: datetime | None = None) -> list[dict]:
    """考试前 N 天推一次复习提醒（默认 15 天），同一场考试只推一次。

    去重键用考试日期作为 day，跨天轮询也不会重复；早上 8 点后才推，
    避免半夜响铃。手动添加 / 修改的考试同样生效。
    """
    p = cfg.get("push", {})
    if not p.get("exam_review_enabled", True):
        return []
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    if now.hour < 8:
        return []
    try:
        days = int(p.get("exam_review_days") or 15)
    except (TypeError, ValueError):
        days = 15
    days = max(1, min(days, 60))
    snap = db.get_exam_snapshot()
    exams = apply_exam_overrides(
        [Exam.from_dict(d) for d in snap["exams"]] if snap else [],
        cfg.get("exam_overrides"))
    pushed: list[dict] = []
    for e in exams:
        if not e.date:
            continue
        try:
            left = (date.fromisoformat(e.date) - now.date()).days
        except ValueError:
            continue
        if not (1 <= left <= days):
            continue
        key = f"review:{e.name}:{e.start}"
        result = await _push_reminder(cfg, e.date, key, "考试复习提醒",
                                      review_message(e, left), now)
        if result:
            pushed.append(result)
    return pushed


def _reminder_job_wrapper():
    """提醒轮询：和定时同步一样逐用户切换上下文。"""
    from .user_context import current_user_id, set_current_user

    user_ids = db.list_user_ids() or [1]
    for uid in user_ids:
        ctx = set_current_user(uid)
        try:
            asyncio.run(run_reminders())
        except Exception as e:  # noqa: BLE001
            db.add_sync_log("fail", f"课前提醒异常：{type(e).__name__}: {e}")
        finally:
            current_user_id.reset(ctx)


def sync_spacing_seconds(n: int) -> int:
    """相邻用户同步间隔：人数越多间隔越小，但限制在 1~5 分钟。"""
    if n <= 1:
        return SYNC_SPACING_MIN
    return min(SYNC_SPACING_MAX, max(SYNC_SPACING_MIN, SYNC_WINDOW_SECONDS // n))


def sync_slots(day: date | None = None) -> dict[int, datetime]:
    """按注册顺序（id 升序）给每个活跃用户分配当天的同步时刻。"""
    ids = db.list_user_ids() or [1]
    day = day or datetime.now(TZ).date()
    base = datetime(day.year, day.month, day.day,
                    SYNC_WINDOW_START[0], SYNC_WINDOW_START[1], tzinfo=TZ)
    gap = sync_spacing_seconds(len(ids))
    return {uid: base + timedelta(seconds=i * gap) for i, uid in enumerate(ids)}


def sync_slot_hhmm(uid: int | None = None) -> str:
    """当前用户被分配的每日同步时刻（HH:MM），供状态栏展示。"""
    from .user_context import get_current_user

    uid = uid if uid is not None else get_current_user()
    slot = sync_slots().get(uid) if uid is not None else None
    return slot.strftime("%H:%M") if slot else "--:--"


def _sync_check_wrapper():
    """每分钟检查：到点且今天还没同步的用户逐个同步（服务器自动错峰）。

    用户不再自设时间：时刻按注册人数均匀分配。判定条件是「已过自己的时段」
    而不是「正好这一分钟」，所以进程重启、错过时段当天也能补跑。
    """
    from .user_context import current_user_id, set_current_user

    now = datetime.now(TZ)
    today = now.date().isoformat()
    slots = sync_slots(now.date())
    for uid in (db.list_user_ids() or [1]):
        slot = slots.get(uid)
        if slot is None or now < slot:
            continue
        ctx = set_current_user(uid)
        try:
            if db.reminder_sent(today, "daily_sync"):
                _sync_retries.pop(uid, None)
                continue
            retry = _sync_retries.get(uid)
            if retry and retry[0].date() == now.date() and now < retry[0]:
                continue
            result = asyncio.run(run_sync(push=True))
            if result.get("ok"):
                db.mark_reminder(today, "daily_sync")
                _sync_retries.pop(uid, None)
            elif not result.get("busy"):
                _defer_sync_retry(uid, datetime.now(TZ))
        except Exception as e:  # noqa: BLE001
            _defer_sync_retry(uid, datetime.now(TZ))
            db.add_sync_log("fail", f"定时同步异常：{type(e).__name__}: {e}")
        finally:
            current_user_id.reset(ctx)


def _job_wrapper():
    """定时任务：逐个用户切换上下文后同步，配置 / 快照 / 推送互不串扰。"""
    from .user_context import current_user_id, set_current_user

    user_ids = db.list_user_ids() or [1]
    for uid in user_ids:
        ctx = set_current_user(uid)
        try:
            asyncio.run(run_sync(push=True))
        except Exception as e:  # noqa: BLE001
            db.add_sync_log("fail", f"定时同步异常：{type(e).__name__}: {e}")
        finally:
            current_user_id.reset(ctx)


def _next_day_job_wrapper():
    """每分钟逐用户检查「明日安排」推送时间（各用户配置不同），命中且当天未推才推。"""
    from .user_context import current_user_id, set_current_user

    user_ids = db.list_user_ids() or [1]
    for uid in user_ids:
        ctx = set_current_user(uid)
        try:
            asyncio.run(push_next_day_summary())
        except Exception as e:  # noqa: BLE001
            db.add_sync_log("fail", f"明日课表推送异常：{type(e).__name__}: {e}")
        finally:
            current_user_id.reset(ctx)


def _notification_job_wrapper():
    """学习通轮询使用独立任务和事件循环，不访问应用的异步 Redis 客户端。"""
    from . import notifications

    async def poll():
        semaphore = asyncio.Semaphore(3)

        async def poll_user(user_id: int):
            async with semaphore:
                try:
                    await notifications.sync(user_id, scheduled=True)
                except notifications.BusyError:
                    pass
                except Exception:
                    # 单个用户失败不影响其他用户，不记录可能包含会话的异常。
                    pass

        user_ids = await asyncio.to_thread(db.list_user_ids)
        await asyncio.gather(*(poll_user(user_id) for user_id in user_ids))

    asyncio.run(poll())


def _backup_job_wrapper():
    """每日备份整库与全局配置；不分用户，失败只打日志，不影响其他任务。"""
    from .backup import backup_now

    try:
        path = backup_now()
        print(f"[backup] 已备份到 {path}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[backup] 备份失败：{type(e).__name__}: {e}", flush=True)
    try:
        removed = db.prune_logs(keep_days=30)
        if removed:
            print(f"[backup] 已清理 {removed} 条过期日志", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[backup] 日志清理失败：{type(e).__name__}: {e}", flush=True)


def start_scheduler(cfg: dict):
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.start()
    # 课前提醒每分钟跑一次；提前量每次运行时读配置，改设置不用重排任务
    _scheduler.add_job(
        _reminder_job_wrapper, IntervalTrigger(minutes=1, timezone=TZ),
        id=REMINDER_JOB_ID, replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=120,
    )
    # 「明日安排」每分钟检查一次各用户配置的推送时间（默认 22:00），按天去重
    _scheduler.add_job(
        _next_day_job_wrapper, IntervalTrigger(minutes=1, timezone=TZ),
        id=NEXT_DAY_JOB_ID, replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=120,
    )
    # 每日同步：每分钟检查，到点且今天未同步才跑；时刻由 sync_slots() 按注册人数错峰分配
    _scheduler.add_job(
        _sync_check_wrapper, IntervalTrigger(minutes=1, timezone=TZ),
        id=SYNC_CHECK_JOB_ID, replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=120,
    )
    _scheduler.add_job(
        _notification_job_wrapper, IntervalTrigger(minutes=1, timezone=TZ),
        id=NOTIFICATION_JOB_ID, replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=120,
    )
    # 每天凌晨备份一次数据库；进程启动时当天还没备份就补一份
    _scheduler.add_job(
        _backup_job_wrapper, CronTrigger(hour=3, minute=20, timezone=TZ),
        id=BACKUP_JOB_ID, replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=3600,
    )
    try:
        from .backup import BACKUP_DIR

        today = datetime.now(TZ).strftime("%Y%m%d")
        if not any(BACKUP_DIR.glob(f"campus-{today}*.db")):
            _backup_job_wrapper()
    except Exception as e:  # noqa: BLE001
        print(f"[backup] 启动备份失败：{type(e).__name__}: {e}", flush=True)


def reschedule(cfg: dict):
    """兼容旧调用：同步时刻改为按注册人数自动分配（见 sync_slots），无需重排 cron。

    保留函数是为了不破坏 post_settings 等既有调用点；真正的调度逻辑见
    _sync_check_wrapper。
    """
    return None


def next_run_time() -> str | None:
    """当前用户下一次自动同步时刻（服务器分配；今天已完成则顺延到明天）。"""
    from .user_context import get_current_user

    now = datetime.now(TZ)
    uid = get_current_user()
    slot = sync_slots(now.date()).get(uid) if uid is not None else None
    if slot is None:
        return None
    try:
        if not db.reminder_sent(now.date().isoformat(), "daily_sync"):
            # 已过时段但今天还没同步：一分钟后补跑
            return (slot if now < slot else now).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        pass
    return (slot + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


def shutdown_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
