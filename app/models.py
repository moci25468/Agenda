"""课表数据模型与通用工具。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta

# 默认节次时间表（青岛理工大学，2026-2027-1 门户课表实测值：
# 每节 50 分钟，上午 4 节、下午 4 节、晚上 2 节；可在设置中覆盖）
DEFAULT_SECTION_TIMES = {
    "1": "08:00", "2": "09:00", "3": "10:10", "4": "11:10",
    "5": "14:00", "6": "15:00", "7": "16:10", "8": "17:10",
    "9": "19:00", "10": "20:00",
}
DEFAULT_SECTION_ENDS = {
    "1": "08:50", "2": "09:50", "3": "11:00", "4": "12:00",
    "5": "14:50", "6": "15:50", "7": "17:00", "8": "18:00",
    "9": "19:50", "10": "20:50",
}


@dataclass
class Course:
    name: str
    day: int = 1               # 1=周一 ... 7=周日
    start: int = 1             # 开始节次
    end: int = 2               # 结束节次
    teacher: str = ""
    room: str = ""
    weeks: list[int] = field(default_factory=list)   # 上课周次列表
    campus: str = ""
    note: str = ""             # 单双周等备注
    source_id: str = ""        # 来源系统内的 ID（用于稳定 diff）
    uid: str = ""              # 手动调整用的稳定标识（不参与同步 diff）

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Course":
        return Course(
            name=d.get("name", ""), day=int(d.get("day", 1)),
            start=int(d.get("start", 1)), end=int(d.get("end", 2)),
            teacher=d.get("teacher", ""), room=d.get("room", ""),
            weeks=[int(w) for w in d.get("weeks", [])],
            campus=d.get("campus", ""), note=d.get("note", ""),
            source_id=d.get("source_id", ""), uid=d.get("uid", ""),
        )

    def key(self) -> tuple:
        """用于变更检测的稳定键：同一门课同一时间段视为同一实体。"""
        return (self.name, self.day, self.start, self.end, tuple(sorted(self.weeks)))

    def in_week(self, week: int) -> bool:
        return not self.weeks or week in self.weeks


def course_uid(c: Course) -> str:
    """课表列表里的稳定标识，用于手动增删改；手动添加的课直接用自身 ID。"""
    if c.uid:
        return c.uid
    if c.source_id.startswith("manual:"):
        return c.source_id
    raw = "|".join([
        c.source_id, c.name, str(c.day), str(c.start), str(c.end),
        ",".join(str(w) for w in sorted(c.weeks)),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def apply_course_overrides(courses: list[Course],
                           overrides: dict | None) -> list[Course]:
    """把用户手动添加 / 修改 / 删除的课叠加到同步结果上。

    同步仍照常更新快照，手动调整单独保存在配置里，不会被下次同步覆盖。
    """
    ov = overrides or {}
    hidden = set(ov.get("hidden") or [])
    edited = ov.get("edited") or {}
    out: list[Course] = []
    for c in courses:
        uid = course_uid(c)
        if uid in hidden:
            continue
        data = edited.get(uid)
        if isinstance(data, dict):
            # 覆盖字段叠加在原课程上，未填的字段保持同步结果
            merged = c.to_dict()
            merged.update(data)
            merged["source_id"] = c.source_id
            merged["uid"] = uid
            out.append(Course.from_dict(merged))
        else:
            c.uid = uid
            out.append(c)
    for data in ov.get("added") or []:
        if isinstance(data, dict):
            c = Course.from_dict(data)
            if not c.source_id:
                raw = "|".join([c.name, str(c.day), str(c.start), str(c.end)])
                c.source_id = f"manual:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"
            c.uid = c.source_id
            out.append(c)
    return out


@dataclass
class Exam:
    """一场考试。时间保留原始文本，同时拆出可排序的 date/start/end。"""
    name: str
    time: str = ""             # 教务原始时间文本
    date: str = ""             # YYYY-MM-DD
    start: str = ""            # HH:MM
    end: str = ""              # HH:MM
    room: str = ""
    seat: str = ""
    campus: str = ""
    note: str = ""
    source_id: str = ""
    uid: str = ""              # 手动调整用的稳定标识（不参与同步 diff）

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Exam":
        raw_time = str(d.get("time", "") or "")
        date_s, start, end = parse_exam_time(raw_time)
        return Exam(
            name=d.get("name", ""),
            time=raw_time,
            date=d.get("date") or date_s,
            start=d.get("start") or start,
            end=d.get("end") or end,
            room=d.get("room", ""),
            seat=d.get("seat", ""),
            campus=d.get("campus", ""),
            note=d.get("note", ""),
            source_id=str(d.get("source_id", "") or ""),
            uid=str(d.get("uid", "") or ""),
        )

    def key(self) -> tuple:
        return (self.name, self.date, self.start, self.room, self.seat)

    def is_finished(self, now: datetime | None = None) -> bool:
        if not self.date:
            return False
        try:
            hh, mm = (self.end or "23:59").split(":")[:2]
            end_at = datetime.fromisoformat(f"{self.date}T{int(hh):02d}:{int(mm):02d}:00")
        except ValueError:
            return False
        return end_at < (now or datetime.now())

    def display_time(self) -> str:
        """用于推送 / AI 的一行时间描述。"""
        day = self.date or self.time
        if self.start and self.end:
            day = f"{day} {self.start}-{self.end}"
        elif self.start:
            day = f"{day} {self.start}"
        return day.strip()


def exam_uid(e: Exam) -> str:
    """考试列表里的稳定标识，用于手动增删改；手动添加的考试直接用自身 ID。"""
    if e.uid:
        return e.uid
    if e.source_id.startswith("manual:"):
        return e.source_id
    raw = "|".join([e.source_id, e.name, e.date, e.start, e.end, e.room, e.seat])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def apply_exam_overrides(exams: list[Exam],
                         overrides: dict | None) -> list[Exam]:
    """把用户手动添加 / 修改 / 删除的考试叠加到同步快照上。

    与课程一致：同步仍照常更新快照，手动调整单独保存在配置里，
    不会被下次同步覆盖。
    """
    ov = overrides or {}
    hidden = set(ov.get("hidden") or [])
    edited = ov.get("edited") or {}
    out: list[Exam] = []
    for e in exams:
        uid = exam_uid(e)
        if uid in hidden:
            continue
        data = edited.get(uid)
        if isinstance(data, dict):
            merged = e.to_dict()
            merged.update(data)
            merged["source_id"] = e.source_id
            merged["uid"] = uid
            out.append(Exam.from_dict(merged))
        else:
            e.uid = uid
            out.append(e)
    for data in ov.get("added") or []:
        if isinstance(data, dict):
            e = Exam.from_dict(data)
            if not e.source_id:
                raw = "|".join([e.name, e.date, e.start])
                e.source_id = f"manual:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"
            e.uid = e.source_id
            out.append(e)
    return out


@dataclass
class Event:
    """个人日程：用户手动添加的安排，独立于课表/考试快照，存 events 表。"""
    title: str
    date: str = ""             # YYYY-MM-DD
    start: str = ""            # HH:MM，空表示全天
    end: str = ""              # HH:MM，可空
    location: str = ""
    note: str = ""
    id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Event":
        return Event(
            title=str(d.get("title", "") or ""),
            date=str(d.get("date", "") or ""),
            start=str(d.get("start", "") or ""),
            end=str(d.get("end", "") or ""),
            location=str(d.get("location", "") or ""),
            note=str(d.get("note", "") or ""),
            id=str(d.get("id", "") or ""),
        )

    def is_finished(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if not self.date:
            return False
        try:
            day = date.fromisoformat(self.date)
        except ValueError:
            return False
        if day < now.date():
            return True
        if day > now.date() or not self.end:
            return False
        try:
            hh, mm = self.end.split(":")[:2]
            end_at = datetime.fromisoformat(f"{self.date}T{int(hh):02d}:{int(mm):02d}:00")
        except ValueError:
            return False
        return end_at < now

    def display_time(self) -> str:
        """用于推送 / AI 的一行时间描述；无开始时间视为全天。"""
        if self.start and self.end:
            return f"{self.date} {self.start}-{self.end}"
        if self.start:
            return f"{self.date} {self.start}"
        return f"{self.date} 全天"


def event_line(e: Event) -> str:
    parts = [e.display_time(), e.title]
    if e.location:
        parts.append(f"@{e.location}")
    if e.note:
        parts.append(e.note)
    return " ".join(p for p in parts if p)


def parse_exam_time(text: str) -> tuple[str, str, str]:
    """从教务时间文本中拆出 (日期, 开始, 结束)。

    兼容 `2026-11-12 09:00-11:00`、`2026-11-12(09:00-11:00)`、
    `2026年11月12日 9:00—11:00` 等常见格式；解析失败时返回空串。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", "", ""
    dm = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?", raw)
    date_s = ""
    if dm:
        date_s = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"

    def hm(m) -> str:
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    tm = re.search(
        r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~—至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})", raw)
    if tm:
        start = f"{int(tm.group(1)):02d}:{tm.group(2)}"
        end = f"{int(tm.group(3)):02d}:{tm.group(4)}"
        return date_s, start, end
    single = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", raw)
    if single:
        return date_s, hm(single), ""
    return date_s, "", ""


