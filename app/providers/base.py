"""课表数据来源抽象。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Course, Exam


class AuthExpiredError(ValueError):
    """登录态过期（被重定向回 SSO / 返回登录页），需要用户更新凭证。"""


class TimetableProvider(ABC):
    name = "base"
    is_demo = False
    supports_exams = False   # 数据源是否提供考试安排

    @abstractmethod
    async def fetch(self) -> list[Course]:
        """拉取完整课表。失败时抛出异常，由调度层记录。"""
        raise NotImplementedError

    async def fetch_exams(self) -> list[Exam]:
        """拉取考试安排；不支持的数据源返回空列表。"""
        return []

    async def fetch_bundle(self) -> tuple[list[Course], list[Exam]]:
        """一次同步拉取课表与考试，供需要复用登录会话的数据源覆盖。"""
        return await self.fetch(), await self.fetch_exams()