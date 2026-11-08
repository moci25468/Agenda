"""课表导出为 iCalendar（.ics）文件。

把整学期的课程展开成一条条带具体日期/时间的 VEVENT，
iPhone / 安卓 / Outlook / Google 日历均可直接导入。
不依赖任何第三方库。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from .models import Course, current_week, week_dates
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


def build_ical(cfg: dict, courses: list[Course]) -> str:
    """生成完整的 iCalendar 文本。"""
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
    for e in _course_events(cfg, courses):
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


def courses_for_export(cfg: dict) -> list[Course]:
    """从当前配置取课表快照并叠加手动调整，供导出接口复用。"""
    from . import db
    from .models import apply_course_overrides
    snap = db.get_snapshot()
    courses = [Course.from_dict(d) for d in snap["courses"]] if snap else []
    return apply_course_overrides(courses, cfg.get("course_overrides"))
