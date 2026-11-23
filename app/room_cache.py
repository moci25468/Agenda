"""Shared, read-only room snapshots, populated only by the operator's source."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import cache
from .models import current_week
from .providers.base import AuthExpiredError
from .providers.zhengfang import ZhengfangProvider, default_term

TZ = ZoneInfo("Asia/Shanghai")
BASE = "https://jxgl.qut.edu.cn/jwglxt"
TTL = 8 * 86400
SLOTS = tuple((start, start + 1) for start in range(1, 12, 2))
log = logging.getLogger(__name__)


def _semester_start() -> str:
    return os.getenv("AGENDA_ROOMS_SHARED_SEMESTER_START", "").strip()


def enabled() -> bool:
    start = _semester_start()
    try:
        day = date.fromisoformat(start)
    except ValueError:
        return False
    return start == day.isoformat() and bool(os.getenv("AGENDA_ROOMS_SHARED_COOKIE", "").strip())


def eligible(provider: ZhengfangProvider, semester_start: str) -> bool:
    return (enabled() and provider.base == BASE and semester_start == _semester_start()
            and (provider.year, provider.term) == default_term(date.fromisoformat(semester_start))
            == default_term(datetime.now(TZ).date()))


def _key(provider: ZhengfangProvider, *parts: str | int) -> str:
    suffix = ":".join(str(part) for part in parts)
    return f"rooms:shared:v1:{provider.year}:{provider.term}:{_semester_start()}:{suffix}"


async def get_meta(provider: ZhengfangProvider, campus: str) -> dict | None:
    entry = await cache.get(_key(provider, "meta", campus))
    data = entry.get("data") if isinstance(entry, dict) else None
    return data if isinstance(data, dict) and data.get("campuses") else None


async def get_rooms(provider: ZhengfangProvider, day: date, campus: str,
                    start: int, end: int) -> dict | None:
    if (start, end) not in SLOTS or not campus:
        return None
    entry = await cache.get(_key(provider, "free", day.isoformat(), campus, start, end))
    if not isinstance(entry, dict) or not isinstance(entry.get("data"), list):
        return None
    return entry if isinstance(entry.get("updated_at"), str) else None


def _refresh_window(now: datetime) -> datetime:
    cutoff = now.replace(hour=0, minute=30, second=0, microsecond=0)
    return cutoff if now >= cutoff else cutoff - timedelta(days=1)


def _refreshed_in_window(entry, cutoff: datetime) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        updated = datetime.fromisoformat(entry["updated_at"]).astimezone(TZ)
        return updated >= cutoff
    except (KeyError, TypeError, ValueError):
        return False


async def _store(key: str, data) -> None:
    await cache.set(key, {"data": data, "updated_at": datetime.now(TZ).isoformat(timespec="seconds")}, TTL)


async def refresh(now: datetime | None = None) -> None:
    if not enabled():
        return
    now = now or datetime.now(TZ)
    today = now.date()
    cutoff = _refresh_window(now)
    semester_start = _semester_start()
    year, term = default_term(today)
    if (year, term) != default_term(date.fromisoformat(semester_start)):
        return  # 不用上学期开学日刷新新学期，等管理员更新配置。
    provider = ZhengfangProvider({"provider": {"zhengfang": {
        "base": BASE, "year": year, "term": term,
        "cookie": os.getenv("AGENDA_ROOMS_SHARED_COOKIE", ""),
    }}})
    monday = today - timedelta(days=today.weekday())
    dates = [monday + timedelta(days=i) for i in range(7)]
    weeks = [(day, current_week(semester_start, day, 40)) for day in dates]
    if not any(1 <= week <= 30 for _, week in weeks):
        return
    first_key = _key(provider, "meta", "")
    first = await cache.get(first_key)
    if not _refreshed_in_window(first, cutoff):
        meta = await asyncio.wait_for(provider.fetch_room_meta(), timeout=40)
        if not meta.get("campuses") or not meta.get("campus"):
            raise ValueError("missing campus metadata")
        await _store(first_key, meta)
        await _store(_key(provider, "meta", meta["campus"]), meta)
    else:
        meta = first["data"]
    # Serial requests with spacing: never fan out across campuses or users.
    failures = 0
    for campus in dict.fromkeys(c["id"] for c in meta["campuses"] if c.get("id")):
        campus_key = _key(provider, "meta", campus)
        try:
            if not _refreshed_in_window(await cache.get(campus_key), cutoff):
                info = await asyncio.wait_for(provider.fetch_room_meta(campus), timeout=40)
                if info.get("campus") != campus or not info.get("campuses"):
                    raise ValueError("missing campus metadata")
                await _store(campus_key, info)
        except AuthExpiredError:
            raise
        except Exception as exc:
            log.warning("Shared room metadata refresh failed (%s)", type(exc).__name__)
            continue
        for day, week in weeks:
            if not 1 <= week <= 30:
                continue
            for start, end in SLOTS:
                key = _key(provider, "free", day.isoformat(), campus, start, end)
                if _refreshed_in_window(await cache.get(key), cutoff):
                    continue
                await asyncio.sleep(0.5)
                try:
                    rooms = await asyncio.wait_for(provider.fetch_free_rooms(
                        week, day.isoweekday(), start, end, campus, strict=True), timeout=40)
                    await _store(key, rooms)
                    failures = 0
                except AuthExpiredError:
                    raise
                except Exception as exc:
                    failures += 1
                    log.warning("Shared room slot refresh failed (%s)", type(exc).__name__)
                    if failures >= 3:
                        return


async def run() -> None:
    """Runs on the app event loop, alongside its async Redis client."""
    while enabled():
        started = datetime.now(TZ)
        next_run = _refresh_window(started) + timedelta(days=1)
        try:
            await refresh(started)
        except Exception as exc:
            log.warning("Shared room refresh stopped (%s)", type(exc).__name__)
        await asyncio.sleep(max(0, (next_run - datetime.now(TZ)).total_seconds()))
