"""演示数据 Provider。

注意：这是虚构的示例课表，仅用于演示与开发，不是真实教务数据。
"""
from __future__ import annotations

from datetime import date, timedelta

from ..models import Course, Exam
from .base import TimetableProvider

ALL = list(range(1, 17))


def _demo_courses() -> list[Course]:
    return [
        Course("高等数学 A(一)", day=1, start=1, end=2, teacher="王建国", room="1号教学楼 201", weeks=ALL, campus="黄岛校区"),
        Course("大学英语(三)", day=1, start=3, end=4, teacher="李雯", room="2号教学楼 305", weeks=ALL, campus="黄岛校区"),
        Course("数据结构", day=2, start=1, end=2, teacher="张鹏飞", room="1号教学楼 402", weeks=ALL, campus="黄岛校区"),
        Course("大学物理 B(二)", day=2, start=5, end=6, teacher="刘敏", room="3号教学楼 101", weeks=ALL, campus="黄岛校区"),
        Course("体育(三)·篮球", day=3, start=3, end=4, teacher="陈志强", room="东区篮球场", weeks=ALL, campus="黄岛校区"),
        Course("数据结构实验", day=3, start=7, end=8, teacher="张鹏飞", room="计算中心机房 3", weeks=list(range(2, 17, 2)), note="双周", campus="黄岛校区"),
        Course("概率论与数理统计", day=4, start=1, end=2, teacher="赵蕾", room="2号教学楼 201", weeks=ALL, campus="黄岛校区"),
        Course("思想政治理论实践", day=4, start=5, end=6, teacher="孙浩", room="2号教学楼 405", weeks=list(range(1, 9)), campus="黄岛校区"),
        Course("计算机组成原理", day=5, start=3, end=4, teacher="周晓东", room="1号教学楼 302", weeks=ALL, campus="黄岛校区"),
        Course("创新创业基础", day=5, start=9, end=10, teacher="吴雅静", room="2号教学楼 105", weeks=list(range(1, 13, 2)), note="单周", campus="黄岛校区"),
    ]


class MockProvider(TimetableProvider):
    name = "mock"
    is_demo = True
    supports_exams = True

    async def fetch(self) -> list[Course]:
        return _demo_courses()

    async def fetch_exams(self) -> list[Exam]:
        d1 = date.today() + timedelta(days=6)
        d2 = date.today() + timedelta(days=20)
        return [
            Exam("高等数学 A(一)", time=f"{d1.isoformat()} 09:00-11:00",
                 date=d1.isoformat(), start="09:00", end="11:00",
                 room="1号教学楼 201", seat="18", campus="黄岛校区",
                 note="闭卷", source_id="demo-1"),
            Exam("数据结构", time=f"{d2.isoformat()} 14:00-16:00",
                 date=d2.isoformat(), start="14:00", end="16:00",
                 room="计算中心机房 3", seat="06", campus="黄岛校区",
                 note="机考", source_id="demo-2"),
        ]