"""Volvo Cars Connected Vehicle v2 + Location v1 client with rate limiting."""
from __future__ import annotations

import logging
from typing import Any

import requests

from .auth import TokenManager
from .config import Config
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


class VolvoClient:
    def __init__(self, cfg: Config, tokens: TokenManager, limiter: RateLimiter, status=None) -> None:
        self.cfg = cfg
        self.tokens = tokens
        self.limiter = limiter
        self.status = status
        self.session = requests.Session()
        self.vin = cfg.vin

    def _request(self, method: str, url: str, json_body: Any = None, _retry: bool = True) -> dict:
        self.limiter.acquire()
        headers = {
            "vcc-api-key": self.cfg.vcc_api_key,
            "Authorization": f"Bearer {self.tokens.get_token()}",
            "accept": "application/json",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        try:
            resp = self.session.request(method, url, headers=headers, json=json_body, timeout=30)
        except requests.RequestException as exc:
            raise ApiError(f"{method} {url}: {exc}") from exc
        if self.status:
            self.status.count_request(resp.status_code)

        if resp.status_code == 401 and _retry and self.tokens.can_refresh:
            log.info("401 from API, refreshing token and retrying once")
            self.tokens.invalidate()
            return self._request(method, url, json_body, _retry=False)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "60")
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 60.0
            self.limiter.backoff(max(delay, 30))
            raise ApiError("Rate limited by Volvo (HTTP 429)", 429)
        if resp.status_code >= 400:
            raise ApiError(f"{method} {url}: HTTP {resp.status_code} {resp.text[:300]}", resp.status_code)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(f"Invalid JSON from {url}") from exc

    # ---------- discovery ----------
    def ensure_vin(self) -> str:
        if not self.vin:
            data = self._request("GET", f"{self.cfg.api_base}/vehicles").get("data", [])
            if not data:
                raise ApiError("No vehicles on this Volvo ID; set VOLVO_VIN")
            self.vin = data[0]["vin"]
            log.info("Discovered VIN %s", self.vin)
        return self.vin

    def _v(self, path: str) -> str:
        return f"{self.cfg.api_base}/vehicles/{self.ensure_vin()}/{path}"

    # ---------- reads ----------
    def fuel(self) -> dict:
        return self._request("GET", self._v("fuel"))

    def energy(self) -> dict:
        """Battery and charging state (Energy API v2). Scope: energy:state:read"""
        return self._request("GET", f"{self.cfg.energy_base}/vehicles/{self.ensure_vin()}/state")

    def odometer(self) -> dict:
        return self._request("GET", self._v("odometer"))

    def doors(self) -> dict:
        return self._request("GET", self._v("doors"))

    def location(self) -> dict:
        return self._request("GET", f"{self.cfg.location_base}/vehicles/{self.ensure_vin()}/location")

    def vehicle_details(self) -> dict:
        return self._request("GET", f"{self.cfg.api_base}/vehicles/{self.ensure_vin()}")

    # ---------- commands ----------
    def lock(self) -> dict:
        return self._request("POST", self._v("commands/lock"))

    def unlock(self) -> dict:
        return self._request("POST", self._v("commands/unlock"))


# ---------- payload parsing ----------
def _val(data: dict, key: str) -> dict:
    item = (data or {}).get("data", {}).get(key) or {}
    return item if isinstance(item, dict) else {}


def parse_fuel(raw: dict) -> dict:
    f = _val(raw, "fuelAmount")
    return {"fuel_level": _num(f.get("value")), "fuel_unit": f.get("unit"), "fuel_timestamp": f.get("timestamp")}


def _field(raw: dict, key: str) -> dict:
    """Energy v2 fields look like {"status": "OK", "value": 87, "unit": "percentage", "updatedAt": "..."}.
    Fields the car can't report have status ERROR and no value."""
    body = (raw or {}).get("data", raw) or {}
    item = body.get(key) or {}
    if not isinstance(item, dict) or str(item.get("status", "OK")).upper() == "ERROR":
        return {}
    return item


def _upper(v):
    return v.upper() if isinstance(v, str) else v


def parse_energy(raw: dict) -> dict:
    level = _field(raw, "batteryChargeLevel")
    rng = _field(raw, "electricRange")
    status = _upper(_field(raw, "chargingStatus").get("value"))
    conn = _upper(_field(raw, "chargerConnectionStatus").get("value"))
    power = _field(raw, "chargingPower")
    eta = _field(raw, "estimatedChargingTimeToTargetBatteryChargeLevel")
    target = _field(raw, "targetBatteryChargeLevel")
    limit = _field(raw, "chargingCurrentLimit")
    out = {
        "battery_level": _num(level.get("value")),
        "electric_range": _num(rng.get("value")),
        "range_unit": rng.get("unit") or ("km" if rng else None),
        "charging_status": status,                     # CHARGING / IDLE / DONE / SCHEDULED / DISCHARGING / ERROR
        "charger_connection": conn,                    # CONNECTED / DISCONNECTED / FAULT
        "charging_type": _upper(_field(raw, "chargingType").get("value")),   # AC / DC / NONE
        "charger_power_status": _upper(_field(raw, "chargerPowerStatus").get("value")),
        "charging_power": _num(power.get("value")),
        "charging_power_unit": power.get("unit"),
        "time_to_target_min": _num(eta.get("value")),
        "target_battery_level": _num(target.get("value")),
        "charging_current_limit": _num(limit.get("value")),
        "energy_timestamp": level.get("updatedAt") or level.get("timestamp"),
        "charging": status == "CHARGING",
        "plugged_in": conn == "CONNECTED",
    }
    return out


def parse_odometer(raw: dict) -> dict:
    o = _val(raw, "odometer")
    return {"odometer": _num(o.get("value")), "odometer_unit": o.get("unit"), "odometer_timestamp": o.get("timestamp")}


def parse_doors(raw: dict) -> dict:
    data = (raw or {}).get("data", {}) or {}
    lock = (data.get("centralLock") or {}).get("value")
    doors = {k: v.get("value") for k, v in data.items() if k != "centralLock" and isinstance(v, dict)}
    return {
        "lock_status": lock,  # LOCKED / UNLOCKED / UNSPECIFIED
        "lock_timestamp": (data.get("centralLock") or {}).get("timestamp"),
        "doors": doors,
        "any_open": any(v == "OPEN" for v in doors.values()),
    }


def parse_location(raw: dict) -> dict:
    data = (raw or {}).get("data", {}) or {}
    coords = (data.get("geometry") or {}).get("coordinates") or []
    props = data.get("properties") or {}
    return {
        "longitude": coords[0] if len(coords) > 0 else None,
        "latitude": coords[1] if len(coords) > 1 else None,
        "altitude": coords[2] if len(coords) > 2 else None,
        "heading": _num(props.get("heading")),
        "location_timestamp": props.get("timestamp"),
    }


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v
