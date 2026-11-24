"""推送通道：Bark（iOS）与企业微信 / 钉钉群机器人。所有发送结果写入 push_log。"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx

from . import db
from .netguard import SafeAsyncHTTPTransport, UnsafeURLError, assert_safe_outbound_url

log = logging.getLogger(__name__)

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
        assert_safe_outbound_url(server)  # 防 SSRF
    except UnsafeURLError as e:
        return False, f"Bark 服务器地址不安全：{e}"
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False,
                                     transport=SafeAsyncHTTPTransport()) as client:
            r = await client.post(f"{server}/{key}", json=payload)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and data.get("code") == 200:
                return True, ""
            return False, f"Bark 返回 {r.status_code}: {r.text[:120]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Bark 请求失败: {type(exc).__name__}"


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
        assert_safe_outbound_url(hook)  # 防 SSRF
    except UnsafeURLError as e:
        return False, f"机器人地址不安全：{e}"
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False,
                                     transport=SafeAsyncHTTPTransport()) as client:
            r = await client.post(hook, json=payload)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and data.get("errcode") == 0:
                return True, ""
            code = data.get("errcode")
            if code is not None:
                msg = WECOM_ERRORS.get(code) or data.get("errmsg") or "群机器人返回错误"
                return False, f"{msg}（errcode {code}）"
            return False, f"群机器人返回 {r.status_code}: {r.text[:120]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"群机器人请求失败: {type(exc).__name__}"


CHANNELS = ("bark", "wecom")
CHANNEL_NAMES = {"bark": "Bark", "wecom": "企业微信 / 钉钉群机器人"}


def channel_ready(cfg: dict, channel: str) -> tuple[bool, str]:
    """通道是否可发（是否已在设置中配置）。"""
    p = cfg.get("push", {})
    if channel == "bark":
        configured = bool(str(p.get("bark_key") or "").strip())
    else:
        hook = str(p.get("wecom_webhook") or "").strip()
        configured = bool(hook) and not wecom_input_error(hook)
    name = CHANNEL_NAMES.get(channel, channel)
    if not configured:
        return False, f"未配置 {name}，请先在设置中填写"
    return True, ""


async def send_selected(cfg: dict, title: str, body: str, channels: list[str]) -> list[dict]:
    """按指定通道发送，逐条写 push_log。"""
    results = []
    for channel in channels:
        if channel not in CHANNELS:
            results.append({"channel": channel, "ok": False,
                            "error": f"不支持的推送通道：{channel}"})
            continue
        ready, err = channel_ready(cfg, channel)
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


async def push_all(cfg: dict, title: str, body: str,
                   on_result: Callable[[dict], None] | None = None) -> list[dict]:
    """向所有已启用通道推送，逐条记录日志，返回结果列表。

    on_result 在每个通道完成后立即同步调用，供 outbox 先持久化成功结果；
    这样后续通道超时或异常时，不会丢掉前面已经送达的通道。
    """
    p = cfg.get("push", {})
    results = []

    async def deliver(channel: str) -> None:
        try:
            if channel == "bark":
                ok, err = await send_bark(cfg, title, body)
            else:
                ok, err = await send_wecom(cfg, title, body)
        except Exception as exc:  # noqa: BLE001
            # 单通道异常不阻断后续通道；原始异常可能含 webhook，不写入日志。
            ok, err = False, f"{CHANNEL_NAMES.get(channel, channel)}推送异常：{type(exc).__name__}"
        result = {"channel": channel, "ok": ok, "error": err}
        results.append(result)
        try:
            db.add_push_log(channel, title, body, "ok" if ok else "fail", err)
        except Exception as exc:  # noqa: BLE001
            log.warning("Push log persistence failed (%s)", type(exc).__name__)
        finally:
            if on_result is not None:
                on_result(result)

    if p.get("bark_enabled"):
        await deliver("bark")
    if p.get("wecom_enabled") and _wecom_url(p.get("wecom_webhook") or ""):
        await deliver("wecom")
    if not results:
        db.add_push_log("none", title, body, "skip", "未启用任何推送通道")
        result = {"channel": "none", "ok": False,
                  "error": "未启用任何推送通道（设置中开启 Bark 或企业微信群机器人）"}
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results
