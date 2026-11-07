"""推送通道：Bark（iOS）与 SMTP 邮件。所有发送结果写入 push_log。"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import httpx

from . import db


async def send_bark(cfg: dict, title: str, body: str, url: str = "") -> tuple[bool, str]:
    p = cfg.get("push", {})
    key = p.get("bark_key", "")
    server = (p.get("bark_server") or "https://api.day.app").rstrip("/")
    if not key:
        return False, "未配置 Bark Key"
    payload = {"title": title, "body": body, "group": "青理课表助手", "sound": "minuet"}
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


def _send_smtp_sync(cfg: dict, title: str, body: str) -> tuple[bool, str]:
    p = cfg.get("push", {})
    host, port = p.get("smtp_host", ""), int(p.get("smtp_port") or 465)
    user, password, to = p.get("smtp_user", ""), p.get("smtp_pass", ""), p.get("smtp_to", "")
    if not (host and user and password and to):
        return False, "SMTP 配置不完整（host/user/pass/to）"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr((str(Header("青理课表助手", "utf-8")), user))
    msg["To"] = to
    try:
        if p.get("smtp_ssl", True):
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as s:
                s.login(user, password)
                s.sendmail(user, [to], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.sendmail(user, [to], msg.as_string())
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"SMTP 发送失败: {e}"


async def send_email(cfg: dict, title: str, body: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_send_smtp_sync, cfg, title, body)


async def push_all(cfg: dict, title: str, body: str) -> list[dict]:
    """向所有已启用通道推送，逐条记录日志，返回结果列表。"""
    p = cfg.get("push", {})
    results = []
    if p.get("bark_enabled"):
        ok, err = await send_bark(cfg, title, body)
        db.add_push_log("bark", title, body, "ok" if ok else "fail", err)
        results.append({"channel": "bark", "ok": ok, "error": err})
    if p.get("smtp_enabled"):
        ok, err = await send_email(cfg, title, body)
        db.add_push_log("email", title, body, "ok" if ok else "fail", err)
        results.append({"channel": "email", "ok": ok, "error": err})
    if not results:
        db.add_push_log("none", title, body, "skip", "未启用任何推送通道")
        results.append({"channel": "none", "ok": False, "error": "未启用任何推送通道（设置中开启 Bark 或邮件）"})
    return results