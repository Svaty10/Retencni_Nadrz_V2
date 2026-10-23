"""Passwords, server-side sessions and brute-force protection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional

from .db import Database

# scrypt N=2^14, r=8 -> 16 MiB per hash. Hashing is serialised so a burst of
# login attempts can never push the process over its memory limit.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 14, 8, 1
_scrypt_lock = threading.Lock()

MIN_PASSWORD_LEN = 10
SESSION_IDLE_S = 7 * 86400
SESSION_ABSOLUTE_S = 30 * 86400
SESSION_TOUCH_S = 300            # write last_seen at most every 5 min (SD wear)

USER_MAX_FAILS = 5
USER_LOCK_S = 15 * 60
USERNAME_MAXLEN = 32


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    with _scrypt_lock:
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                            p=SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


_DUMMY_HASH: Optional[str] = None


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Constant-time verification; unknown users cost the same as known ones."""
    global _DUMMY_HASH
    if not stored:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_hex(8))
        stored, password = _DUMMY_HASH, password + "\x00invalid"
        result_override = False
    else:
        result_override = None
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        if n > (1 << 15) or r > 8 or p > 1:      # refuse hashes that could exhaust RAM
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        with _scrypt_lock:
            dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                maxmem=64 * 1024 * 1024, dklen=len(expected))
        ok = hmac.compare_digest(dk, expected)
        return ok if result_override is None else False
    except (ValueError, TypeError):
        return False


def password_problem(username: str, password: str) -> Optional[str]:
    if len(password) < MIN_PASSWORD_LEN:
        return f"Heslo musí mít alespoň {MIN_PASSWORD_LEN} znaků."
    if len(password) > 256:
        return "Heslo je příliš dlouhé."
    if password.lower() == username.lower():
        return "Heslo nesmí být stejné jako jméno."
    if len(set(password)) < 5:
        return "Heslo je příliš jednoduché."
    return None


def username_problem(username: str) -> Optional[str]:
    if not 2 <= len(username) <= USERNAME_MAXLEN:
        return "Jméno musí mít 2–32 znaků."
    if any(ch.isspace() or ord(ch) < 32 or ch in "<>\"'&/\\" for ch in username):
        return "Jméno obsahuje nepovolené znaky."
    if username.lower().startswith("system"):
        return "Jména začínající „system“ jsou vyhrazena pro automatiku."
    return None


def _sid_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


class Sessions:
    """Opaque random session tokens; only their SHA-256 is stored."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, username: str, ip: str, ua: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = int(time.time())
        self.db.execute(
            "INSERT INTO sessions(id_hash, username, csrf, created, last_seen, ip, ua) "
            "VALUES(?,?,?,?,?,?,?)", (_sid_hash(token), username, csrf, now, now, ip, ua[:200]))
        return token, csrf

    def lookup(self, token: Optional[str]) -> Optional[dict]:
        if not token or len(token) > 100:
            return None
        h = _sid_hash(token)
        row = self.db.one(
            "SELECT s.id_hash, s.username, s.csrf, s.created, s.last_seen, u.role "
            "FROM sessions s JOIN users u ON u.username = s.username WHERE s.id_hash = ?", (h,))
        if row is None:
            return None
        now = int(time.time())
        if now - row["last_seen"] > SESSION_IDLE_S or now - row["created"] > SESSION_ABSOLUTE_S:
            self.db.execute("DELETE FROM sessions WHERE id_hash = ?", (h,))
            return None
        if now - row["last_seen"] > SESSION_TOUCH_S:
            self.db.execute("UPDATE sessions SET last_seen = ? WHERE id_hash = ?", (now, h))
        return {"id_hash": h, "username": row["username"], "role": row["role"], "csrf": row["csrf"]}

    def delete(self, token: Optional[str]) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE id_hash = ?", (_sid_hash(token),))

    def delete_user_sessions(self, username: str, except_hash: Optional[str] = None) -> None:
        self.db.execute("DELETE FROM sessions WHERE username = ? AND id_hash != ?",
                        (username, except_hash or ""))

    def purge(self) -> None:
        now = int(time.time())
        self.db.execute("DELETE FROM sessions WHERE last_seen < ? OR created < ?",
                        (now - SESSION_IDLE_S, now - SESSION_ABSOLUTE_S))


class LoginLocks:
    """Lock-out per (username, client IP) in memory. A stranger hammering an account
    from his own address locks only himself out, never the real owner."""

    def __init__(self, max_fails: int = USER_MAX_FAILS, lock_s: float = USER_LOCK_S):
        self.max_fails, self.lock_s = max_fails, lock_s
        self._d: dict[tuple[str, str], tuple[int, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(username: str, ip: str) -> tuple[str, str]:
        return username.lower(), ip

    def locked(self, username: str, ip: str) -> bool:
        with self._lock:
            _, until = self._d.get(self._key(username, ip), (0, 0.0))
        return until > time.time()

    def fail(self, username: str, ip: str) -> None:
        now = time.time()
        with self._lock:
            k = self._key(username, ip)
            fails, until = self._d.get(k, (0, 0.0))
            if until and until <= now:
                fails = 0
            fails += 1
            if fails >= self.max_fails:
                self._d[k] = (0, now + self.lock_s)
            else:
                self._d[k] = (fails, 0.0)
            if len(self._d) > 10000:
                self._d = {k: v for k, v in self._d.items() if v[1] > now}

    def success(self, username: str, ip: str) -> None:
        with self._lock:
            self._d.pop(self._key(username, ip), None)


class IpLimiter:
    """Per-IP exponential back-off for failed logins (in memory)."""

    def __init__(self, free_attempts: int = 3, max_delay: float = 3600.0):
        self.free = free_attempts
        self.max_delay = max_delay
        self._d: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, ip: str) -> int:
        with self._lock:
            fails, until = self._d.get(ip, (0, 0.0))
        return max(0, int(until - time.time() + 0.999))

    def fail(self, ip: str) -> None:
        with self._lock:
            fails, _ = self._d.get(ip, (0, 0.0))
            fails += 1
            delay = 0.0 if fails <= self.free else min(self.max_delay, 2.0 ** (fails - self.free))
            self._d[ip] = (fails, time.time() + delay)
            if len(self._d) > 10000:   # bound memory under a distributed attack
                cutoff = time.time() - self.max_delay
                self._d = {k: v for k, v in self._d.items() if v[1] > cutoff}

    def success(self, ip: str) -> None:
        with self._lock:
            self._d.pop(ip, None)