def parse_weeks(text: str) -> tuple[list[int], str]:
    """解析正方/常见课表周次字符串。

    支持同一串里混用单双周，例如：
      '1-16周'、'第15周'、'1-15周(单)'、'1-3周(单),4-8周(双),9周'。
    返回 (周次列表, 备注)；无法解析时返回 ([], 原文)。
    """
    if not text:
        return [], ""
    raw = str(text).strip()
    if not raw:
        return [], ""
    raw = (raw.replace("，", ",").replace("、", ",")
              .replace("；", ",").replace(";", ","))
    weeks: set[int] = set()
    parity: set[str] = set()
    plain = False
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        odd = "(单)" in part or "（单）" in part
        even = "(双)" in part or "（双）" in part
        if odd:
            parity.add("单")
        elif even:
            parity.add("双")
        else:
            plain = True
        cleaned = re.sub(r"[第周（）()\s单双]", "", part)
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", cleaned)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            segment = range(lo, hi + 1)
        elif cleaned.isdigit():
            segment = [int(cleaned)]
        else:
            continue
        if odd:
            segment = [w for w in segment if w % 2 == 1]
        if even:
            segment = [w for w in segment if w % 2 == 0]
        weeks.update(segment)
    if not weeks:
        return [], raw
    # 只有整串都是单/双周时才标注，混有连续周次时不赘述
    note = ""
    if not plain:
        if parity == {"单"}:
            note = "单周"
        elif parity == {"双"}:
            note = "双周"
        elif parity:
            note = "单双周"
    return sorted(weeks), note


