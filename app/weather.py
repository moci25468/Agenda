"""上课时段天气：Open-Meteo 免费接口，无需 Key，带内存缓存。

只在上课前提醒里用一次，按经纬度缓存 30 分钟；拉取失败时静默跳过，
不影响提醒本身。
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

API = "https://api.open-meteo.com/v1/forecast"
# 备用源：部分服务器（尤其境内 VPS）访问 Open-Meteo 会 SSL 握手超时，
# 用 wttr.in 兜底，它返回逐小时数据且无需 Key。按 地点名 查询。
WTTR_API = "https://wttr.in/{place}?format=j1"
WTTR_PLACE = "Qingdao"
CACHE_TTL = 1800          # 成功后缓存 30 分钟
FAIL_TTL = 120            # 失败只短暂回避，两分钟后就允许重试
STALE_MAX = 6 * 3600      # 兜底数据最多沿用 6 小时，避免把一天前的天气当今日
TIMEOUT = 10.0            # 跨境访问偶发 5s 级抖动，8s 太紧
PRIMARY_TIMEOUT = 5.0     # 主源连不通时是干等满超时，给短一点免得卡住页面
RETRY_DELAYS = (0.5,)     # 失败后再试一次，最多两次请求
DEAD_TTL = 900            # 主源连续失败后熔断 15 分钟，期间直接走备用源

# wttr.in 英文天气描述 → 近似 WMO code，复用 WMO_TEXT 得到统一中文文案
_WTTR_CODE = [
    ("sunny", 0), ("clear", 0), ("cloudy", 3), ("overcast", 3),
    ("partly", 2), ("thunder", 95), ("snow", 71), ("sleet", 85),
    ("drizzle", 51), ("rain", 61), ("fog", 45), ("mist", 45),
]


def _wttr_code(desc: str) -> int | None:
    text = (desc or "").lower()
    for needle, code in _WTTR_CODE:
        if needle in text:
            return code
    return None

log = logging.getLogger(__name__)

# WMO weather code → 中文描述
WMO_TEXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨",
    56: "冻毛毛雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}

# (经度, 纬度) → (抓取时间, 逐小时数据)
_cache: dict[tuple[float, float], tuple[float, dict]] = {}
# 最近一次成功的数据：抓取失败时用它兜底，避免一次网络抖动就把天气全部变空
_last_good: dict[tuple[float, float], dict] = {}
# 主源熔断截止时间（monotonic）：到点前不再尝试主源，直接用备用源
_primary_dead_until = 0.0


def describe_code(code) -> str:
    try:
        return WMO_TEXT.get(int(code), "天气未知")
    except (TypeError, ValueError):
        return "天气未知"


def _hour_key(day_s: str, hhmm: str) -> str:
    try:
        hour = int(str(hhmm).split(":")[0])
    except (ValueError, IndexError):
        return ""
    return f"{day_s}T{hour:02d}:00"


def _params(lat: float, lon: float) -> dict:
    """Open-Meteo 请求参数；纬度在前、经度在后，避免写反。"""
    return {
        "latitude": round(float(lat), 2),
        "longitude": round(float(lon), 2),
        "hourly": "temperature_2m,weather_code,precipitation_probability",
        "timezone": "Asia/Shanghai",
        "forecast_days": 2,
    }


def _parse_hourly(data: dict) -> dict:
    """把 Open-Meteo 返回的列表结构转成 {小时键: {temp, code, pop}}。"""
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weather_code") or []
    pops = hourly.get("precipitation_probability") or []
    out: dict[str, dict] = {}
    for i, t in enumerate(times):
        out[str(t)] = {
            "temp": temps[i] if i < len(temps) else None,
            "code": codes[i] if i < len(codes) else None,
            "pop": pops[i] if i < len(pops) else None,
        }
    return out


async def _fetch_from_wttr(lat: float, lon: float) -> dict:
    """备用源：wttr.in 逐小时数据，转成与主源相同结构。失败返回 {}。"""
    out: dict = {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            r = await client.get(
                # 用经纬度而非写死城市名，用户改了位置也能正确回退
                WTTR_API.format(place=f"{lat},{lon}"),
                headers={"User-Agent": "curl/7.68"},
            )
            r.raise_for_status()
            days = (r.json() or {}).get("weather") or []
        for day in days:
            date_s = day.get("date")
            if not date_s:
                continue
            # wttr.in 每 3 小时一个点（00/03/06…），课表按整点查（08:00、10:00…），
            # 先把稀疏点铺满 0~23 每个整点，保证任意上课时刻都能命中。
            slots: dict[int, dict] = {}
            for h in day.get("hourly") or []:
                hour = int(h.get("time", 0) or 0) // 100
                desc = ((h.get("weatherDesc") or [{}])[0] or {}).get("value", "")
                slots[hour] = {
                    "temp": h.get("tempC"),
                    "code": _wttr_code(desc),
                    "pop": h.get("chanceofrain"),
                }
            if not slots:
                continue
            for hour in range(24):
                # 取不晚于当前时刻的最近一个已知点
                known = [h for h in slots if h <= hour]
                src = slots[max(known)] if known else slots[min(slots)]
                out[f"{date_s}T{hour:02d}:00"] = dict(src)
    except Exception as e:  # noqa: BLE001
        log.warning("备用天气源 wttr.in 获取失败：%s", e)
    return out


async def fetch_hourly(lat: float, lon: float) -> dict:
    """拉取未来 2 天的逐小时天气，返回 {"YYYY-MM-DDTHH:00": {...}}。

    主源 Open-Meteo 失败时自动切换到 wttr.in；都失败则回落上次成功的数据
    （宁可显示几小时前的天气，也不要整块变空），完全没有历史数据时返回 {}。
    主源在境内 VPS 上经常连不通，会「干等满超时」，所以连续失败后熔断 15 分钟，
    期间直接走备用源，避免每个请求都白等十几秒。
    """
    global _primary_dead_until
    key = (round(float(lat), 2), round(float(lon), 2))
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < (CACHE_TTL if hit[1] else FAIL_TTL):
        return hit[1]
    params = _params(lat, lon)
    out: dict = {}
    err = ""
    if now < _primary_dead_until:
        err = "主源熔断中，直接走备用源"
    else:
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                async with httpx.AsyncClient(timeout=PRIMARY_TIMEOUT,
                                             trust_env=False) as client:
                    r = await client.get(API, params=params)
                    r.raise_for_status()
                    out = _parse_hourly(r.json())
                if out:
                    _primary_dead_until = 0.0
                    break
                err = "接口返回空数据"
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])
        if not out:
            # 主源这一轮彻底失败：接下来 15 分钟不再尝试，省下每次十几秒的空等
            _primary_dead_until = time.monotonic() + DEAD_TTL
    # 主源不可用（境内 VPS 常见 SSL 握手超时）时切备用源
    if not out:
        out = await _fetch_from_wttr(lat, lon)
        if out:
            log.info("主天气源不可用，已切换到备用源 wttr.in（%s）", err)
    if out:
        _last_good[key] = (now, out)
        _cache[key] = (now, out)
        return out
    entry = _last_good.get(key)
    stale = entry[1] if entry and now - entry[0] < STALE_MAX else {}
    if stale:
        log.warning("天气获取失败（%s, %s）：%s；沿用上次成功的数据", lat, lon, err)
        # 按「有数据」缓存，避免持续不通时反复打接口
        _cache[key] = (now, stale)
    else:
        log.warning("天气获取失败（%s, %s）：%s；暂无历史数据可兜底", lat, lon, err)
        _cache[key] = (now, {})
    return stale


def _summary(item: dict | None) -> dict | None:
    """把一小时的数据整理成前端/推送都能用的短描述。"""
    if not item:
        return None
    out = {"text": describe_code(item.get("code"))}
    try:
        out["temp"] = round(float(item["temp"]))
    except (KeyError, TypeError, ValueError):
        pass
    try:
        out["pop"] = int(float(item["pop"]))
    except (KeyError, TypeError, ValueError):
        pass
    return out


async def hours_map(cfg: dict) -> dict:
    """按小时返回未来两天的天气，键为 'YYYY-MM-DDTHH:00'。"""
    w = cfg.get("weather") or {}
    if w.get("enabled") is False:
        return {}
    try:
        lat = float(w.get("lat") or 35.96)
        lon = float(w.get("lon") or 120.20)
    except (TypeError, ValueError):
        return {}
    hourly = await fetch_hourly(lat, lon)
    out: dict[str, dict] = {}
    for key, item in hourly.items():
        summary = _summary(item)
        if summary:
            out[str(key)] = summary
    return out


async def weather_line(cfg: dict, day_s: str, hhmm: str) -> str:
    """生成「上课时天气（青岛黄岛区）：多云 15℃，降水概率 40%」这样一行。

    未开启、坐标无效或接口不可用时返回空串。
    """
    w = cfg.get("weather") or {}
    if w.get("enabled") is False or not day_s or not hhmm:
        return ""
    try:
        lat = float(w.get("lat") or 35.96)
        lon = float(w.get("lon") or 120.20)
    except (TypeError, ValueError):
        return ""
    hourly = await fetch_hourly(lat, lon)
    summary = _summary(hourly.get(_hour_key(day_s, hhmm)))
    if not summary:
        return ""
    parts = [summary["text"]]
    if "temp" in summary:
        parts.append(f"{summary['temp']}℃")
    place = str(w.get("place") or "青岛黄岛区").strip() or "青岛黄岛区"
    line = f"上课时天气（{place}）：" + " ".join(parts)
    if summary.get("pop", -1) >= 30:
        line += f"，降水概率 {summary['pop']}%"
    return line


async def daily_summary(cfg: dict, day_s: str) -> str:
    """返回某一天（YYYY-MM-DD）的天气概况一句话，如「多云，12~18℃，最高降水概率 40%」。

    未开启、坐标无效或接口不可用时返回空串。
    """
    w = cfg.get("weather") or {}
    if w.get("enabled") is False or not day_s:
        return ""
    try:
        lat = float(w.get("lat") or 35.96)
        lon = float(w.get("lon") or 120.20)
    except (TypeError, ValueError):
        return ""
    hourly = await fetch_hourly(lat, lon)
    day_items = [v for k, v in hourly.items() if k.startswith(day_s)]
    if not day_items:
        return ""
    temps = [round(float(v["temp"])) for v in day_items
             if isinstance(v.get("temp"), (int, float))]
    pops = [int(float(v["pop"])) for v in day_items
            if isinstance(v.get("pop"), (int, float))]
    # 取白天时段（8~20 点）代表性天气码，避免凌晨/夜间码干扰
    day_codes = [v["code"] for k, v in hourly.items()
                 if k.startswith(day_s) and 8 <= int(k[11:13]) <= 20 and v.get("code") is not None]
    if not day_codes:
        day_codes = [v["code"] for v in day_items if v.get("code") is not None]
    code = day_codes[0] if day_codes else None
    text = describe_code(code) if code is not None else "天气未知"
    parts = [text]
    if temps:
        parts.append(f"{min(temps)}~{max(temps)}℃")
    line = " ".join(parts)
    if pops:
        line += f"，最高降水概率 {max(pops)}%"
    return line


async def lesson_weather_map(cfg: dict) -> dict:
    """构建「日期 + 上课时刻 → 上课时天气短描述」的映射，供日历导出给每节课附天气。

    返回形如 {"2026-11-10": {"08:00": "小雨 14℃", "10:00": "多云 16℃"}}，
    键的上课时刻按整点归一（HH:00），与日历事件对齐。
    未开启、坐标无效或接口不可用时返回空 dict，不影响日历生成。
    """
    w = cfg.get("weather") or {}
    if w.get("enabled") is False:
        return {}
    try:
        lat = float(w.get("lat") or 35.96)
        lon = float(w.get("lon") or 120.20)
    except (TypeError, ValueError):
        return {}
    hourly = await fetch_hourly(lat, lon)
    out: dict[str, dict] = {}
    for key, item in hourly.items():
        # key 形如 '2026-11-10T08:00'
        try:
            day_s, hhmm = key.split("T", 1)
        except ValueError:
            continue
        summary = _summary(item)
        if not summary:
            continue
        parts = [summary["text"]]
        if "temp" in summary:
            parts.append(f"{summary['temp']}℃")
        text = " ".join(parts)
        if summary.get("pop", -1) >= 30:
            text += f"，降水概率 {summary['pop']}%"
        out.setdefault(day_s, {})[hhmm] = text
    return out


def clear_cache():
    """测试或手动刷新用。"""
    global _primary_dead_until
    _cache.clear()
    _last_good.clear()
    _primary_dead_until = 0.0
