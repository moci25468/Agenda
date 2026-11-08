"""上课时段天气：Open-Meteo 免费接口，无需 Key，带内存缓存。

只在上课前提醒里用一次，按经纬度缓存 30 分钟；拉取失败时静默跳过，
不影响提醒本身。
"""
from __future__ import annotations

import time

import httpx

API = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL = 1800
FAIL_TTL = 300

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


async def fetch_hourly(lat: float, lon: float) -> dict:
    """拉取未来 2 天的逐小时天气，返回 {"YYYY-MM-DDTHH:00": {...}}。"""
    key = (round(float(lat), 2), round(float(lon), 2))
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < (CACHE_TTL if hit[1] else FAIL_TTL):
        return hit[1]
    params = _params(lat, lon)
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            r = await client.get(API, params=params)
            r.raise_for_status()
            data = r.json()
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
    except Exception:  # noqa: BLE001
        out = {}
    _cache[key] = (now, out)
    return out


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

    供 AI 上下文注入使用；未开启、坐标无效或接口不可用时返回空串。
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


def clear_cache():
    """测试或手动刷新用。"""
    _cache.clear()
