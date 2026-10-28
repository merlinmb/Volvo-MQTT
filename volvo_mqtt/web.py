"""Built-in dashboard (stdlib only) with login, sessions and CSRF protection.

Public:   GET /            holding page (or the dashboard when signed in)
          GET /terms, /privacy   Terms and Conditions, Privacy Notice
          GET /callback    Volvo ID OAuth redirect (VOLVO_REDIRECT_URI); needs a matching state
Signed in: GET /oauth/start  begins "Connect Volvo ID"
          GET /login       login form;  POST /login
          GET /healthz     {"ok": bool} for the Docker health check
Signed in: GET /api/status, /api/raw, /api/logs;  POST /api/refresh|lock|unlock, POST /logout
"""
from __future__ import annotations

import html
import json
import logging
import secrets
import platform
import socket
import threading
import time
from dataclasses import asdict
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from . import __version__
from .auth import AuthError, _pkce_pair, authorize_url, exchange_code
from .webauth import SESSION_COOKIE, Auth

if TYPE_CHECKING:
    from .bridge import Bridge

log = logging.getLogger(__name__)

SECRET_KEYS = {"vcc_api_key", "client_secret", "access_token", "refresh_token",
               "mqtt_password", "web_control_pin", "web_password_hash", "web_password"}


def _mask(value) -> str | None:
    if not value:
        return None
    return "•••••• (set)"


def redacted_config(bridge: "Bridge") -> dict:
    out = {}
    for k, v in asdict(bridge.cfg).items():
        if k == "extra":
            continue
        out[k] = _mask(v) if k in SECRET_KEYS else (str(v) if hasattr(v, "__fspath__") else v)
    if bridge.api.vin:
        out["vin"] = bridge.api.vin
    return out


def build_status(bridge: "Bridge") -> dict:
    snap = bridge.status.snapshot()
    t = bridge.tokens
    snap.update({
        "version": __version__,
        "now": time.time(),
        "vin": bridge.api.vin or bridge.cfg.vin,
        "topic_prefix": bridge.p,
        "container": {
            "hostname": socket.gethostname(),
            "python": platform.python_version(),
            "platform": platform.platform(terse=True),
        },
        "token": {
            "expires_at": t.expires_at,
            "expires_in": int(t.seconds_left()),
            "can_refresh": t.can_refresh,
            "refresh_loop_alive": bool(t._thread and t._thread.is_alive()),
            "last_error": t.last_error,
        },
        "rate": {
            "used_today": bridge.limiter.used_today,
            "per_day": bridge.cfg.requests_per_day,
            "per_minute": bridge.cfg.requests_per_minute,
        },
        "volvo": {
            "connected": t.has_tokens,
            "oauth_configured": bool(bridge.cfg.client_id and bridge.cfg.client_secret),
            "redirect_uri": bridge.cfg.redirect_uri,
            "scope": t.granted_scope,
        },
        "controls_enabled": bridge.cfg.web_enable_controls,
        "controls_need_pin": bool(bridge.cfg.web_control_pin),
        "config": redacted_config(bridge),
    })
    return snap


def _static(name: str) -> str:
    return resources.files("volvo_mqtt").joinpath(f"static/{name}").read_text(encoding="utf-8")