def current_week(semester_start: str, today: date | None = None, total_weeks: int = 30) -> int:
    """根据开学日期计算当前教学周（开学当天为第 1 周，周一开始）。"""
    today = today or date.today()
    try:
        start = date.fromisoformat(semester_start)
    except (ValueError, TypeError):
        return 1
    delta = (today - start).days
    if delta < 0:
        return 0  # 未开学
    return min(delta // 7 + 1, total_weeks)


def week_dates(semester_start: str, week: int) -> list[date]:
    """第 week 教学周的周一到周日日期。"""
    try:
        start = date.fromisoformat(semester_start)
    except (ValueError, TypeError):
        start = date.today()
    monday = start + timedelta(weeks=week - 1)
    # 对齐到周一
    monday -= timedelta(days=monday.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def diff_courses(old: list[Course], new: list[Course]) -> dict:
    """对比两次课表快照，返回新增/删除/修改（教室、教师、备注变化）。"""
    old_map = {c.key(): c for c in old}
    new_map = {c.key(): c for c in new}
    added = [c for k, c in new_map.items() if k not in old_map]
    removed = [c for k, c in old_map.items() if k not in new_map]
    changed = []
    # 名称+星期+节次相同但周次不同 → 视为调整
    def soft_key(c: Course):
        return (c.name, c.day, c.start, c.end)
    old_soft = {}
    for c in old:
        old_soft.setdefault(soft_key(c), []).append(c)
    for c in added[:]:
        candidates = old_soft.get(soft_key(c), [])
        match = next((o for o in candidates if o.key() not in new_map), None)
        if match:
            changed.append({"before": match.to_dict(), "after": c.to_dict()})
            added.remove(c)
            if match in removed:
                removed.remove(match)
    for k in old_map.keys() & new_map.keys():
        o, n = old_map[k], new_map[k]
        fields = {}
        for f in ("room", "teacher", "campus", "note"):
            if getattr(o, f) != getattr(n, f):
                fields[f] = {"before": getattr(o, f), "after": getattr(n, f)}
        if fields:
            changed.append({"before": o.to_dict(), "after": n.to_dict(), "fields": fields})
    return {
        "added": [c.to_dict() for c in added],
        "removed": [c.to_dict() for c in removed],
        "changed": changed,
        "has_change": bool(added or removed or changed),
    }