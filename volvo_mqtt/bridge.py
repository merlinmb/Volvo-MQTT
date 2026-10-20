"""Main service: polls Volvo, publishes to MQTT, executes lock/unlock commands."""
from __future__ import annotations

import json
import logging
import queue
import signal
import time

import paho.mqtt.client as mqtt

from . import __version__
from .api import ApiError, VolvoClient, parse_doors, parse_energy, parse_fuel, parse_location, parse_odometer
from .auth import AuthError, TokenManager
from .config import Config, load_config
from .ratelimit import RateLimiter, RateLimitExceeded
from .status import Status

log = logging.getLogger("volvo_mqtt")

LOCK_WORDS = {"LOCK", "LOCKED", "S", "SECURED", "1", "ON", "TRUE"}
UNLOCK_WORDS = {"UNLOCK", "UNLOCKED", "U", "UNSECURED", "0", "OFF", "FALSE"}
OK_STATUSES = {"COMPLETED", "SUCCESS", "DELIVERED"}
PENDING_STATUSES = {"RUNNING", "WAITING", "QUEUED", "SENT"}


class Bridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.p = cfg.mqtt_prefix
        self.status = Status(cfg.state_file)
        self.tokens = TokenManager(cfg)
        self.limiter = RateLimiter(cfg.requests_per_minute, cfg.requests_per_day)
        self.api = VolvoClient(cfg, self.tokens, self.limiter, self.status)
        self.commands: queue.Queue = queue.Queue()
        self.running = True
        self.last_command_at = 0.0
        self.last_manual_refresh = 0.0
        self.next_main_poll = 0.0
        self.next_location_poll = 0.0
        self.failures = 0
        self.mqtt = self._make_mqtt()

    # ---------- MQTT ----------
    def _make_mqtt(self) -> mqtt.Client:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cfg.mqtt_client_id)
        if self.cfg.mqtt_username:
            c.username_pw_set(self.cfg.mqtt_username, self.cfg.mqtt_password)
        if self.cfg.mqtt_tls:
            c.tls_set()
        c.will_set(f"{self.p}/availability", "offline", qos=1, retain=True)
        c.reconnect_delay_set(min_delay=2, max_delay=60)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        return c

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("Connected to MQTT %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
        self.status.mqtt_connected = True
        self.status.event("info", "MQTT connected")
        client.publish(f"{self.p}/availability", "online", qos=1, retain=True)
        client.subscribe([(f"{self.p}/lock/set", 1), (f"{self.p}/refresh", 0)])
        # Re-publish last known state so subscribers (Homebridge) are in sync.
        if self.status.vehicle:
            self.publish_state()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.status.mqtt_connected = False
        if reason_code != 0:
            log.warning("MQTT disconnected (%s), will reconnect", reason_code)
            self.status.event("warn", f"MQTT disconnected: {reason_code}")

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="ignore").strip()
        if msg.retain:
            return  # never act on a stale retained command
        if msg.topic.endswith("/lock/set"):
            word = payload.upper()
            if word in LOCK_WORDS:
                self.commands.put(("lock", "mqtt"))
            elif word in UNLOCK_WORDS:
                self.commands.put(("unlock", "mqtt"))
            else:
                log.warning("Ignoring unknown lock payload %r", payload)
        elif msg.topic.endswith("/refresh"):
            self.commands.put(("refresh", "mqtt"))

    def pub(self, sub: str, value, retain: bool | None = None) -> None:
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        elif value is None:
            value = ""
        self.mqtt.publish(f"{self.p}/{sub}", str(value), qos=1,
                          retain=self.cfg.mqtt_retain if retain is None else retain)

    def publish_state(self) -> None:
        v = self.status.vehicle
        self.pub("state", v)
        for key in ("battery_level", "electric_range", "range_unit", "charging_status", "charger_connection",
                    "charging_type", "charging_power", "time_to_target_min", "target_battery_level",
                    "charging", "plugged_in",
                    "fuel_level", "fuel_unit", "odometer", "odometer_unit", "lock_status",
                    "latitude", "longitude", "heading", "any_open"):
            if key in v:
                val = v[key]
                self.pub(f"sensor/{key}", str(val).lower() if isinstance(val, bool) else val)
        lock = v.get("lock_status")
        if lock:
            self.pub("lock/state", lock)
            hk = {"LOCKED": "S", "UNLOCKED": "U"}.get(lock, "?")
            self.pub("lock/current", hk)
            if hk in ("S", "U"):
                self.pub("lock/target", hk)
        if v.get("charging_status") is not None or v.get("charger_connection") is not None:
            # HomeKit battery service values (homebridge-mqttthing chargingStateValues)
            hk_charge = "CHARGING" if v.get("charging") else "NOT_CHARGING"
            self.pub("sensor/homekit_charging_state", hk_charge)

    def publish_bridge_status(self) -> None:
        self.pub("bridge/status", {
            "version": __version__,
            "token_expires_in": int(self.tokens.seconds_left()),
            "requests_today": self.limiter.used_today,
            "daily_budget": self.cfg.requests_per_day,
            "last_poll": self.status.last_poll,
            "last_error": self.status.last_error,
        })

    # ---------- polling ----------
    def poll(self, include_location: bool, only: tuple[str, ...] | None = None) -> bool:
        jobs = []
        if self.cfg.powertrain in ("bev", "phev"):
            jobs.append(("energy", self.api.energy, parse_energy))
        if self.cfg.powertrain in ("phev", "ice"):
            jobs.append(("fuel", self.api.fuel, parse_fuel))
        jobs += [("odometer", self.api.odometer, parse_odometer),
                ("doors", self.api.doors, parse_doors)]
        if include_location and self.cfg.enable_location:
            jobs.append(("location", self.api.location, parse_location))
        if only:
            jobs = [j for j in jobs if j[0] in only]
        ok = True
        for key, fn, parser in jobs:
            try:
                raw = fn()
                self.status.update_vehicle(key, raw, parser(raw))
                self.pub(key, raw)  # raw API JSON, as in the original script
                log.debug("Published %s/%s", self.p, key)
            except RateLimitExceeded as exc:
                log.warning("Skipping %s: %s", key, exc)
                self.status.error(f"{key}: {exc}")
                ok = False
                break
            except (ApiError, AuthError) as exc:
                log.error("Fetching %s failed: %s", key, exc)
                self.status.error(f"{key}: {exc}")
                ok = False
                if isinstance(exc, ApiError) and exc.status == 429:
                    break
        self.status.last_poll = time.time()
        self.status.last_poll_ok = ok
        self.publish_state()
        self.publish_bridge_status()
        self.status.save()
        return ok

    # ---------- commands ----------
    def run_command(self, action: str, source: str) -> dict:
        now = time.time()
        if action == "refresh":
            if now - self.last_manual_refresh < self.cfg.manual_refresh_min_interval:
                wait = int(self.cfg.manual_refresh_min_interval - (now - self.last_manual_refresh))
                result = {"action": "refresh", "ok": False, "message": f"Refresh throttled, try again in {wait}s"}
            else:
                self.last_manual_refresh = now
                ok = self.poll(include_location=True)
                result = {"action": "refresh", "ok": ok, "message": "Refreshed" if ok else "Refresh had errors"}
            self.status.event("info" if result["ok"] else "warn", result["message"])
            return result

        if now - self.last_command_at < self.cfg.command_cooldown:
            wait = int(self.cfg.command_cooldown - (now - self.last_command_at))
            result = {"action": action, "ok": False, "source": source, "t": now,
                      "message": f"Command cooldown: wait {wait}s"}
            self._finish_command(result)
            return result

        self.last_command_at = now
        self.pub("lock/target", "S" if action == "lock" else "U")
        log.info("Sending %s command (from %s)", action, source)
        try:
            resp = self.api.lock() if action == "lock" else self.api.unlock()
            data = resp.get("data", resp) or {}
            invoke = str(data.get("invokeStatus", "COMPLETED")).upper()
            ok = invoke in OK_STATUSES or invoke in PENDING_STATUSES
            result = {"action": action, "ok": ok, "invokeStatus": invoke, "source": source, "t": now,
                      "message": data.get("message") or invoke}
            if invoke in OK_STATUSES:
                # Optimistic update; the follow-up doors poll confirms it.
                self.status.vehicle["lock_status"] = "LOCKED" if action == "lock" else "UNLOCKED"
                self.publish_state()
        except (ApiError, AuthError, RateLimitExceeded) as exc:
            result = {"action": action, "ok": False, "source": source, "t": now, "message": str(exc)}
        self._finish_command(result)
        # Confirm actual state shortly afterwards (single doors request).
        self.commands.put(("confirm", source))
        return result

    def _finish_command(self, result: dict) -> None:
        self.status.last_command = result
        self.pub("command/result", result, retain=False)
        level = "info" if result["ok"] else "error"
        self.status.event(level, f"{result['action']} ({result.get('source')}): {result['message']}")
        if not result["ok"]:
            # Snap HomeKit's target back to the real state.
            self.publish_state()
        self.status.save()

    # ---------- main loop ----------
    def run(self) -> None:
        log.info("volvo-mqtt %s starting; poll %ss, location %ss, %s req/min, %s req/day",
                 __version__, self.cfg.poll_interval, self.cfg.location_poll_interval,
                 self.cfg.requests_per_minute, self.cfg.requests_per_day)
        self.tokens.start_refresh_loop()
        self.mqtt.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
        self.mqtt.loop_start()

        if self.cfg.web_enabled:
            from .web import start_web
            start_web(self)

        confirm_at = 0.0
        while self.running:
            now = time.time()
            if not self.tokens.has_tokens:
                # Not connected to Volvo yet: wait for the dashboard OAuth flow
                self.status.next_poll = None
                try:
                    action, source = self.commands.get(timeout=30)
                except queue.Empty:
                    continue
                if action == "authorised":
                    self.next_main_poll = 0
                continue
            if now >= self.next_main_poll:
                include_loc = now >= self.next_location_poll
                ok = self.poll(include_location=include_loc)
                if include_loc:
                    self.next_location_poll = now + self.cfg.location_poll_interval
                if ok:
                    self.failures = 0
                    self.next_main_poll = now + self.cfg.poll_interval
                else:
                    # Exponential back-off on failure, never faster than 1 minute, max 1 hour
                    self.failures += 1
                    backoff = min(60 * 2 ** (self.failures - 1), 3600)
                    self.next_main_poll = now + min(backoff, self.cfg.poll_interval * 4)
                    log.info("Poll failed, next attempt in %ss", int(self.next_main_poll - now))
            if confirm_at and now >= confirm_at:
                confirm_at = 0.0
                self.poll(include_location=False, only=("doors",))

            self.status.next_poll = self.next_main_poll
            wake = min(t for t in (self.next_main_poll, confirm_at or float("inf")))
            try:
                action, source = self.commands.get(timeout=max(1.0, min(wake - time.time(), 30)))
            except queue.Empty:
                continue
            if action == "confirm":
                confirm_at = time.time() + self.cfg.command_refresh_delay
            elif action == "authorised":
                self.failures = 0
                self.next_main_poll = 0
                self.next_location_poll = 0
            elif action in ("lock", "unlock", "refresh"):
                self.run_command(action, source)

        self.shutdown()

    def shutdown(self) -> None:
        log.info("Shutting down")
        self.tokens.stop()
        self.status.save()
        try:
            self.mqtt.publish(f"{self.p}/availability", "offline", qos=1, retain=True).wait_for_publish(3)
        except Exception:  # noqa: BLE001
            pass
        self.mqtt.loop_stop()
        self.mqtt.disconnect()


def main() -> None:
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        bridge = Bridge(cfg)
    except AuthError as exc:
        log.error("%s", exc)
        raise SystemExit(2)

    def stop(*_):
        bridge.running = False
        bridge.commands.put(("noop", "signal"))

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    bridge.run()


if __name__ == "__main__":
    main()
