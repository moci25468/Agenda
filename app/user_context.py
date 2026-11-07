"""当前请求所属用户：用 ContextVar 让配置/快照按用户自动隔离。

中间件在纯 ASGI 层 set，后续协程（含 endpoint、依赖）都能读到；
定时任务则在每个用户的上下文里显式切换。
"""
from __future__ import annotations

from contextvars import ContextVar

current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)


def set_current_user(user_id: int | None):
    return current_user_id.set(user_id)


def get_current_user() -> int | None:
    return current_user_id.get()
