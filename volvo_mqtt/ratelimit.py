"""Thread-safe rate limiter: per-minute sliding window, daily cap and server back-off."""
from __future__ import annotations

import collections
import logging
import threading
import time
from datetime import date

log = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when the daily budget is spent; callers should skip, not wait."""


class RateLimiter:
    def __init__(self, per_minute: int, per_day: int) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self._window: collections.deque[float] = collections.deque()
        self._day = date.today()
        self._day_count = 0
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def backoff(self, seconds: float) -> None:
        """Block all calls for `seconds` (e.g. after an HTTP 429 Retry-After)."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)
        log.warning("API back-off for %.0fs", seconds)

    def acquire(self, max_wait: float = 120.0) -> None:
        """Wait for a slot. Raises RateLimitExceeded if the daily budget is spent
        or the wait would exceed max_wait."""
        deadline = time.monotonic() + max_wait
        while True:
            with self._lock:
                now = time.monotonic()
                today = date.today()
                if today != self._day:
                    self._day, self._day_count = today, 0
                if self._day_count >= self.per_day:
                    raise RateLimitExceeded(f"daily budget of {self.per_day} requests used")
                while self._window and now - self._window[0] >= 60:
                    self._window.popleft()
                wait = 0.0
                if now < self._blocked_until:
                    wait = self._blocked_until - now
                elif len(self._window) >= self.per_minute:
                    wait = 60 - (now - self._window[0]) + 0.05
                if wait <= 0:
                    self._window.append(now)
                    self._day_count += 1
                    return
            if time.monotonic() + wait > deadline:
                raise RateLimitExceeded(f"would need to wait {wait:.0f}s for a request slot")
            time.sleep(min(wait, 5))

    @property
    def used_today(self) -> int:
        return self._day_count
