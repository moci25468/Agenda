"""推送通道：Bark（iOS）与企业微信 / 钉钉群机器人。所有发送结果写入 push_log。"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import httpx

from . import db

WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
WECOM_HOST = "qyapi.weixin.qq.com"
# 管理后台的网页地址，长得像但调不通，必须挡在保存这一步
ADMIN_HOSTS = ("work.weixin.qq.com", "open.work.weixin.qq.com")
# 机器人 key 是带横线的 UUID；钉钉 access_token 是 64 位十六进制
BARE_KEY_RE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{64})$")
WECOM_HINT = ("在企业微信群里点右上角「…」→ 群机器人 → 添加机器人，"
              "复制形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=… 的地址")
WECOM_ERRORS = {
    93000: "Webhook 无效：key 不存在或机器人已被移出群，请重新复制完整地址",
    40001: "Webhook key 无效，或消息内容为空",
    40013: "Webhook 所属企业不存在，请确认机器人还在原群里",
    45009: "发送太频繁（群机器人每分钟最多 20 条），稍后重试",
}


def wecom_input_error(value: str) -> str:
    """校验填写的群机器人地址，返回可直接展示的中文说明；合法或为空返回空串。"""
    v = (value or "").strip()
    if not v:
        return ""
    if "://" in v:
        parts = urlsplit(v)
        host = (parts.hostname or "").lower()
        if host in ADMIN_HOSTS:
            return f"这是企业微信管理后台的网页地址，不是推送地址。{WECOM_HINT}"
        if host == WECOM_HOST and not (parse_qs(parts.query).get("key") or [""])[0].strip():
            return f"地址里缺少 key 参数。{WECOM_HINT}"
        return ""
    if BARE_KEY_RE.match(v):
        return ""
    return f"只填 key 时必须是 Webhook 里的 key（36 位带横线），当前值既不是完整地址也不是 key。{WECOM_HINT}"


def _wecom_url(hook: str) -> str:
    """允许只填 key，自动补全企业微信 webhook 地址；完整 URL 原样使用。"""
    hook = (hook or "").strip()
    if not hook:
        return ""
    if hook.startswith("http://") or hook.startswith("https://"):
        return hook
    return WECOM_API + hook


async def send_bark(cfg: dict, title: str, body: str, url: str = "") -> tuple[bool, str]:
    p = cfg.get("push", {})
    key = p.get("bark_key", "")
    server = (p.get("bark_server") or "https://api.day.app").rstrip("/")
    if not key:
        return False, "未配置 Bark Key"
    payload = {"title": title, "body": body, "group": "Agenda", "sound": "minuet"}
    if url:
        payload["url"] = url
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            r = await client.post(f"{server}/{key}", json=payload)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and data.get("code") == 200:
                return True, ""
            return False, f"Bark 返回 {r.status_code}: {r.text[:120]}"
    except Exception as e:  # noqa: BLE001
        return False, f"Bark 请求失败: {e}"


async def send_wecom(cfg: dict, title: str, body: str) -> tuple[bool, str]:
    """企业微信 / 钉钉群机器人：两者 text 消息体相同，webhook 地址不同。"""
    p = cfg.get("push", {})
    raw = p.get("wecom_webhook") or ""
    if not (raw or "").strip():
        return False, "未配置群机器人 Webhook"
    bad = wecom_input_error(raw)
    if bad:
        return False, bad
    hook = _wecom_url(raw)
    payload = {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            r = await client.post(hook, json=payload)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and data.get("errcode") == 0:
                return True, ""
            code = data.get("errcode")
            if code is not None:
                msg = WECOM_ERRORS.get(code) or data.get("errmsg") or "群机器人返回错误"
                return False, f"{msg}（errcode {code}）"
            return False, f"群机器人返回 {r.status_code}: {r.text[:120]}"
    except Exception as e:  # noqa: BLE001
        return False, f"群机器人请求失败: {e}"


CHANNELS = ("bark", "wecom")
CHANNEL_NAMES = {"bark": "Bark", "wecom": "企业微信 / 钉钉群机器人"}


def channel_ready(cfg: dict, channel: str, need_enabled: bool = False) -> tuple[bool, str]:
    """通道是否可发：need_enabled 时还要求设置里的启用开关已打开。"""
    p = cfg.get("push", {})
    if channel == "bark":
        configured = bool(str(p.get("bark_key") or "").strip())
    else:
        hook = str(p.get("wecom_webhook") or "").strip()
        configured = bool(hook) and not wecom_input_error(hook)
    name = CHANNEL_NAMES.get(channel, channel)
    if not configured:
        return False, f"未配置 {name}，请先在设置中填写"
    if need_enabled and not p.get(f"{channel}_enabled"):
        return False, f"{name} 推送未启用，请先在设置中开启"
    return True, ""


def available_channels(cfg: dict) -> list[str]:
    """已配置并启用的通道，供 AI 判断能不能主动推送。"""
    return [c for c in CHANNELS if channel_ready(cfg, c, need_enabled=True)[0]]


async def send_selected(cfg: dict, title: str, body: str, channels: list[str],
                        need_enabled: bool = False) -> list[dict]:
    """按指定通道发送，逐条写 push_log。测试时可绕过启用开关。"""
    results = []
    for channel in channels:
        if channel not in CHANNELS:
            results.append({"channel": channel, "ok": False,
                            "error": f"不支持的推送通道：{channel}"})
            continue
        ready, err = channel_ready(cfg, channel, need_enabled=need_enabled)
        if not ready:
            db.add_push_log(channel, title, body, "skip", err)
            results.append({"channel": channel, "ok": False, "error": err})
            continue
        if channel == "bark":
            ok, err = await send_bark(cfg, title, body)
        else:
            ok, err = await send_wecom(cfg, title, body)
        db.add_push_log(channel, title, body, "ok" if ok else "fail", err)
        results.append({"channel": channel, "ok": ok, "error": err})
    return results


async def push_all(cfg: dict, title: str, body: str) -> list[dict]:
    """向所有已启用通道推送，逐条记录日志，返回结果列表。"""
    p = cfg.get("push", {})
    results = []
    if p.get("bark_enabled"):
        ok, err = await send_bark(cfg, title, body)
        db.add_push_log("bark", title, body, "ok" if ok else "fail", err)
        results.append({"channel": "bark", "ok": ok, "error": err})
    if p.get("wecom_enabled") and _wecom_url(p.get("wecom_webhook") or ""):
        ok, err = await send_wecom(cfg, title, body)
        db.add_push_log("wecom", title, body, "ok" if ok else "fail", err)
        results.append({"channel": "wecom", "ok": ok, "error": err})
    if not results:
        db.add_push_log("none", title, body, "skip", "未启用任何推送通道")
        results.append({"channel": "none", "ok": False, "error": "未启用任何推送通道（设置中开启 Bark 或企业微信群机器人）"})
    return results