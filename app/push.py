"""推送通道：Bark（iOS）。所有发送结果写入 push_log。"""
from __future__ import annotations

import httpx

from . import db


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


async def push_all(cfg: dict, title: str, body: str) -> list[dict]:
    """向所有已启用通道推送，逐条记录日志，返回结果列表。"""
    p = cfg.get("push", {})
    results = []
    if p.get("bark_enabled"):
        ok, err = await send_bark(cfg, title, body)
        db.add_push_log("bark", title, body, "ok" if ok else "fail", err)
        results.append({"channel": "bark", "ok": ok, "error": err})
    if not results:
        db.add_push_log("none", title, body, "skip", "未启用任何推送通道")
        results.append({"channel": "none", "ok": False, "error": "未启用任何推送通道（设置中开启 Bark）"})
    return results