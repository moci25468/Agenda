"""配置管理：data/config.json，首次运行生成默认配置。"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CAMPUS_DATA_DIR", BASE_DIR / "data"))
CONFIG_PATH = DATA_DIR / "config.json"

SECRET_FIELDS = [
    ("provider", "zf_password"), ("provider", "zf_cookie"),
    ("provider", "http_headers"),
    ("push", "bark_key"), ("push", "smtp_pass"),
    ("ai", "api_key"), ("ai", "headers"),
    ("security", "token"),
]
MASK = "********"

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
    "provider": {
        "type": "mock",               # mock | zhengfang | httpjson
        # --- 正方教务（jwglxt）---
        "zf_base": "https://jxgl.qut.edu.cn/jwglxt",
        "zf_username": "",
        "zf_password": "",
        "zf_cookie": "",              # 也可直填登录后的 Cookie，跳过账号登录
        "zf_year": "2026",            # 学年
        "zf_term": "3",               # 学期：3=第一学期(秋季) 12=第二学期(春季)
        # --- 通用 HTTP JSON 接口 ---
        "http_url": "",
        "http_method": "GET",
        "http_headers": "",           # JSON 字符串，如 {"Authorization": "Bearer xxx"}
        "http_body": "",              # POST 时的 JSON body
        "http_list_path": "data",     # 课程数组在响应 JSON 中的点路径
        "http_mapping": {             # 响应字段 → 标准字段
            "name": "name", "teacher": "teacher", "room": "room",
            "day": "day", "start": "start", "end": "end", "weeks": "weeks",
        },
    },
    "push": {
        "bark_enabled": False,
        "bark_server": "https://api.day.app",
        "bark_key": "",
        "smtp_enabled": False,
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_user": "",
        "smtp_pass": "",
        "smtp_to": "",
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
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        # 手动调整是整体状态，必须整体替换：否则删除的条目会被旧的合回来
        if k == "course_overrides":
            out[k] = v
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


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
        return json.loads(json.dumps(_deep_merge(DEFAULT_CONFIG, db.get_user_config(uid))))
    return global_config()


def save_config(cfg: dict) -> dict:
    """保存配置；掩码值保留原密钥。按当前用户写入 DB，否则写全局模板文件。"""
    from .user_context import current_user_id

    current = load_config()
    merged = _deep_merge(current, cfg)
    for section, key in SECRET_FIELDS:
        if merged.get(section, {}).get(key) == MASK:
            merged[section][key] = current.get(section, {}).get(key, "")
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
    for section, key in SECRET_FIELDS:
        if out.get(section, {}).get(key):
            out[section][key] = MASK
    return out