"""Shared, read-only room snapshots, populated only by the operator's source."""
from __future__ import annotations

import asyncio
import json
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
ON_DEMAND_WINDOW_DAYS = 7
ON_DEMAND_TTL = 7 * 86400
SLOTS = tuple((start, start + 1) for start in range(1, 12, 2))
log = logging.getLogger(__name__)


def on_demand_write_eligible(day: date, now: datetime | None = None) -> bool:
    today = (now or datetime.now(TZ)).astimezone(TZ).date()
    return abs((day - today).days) <= ON_DEMAND_WINDOW_DAYS


def on_demand_key(provider: ZhengfangProvider, semester_start: str, day: date,
                  campus: str, building: str, start: int, end: int) -> str | None:
    if os.getenv("AGENDA_ROOMS_SHARE_ON_DEMAND", "").strip() != "1":
        return None
    if provider.base != BASE or not campus:
        return None
    try:
        semester = date.fromisoformat(semester_start)
    except (TypeError, ValueError):
        return None
    if (provider.year, provider.term) != default_term(semester):
        return None
    identity = json.dumps([provider.base, provider.year, provider.term, semester.isoformat(),
                           day.isoformat(), campus, building, start, end])
    return f"rooms:on-demand:v1:{cache.fingerprint(identity)}"


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
                    start: int, end: int, building: str = "") -> dict | None:
    if (start, end) not in SLOTS or not campus:
        return None
    entry = await cache.get(_key(provider, "free", day.isoformat(), campus, start, end))
    if not isinstance(entry, dict) or not isinstance(entry.get("data"), list):
        return None
    if not isinstance(entry.get("updated_at"), str):
        return None
    if not building:
        return entry
    meta = await get_meta(provider, campus)
    buildings = (meta or {}).get("buildings") or []
    names = [b.get("name") for b in buildings if str(b.get("id")) == building]
    if len(names) != 1 or not names[0]:
        return None
    name = names[0]
    # 上游查询按楼号，而快照只有楼名。仅在整批名称可无歧义映射时过滤。
    counts: dict[str, int] = {}
    for b in buildings:
        label = b.get("name") or ""
        if b.get("id") and label:
            counts[label] = counts.get(label, 0) + 1
    rooms = entry["data"]
    if counts.get(name) != 1 or any(counts.get(r.get("building")) != 1 for r in rooms):
        return None
    return {**entry, "data": [r for r in rooms if r.get("building") == name]}


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


def snapshot_time() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


async def _store(key: str, data) -> None:
    await cache.set(key, {"data": data, "updated_at": snapshot_time()}, TTL)


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
    }}}, shared_rooms=True)
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
