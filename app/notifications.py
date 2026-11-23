"""学习通会话与站内通知同步，和课表数据源独立。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from time import monotonic
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db
from .config import DEFAULT_CONFIG, load_config
from .providers.base import AuthExpiredError
from .push import push_all
from .user_context import current_user_id, set_current_user

BUSY_ERROR = "学习通正在处理另一项操作，请稍后重试"
UNCONFIGURED_ERROR = "请先绑定学习通账号"
AUTH_ERROR = "学习通登录已失效，请重新登录"
SYNC_ERROR = "学习通同步失败，请稍后重试"
TZ = ZoneInfo("Asia/Shanghai")

_busy_users: set[int] = set()
_busy_guard = threading.Lock()
_sms_guard = threading.Lock()
_sms_sessions: dict[tuple[int, str], dict] = {}
_sms_sent_at: dict[tuple[int, str], float] = {}


def _clear_sms(user_id: int):
    with _sms_guard:
        for key in list(_sms_sessions):
            if key[0] == user_id:
                del _sms_sessions[key]


def _prune_sms(now: float):
    # 调用方持有线程锁；过期 Cookie 不再参与后续登录。
    for key, session in list(_sms_sessions.items()):
        if now >= session["expires"]:
            del _sms_sessions[key]
    for key, sent_at in list(_sms_sent_at.items()):
        if now - sent_at >= 60:
            del _sms_sent_at[key]


class BusyError(Exception):
    pass


class OperationError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@contextmanager
def _user_lock(user_id: int):
    # APScheduler 使用独立线程和事件循环，因此用线程安全的非阻塞占位。
    with _busy_guard:
        if user_id in _busy_users:
            raise BusyError(BUSY_ERROR)
        _busy_users.add(user_id)
    try:
        yield
    finally:
        with _busy_guard:
            _busy_users.remove(user_id)


def _configured(cx: dict) -> bool:
    cookies = cx.get("cookies")
    return bool(cx.get("uid") and isinstance(cookies, list) and cookies)


def _poll_minutes(cx: dict) -> int:
    value = cx.get("poll_minutes")
    return min(value, 1440) if type(value) is int and value > 0 else DEFAULT_CONFIG["chaoxing"]["poll_minutes"]


def _status(cx: dict, sync: dict) -> dict:
    configured = _configured(cx)
    enabled = cx.get("enabled") is True
    error = sync.get("error") or ""
    warning = "；".join(value for value in (sync.get("warning"), sync.get("push_warning")) if value)
    if not configured:
        state = "unconfigured"
    elif sync.get("auth_expired"):
        state = "auth_expired"
    elif not enabled:
        state = "disabled"
    elif error:
        state = "error"
    elif warning:
        state = "partial"
    else:
        state = "ready"
    return {"configured": configured, "username": cx.get("username") or "",
            "name": cx.get("name") or "", "enabled": enabled,
            "push_enabled": cx.get("push_enabled") is True, "poll_minutes": _poll_minutes(cx),
            "last_sync": sync.get("last_sync"), "last_attempt": sync.get("last_attempt"),
            "error": error, "warning": warning,
            "baseline_complete": bool(sync.get("baseline_complete")), "state": state}


def status(user_id: int) -> dict:
    return _status(*db.get_chaoxing_status_data(user_id))


def list_items(user_id: int, kind: str, unread: bool, limit: int) -> dict:
    result = db.list_notifications(user_id, kind, unread, limit)
    meta = _status(result.pop("config"), result.pop("sync"))
    result.update({key: meta[key] for key in ("last_sync", "error", "warning", "configured", "state")})
    return result


async def login(user_id: int, username: str, password: str, captcha: str = "") -> dict:
    from .providers.chaoxing import ChaoxingClient, ChaoxingError
    
    with _user_lock(user_id):
        # 登录必须从空 Cookie 开始，避免沿用已绑定账号的会话。
        try:
            async with ChaoxingClient(dict(DEFAULT_CONFIG["chaoxing"])) as client:
                operation = client.login(username, password, captcha) if captcha else client.login(username, password)
                account = await asyncio.wait_for(operation, timeout=45)
                cookies = client.export_cookies()
            uid = str(account.get("uid") or "").strip()
            if not uid or not isinstance(cookies, list) or not cookies:
                raise ValueError("学习通未返回完整会话")
        except (AuthExpiredError, ChaoxingError):
            raise OperationError("学习通登录失败，请检查账号密码或验证码") from None
        except Exception:
            # 上游异常可能包含登录请求地址或敏感参数，不传给前端和日志。
            raise OperationError("暂时无法登录学习通，请稍后重试", 502) from None
        binding = {"username": username, "uid": uid, "name": str(account.get("name") or ""),
                   "cookies": cookies}
        if not await asyncio.to_thread(db.set_chaoxing_binding, user_id, binding):
            raise OperationError("当前账号不可用，请重新登录", 401)
        _clear_sms(user_id)
        return await asyncio.to_thread(status, user_id)


async def send_sms(user_id: int, username: str, validate: str) -> dict:
    from .providers.chaoxing import ChaoxingClient, ChaoxingError, SmsSendError

    with _user_lock(user_id):
        key = (user_id, username)
        with _sms_guard:
            now = monotonic()
            _prune_sms(now)
            if any(uid == user_id or phone == username for uid, phone in _sms_sent_at):
                raise OperationError("请等待 60 秒后再发送短信", 429)
            # 请求之前占位，失败也不能立即重发。
            _sms_sent_at[key] = now
            _sms_sessions.pop(key, None)
        try:
            async with ChaoxingClient({}) as client:
                await asyncio.wait_for(client.send_sms(username, validate), timeout=65)
                cookies = client.export_cookies()
        except SmsSendError as exc:
            logging.getLogger(__name__).warning("chaoxing_sms_send_failed reason=%s", exc.reason)
            raise OperationError(str(exc), 429 if exc.reason == "quota" else 400) from None
        except (AuthExpiredError, ChaoxingError):
            logging.getLogger(__name__).warning("chaoxing_sms_send_failed reason=unclassified")
            raise OperationError("短信发送失败，请重新完成人工验证或稍后重试 [SMS_UNCLASSIFIED]") from None
        except Exception:
            raise OperationError("暂时无法发送短信，请稍后重试", 502) from None
        with _sms_guard:
            _sms_sessions[key] = {"cookies": cookies, "expires": monotonic() + 600, "attempts": 0}
        return {"sent": True, "retry_after": 60}


async def login_sms(user_id: int, username: str, code: str) -> dict:
    from .providers.chaoxing import ChaoxingClient, ChaoxingError, SmsLoginError

    with _user_lock(user_id):
        key = (user_id, username)
        with _sms_guard:
            _prune_sms(monotonic())
            session = _sms_sessions.get(key)
            if session is None:
                raise OperationError("短信会话已失效，请重新发送验证码")
            if session["attempts"] >= 5:
                raise OperationError("验证码尝试次数已达上限，请重新发送", 429)
            session["attempts"] += 1
        try:
            async with ChaoxingClient({"cookies": session["cookies"]}) as client:
                try:
                    account = await asyncio.wait_for(client.login_sms(username, code), timeout=45)
                finally:
                    # 失败响应也可能更新会话，保留以供剩余次数重试。
                    with _sms_guard:
                        session["cookies"] = client.export_cookies()
                cookies = client.export_cookies()
            uid = str(account.get("uid") or "").strip()
            if not uid or not cookies:
                raise ValueError("会话不完整")
        except SmsLoginError as exc:
            logging.getLogger(__name__).warning("chaoxing_sms_login_failed reason=%s", exc.reason)
            raise OperationError(str(exc), 502 if exc.reason in ("transport", "response", "profile") else 400) from None
        except (AuthExpiredError, ChaoxingError):
            logging.getLogger(__name__).warning("chaoxing_sms_login_failed reason=unclassified")
            raise OperationError("学习通登录未完成，原因暂未确认，请稍后重试 [SMS_LOGIN_UNCLASSIFIED]") from None
        except TimeoutError:
            logging.getLogger(__name__).warning("chaoxing_sms_login_failed reason=timeout")
            raise OperationError("学习通登录确认超时，请稍后重试 [SMS_LOGIN_TIMEOUT]", 502) from None
        except Exception:
            raise OperationError("暂时无法登录学习通，请稍后重试", 502) from None
        if monotonic() >= session["expires"]:
            _clear_sms(user_id)
            raise OperationError("短信会话已失效，请重新发送验证码")
        binding = {"username": username, "uid": uid, "name": str(account.get("name") or ""),
                   "cookies": cookies}
        if not await asyncio.to_thread(db.set_chaoxing_binding, user_id, binding):
            raise OperationError("当前账号不可用，请重新登录", 401)
        _clear_sms(user_id)
        return await asyncio.to_thread(status, user_id)


async def disconnect(user_id: int) -> dict:
    with _user_lock(user_id):
        if not await asyncio.to_thread(db.set_chaoxing_binding, user_id, None):
            raise OperationError("当前账号不可用，请重新登录", 401)
        _clear_sms(user_id)
        return await asyncio.to_thread(status, user_id)


async def set_options(user_id: int, options: dict) -> dict:
    with _user_lock(user_id):
        if options and not await asyncio.to_thread(db.patch_chaoxing_options, user_id, options):
            raise OperationError(UNCONFIGURED_ERROR)
        return await asyncio.to_thread(status, user_id)


def _sync_due(cx: dict, sync: dict, now: datetime) -> bool:
    if not _configured(cx) or cx.get("enabled") is not True or sync.get("auth_expired"):
        return False
    last_attempt = sync.get("last_attempt")
    if not last_attempt:
        return True
    minutes = _poll_minutes(cx)
    failures = sync.get("failures", 0)
    if failures:
        minutes = min(360, minutes * 2 ** min(failures, 4)) if minutes <= 360 else minutes
    return now >= datetime.fromisoformat(last_attempt) + timedelta(minutes=minutes)


def _sync_failure(error: str) -> dict:
    return {"ok": False, "count": 0, "new_count": 0, "error": error}


async def sync(user_id: int, *, scheduled: bool = False) -> dict | None:
    with _user_lock(user_id):
        cx, sync_meta = await asyncio.to_thread(db.get_chaoxing_status_data, user_id)
        ctx = set_current_user(user_id)
        try:
            now = datetime.now(TZ)
            if scheduled and not _sync_due(cx, sync_meta, now):
                await _retry_outbox(user_id)
                return None
            if not _configured(cx):
                return _sync_failure(UNCONFIGURED_ERROR)
            if not await asyncio.to_thread(db.start_chaoxing_attempt, user_id, cx,
                                           now.isoformat(timespec="seconds")):
                return _sync_failure("学习通绑定已变更，请重试")

            from .providers.chaoxing import ChaoxingClient

            error, auth_expired = "", False
            warnings = []
            try:
                async with ChaoxingClient(cx) as client:
                    result = await asyncio.wait_for(client.fetch_items(), timeout=240)
                    cookies = client.export_cookies()
                # 同次扫描也可能遇到重复项，保留每个来源的最后一个版本。
                items = {(item["kind"], item["source_id"]): item for item in result["items"]}
                warnings = result.get("warnings") or []
            except AuthExpiredError:
                error, auth_expired = AUTH_ERROR, True
            except Exception:
                error = SYNC_ERROR

            if error:
                await asyncio.to_thread(db.record_chaoxing_failure, user_id, cx, error, auth_expired)
                answer = _sync_failure(error)
            else:
                synced_at = datetime.now(TZ).isoformat(timespec="seconds")
                saved = await asyncio.to_thread(db.save_chaoxing_notifications, user_id, cx,
                                                list(items.values()), cookies, synced_at,
                                                "；".join(str(w) for w in warnings)[:4000])
                if saved is None:
                    return _sync_failure("学习通绑定已变更，请重试")
                _, _, new_count = saved
                answer = {"ok": True, "count": len(items), "new_count": new_count}
            # 拉取失败、没有变更或尚未到轮询时间，都不阻止独立的到期推送重试。
            await _retry_outbox(user_id)
            current_cx, current_sync = await asyncio.to_thread(db.get_chaoxing_status_data, user_id)
            meta = _status(current_cx, current_sync)
            answer["warning"] = meta["warning"]
            push_warning = current_sync.get("push_warning")
            warnings = [*warnings, *([push_warning] if push_warning else [])]
            if warnings:
                answer["warnings"] = warnings
            return answer
        finally:
            current_user_id.reset(ctx)


async def _retry_outbox(user_id: int):
    cfg = await asyncio.to_thread(load_config)
    cx = cfg["chaoxing"]
    if not _configured(cx):
        return
    rows = await asyncio.to_thread(db.get_due_notification_outbox, user_id, cx,
                                   datetime.now(TZ).isoformat(timespec="seconds"))
    if not rows:
        return
    delivered = set(json.loads(rows[0]["delivered_channels"]))
    push = dict(cfg.get("push") or {})
    enabled = {channel for channel in ("bark", "wecom") if push.get(f"{channel}_enabled")}
    succeeded = bool(enabled) and enabled <= delivered
    if not succeeded:
        for channel in delivered:
            push[f"{channel}_enabled"] = False
        try:
            # 每轮最多一批、一次 push_all；不在请求内循环排空积压。
            results = await asyncio.wait_for(
                _push_changes(dict(cfg, push=push), [json.loads(row["payload"]) for row in rows]),
                timeout=35,
            )
            delivered.update(result["channel"] for result in results
                             if result.get("ok") is True and result.get("channel") in enabled)
            succeeded = bool(enabled) and enabled <= delivered
        except Exception:
            # 不保存通道原始错误，异常可能包含 webhook 或 Cookie。
            succeeded = False
    await asyncio.to_thread(db.finish_notification_outbox, user_id, cx, rows,
                            sorted(delivered), succeeded)


async def _push_changes(cfg: dict, changed: list[dict]):
    if not changed:
        return []
    # 分批分类：截止提醒和内容更新使用不同格式。
    is_due = changed[0].get("event") == "homework_due"
    lines = []
    if is_due:
        for item in changed:
            due_at = item.get("due_at", "")
            line = f"📌 {str(item['title'])[:100]}"
            if item.get("course"):
                line += f" · {str(item['course'])[:60]}"
            if due_at:
                line += f" · 截止 {str(due_at)[:19]}"
            lines.append(line)
        title = f"作业截止提醒（{len(changed)} 项）"
    else:
        for item in changed[:5]:
            label = "作业" if item["kind"] == "homework" else "通知"
            course = item.get("course")
            due_at = item.get("due_at")
            line = f"【{label}】{str(item['title'])[:100]}"
            if course:
                line += f" · {str(course)[:60]}"
            if due_at:
                line += f" · 截止 {str(due_at)[:40]}"
            lines.append(line)
        if len(changed) > 5:
            lines.append(f"另有 {len(changed) - 5} 条更新，请在站内查看")
        title = "学习通通知更新"
    return await push_all(cfg, title, "\n".join(lines))
