"""Configuration loaded from <CONFIG_DIR>/.env (plus real environment variables)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Hard safety limits so a typo in .env can never hammer the Volvo API.
MIN_POLL_INTERVAL = 300          # 5 minutes
MAX_REQUESTS_PER_MINUTE = 90     # Volvo caps at ~100/min per client
_BASE_SCOPES = "openid conve:odometer_status conve:doors_status conve:lock_status conve:lock conve:unlock location:read"
# Scopes per powertrain: bev = fully electric, phev = plug-in hybrid, ice = petrol/diesel
POWERTRAIN_SCOPES = {
    "bev": "energy:state:read",
    "phev": "energy:state:read conve:fuel_status",
    "ice": "conve:fuel_status",
}
DEFAULT_SCOPES = f"{_BASE_SCOPES} {POWERTRAIN_SCOPES['bev']}"


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    return value or default


def _int(name: str, default: int) -> int:
    raw = _get(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        log.warning("Invalid integer for %s=%r, using %s", name, raw, default)
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    config_dir: Path
    data_dir: Path

    # Volvo API / OAuth
    vcc_api_key: str
    vin: str | None
    client_id: str | None
    client_secret: str | None
    redirect_uri: str
    scopes: str
    access_token: str | None
    refresh_token: str | None
    token_url: str
    authorize_url: str
    api_base: str
    location_base: str
    energy_base: str
    powertrain: str

    # Polling and rate limiting
    poll_interval: int
    location_poll_interval: int
    enable_location: bool
    requests_per_minute: int
    requests_per_day: int
    command_cooldown: int
    command_refresh_delay: int
    manual_refresh_min_interval: int
    token_refresh_margin: int

    # MQTT
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None
    mqtt_client_id: str
    mqtt_prefix: str
    mqtt_retain: bool
    mqtt_tls: bool

    # Web dashboard
    web_enabled: bool = True
    web_port: int = 8084
    web_enable_controls: bool = False
    web_control_pin: str | None = None
    fuel_tank_litres: int | None = None
    web_auth_enabled: bool = True
    web_username: str = "admin"
    web_password_hash: str | None = None
    web_password: str | None = None
    web_session_hours: int = 12
    web_login_max_attempts: int = 5
    web_login_lockout_minutes: int = 15
    web_trust_proxy: bool = False
    web_cookie_secure: str = "auto"
    site_owner: str = "the operator of this service"
    site_contact_email: str | None = None
    legal_updated: str = "20 October 2026"

    log_level: str = "INFO"
    extra: dict = field(default_factory=dict)

    @property
    def tokens_file(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"


def load_config() -> Config:
    config_dir = Path(os.getenv("CONFIG_DIR", "/config"))
    env_file = config_dir / ".env"
    if env_file.exists():
        # Real environment variables win over the file.
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)  # fall back to ./.env for local runs

    data_dir = Path(_get("DATA_DIR", str(config_dir / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)

    poll = max(_int("POLL_INTERVAL", 600), MIN_POLL_INTERVAL)
    loc_poll = max(_int("LOCATION_POLL_INTERVAL", poll), MIN_POLL_INTERVAL)
    rpm = min(max(_int("RATE_LIMIT_PER_MINUTE", 20), 1), MAX_REQUESTS_PER_MINUTE)

    powertrain = (_get("POWERTRAIN", "bev") or "bev").lower()
    if powertrain not in POWERTRAIN_SCOPES:
        log.warning("Unknown POWERTRAIN=%r (use bev, phev or ice); assuming bev", powertrain)
        powertrain = "bev"

    api_key = _get("VCC_API_KEY")
    if not api_key:
        raise SystemExit(f"VCC_API_KEY is not set (expected in {env_file})")

    return Config(
        config_dir=config_dir,
        data_dir=data_dir,
        vcc_api_key=api_key,
        vin=_get("VOLVO_VIN"),
        client_id=_get("VOLVO_CLIENT_ID"),
        client_secret=_get("VOLVO_CLIENT_SECRET"),
        redirect_uri=_get("VOLVO_REDIRECT_URI", "http://volvo.mgbeets.com/callback"),
        scopes=_get("VOLVO_SCOPES", f"{_BASE_SCOPES} {POWERTRAIN_SCOPES[powertrain]}"),
        access_token=_get("VOLVO_ACCESS_TOKEN"),
        refresh_token=_get("VOLVO_REFRESH_TOKEN"),
        token_url=_get("VOLVO_TOKEN_URL", "https://volvoid.eu.volvocars.com/as/token.oauth2"),
        authorize_url=_get(
            "VOLVO_AUTHORIZE_URL", "https://volvoid.eu.volvocars.com/as/authorization.oauth2"
        ),
        api_base=_get("VOLVO_API_BASE", "https://api.volvocars.com/connected-vehicle/v2"),
        location_base=_get("VOLVO_LOCATION_BASE", "https://api.volvocars.com/location/v1"),
        energy_base=_get("VOLVO_ENERGY_BASE", "https://api.volvocars.com/energy/v2"),
        powertrain=powertrain,
        poll_interval=poll,
        location_poll_interval=loc_poll,
        enable_location=_bool("ENABLE_LOCATION", True),
        requests_per_minute=rpm,
        requests_per_day=max(_int("RATE_LIMIT_PER_DAY", 2000), 10),
        command_cooldown=max(_int("COMMAND_COOLDOWN", 30), 10),
        command_refresh_delay=max(_int("COMMAND_REFRESH_DELAY", 20), 5),
        manual_refresh_min_interval=max(_int("MANUAL_REFRESH_MIN_INTERVAL", 120), 60),
        token_refresh_margin=max(_int("TOKEN_REFRESH_MARGIN", 300), 30),
        mqtt_host=_get("MQTT_HOST", "192.168.1.55"),
        mqtt_port=_int("MQTT_PORT", 1883),
        mqtt_username=_get("MQTT_USERNAME"),
        mqtt_password=_get("MQTT_PASSWORD"),
        mqtt_client_id=_get("MQTT_CLIENT_ID", "volvo-mqtt"),
        mqtt_prefix=(_get("MQTT_TOPIC_PREFIX", "volvo/vehicle") or "volvo/vehicle").rstrip("/"),
        mqtt_retain=_bool("MQTT_RETAIN", True),
        mqtt_tls=_bool("MQTT_TLS", False),
        web_enabled=_bool("WEB_ENABLED", True),
        web_port=_int("WEB_PORT", 8084),
        web_enable_controls=_bool("WEB_ENABLE_CONTROLS", False),
        web_control_pin=_get("WEB_CONTROL_PIN"),
        fuel_tank_litres=_int("FUEL_TANK_LITRES", 0) or None,
        web_auth_enabled=_bool("WEB_AUTH_ENABLED", True),
        web_username=_get("WEB_USERNAME", "admin") or "admin",
        web_password_hash=_get("WEB_PASSWORD_HASH"),
        web_password=_get("WEB_PASSWORD"),
        web_session_hours=_int("WEB_SESSION_HOURS", 12),
        web_login_max_attempts=_int("WEB_LOGIN_MAX_ATTEMPTS", 5),
        web_login_lockout_minutes=_int("WEB_LOGIN_LOCKOUT_MINUTES", 15),
        web_trust_proxy=_bool("WEB_TRUST_PROXY", False),
        web_cookie_secure=(_get("WEB_COOKIE_SECURE", "auto") or "auto").lower(),
        site_owner=_get("SITE_OWNER", "the operator of this service") or "the operator of this service",
        site_contact_email=_get("SITE_CONTACT_EMAIL"),
        legal_updated=_get("LEGAL_UPDATED", "20 October 2026") or "20 October 2026",
        log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
    )
    return cfg
