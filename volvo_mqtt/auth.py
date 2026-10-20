"""OAuth 2.0 token handling for Volvo ID.

* Tokens are persisted to <DATA_DIR>/tokens.json, seeded from .env on first run.
* Volvo rotates refresh tokens, so every refresh writes the new one back to disk.
* TokenManager.start_refresh_loop() runs a background thread that renews the
  access token TOKEN_REFRESH_MARGIN seconds before it expires, with back-off
  on failure.

Run `python -m volvo_mqtt.auth` for a one-off authorisation-code (PKCE) login
that produces the first refresh token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import Config, load_config

log = logging.getLogger(__name__)


class AuthError(Exception):
    pass


class TokenManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: float = 0.0
        self.last_error: str | None = None
        self.granted_scope: str | None = None
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        f = self.cfg.tokens_file
        if f.exists():
            try:
                data = json.loads(f.read_text())
                self.access_token = data.get("access_token")
                self.refresh_token = data.get("refresh_token")
                self.expires_at = float(data.get("expires_at", 0))
                log.info("Loaded tokens from %s", f)
            except (ValueError, OSError) as exc:
                log.error("Could not read %s: %s", f, exc)
        # .env seeds (or overrides with a newer refresh token if the user pasted one)
        env_rt = self.cfg.refresh_token
        seed_marker = self.cfg.data_dir / ".env_refresh_seed"
        seeded = seed_marker.read_text().strip() if seed_marker.exists() else ""
        if env_rt and env_rt != seeded:
            log.info("Using refresh token from .env")
            self.refresh_token = env_rt
            self.expires_at = 0  # force a refresh
            seed_marker.write_text(env_rt)
        if not self.access_token and self.cfg.access_token:
            self.access_token = self.cfg.access_token
            # Unknown expiry: assume valid now; a 401 will trigger a refresh.
            self.expires_at = self.expires_at or time.time() + 60
        if not self.has_tokens:
            log.warning("No Volvo tokens yet. Sign in to the dashboard and click 'Connect Volvo ID' "
                        "(or set VOLVO_REFRESH_TOKEN in .env / run python -m volvo_mqtt.auth)")

    @property
    def has_tokens(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    def _save(self) -> None:
        f = self.cfg.tokens_file
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "updated": int(time.time()),
        }, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(f)

    # ---------- token operations ----------
    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.cfg.client_id and self.cfg.client_secret)

    def seconds_left(self) -> float:
        return self.expires_at - time.time()

    def store(self, payload: dict) -> None:
        if "access_token" not in payload:
            raise AuthError(f"Token response has no access_token: {str(payload)[:200]}")
        with self._lock:
            self.access_token = payload["access_token"]
            # Volvo rotates refresh tokens; keep the old one only if none is returned.
            self.refresh_token = payload.get("refresh_token", self.refresh_token)
            self.expires_at = time.time() + int(payload.get("expires_in", 1799))
            self._save()

    def refresh(self) -> None:
        with self._lock:
            if not self.can_refresh:
                raise AuthError(
                    "Cannot refresh: VOLVO_CLIENT_ID, VOLVO_CLIENT_SECRET and a refresh token are required"
                )
            log.info("Refreshing access token")
            resp = requests.post(
                self.cfg.token_url,
                data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                auth=(self.cfg.client_id, self.cfg.client_secret),
                headers={"accept": "application/json"},
                timeout=20,
            )
            if resp.status_code != 200:
                raise AuthError(f"Token refresh failed: HTTP {resp.status_code} {resp.text[:300]}")
            self.store(resp.json())
            log.info("Access token refreshed, valid for %.0f min", self.seconds_left() / 60)

    def get_token(self) -> str:
        """Return a valid access token, refreshing if it is (nearly) expired."""
        with self._lock:
            if self.seconds_left() < 15 and self.can_refresh:
                self.refresh()
            if not self.access_token:
                raise AuthError("No access token")
            return self.access_token

    def invalidate(self) -> None:
        """Called after an HTTP 401: force the next get_token() to refresh."""
        with self._lock:
            self.expires_at = 0

    # ---------- background refresh loop ----------
    def start_refresh_loop(self) -> None:
        if not (self.cfg.client_id and self.cfg.client_secret):
            log.warning("Refresh loop disabled: VOLVO_CLIENT_ID / VOLVO_CLIENT_SECRET missing. "
                        "The access token will expire and must be replaced manually.")
            return
        self._thread = threading.Thread(target=self._refresh_loop, name="token-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _refresh_loop(self) -> None:
        failures = 0
        margin = self.cfg.token_refresh_margin
        while not self._stop.is_set():
            if not self.refresh_token:
                # Waiting for the first authorisation (dashboard Connect / .env)
                if self._stop.wait(15):
                    return
                continue
            wait = self.seconds_left() - margin
            if wait > 0:
                log.debug("Next token refresh in %.0fs", wait)
                if self._stop.wait(min(wait, 3600)):
                    return
                continue
            try:
                self.refresh()
                failures = 0
                self.last_error = None
            except (AuthError, requests.RequestException) as exc:
                failures += 1
                self.last_error = str(exc)
                delay = min(30 * 2 ** (failures - 1), 900)
                log.error("Token refresh failed (attempt %d): %s; retrying in %ds", failures, exc, delay)
                if self._stop.wait(delay):
                    return


# ---------- authorisation-code flow with PKCE ----------
def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(cfg: Config, state: str, challenge: str) -> str:
    return cfg.authorize_url + "?" + urlencode({
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def exchange_code(cfg: Config, code: str, verifier: str) -> dict:
    resp = requests.post(
        cfg.token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "code_verifier": verifier,
        },
        auth=(cfg.client_id, cfg.client_secret),
        headers={"accept": "application/json"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json()


def interactive_login() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()
    if not (cfg.client_id and cfg.client_secret):
        sys.exit("VOLVO_CLIENT_ID and VOLVO_CLIENT_SECRET must be set in .env")
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    url = authorize_url(cfg, state, challenge)
    print("\n1. Open this URL in a browser and sign in with your Volvo ID:\n")
    print(url)
    print("\n2. After approving, the browser is redirected to", cfg.redirect_uri)
    print("   (the page may fail to load - that's fine). Copy the FULL address bar URL.\n")
    pasted = input("Paste the redirect URL (or just the code): ").strip()
    if pasted.startswith("http"):
        qs = parse_qs(urlparse(pasted).query)
        if qs.get("state", [state])[0] != state:
            sys.exit("State mismatch - start again.")
        code = qs.get("code", [""])[0]
    else:
        code = pasted
    if not code:
        sys.exit("No authorisation code found.")
    try:
        payload = exchange_code(cfg, code, verifier)
    except AuthError as exc:
        sys.exit(str(exc))
    tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at": time.time() + int(payload.get("expires_in", 1799)),
        "updated": int(time.time()),
    }
    Path(cfg.tokens_file).write_text(json.dumps(tokens, indent=2))
    os.chmod(cfg.tokens_file, 0o600)
    print(f"\nSaved tokens to {cfg.tokens_file}. Granted scopes: {payload.get('scope', '?')}")
    print("Restart the container: docker compose restart volvo-mqtt")


if __name__ == "__main__":
    interactive_login()
