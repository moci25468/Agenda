"""配置管理：data/config.json，首次运行生成默认配置。

多用户下每位用户一份完整 config（存在 user_config 表），无用户上下文时读全局模板。
provider 同时支持「门户(portal)」与「正方教务(zhengfang)」两套独立凭证，由
provider.primary 决定课表主数据源：auto = 门户优先，门户不在再用正方；后期接入门户
后下次同步自动以门户整表覆盖。考试始终优先从正方拉取（门户只提供课表）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CAMPUS_DATA_DIR", BASE_DIR / "data"))
CONFIG_PATH = DATA_DIR / "config.json"

MASK = "********"

# 需要脱敏的字段（点路径）。课程接口 headers、正方密码/Cookie 等都不能回传前端。
MASK_PATHS = [
    ("ai", "api_key"), ("ai", "headers"),
    ("push", "bark_key"),
    ("security", "token"),
    ("provider", "zhengfang", "password"),
    ("provider", "zhengfang", "cookie"),
    ("provider", "portal", "headers"),
    ("calendar_subscribe_token",),
]

DEFAULT_CONFIG = {
    "school": "青岛理工大学",
    "semester_start": "2026-08-31",   # 开学日期（第 1 周周一），门户校历 2026-2027-1 实测
    "total_weeks": 30,
    "sync_time": "12:00",             # 每日同步时间（Asia/Shanghai）
    "push_next_day": True,            # 同步后推送次日课表
    "alert_on_fail": True,            # 凭证过期等同步失败时主动推送告警
    "section_times": {},              # 覆盖默认节次开始时间，如 {"1": "08:30"}
    "section_ends": {},               # 覆盖默认节次结束时间，如 {"1": "09:15"}
    # 手动调整：叠加在同步结果之上，下次同步不会覆盖
    "course_overrides": {
        "hidden": [],                 # 隐藏的课程 uid
        "edited": {},                 # uid → 覆盖字段
        "added": [],                  # 手动添加的课程
    },
    # 数据源：门户与正方并存，primary 决定课表主来源
    "provider": {
        "type": "mock",               # 展示用：portal / zhengfang / mock
        "primary": "auto",            # auto | portal | zhengfang（课表主数据源）
        "zhengfang": {                # 正方教务（jwglxt）
            "base": "https://jxgl.qut.edu.cn/jwglxt",
            "username": "",
            "password": "",
            "cookie": "",             # 也可直填登录后的 Cookie，跳过账号登录
            "year": "2026",           # 学年
            "term": "3",              # 学期：3=第一学期(秋季) 12=第二学期(春季)
        },
        "portal": {                   # 青岛理工融合门户（cURL / 一键登录写入）
            "url": "",
            "method": "GET",
            "headers": "",            # JSON 字符串，如 {"Authorization": "Bearer xxx"}
            "body": "",               # POST 时的 JSON body
            "list_path": "data",      # 课程数组在响应 JSON 中的点路径
            "mapping": {              # 响应字段 → 标准字段
                "name": "name", "teacher": "teacher", "room": "room",
                "day": "day", "start": "start", "end": "end", "weeks": "weeks",
            },
        },
    },
    "push": {
        "bark_enabled": False,
        "bark_server": "https://api.day.app",
        "bark_key": "",
        "wecom_enabled": False,
        "wecom_webhook": "",        # 企业微信 / 钉钉群机器人完整地址，或只填 key
        "reminder_enabled": True,   # 课程/日程/考试开始前推送
        "reminder_advance": 15,     # 提前多少分钟
        "exam_review_enabled": True,  # 考试前推送复习提醒
        "exam_review_days": 15,     # 提前多少天
        "daily_time": "22:00",      # 每日「明日安排」推送时间（HH:MM）
        "daily_courses": True,      # 明日安排内容：课表
        "daily_events": True,       # 明日安排内容：日程与考试
        "daily_weather": False,     # 明日安排内容：天气概况
    },
    "weather": {
        "enabled": True,            # 课前提醒附带上课时天气
        "place": "青岛黄岛区",       # 展示用位置名称
        "lat": 35.96,               # 纬度
        "lon": 120.20,              # 经度
    },
    "rooms": {
        "default_campus": "",       # 空教室默认校区（正方 xqh_id），查询时自动带入
        "default_campus_name": "",  # 校区名称，供 AI 上下文与展示使用
    },
    "ai": {
        "enabled": False,
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "model": "qwen-plus",
        "headers": "",            # 自定义请求头（JSON），如网关要求 X-API-Key
    },
    "security": {
        "enabled": False,         # 默认关闭，不影响本地页面与现有用法
        "token": "",              # 跨源（App WebView / 扩展）调用需携带
        "allowed_origins": [],    # 额外允许的来源
    },
    # 日历订阅：私有 token 绑定当前用户，用于 webcal:// 一键订阅自动同步
    "calendar_subscribe_token": "",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        # 手动调整是整体状态，必须整体替换：否则删除的条目会被旧的合回来
        if k in ("course_overrides", "exam_overrides"):
            out[k] = v
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _get_path(obj, path):
    cur = obj
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _set_path(obj, path, value):
    cur = obj
    for k in path[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[path[-1]] = value


def normalize_provider(p: dict | None) -> dict:
    """把 provider 整理成 canonical 结构（门户 / 正方 两套子配置 + primary）。

    兼容旧版扁平字段（zf_*、http_*）：子配置优先，扁平键回退。注意 DEFAULT_CONFIG
    的 portal 子配置带非空默认值（list_path=\"data\"、mapping={默认}），会干扰「扁平键
    回退」，因此对带默认值的字段采用「扁平键非空则优先」的迁移规则（扁平键只在旧配置出现）。
    """
    p = p or {}
    zf = p.get("zhengfang") or {}
    portal = p.get("portal") or {}
    if not isinstance(zf, dict):
        zf = {}
    if not isinstance(portal, dict):
        portal = {}

    def _truthy(v):
        return v not in (None, "", {}, [])

    zhengfang = {
        "base": zf.get("base") or p.get("zf_base") or DEFAULT_CONFIG["provider"]["zhengfang"]["base"],
        "username": zf.get("username") or p.get("zf_username") or "",
        "password": zf.get("password") or p.get("zf_password") or "",
        "cookie": zf.get("cookie") or p.get("zf_cookie") or "",
        "year": zf.get("year") or p.get("zf_year") or "2026",
        "term": str(zf.get("term") or p.get("zf_term") or "3"),
    }
    portal = {
        "url": portal.get("url") or p.get("http_url") or "",
        "method": (portal.get("method") or p.get("http_method") or "GET").upper(),
        # 扁平键只在旧配置出现：旧配置迁移时优先，新配置直接走子配置。
        "headers": p.get("http_headers") if _truthy(p.get("http_headers"))
        else (portal.get("headers") if _truthy(portal.get("headers")) else ""),
        "body": p.get("http_body") if _truthy(p.get("http_body"))
        else (portal.get("body") if _truthy(portal.get("body")) else ""),
        "list_path": p.get("http_list_path") or portal.get("list_path") or "",
        "mapping": p.get("http_mapping") if _truthy(p.get("http_mapping"))
        else (portal.get("mapping") or {}),
    }
    primary = p.get("primary") or "auto"
    ptype = p.get("type") or (
        "portal" if portal["url"] else ("zhengfang" if zhengfang["base"] else "mock")
    )
    return {"type": ptype, "primary": primary, "zhengfang": zhengfang, "portal": portal}


def zhengfang_configured(cfg: dict) -> bool:
    """是否填了可用于拉课表/考试的正方凭证（Cookie 或账号密码）。"""
    p = (cfg.get("provider") or {}).get("zhengfang") or {}
    if not p:
        p = cfg.get("provider", {})
    base = p.get("base") or p.get("zf_base")
    cookie = p.get("cookie") or p.get("zf_cookie")
    username = p.get("username") or p.get("zf_username")
    password = p.get("password") or p.get("zf_password")
    return bool(base and (cookie or (username and password)))


def portal_configured(cfg: dict) -> bool:
    """门户课表接口是否就绪（需 URL + 登录态 Cookie/Token）。"""
    p = (cfg.get("provider") or {}).get("portal") or {}
    if not p.get("url"):
        # 兼容旧配置：http_url + http_headers
        p = cfg.get("provider", {})
    if not p.get("http_url" if "http_url" in p else "url"):
        return False
    headers = p.get("headers") or ""
    if isinstance(headers, dict):
        lower = {str(k).lower(): v for k, v in headers.items()}
    else:
        try:
            lower = {str(k).lower(): v for k, v in json.loads(headers or "{}").items()}
        except (ValueError, TypeError):
            lower = {}
    has_cookie = bool(lower.get("cookie"))
    has_token = any(lower.get(k) for k in (
        "authorization", "x-token", "x-access-token", "x-auth-token", "token"))
    return bool(has_cookie or has_token)


def resolve_course_source(cfg: dict) -> str:
    """按 primary 决定课表主数据源：portal / zhengfang / mock。

    auto：门户优先，门户不在配置则用正方；两者都没有回退 mock。
    """
    p = cfg.get("provider", {})
    ptype = p.get("type") or "mock"
    # 用户显式选择演示或通用 HTTP 时，不再被 auto 的门户优先策略抢回。
    if ptype in ("mock", "httpjson"):
        return ptype
    primary = p.get("primary") or "auto"
    zf_ok = zhengfang_configured(cfg)
    portal_ok = portal_configured(cfg)
    if primary == "zhengfang" and zf_ok:
        return "zhengfang"
    if primary == "portal" and portal_ok:
        return "portal"
    if portal_ok:
        return "portal"
    if zf_ok:
        return "zhengfang"
    return ptype


def global_config() -> dict:
    """读磁盘上的全局模板配置（不受当前用户上下文影响，用于新用户初始化）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return json.loads(json.dumps(_deep_merge(DEFAULT_CONFIG, saved)))
        except (json.JSONDecodeError, OSError):
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def load_config() -> dict:
    """按当前登录用户读配置；无用户上下文时读全局模板（启动 / 离线脚本）。"""
    from .user_context import current_user_id

    uid = current_user_id.get()
    if uid is not None:
        from . import db
        cfg = json.loads(json.dumps(_deep_merge(DEFAULT_CONFIG, db.get_user_config(uid))))
    else:
        cfg = global_config()
    cfg["provider"] = normalize_provider(cfg.get("provider", {}))
    return cfg


def save_config(cfg: dict) -> dict:
    """保存配置；掩码值保留原密钥。按当前用户写入 DB，否则写全局模板文件。"""
    from .user_context import current_user_id

    current = load_config()
    merged = _deep_merge(current, cfg)
    # 掩码占位 → 沿用已保存的原值
    for path in MASK_PATHS:
        if _get_path(merged, path) == MASK:
            old = _get_path(current, path)
            if old:
                _set_path(merged, path, old)
    uid = current_user_id.get()
    if uid is not None:
        from . import db
        db.save_user_config(uid, merged)
        return merged
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return merged


def masked_config(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    for path in MASK_PATHS:
        if _get_path(out, path):
            _set_path(out, path, MASK)
    return out
