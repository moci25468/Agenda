"""定时同步：每日指定时间拉取课表 → 变更检测 → Bark 推送。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db
from .config import load_config
from .models import (Course, Event, Exam, apply_course_overrides, apply_exam_overrides,
                     current_week,
                     diff_courses, event_line, DEFAULT_SECTION_TIMES, DEFAULT_SECTION_ENDS)
from .providers import get_provider
from .push import push_all
from .study import review_message
from .weather import weather_line

TZ = ZoneInfo("Asia/Shanghai")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_scheduler: BackgroundScheduler | None = None
JOB_ID = "daily_timetable_sync"
REMINDER_JOB_ID = "class_reminders"
NEXT_DAY_JOB_ID = "next_day_summary"


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
    week = current_week(cfg.get("semester_start", ""), day, cfg.get("total_weeks", 25))
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


async def apply_snapshot(cfg: dict, courses: list[Course], source: str,
                         push: bool = True, exams: list[Exam] | None = None,
                         exam_source: str | None = None) -> dict:
    """把一份课表数据走完整的 diff → 保存 → 推送链路。

    定时同步与外部注入（App WebView / 浏览器扩展）共用，换登录方式不动这段逻辑。
    exams 为 None 时表示当前数据源不提供考试，保留已有考试快照。
    """
    old = db.get_snapshot()
    if old:
        old_courses = [Course.from_dict(d) for d in old["courses"]]
        changes = diff_courses(old_courses, courses)
    else:
        changes = {"added": [], "removed": [], "changed": [], "has_change": False, "first_sync": True}

    db.save_snapshot([c.to_dict() for c in courses], source)
    if exams is not None:
        db.save_exam_snapshot([e.to_dict() for e in exams], exam_source or source)
    summary = f"同步成功：{len(courses)} 门课程" + ("（首次同步）" if changes.get("first_sync") else "")
    if exams is not None:
        summary += f"，{len(exams)} 场考试"
    if changes.get("has_change"):
        summary += f"，检测到变更：新增 {len(changes['added'])} / 取消 {len(changes['removed'])} / 调整 {len(changes['changed'])}"
    db.add_sync_log("ok", summary, changes)

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
        # 同步后推送：优先推「今天剩余课程」（同步时刻之后还没结束的课）；
        # 今天没课了才推「明日课表」。明日课表的定时推送由每晚 22:00 任务负责，
        # 两者共用 reminders 表去重，同一天「明日课表」只推一次。
        if cfg.get("push_next_day", True):
            today_courses = courses_on(cfg, effective, today)
            remaining = _courses_after(cfg, today_courses, now)
            if remaining:
                parts.append(
                    f"【今日剩余课程】{WEEKDAY_CN[today.isoweekday() - 1]}\n"
                    + "\n".join(course_line(cfg, c) for c in remaining))
            else:
                tomorrow = today + timedelta(days=1)
                tomorrow_s = tomorrow.isoformat()
                if not db.reminder_sent(tomorrow_s, "next_day"):
                    next_courses = courses_on(cfg, effective, tomorrow)
                    if next_courses:
                        parts.append(f"【明日课表】{tomorrow.strftime('%m月%d日')} {WEEKDAY_CN[tomorrow.isoweekday() - 1]}\n"
                                     + "\n".join(course_line(cfg, c) for c in next_courses))
                    else:
                        parts.append(f"【明日课表】{tomorrow.strftime('%m月%d日')} 没有课，好好休息。")
                    next_events = [Event.from_dict(d) for d in
                                   db.list_events(date_from=tomorrow_s, date_to=tomorrow_s)]
                    if next_events:
                        parts.append("【明日日程】\n" + "\n".join(event_line(e) for e in next_events))
                    if not has_change:
                        title = "明日安排提醒" if next_events else "明日课表提醒"
                    db.mark_reminder(tomorrow_s, "next_day")
        if parts:
            pushed = await push_all(cfg, title, "\n\n".join(parts))
    return {"ok": True, "message": summary, "changes": changes, "pushed": pushed}


async def run_sync(push: bool = True) -> dict:
    """按当前数据源拉取一次课表并应用快照。

    课表主数据源由 provider.primary 决定（门户优先、正方兜底）；考试始终优先从正方
    拉取（门户只提供课表），门户存在时也不影响考试同步。
    """
    cfg = load_config()
    try:
        provider = get_provider(cfg)
        courses, exams = await provider.fetch_bundle()
    except Exception as e:  # noqa: BLE001
        msg = f"课表拉取失败：{e}"
        db.add_sync_log("fail", msg)
        # 凭证过期等失败主动告警，避免静默停更
        if cfg.get("alert_on_fail", True) and ("过期" in str(e) or "Cookie" in str(e) or "cURL" in str(e)):
            await push_all(cfg, "课表同步失败", f"{msg}\n\n请打开门户重新登录，或在「课表来源」重新接入。")
        return {"ok": False, "message": msg, "changes": {}, "pushed": []}

    exam_payload = exams if provider.supports_exams else None
    exam_source = provider.name
    exam_error = getattr(provider, "last_exam_error", "") or ""
    if exam_error:
        db.add_sync_log("fail", f"考试拉取失败（课表已正常更新）：{exam_error}")
    # 门户 / 通用接口只提供课表；若另填了正方凭证，额外拉考试，不影响课表同步。
    if not provider.supports_exams:
        from .providers.zhengfang import ZhengfangProvider, zhengfang_configured
        if zhengfang_configured(cfg):
            try:
                exam_payload = await ZhengfangProvider(cfg).fetch_exams()
                exam_source = "zhengfang"
            except Exception as e:  # noqa: BLE001
                exam_error = str(e)
                db.add_sync_log("fail", f"考试拉取失败（课表已正常更新）：{e}")

    result = await apply_snapshot(cfg, courses, provider.name, push,
                                  exams=exam_payload, exam_source=exam_source)
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


def _courses_after(cfg: dict, courses: list[Course], moment: datetime) -> list[Course]:
    """过滤出「moment 时刻之后仍未结束」的课（结束时间 > 当前时刻）。"""
    out = []
    for c in courses:
        end_at = _parse_hhmm(moment.date().isoformat(), section_time(cfg, c.end, end=True))
        if end_at and end_at > moment:
            out.append(c)
    return out


async def push_next_day_summary(now: datetime | None = None) -> list[dict]:
    """每晚 22:00 定时推送「明日课表 + 明日日程」，按天去重（每天只推一次）。

    与同步推送解耦：同步时若今天还有课只推「今日剩余」，明天的课交给本任务统一推。
    """
    cfg = load_config()
    if not cfg.get("push_next_day", True):
        return []
    if not _push_ready(cfg):
        return []
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    tomorrow = (now + timedelta(days=1)).date()
    tomorrow_s = tomorrow.isoformat()
    dedup_key = "next_day"
    if db.reminder_sent(tomorrow_s, dedup_key):
        return []
    snap = db.get_snapshot()
    if not snap:
        return []
    courses = apply_course_overrides(
        [Course.from_dict(d) for d in snap["courses"]], cfg.get("course_overrides"))
    next_courses = courses_on(cfg, courses, tomorrow)
    parts = []
    if next_courses:
        parts.append(f"【明日课表】{tomorrow.strftime('%m月%d日')} {WEEKDAY_CN[tomorrow.isoweekday() - 1]}\n"
                     + "\n".join(course_line(cfg, c) for c in next_courses))
    else:
        parts.append(f"【明日课表】{tomorrow.strftime('%m月%d日')} 没有课，好好休息。")
    next_events = [Event.from_dict(d) for d in
                   db.list_events(date_from=tomorrow_s, date_to=tomorrow_s)]
    if next_events:
        parts.append("【明日日程】\n" + "\n".join(event_line(e) for e in next_events))
    title = "明日安排提醒" if next_events else "明日课表提醒"
    # 先标记再推，推送失败也不重复打扰
    db.mark_reminder(tomorrow_s, dedup_key)
    results = await push_all(cfg, title, "\n\n".join(parts))
    return [{"title": title, "key": dedup_key, "results": results}]


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
    if snap:
        courses = apply_course_overrides(
            [Course.from_dict(d) for d in snap["courses"]], cfg.get("course_overrides"))
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
    if exam_snap:
        day_exams = apply_exam_overrides(
            [Exam.from_dict(d) for d in exam_snap["exams"]],
            cfg.get("exam_overrides"))
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
        if db.reminder_sent(day_s, key):
            continue
        db.mark_reminder(day_s, key)  # 先标记再推，推送失败也不重复打扰
        remaining = max(1, int((start_at - now).total_seconds() // 60))
        results = await push_all(cfg, title, f"还有 {remaining} 分钟\n{body}")
        pushed.append({"title": title, "key": key, "results": results})
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
    if not snap:
        return []
    exams = apply_exam_overrides(
        [Exam.from_dict(d) for d in snap["exams"]], cfg.get("exam_overrides"))
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
        if db.reminder_sent(e.date, key):
            continue
        db.mark_reminder(e.date, key)
        results = await push_all(cfg, "考试复习提醒", review_message(e, left))
        pushed.append({"title": "考试复习提醒", "key": key, "results": results})
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
    """每晚 22:00 逐用户推「明日课表 + 明日日程」。"""
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
    # 每晚 22:00 推「明日课表 + 明日日程」，按天去重
    _scheduler.add_job(
        _next_day_job_wrapper, CronTrigger(hour=22, minute=0, timezone=TZ),
        id=NEXT_DAY_JOB_ID, replace_existing=True, misfire_grace_time=3600,
    )
    reschedule(cfg)


def reschedule(cfg: dict):
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(JOB_ID)
    except Exception:  # noqa: BLE001
        pass
    try:
        parts = (cfg.get("sync_time") or "12:00").split(":")
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except (ValueError, IndexError, TypeError):
        hh, mm = 12, 0   # 时间填错时回退默认，避免启动崩溃
    _scheduler.add_job(
        _job_wrapper, CronTrigger(hour=hh, minute=mm, timezone=TZ),
        id=JOB_ID, replace_existing=True, misfire_grace_time=3600,
    )


def next_run_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    if job and job.next_run_time:
        return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
    return None


def shutdown_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None