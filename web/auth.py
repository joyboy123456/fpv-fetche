"""口令 cookie：HMAC 签名，不落库。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

COOKIE_NAME = "fpv_session"
TTL_SECONDS = 30 * 24 * 3600


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(secret: str) -> str:
    exp = str(int(time.time()) + TTL_SECONDS)
    return f"{exp}.{_sign(secret, exp)}"


def token_ok(secret: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, sig = token.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    expected = _sign(secret, exp_s)
    return hmac.compare_digest(sig, expected)


@dataclass
class LoginGate:
    """按 IP 限制口令尝试，避免 1C 机器被打满。"""

    max_fails: int = 8
    window: float = 600.0
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def blocked(self, ip: str) -> bool:
        now = time.monotonic()
        times = [t for t in self._hits.get(ip, []) if now - t < self.window]
        self._hits[ip] = times
        return len(times) >= self.max_fails

    def fail(self, ip: str) -> None:
        self._hits.setdefault(ip, []).append(time.monotonic())

    def ok(self, ip: str) -> None:
        self._hits.pop(ip, None)


def new_secret() -> str:
    return secrets.token_hex(16)


def password_ok(given: str, expected: str) -> bool:
    """长度不同时 compare_digest 会抛 TypeError，先哈希再比。"""
    left = hashlib.sha256(given.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)
