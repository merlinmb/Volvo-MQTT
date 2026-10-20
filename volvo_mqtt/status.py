"""Shared, thread-safe runtime status used by the bridge and the web dashboard."""
from __future__ import annotations

import collections
import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Status:
    def __init__(self, state_file: Path) -> None:
        self._lock = threading.Lock()
        self.state_file = state_file
        self.started = time.time()
        self.vehicle: dict = {}
        self.raw: dict = {}
        self.mqtt_connected = False
        self.last_poll: float | None = None
        self.last_poll_ok: bool | None = None
        self.next_poll: float | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.last_command: dict | None = None
        self.http_codes: collections.Counter = collections.Counter()
        self.requests_total = 0
        self.events: collections.deque = collections.deque(maxlen=50)
        self._load()

    def _load(self) -> None:
        if self.state_file.exists():
            try:
                saved = json.loads(self.state_file.read_text())
                self.vehicle = saved.get("vehicle", {})
                self.raw = saved.get("raw", {})
                self.last_command = saved.get("last_command")
            except (ValueError, OSError):
                pass

    def save(self) -> None:
        with self._lock:
            data = {"vehicle": self.vehicle, "raw": self.raw, "last_command": self.last_command}
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.state_file)
        except OSError as exc:
            log.warning("Could not save state: %s", exc)

    def event(self, level: str, msg: str) -> None:
        with self._lock:
            self.events.appendleft({"t": time.time(), "level": level, "msg": msg})

    def error(self, msg: str) -> None:
        with self._lock:
            self.last_error, self.last_error_at = msg, time.time()
        self.event("error", msg)

    def count_request(self, code: int) -> None:
        with self._lock:
            self.requests_total += 1
            self.http_codes[str(code)] += 1

    def update_vehicle(self, key: str, raw: dict, parsed: dict) -> None:
        with self._lock:
            self.raw[key] = raw
            self.vehicle.update(parsed)
            self.vehicle["updated"] = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "vehicle": dict(self.vehicle),
                "mqtt_connected": self.mqtt_connected,
                "started": self.started,
                "uptime": time.time() - self.started,
                "last_poll": self.last_poll,
                "last_poll_ok": self.last_poll_ok,
                "next_poll": self.next_poll,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "last_command": self.last_command,
                "requests_total": self.requests_total,
                "http_codes": dict(self.http_codes),
                "events": list(self.events),
            }
