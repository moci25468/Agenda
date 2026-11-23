"""Optional Redis-backed JSON cache with a bounded process-local fallback."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

import asyncio

log = logging.getLogger(__name__)
_PREFIX = os.getenv("AGENDA_CACHE_PREFIX", "agenda:v1").strip(":") or "agenda:v1"
_TIMEOUT = max(0.05, min(float(os.getenv("REDIS_TIMEOUT", "0.25")), 2.0))
_OPERATION_TIMEOUT = min(_TIMEOUT, 0.1)
_MAX_LOCAL_ITEMS = 512
_LOCAL: dict[str, tuple[float, Any]] = {}
_REDIS = None
_REDIS_DOWN_UNTIL = 0.0
_REDIS_URL_ACTIVE = ""


def _redis_url() -> str:
    return os.getenv("REDIS_URL", "").strip()


def _key(key: str) -> str:
    return f"{_PREFIX}:{key}"


def fingerprint(value: str) -> str:
    """Return a non-reversible digest suitable for cache identity keys."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


def _local_get(key: str):
    item = _LOCAL.get(key)
    if item is None:
        return None
    expires, value = item
    if expires <= time.monotonic():
        _LOCAL.pop(key, None)
        return None
    return value


def _local_set(key: str, value: Any, ttl: int):
    now = time.monotonic()
    for stale in [k for k, (expires, _) in _LOCAL.items() if expires <= now]:
        _LOCAL.pop(stale, None)
    if len(_LOCAL) >= _MAX_LOCAL_ITEMS and key not in _LOCAL:
        oldest = min(_LOCAL, key=lambda k: _LOCAL[k][0])
        _LOCAL.pop(oldest, None)
    _LOCAL[key] = (now + max(1, ttl), value)


async def start() -> None:
    """Create a lazy Redis connection pool; Redis remains optional."""
    if not _redis_url():
        log.info("Redis cache disabled; using process-local cache")
        return
    await _connect()


async def _connect() -> None:
    global _REDIS, _REDIS_DOWN_UNTIL, _REDIS_URL_ACTIVE
    url = _redis_url()
    if not url:
        return
    if _REDIS is not None and _REDIS_URL_ACTIVE == url:
        return
    if _REDIS is not None:
        await close()
    try:
        from redis.asyncio import Redis
        client = Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=_TIMEOUT,
            socket_timeout=_TIMEOUT, health_check_interval=30,
        )
        await asyncio.wait_for(client.ping(), timeout=_TIMEOUT)
        _REDIS = client
        _REDIS_URL_ACTIVE = url
        _REDIS_DOWN_UNTIL = 0.0
        log.info("Redis cache connected")
    except Exception as exc:  # noqa: BLE001
        if "client" in locals():
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
        _REDIS = None
        _REDIS_URL_ACTIVE = ""
        _REDIS_DOWN_UNTIL = time.monotonic() + 10
        log.warning("Redis cache unavailable; using local cache (%s)", type(exc).__name__)


async def _ensure_connection() -> None:
    if _REDIS is None and _redis_url() and time.monotonic() >= _REDIS_DOWN_UNTIL:
        await _connect()


async def close() -> None:
    global _REDIS, _REDIS_URL_ACTIVE
    if _REDIS is not None:
        try:
            await _REDIS.aclose()
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis cache close failed (%s)", type(exc).__name__)
        finally:
            _REDIS = None
            _REDIS_URL_ACTIVE = ""


async def get(key: str):
    full_key = _key(key)
    await _ensure_connection()
    if _REDIS is not None and time.monotonic() >= _REDIS_DOWN_UNTIL:
        try:
            raw = await asyncio.wait_for(_REDIS.get(full_key), timeout=_OPERATION_TIMEOUT)
            if raw is not None:
                return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            _mark_redis_down(exc)
    return _local_get(full_key)


async def set(key: str, value: Any, ttl: int) -> None:
    full_key = _key(key)
    ttl = max(1, int(ttl))
    await _ensure_connection()
    _local_set(full_key, value, ttl)
    if _REDIS is not None and time.monotonic() >= _REDIS_DOWN_UNTIL:
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            await asyncio.wait_for(
                _REDIS.set(full_key, encoded, ex=ttl), timeout=_OPERATION_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            _mark_redis_down(exc)


def _mark_redis_down(exc: Exception) -> None:
    global _REDIS_DOWN_UNTIL
    _REDIS_DOWN_UNTIL = time.monotonic() + 10
    log.warning("Redis cache operation failed; using local cache (%s)", type(exc).__name__)


def clear_local(prefix: str = "") -> None:
    if not prefix:
        _LOCAL.clear()
        return
    full_prefix = _key(prefix)
    for key in [key for key in _LOCAL if key.startswith(full_prefix)]:
        _LOCAL.pop(key, None)
