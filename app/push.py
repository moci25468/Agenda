"""推送通道：Bark（iOS）与企业微信 / 钉钉群机器人。所有发送结果写入 push_log。"""
from __future__ import annotations

import httpx

from . import db

WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="


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
    hook = _wecom_url(p.get("wecom_webhook") or "")
    if not hook:
        return False, "未配置群机器人 Webhook"
    payload = {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            r = await client.post(hook, json=payload)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and data.get("errcode") == 0:
                return True, ""
            return False, f"群机器人返回 {r.status_code}: {r.text[:120]}"
    except Exception as e:  # noqa: BLE001
        return False, f"群机器人请求失败: {e}"


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