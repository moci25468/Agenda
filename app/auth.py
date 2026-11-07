"""账号辅助：密码哈希、会话令牌、登录限流、邀请码。

不引入第三方依赖，只用标准库。密码用 PBKDF2-HMAC-SHA256 + 随机 salt。
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict

PBKDF2_ITERATIONS = 200_000
SESSION_COOKIE = "qut_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
INVITE_TTL_SECONDS = 7 * 24 * 3600
MIN_PASSWORD_LENGTH = 8

_attempt_lock = threading.Lock()
_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS_PER_MINUTE = 5


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iterations))
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
    return None


def check_rate_limit(key: str) -> bool:
    """每分钟最多 5 次失败尝试；返回 False 表示已被限流。"""
    now = time.time()
    with _attempt_lock:
        hits = [t for t in _attempts[key] if now - t < 60]
        _attempts[key] = hits
        if len(hits) >= MAX_ATTEMPTS_PER_MINUTE:
            return False
        hits.append(now)
        return True


def clear_rate_limit(key: str):
    with _attempt_lock:
        _attempts.pop(key, None)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_invite() -> str:
    return secrets.token_urlsafe(12)
