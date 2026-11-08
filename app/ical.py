"""课表导出为 iCalendar（.ics）文件。

把整学期的课程展开成一条条带具体日期/时间的 VEVENT，
iPhone / 安卓 / Outlook / Google 日历均可直接导入。
不依赖任何第三方库。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from .models import Course, Exam, current_week, week_dates
from .scheduler import section_time

TZID = "Asia/Shanghai"


def _fmt_dt(d: date, hhmm: str) -> str:
    """date + 'HH:MM' → 'YYYYMMDDTHHMMSS'（本地时间，带 TZID）。"""
    h, m = (hhmm or "00:00").split(":")[:2]
    return f"{d.strftime('%Y%m%d')}T{int(h):02d}{int(m):02d}00"


def _fmt_dt_utc(d: date, hhmm: str) -> str:
    """同上，但转成 UTC 时间戳（DTSTAMP / 备用）。"""
    h, m = (hhmm or "00:00").split(":")[:2]
    local = datetime(d.year, d.month, d.day, int(h), int(m), 0)
    # 简化：本地视为东八区，转 UTC 需 -8h；此处仅用于 DTSTAMP，用当前时间即可
    return local.strftime("%Y%m%dT%H%M%SZ")


def _escape(s: str) -> str:
    """iCalendar 文本字段转义：反斜杠、分号、逗号、换行。"""
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _uid(c: Course, d: date) -> str:
    raw = f"{c.name}|{c.day}|{c.start}|{c.end}|{d.isoformat()}|{c.room}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _course_events(cfg: dict, courses: list[Course]) -> list[dict]:
    """把课程展开成具体事件列表（已去重、按时间排序）。"""
    semester_start = cfg.get("semester_start", "")
    total_weeks = cfg.get("total_weeks", 30) or 30
    events: list[dict] = []
    seen: set[str] = set()

    for c in courses:
        weeks = c.weeks or list(range(1, total_weeks + 1))
        start_hhmm = section_time(cfg, c.start) or "08:00"
        end_hhmm = section_time(cfg, c.end, end=True) or "08:50"
        for w in weeks:
            if not (1 <= w <= total_weeks):
                continue
            try:
                # 第 w 周的周一到周日日期
                dates = week_dates(semester_start, w)
            except (ValueError, TypeError):
                continue
            if not (1 <= c.day <= 7):
                continue
            day = dates[c.day - 1]
            key = f"{c.name}|{day.isoformat()}|{c.start}|{c.end}"
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "uid": _uid(c, day),
                "dtstart": _fmt_dt(day, start_hhmm),
                "dtend": _fmt_dt(day, end_hhmm),
                "summary": c.name,
                "location": c.room,
                "description": _desc(c),
            })
    events.sort(key=lambda e: (e["dtstart"], e["summary"]))
    return events


def _desc(c: Course) -> str:
    parts = []
    if c.teacher:
        parts.append(f"教师：{c.teacher}")
    if c.note:
        parts.append(c.note)
    if c.campus:
        parts.append(f"校区：{c.campus}")
    parts.append(f"第 {c.start}-{c.end} 节")
    return "；".join(p for p in parts if p)


def _exam_desc(e: Exam) -> str:
    parts = []
    if e.room:
        parts.append(f"考场：{e.room}")
    if e.seat:
        parts.append(f"座位号：{e.seat}")
    if e.campus:
        parts.append(f"校区：{e.campus}")
    if e.note:
        parts.append(e.note)
    return "；".join(p for p in parts if p)


def _exam_events(exams: list[Exam]) -> list[dict]:
    """把考试展开成带具体日期/时间的 VEVENT（考试是固定时间，非按周）。"""
    events: list[dict] = []
    for e in exams:
        if not e.date:
            continue
        # 缺省时间兜底：没有起止就全天/占 09:00-11:00，避免生成非法事件
        start_hhmm = e.start or "09:00"
        end_hhmm = e.end or start_hhmm
        try:
            d = date.fromisoformat(e.date)
        except ValueError:
            continue
        raw = f"exam|{e.name}|{e.date}|{e.start}|{e.end}|{e.room}|{e.seat}"
        uid = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
        events.append({
            "uid": uid,
            "dtstart": _fmt_dt(d, start_hhmm),
            "dtend": _fmt_dt(d, end_hhmm),
            "summary": f"考试：{e.name}",
            "location": e.room,
            "description": _exam_desc(e),
        })
    events.sort(key=lambda x: (x["dtstart"], x["summary"]))
    return events


def build_ical(cfg: dict, courses: list[Course], exams: list[Exam] | None = None) -> str:
    """生成完整的 iCalendar 文本（课程 + 可选考试合并到同一日历）。"""
    now = datetime.now()
    dtstamp = now.strftime("%Y%m%dT%H%M%SZ")
    school = cfg.get("school", "") or "课程表"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Agenda ZhiXu//Course Export//CN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(school)}课表",
        f"X-WR-TIMEZONE:{TZID}",
        "BEGIN:VTIMEZONE",
        f"TZID:{TZID}",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    all_events = _course_events(cfg, courses) + _exam_events(exams or [])
    all_events.sort(key=lambda x: (x["dtstart"], x["summary"]))
    for e in all_events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['uid']}@zhi-xu",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID={TZID}:{e['dtstart']}",
            f"DTEND;TZID={TZID}:{e['dtend']}",
            f"SUMMARY:{_escape(e['summary'])}",
        ]
        if e["location"]:
            lines.append(f"LOCATION:{_escape(e['location'])}")
        if e["description"]:
            lines.append(f"DESCRIPTION:{_escape(e['description'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    # iCalendar 规范要求每行不超过 75 字节，这里课程名通常较短，不强制折行
    return "\r\n".join(lines) + "\r\n"


def courses_for_export(cfg: dict, user_id: int | None = None) -> list[Course]:
    """从当前配置取课表快照并叠加手动调整，供导出接口复用。

    user_id 非空时按指定用户读快照（日历订阅无 Cookie 上下文），
    否则走当前用户上下文。
    """
    from . import db
    from .models import apply_course_overrides
    snap = db.get_snapshot_for(user_id) if user_id is not None else db.get_snapshot()
    courses = [Course.from_dict(d) for d in snap["courses"]] if snap else []
    return apply_course_overrides(courses, cfg.get("course_overrides"))


def exams_for_export(cfg: dict, user_id: int | None = None) -> list[Exam]:
    """从考试快照叠加手动调整（隐藏/修改/新增），供导出接口复用。

    user_id 非空时按指定用户读快照（日历订阅无 Cookie 上下文）。
    """
    from . import db
    from .models import apply_exam_overrides
    snap = db.get_exam_snapshot_for(user_id) if user_id is not None else db.get_exam_snapshot()
    exams = [Exam.from_dict(d) for d in snap["exams"]] if snap else []
    return apply_exam_overrides(exams, cfg.get("exam_overrides"))
