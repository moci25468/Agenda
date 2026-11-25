"""账号辅助：密码哈希、会话令牌、登录限流、邀请码。

不引入第三方依赖，只用标准库。密码用 PBKDF2-HMAC-SHA256 + 随机 salt。
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

PBKDF2_ITERATIONS = 600_000
SESSION_COOKIE = "qut_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
INVITE_TTL_SECONDS = 7 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256

_attempt_lock = threading.Lock()
_attempts: dict[str, list[float]] = defaultdict(list)
_pending_attempts: dict[str, dict[object, float]] = {}
MAX_ATTEMPTS_PER_MINUTE = 5
MAX_IP_ATTEMPTS_PER_MINUTE = 30
_DUMMY_HASH = (f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"
               f"{'00' * 16}${'00' * 32}")


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"密码不能超过 {MAX_PASSWORD_LENGTH} 位")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        algo, iterations, salt_hex, digest = (stored or _DUMMY_HASH).split("$")
        iterations = int(iterations)
        if algo != "pbkdf2_sha256" or not 1 <= iterations <= 10_000_000:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), iterations)
        return secrets.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def validate_credentials(username: str, password: str) -> str | None:
    """返回错误说明，合法则返回 None。"""
    u = (username or "").strip()
    if not (3 <= len(u) <= 32) or not all(c.isalnum() or c in "_-." for c in u):
        return "用户名需 3-32 位，只能含字母、数字、_ - ."
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"密码至少 {MIN_PASSWORD_LENGTH} 位"
    if len(password or "") > MAX_PASSWORD_LENGTH:
        return f"密码不能超过 {MAX_PASSWORD_LENGTH} 位"
    return None


def rate_limit_allowed(key: str, limit: int = MAX_ATTEMPTS_PER_MINUTE) -> bool:
    now = time.time()
    with _attempt_lock:
        hits = [t for t in _attempts.get(key, []) if now - t < 60]
        _attempts[key] = hits
        return len(hits) < limit


def record_auth_failure(key: str, limit: int = MAX_ATTEMPTS_PER_MINUTE) -> None:
    now = time.time()
    with _attempt_lock:
        hits = [t for t in _attempts.get(key, []) if now - t < 60]
        if len(hits) < limit:
            hits.append(now)
        _attempts[key] = hits


def consume_rate_limit(key: str, limit: int = MAX_ATTEMPTS_PER_MINUTE) -> bool:
    now = time.time()
    with _attempt_lock:
        hits = [t for t in _attempts.get(key, []) if now - t < 60]
        _attempts[key] = hits
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


@dataclass(frozen=True)
class LoginAttempt:
    account_key: str
    ip_key: str
    started_at: float
    token: object = field(default_factory=object)


def reserve_login_attempt(account_key: str, ip_key: str) -> LoginAttempt | None:
    """在验证密码前同时占用账号和 IP 额度，拒绝时不占用任意一组。"""
    now = time.time()
    limits = ((account_key, MAX_ATTEMPTS_PER_MINUTE),
              (ip_key, MAX_IP_ATTEMPTS_PER_MINUTE))
    with _attempt_lock:
        for key, _ in limits:
            hits = [t for t in _attempts.get(key, []) if now - t < 60]
            if hits:
                _attempts[key] = hits
            else:
                _attempts.pop(key, None)
            pending = {
                token: t for token, t in _pending_attempts.get(key, {}).items()
                if now - t < 60
            }
            if pending:
                _pending_attempts[key] = pending
            else:
                _pending_attempts.pop(key, None)
        if any(len(_attempts.get(key, [])) + len(_pending_attempts.get(key, {})) >= limit
               for key, limit in limits):
            return None
        attempt = LoginAttempt(account_key, ip_key, now)
        for key, _ in limits:
            _pending_attempts.setdefault(key, {})[attempt.token] = now
        return attempt


def complete_login_attempt(attempt: LoginAttempt, *, success: bool) -> None:
    """释放验证占位；成功重置账号失败记录，保留其他请求的 IP 额度。"""
    now = time.time()
    with _attempt_lock:
        for key in (attempt.account_key, attempt.ip_key):
            pending = _pending_attempts.get(key, {})
            existed = pending.pop(attempt.token, None) is not None
            if not pending:
                _pending_attempts.pop(key, None)
            if not success and existed and now - attempt.started_at < 60:
                _attempts.setdefault(key, []).append(attempt.started_at)
        if success:
            _attempts.pop(attempt.account_key, None)


def clear_rate_limit(key: str):
    with _attempt_lock:
        _attempts.pop(key, None)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_invite() -> str:
    return secrets.token_urlsafe(12)