CSP = ("default-src 'self'; "
       "script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net https://maps.googleapis.com; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; "
       "img-src 'self' data: https://maps.gstatic.com https://maps.googleapis.com; "
       "connect-src 'self' https://maps.googleapis.com https://maps.gstatic.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def make_handler(bridge: "Bridge"):
    cfg = bridge.cfg
    pages = {name: _static(name) for name in ("index.html", "public.html", "login.html", "legal.html", "notice.html")}
    callback_path = urlparse(cfg.redirect_uri).path or "/callback"
    pending_oauth: dict[str, dict] = {}   # state -> {verifier, sid, t}
    oauth_lock = threading.Lock()
    if cfg.site_contact_email:
        e = html.escape(cfg.site_contact_email)
        contact = f'email <a href="mailto:{e}">{e}</a>'
    else:
        contact = "contact the operator using the details provided with your account"
    legal_subs = {"owner": html.escape(cfg.site_owner), "contact": contact}
    legal = {}
    for slug, title in (("terms", "Terms and Conditions"), ("privacy", "Privacy Notice")):
        body = _static(f"{slug}_body.html")
        for k, v in legal_subs.items():
            body = body.replace("{{" + k + "}}", v)
        legal[slug] = {"title": title, "body": body, "updated": html.escape(cfg.legal_updated),
                       "terms_on": "on" if slug == "terms" else "", "privacy_on": "on" if slug == "privacy" else ""}
    auth = Auth(cfg.web_username, cfg.web_password_hash, cfg.web_password, cfg.web_session_hours,
                cfg.web_login_max_attempts, cfg.web_login_lockout_minutes)
    auth_on = cfg.web_auth_enabled
    if not auth_on:
        log.warning("Dashboard authentication is DISABLED (WEB_AUTH_ENABLED=false) - LAN use only!")

    class Handler(BaseHTTPRequestHandler):
        server_version = "volvo-mqtt"
        sys_version = ""

        def log_message(self, fmt, *args):  # quieter logs
            log.debug("web %s - %s", self.client_ip(), fmt % args)

        # ---------- helpers ----------
        def client_ip(self) -> str:
            if cfg.web_trust_proxy:
                fwd = self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP")
                if fwd:
                    return fwd.split(",")[0].strip()
            return self.client_address[0]

        def is_https(self) -> bool:
            if cfg.web_cookie_secure in ("true", "1", "yes"):
                return True
            if cfg.web_cookie_secure in ("false", "0", "no"):
                return False
            return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

        def sid(self) -> str | None:
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            c = SimpleCookie()
            try:
                c.load(raw)
            except Exception:  # noqa: BLE001
                return None
            m = c.get(SESSION_COOKIE)
            return m.value if m else None

        def session(self) -> dict | None:
            if not auth_on:
                return {"user": "anonymous", "csrf": "disabled"}
            return auth.session(self.sid())

        def same_origin(self) -> bool:
            origin = self.headers.get("Origin") or self.headers.get("Referer")
            if not origin:
                return True  # non-browser clients
            host = self.headers.get("X-Forwarded-Host") if cfg.web_trust_proxy else None
            host = host or self.headers.get("Host", "")
            return urlparse(origin).netloc == host

        def _headers(self, code: int, ctype: str, length: int, nonce: str | None = None, extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if nonce:
                self.send_header("Content-Security-Policy", CSP.format(nonce=nonce))
            if self.is_https():
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj, default=str).encode()
            self._headers(code, "application/json", len(body))
            self.wfile.write(body)

        def _page(self, name: str, subs: dict | None = None, code: int = 200, extra=()) -> None:
            nonce = secrets.token_urlsafe(16)
            text = pages[name].replace("<script", f'<script nonce="{nonce}"')
            for k, v in (subs or {}).items():
                text = text.replace("{{" + k + "}}", v)
            body = text.encode()
            self._headers(code, "text/html; charset=utf-8", len(body), nonce, extra)
            self.wfile.write(body)

        def _redirect(self, location: str, extra=()) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()

        def _cookie(self, value: str, max_age: int) -> tuple[str, str]:
            parts = [f"{SESSION_COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
            if self.is_https():
                parts.append("Secure")
            return ("Set-Cookie", "; ".join(parts))

        def _form(self) -> dict:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            data = self.rfile.read(length).decode(errors="ignore") if length else ""
            return {k: v[0] for k, v in parse_qs(data).items()}

        # ---------- GET ----------
        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            if path == "/healthz":
                ok = bridge.status.mqtt_connected and bridge.tokens.seconds_left() > 0
                return self._json(200 if ok else 503, {"ok": ok})
            if path == callback_path:
                return self.oauth_callback(parse_qs(url.query))
            slug = path.strip("/").removesuffix(".html")
            if slug in legal:
                return self._page("legal.html", legal[slug])
            sess = self.session()
            if path in ("/", "/index.html"):
                if sess:
                    return self._page("index.html", {
                        "csrf": html.escape(sess["csrf"]),
                        "user": html.escape(sess["user"]),
                        "auth": "on" if auth_on else "off",
                        "google_maps_api_key": html.escape(cfg.extra.get("google_maps_api_key", "")),
                    })
                return self._page("public.html", {"version": __version__})
            if path == "/oauth/start":
                return self.oauth_start(sess)
            if path == "/login":
                if sess:
                    return self._redirect("/")
                q = parse_qs(url.query)
                msg, cls = "", ""
                locked = auth.locked_for(self.client_ip())
                if locked:
                    msg, cls = f"Too many attempts. Try again in {max(1, locked // 60)} min.", "error"
                elif "e" in q:
                    msg, cls = "Incorrect username or password.", "error"
                elif "out" in q:
                    msg, cls = "You have been signed out.", "info"
                elif not auth.configured:
                    msg, cls = "Sign-in is not configured yet. Set WEB_PASSWORD_HASH in .env.", "error"
                return self._page("login.html", {"message": html.escape(msg), "message_class": cls})
            if path.startswith("/api/"):
                if not sess:
                    return self._json(401, {"error": "authentication required"})
                if path == "/api/status":
                    return self._json(200, build_status(bridge))
                if path == "/api/raw":
                    return self._json(200, bridge.status.raw)
                if path == "/api/logs":
                    log_cap = logging.getLogger("volvo_mqtt").handlers
                    from .bridge import LogCapture
                    logs = []
                    for handler in log_cap:
                        if isinstance(handler, LogCapture):
                            logs = handler.get_logs()
                            break
                    return self._json(200, {"logs": logs})
            self._json(404, {"error": "not found"})

        # ---------- Volvo ID OAuth (authorisation code + PKCE) ----------
        def notice(self, ok: bool, title: str, message: str, code: int = 200) -> None:
            self._page("notice.html", {
                "kind": "ok" if ok else "error", "title": html.escape(title),
                "message": html.escape(message), "link": "/", "link_text": "Back to dashboard",
            }, code=code)

        def oauth_start(self, sess: dict | None) -> None:
            if not sess:
                return self._redirect("/login")
            if not (cfg.client_id and cfg.client_secret):
                return self.notice(False, "Not configured",
                                   "Set VOLVO_CLIENT_ID and VOLVO_CLIENT_SECRET in .env first.", 400)
            verifier, challenge = _pkce_pair()
            state = secrets.token_urlsafe(24)
            now = time.time()
            with oauth_lock:
                for k in [k for k, v in pending_oauth.items() if now - v["t"] > 600]:
                    pending_oauth.pop(k, None)
                pending_oauth[state] = {"verifier": verifier, "sid": self.sid(), "t": now}
            log.info("Starting Volvo ID authorisation (redirect %s)", cfg.redirect_uri)
            self._redirect(authorize_url(cfg, state, challenge))

        def oauth_callback(self, q: dict) -> None:
            state = (q.get("state") or [""])[0]
            with oauth_lock:
                pending = pending_oauth.pop(state, None) if state else None
            if "error" in q:
                desc = (q.get("error_description") or q.get("error"))[0]
                return self.notice(False, "Volvo ID declined", f"Volvo returned: {desc}", 400)
            if not pending or time.time() - pending["t"] > 600:
                return self.notice(False, "Link expired",
                                   "This sign-in link is invalid or has expired. Start again from the dashboard.", 400)
            if auth_on and not secrets.compare_digest(pending["sid"] or "", self.sid() or ""):
                return self.notice(False, "Session mismatch",
                                   "Finish the Volvo sign-in in the same browser you started it from.", 403)
            code = (q.get("code") or [""])[0]
            if not code:
                return self.notice(False, "No code", "Volvo did not return an authorisation code.", 400)
            try:
                payload = exchange_code(cfg, code, pending["verifier"])
                bridge.tokens.store(payload)
            except (AuthError, Exception) as exc:  # noqa: BLE001
                log.error("Volvo authorisation failed: %s", exc)
                bridge.status.error(f"Volvo authorisation failed: {exc}")
                return self.notice(False, "Connection failed", str(exc)[:300], 502)
            bridge.tokens.granted_scope = payload.get("scope")
            bridge.status.event("info", "Volvo ID connected")
            bridge.commands.put(("authorised", "web"))
            log.info("Volvo ID connected; scopes: %s", payload.get("scope"))
            self.notice(True, "Volvo ID connected",
                        "Tokens are saved and will refresh automatically. The first poll is running now.")

        # ---------- POST ----------
        def do_POST(self):
            path = urlparse(self.path).path
            if not self.same_origin():
                return self._json(403, {"error": "cross-origin request blocked"})

            if path == "/login":
                form = self._form()
                ip = self.client_ip()
                sid = auth.login(form.get("username", ""), form.get("password", ""), ip)
                if not sid:
                    return self._redirect("/login?e=1")
                return self._redirect("/", extra=[self._cookie(sid, auth.session_ttl)])

            if path == "/logout":
                form = self._form()
                sid = self.sid()
                if auth_on and not auth.check_csrf(sid, form.get("csrf") or self.headers.get("X-CSRF-Token")):
                    return self._redirect("/")
                auth.logout(sid)
                return self._redirect("/login?out=1", extra=[self._cookie("", 0)])

            sess = self.session()
            if not sess:
                return self._json(401, {"error": "authentication required"})
            if auth_on and not auth.check_csrf(self.sid(), self.headers.get("X-CSRF-Token")):
                return self._json(403, {"error": "invalid CSRF token, reload the page"})

            action = path.rsplit("/", 1)[-1]
            if path == "/api/refresh":
                bridge.commands.put(("refresh", "web"))
                return self._json(202, {"queued": "refresh"})
            if path in ("/api/lock", "/api/unlock"):
                if not cfg.web_enable_controls:
                    return self._json(403, {"error": "Web controls are disabled (WEB_ENABLE_CONTROLS=false)"})
                pin = cfg.web_control_pin
                if pin and not secrets.compare_digest(self.headers.get("X-Control-Pin", ""), pin):
                    return self._json(401, {"error": "Invalid PIN"})
                log.info("Web %s requested by %s from %s", action, sess["user"], self.client_ip())
                bridge.commands.put((action, "web"))
                return self._json(202, {"queued": action})
            self._json(404, {"error": "unknown action"})

    return Handler


def start_web(bridge: "Bridge") -> None:
    server = ThreadingHTTPServer(("0.0.0.0", bridge.cfg.web_port), make_handler(bridge))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("Dashboard on http://0.0.0.0:%s", bridge.cfg.web_port)
