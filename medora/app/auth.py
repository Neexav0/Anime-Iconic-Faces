"""Accounts and sessions. Standard library only.

- Passwords: scrypt (n=2^14, r=8, p=1) with a 16-byte random salt, compared in constant time.
- Sessions: a 32-byte random token in an HttpOnly, SameSite=Lax cookie; only its SHA-256 is stored.
- Brute force: at most MAX_FAILS failed sign-ins per login and per IP address in FAIL_WINDOW.
- Password reset without email or SMS: a one-time recovery code shown at sign-up, stored only as a scrypt hash.
- Abuse: sign-ups and sample accounts are capped per IP address per hour.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque

SESSION_COOKIE = "sid"
SESSION_DAYS = 30
MIN_PASSWORD = 8
MAX_FAILS = 5
FAIL_WINDOW = 15 * 60

_N, _R, _P = 2 ** 14, 8, 1
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Spend the same time as a real check so missing accounts are not revealed by timing.
        hashlib.scrypt(password.encode(), salt=b"0" * 16, n=_N, r=_R, p=_P, dklen=32)
        return False
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def normalise_login(raw: str) -> str | None:
    """Email (lower-cased) or Indian mobile number (10 digits, +91/0 prefix removed). Anything else: None."""
    s = (raw or "").strip()
    if "@" in s:
        s = s.lower()
        return s if EMAIL_RE.match(s) and len(s) <= 160 else None
    digits = re.sub(r"[\s\-()]", "", s)
    if digits.startswith("+91"):
        digits = digits[3:]
    elif digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def password_problem(password: str, login: str = "") -> str | None:
    """Return an error code, or None if the password is acceptable."""
    if len(password) < MIN_PASSWORD:
        return "pw_short"
    if len(password) > 200:
        return "pw_long"
    if login and password.lower() == login.lower():
        return "pw_same"
    if password.lower() in {"password", "12345678", "123456789", "qwertyuiop", "medora123", "11111111"}:
        return "pw_common"
    return None


RECOVERY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L: easy to copy by hand


def new_recovery_code() -> str:
    """16 characters (~78 bits) shown as four groups: 'K7QM-3XRP-...'."""
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def normalise_recovery(code: str) -> str:
    """Ignore spaces, dashes and case, so 'k7qm 3xrp …' matches 'K7QM-3XRP-…'."""
    return re.sub(r"[\s\-]", "", (code or "").upper())[:64]


def new_token() -> tuple[str, str]:
    """(token for the cookie, hash for the database)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Throttle:
    """In-memory sliding window of failed attempts. Per process; enough for a single-server prototype."""

    def __init__(self, limit: int = MAX_FAILS, window: int = FAIL_WINDOW):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        q = self._fails[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(len(self._prune(k, now)) >= self.limit for k in keys if k)

    def fail(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for k in keys:
                if k:
                    self._prune(k, now).append(now)

    hit = fail  # count an event that is not a failure (e.g. an account created)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)


LOGIN_THROTTLE = Throttle()
RESET_THROTTLE = Throttle()
SIGNUP_THROTTLE = Throttle(limit=10, window=60 * 60)
SAMPLE_THROTTLE = Throttle(limit=20, window=60 * 60)
ALL_THROTTLES = (LOGIN_THROTTLE, RESET_THROTTLE, SIGNUP_THROTTLE, SAMPLE_THROTTLE)
