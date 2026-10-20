"""Dashboard authentication: password hashing, sessions, CSRF and login lockout.

Generate a password hash for .env:
    python -m volvo_mqtt.webauth
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import logging
import secrets
import sys
import threading
import time

log = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 600_000
SESSION_COOKIE = "vmqtt_session"


# ---------- password hashing (stdlib PBKDF2-SHA256) ----------
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    # ':' separators so the value is safe in .env files (no '$' interpolation)
    return f"pbkdf2_sha256:{iterations}:{salt}:{digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split(":")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(calc, digest)
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, username: str | None, password_hash: str | None, plain_password: str | None,
                 session_hours: int, max_attempts: int, lockout_minutes: int) -> None:
        self.username = username or "admin"
        if not password_hash and plain_password:
            password_hash = hash_password(plain_password, 100_000)
            log.warning("WEB_PASSWORD is set in plain text; prefer WEB_PASSWORD_HASH "
                        "(python -m volvo_mqtt.webauth)")
        self.password_hash = password_hash
        self.session_ttl = max(session_hours, 1) * 3600
        self.max_attempts = max(max_attempts, 3)
        self.lockout = max(lockout_minutes, 1) * 60
        self._sessions: dict[str, dict] = {}
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()
        # Dummy hash so failed lookups take the same time as real checks
        self._dummy = hash_password(secrets.token_hex(8), 100_000)
        if not self.password_hash:
            log.warning("Dashboard login is not configured: set WEB_PASSWORD_HASH in .env")

    @property
    def configured(self) -> bool:
        return bool(self.password_hash)

    # ---------- lockout ----------
    def locked_for(self, ip: str) -> int:
        with self._lock:
            until = self._locked_until.get(ip, 0)
            return max(0, int(until - time.time()))

    def _record_failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < self.lockout] + [now]
            self._failures[ip] = recent
            if len(recent) >= self.max_attempts:
                self._locked_until[ip] = now + self.lockout
                self._failures[ip] = []
                log.warning("Login locked for %s after %d failed attempts", ip, len(recent))

    # ---------- login ----------
    def login(self, username: str, password: str, ip: str) -> str | None:
        """Returns a new session id, or None on failure."""
        if self.locked_for(ip):
            return None
        user_ok = hmac.compare_digest(username.strip().lower(), self.username.lower())
        pw_ok = verify_password(password, self.password_hash if (self.configured and user_ok) else self._dummy)
        if user_ok and pw_ok and self.configured:
            sid = secrets.token_urlsafe(32)
            with self._lock:
                self._prune()
                self._sessions[sid] = {
                    "user": self.username,
                    "exp": time.time() + self.session_ttl,
                    "csrf": secrets.token_urlsafe(24),
                    "ip": ip,
                }
                self._failures.pop(ip, None)
            log.info("Dashboard login: %s from %s", self.username, ip)
            return sid
        self._record_failure(ip)
        reason = ("login not configured - WEB_PASSWORD_HASH is empty in the .env the container loaded"
                  if not self.configured else "unknown username" if not user_ok else "wrong password")
        log.warning("Failed dashboard login for %r from %s (%s)", username[:40], ip, reason)
        time.sleep(1.0)  # slow down guessing
        return None

    def session(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if not s:
                return None
            if s["exp"] < time.time():
                self._sessions.pop(sid, None)
                return None
            return s

    def logout(self, sid: str | None) -> None:
        if sid:
            with self._lock:
                self._sessions.pop(sid, None)

    def check_csrf(self, sid: str | None, token: str | None) -> bool:
        s = self.session(sid)
        return bool(s and token and hmac.compare_digest(s["csrf"], token))

    def _prune(self) -> None:
        now = time.time()
        for k in [k for k, v in self._sessions.items() if v["exp"] < now]:
            self._sessions.pop(k, None)


def check() -> None:
    """Verify a password against the hash in the .env this process loads."""
    from .config import load_config
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config()
    print(f"Config: {cfg.config_dir / '.env'}")
    print(f"WEB_USERNAME: {cfg.web_username}")
    stored = cfg.web_password_hash
    if not stored:
        sys.exit("WEB_PASSWORD_HASH is EMPTY in this .env - sign-in cannot work.")
    parts = stored.split(":")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        sys.exit("WEB_PASSWORD_HASH is malformed (expected pbkdf2_sha256:<iterations>:<salt>:<hash>).")
    print(f"Hash fingerprint: ...{parts[3][-8:]}")
    pw = getpass.getpass("Password to test: ")
    print("MATCH - this password will work." if verify_password(pw, stored) else "NO MATCH - wrong password for this hash.")


def main() -> None:
    if "--check" in sys.argv:
        return check()
    pw = getpass.getpass("New dashboard password: ")
    if len(pw) < 12:
        sys.exit("Use at least 12 characters for a publicly exposed dashboard.")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    print("\nAdd this line to .env:\n")
    print(f"WEB_PASSWORD_HASH={hash_password(pw)}")


if __name__ == "__main__":
    main()
