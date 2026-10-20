# volvo-mqtt

Bridges the Volvo Cars Connected Vehicle API to MQTT (and Homebridge), with
remote lock/unlock, automatic OAuth 2.0 token refresh, client-side rate
limiting and a built-in web dashboard.

## Highlights

- Powertrain-aware telemetry for BEV, PHEV and ICE vehicles, including energy,
  fuel, odometer, doors and optional location data.
- Volvo ID authorization with OAuth 2.0 and PKCE, rotating refresh-token
  persistence, and automatic access-token renewal.
- MQTT telemetry and commands, with retained state, online/offline status,
  polling controls and lock/unlock confirmation.
- Homebridge accessories for the vehicle lock, battery/charging state and
  door-open sensor.
- Built-in dashboard for vehicle status, configuration and Volvo account
  connection, protected by password login, sessions and CSRF checks.
- Request limits and backoff for API failures, plus Docker health checks and
  persistent token and vehicle-state files.

| What | Where (on hal) |
|---|---|
| Source + `docker-compose.yml` | `/home/pi/volvo-mqtt` |
| Config (`.env`) | `/home/pi/portainer_data/volvo-mqtt/.env` |
| Data (`tokens.json`, `state.json`) | `/home/pi/portainer_data/volvo-mqtt/data/` |
| Dashboard | http://hal.local:8084 (port 8084; 80 and 8080 are in use on hal) |

## 1. Volvo developer setup

1. Sign in at developer.volvocars.com and create an API application.
2. Copy the **VCC API key**.
3. Publish the application to get a **client ID / client secret**, request the
   scopes below, register the redirect URI `http://volvo.your_url.com/callback`,
   and use `/terms` and `/privacy` on the same host as the Terms and Privacy URLs.

Scopes for a fully electric car (`POWERTRAIN=bev`, the default):
`openid energy:state:read conve:odometer_status conve:doors_status conve:lock_status conve:lock conve:unlock location:read`

Battery and charging data come from the **Energy API** (`/energy/v2/vehicles/{vin}/state`),
so make sure the Energy API is added to your application. For a plug-in hybrid
set `POWERTRAIN=phev` (adds `conve:fuel_status`); for petrol/diesel `POWERTRAIN=ice`.

## 2. Deploy

From PowerShell in this folder:

```powershell
.\deploy.ps1                       # or: .\deploy.ps1 -Target pi@192.168.1.55 -Port 8084
```

The first run creates `/home/pi/portainer_data/volvo-mqtt/.env` from the
template and stops. Edit it (`ssh pi@hal.local nano /home/pi/portainer_data/volvo-mqtt/.env`),
then run `.\deploy.ps1` again to build and start the container.
Re-running deploy updates the code and preserves `data/`. If a local `.env`
exists, it replaces the server `.env` after saving a timestamped backup; use
`-KeepServerEnv` to keep the server copy instead.

## 3. Connecting your Volvo ID

The callback URL is built in. Register **exactly** this redirect URI on your
Volvo application and put the same value in `.env`:

```dotenv
VOLVO_REDIRECT_URI=http://volvo.your_url.com/callback
```

Then sign in to the dashboard and click **Connect Volvo ID**. You're sent to
Volvo's own sign-in page, approve the requested access, and Volvo redirects
to `/callback`. The bridge checks the one-time `state` (it must be the same
browser session that started, used once, within 10 minutes), exchanges the
code using PKCE, saves the tokens and starts polling straight away. No restart
needed. Use **Reconnect** on the Volvo ID tile to re-authorise later, for
example after adding scopes.

Until a Volvo ID is connected, the bridge runs normally but skips polling.

Alternatives: paste a refresh token into `VOLVO_REFRESH_TOKEN`, or use the
command-line login (`docker compose run --rm volvo-mqtt python -m volvo_mqtt.auth`),
which prints the sign-in URL and asks you to paste back the redirect URL.

### Token refresh loop

A background thread renews the access token `TOKEN_REFRESH_MARGIN` seconds
(default 300) before it expires. Volvo rotates refresh tokens, so each new one
is written to `data/tokens.json` straight away; `.env` only seeds the first
token. A 401 from the API also forces a refresh and a single retry. If a
refresh fails, it retries with back-off (30s up to 15 min) and the dashboard
shows the error. If the refresh token is revoked, run the login again.

## 4. Rate limiting

- Polls every `POLL_INTERVAL` (default 600s, **minimum 300s enforced**):
  energy (battery), odometer and doors (3 requests; a PHEV adds fuel), plus location every `LOCATION_POLL_INTERVAL`.
  That's roughly 500 requests a day with the defaults.
- Sliding-window limiter: `RATE_LIMIT_PER_MINUTE` (default 20, hard ceiling 90,
  below Volvo's ~100/min) and a daily cap `RATE_LIMIT_PER_DAY` (default 2000).
- HTTP 429 honours `Retry-After` (at least 30s) and pauses all calls.
- Failed polls back off exponentially (1 min up to 1 h).
- Lock/unlock: `COMMAND_COOLDOWN` (30s) between commands; a single doors
  request confirms the state `COMMAND_REFRESH_DELAY` seconds later.
- Manual refreshes are throttled to one per `MANUAL_REFRESH_MIN_INTERVAL` (120s).

## 5. MQTT topics (prefix `volvo/vehicle`)

| Topic | Payload |
|---|---|
| `energy`, `odometer`, `doors`, `location` (`fuel` for phev/ice) | raw API JSON (retained) |
| `state` | combined, flattened JSON |
| `sensor/<name>` | `battery_level` (%), `electric_range`, `charging_status` (CHARGING/IDLE/DONE/SCHEDULED), `charger_connection` (CONNECTED/DISCONNECTED), `charging` and `plugged_in` (true/false), `charging_power`, `time_to_target_min`, `target_battery_level`, `odometer`, `lock_status`, `latitude`, `longitude`, `heading`, `any_open`... |
| `sensor/homekit_charging_state` | `CHARGING` / `NOT_CHARGING` (Homebridge battery service) |
| `lock/state` | `LOCKED` / `UNLOCKED` |
| `lock/current`, `lock/target` | `S` / `U` (HomeKit / mqttthing) |
| `lock/set` | **publish** `LOCK`/`UNLOCK` (also `S`/`U`, `1`/`0`) |
| `refresh` | **publish** anything to force a poll |
| `command/result` | result of the last command |
| `availability` | `online` / `offline` (LWT) |
| `bridge/status` | token expiry, requests used today, last error |

Retained messages on `lock/set` are ignored, so a stale command can never
re-fire on reconnect.

```bash
mosquitto_pub -h 192.168.1.55 -t volvo/vehicle/lock/set -m LOCK
mosquitto_sub -h 192.168.1.55 -t 'volvo/vehicle/#' -v
```

## 6. Homebridge

Install `homebridge-mqttthing` and add the accessories in
`homebridge-mqttthing.json`: a Lock Mechanism that also shows the battery level
and charging state in Apple Home, plus a contact sensor for "any door open". If HomeKit asks to unlock and the command fails or hits the
cooldown, the target state snaps back to the real state.

## 7. Dashboard and login

| URL | Access |
|---|---|
| `/` | Public holding page with a **Log in** button; the dashboard once signed in |
| `/login` | Sign-in page |
| `/terms` | **Public** Terms and Conditions (use this URL for the Volvo app review) |
| `/privacy` | **Public** Privacy Notice |
| `/callback` | Volvo ID OAuth redirect (only accepts a valid one-time state) |
| `/oauth/start` | Signed-in users: starts "Connect Volvo ID" |
| `/healthz` | Public `{"ok": true/false}` only, used by the Docker health check |
| `/api/*` | Signed-in users only |

Set a password before exposing the site:

```bash
cd ~/volvo-mqtt
docker compose run --rm volvo-mqtt python -m volvo_mqtt.webauth
# paste the printed WEB_PASSWORD_HASH=... line into
# /home/pi/portainer_data/volvo-mqtt/.env, then:
docker compose restart volvo-mqtt
```

Security built in:
- PBKDF2-SHA256 password hash (600,000 iterations); the password is never stored in plain text
- server-side sessions in an HttpOnly, SameSite cookie, expiring after `WEB_SESSION_HOURS` (12); signing out ends the session
- an IP is locked out for 15 minutes after 5 failed sign-ins (configurable), plus a 1-second delay on every failure
- CSRF token on every action (refresh, lock, unlock, sign out) and cross-origin POSTs blocked
- Content Security Policy with per-request nonces, `X-Frame-Options: DENY`, `nosniff`, and HSTS over HTTPS
- secrets are always shown fully masked in the config view

Dashboard Lock/Unlock buttons stay off until `WEB_ENABLE_CONTROLS=true`. For a
public site, also set `WEB_CONTROL_PIN` so an unlock needs the PIN as well as a
signed-in session.

Sessions are held in memory, so restarting the container signs everyone out.

The legal pages take `SITE_OWNER`, `SITE_CONTACT_EMAIL` and `LEGAL_UPDATED`
from `.env`. Review the wording before submitting; it is a sensible starting
point for a private, non-commercial integration, not legal advice.

### Making it public safely

Only expose the dashboard over **HTTPS**. Over plain HTTP the password crosses
the internet unencrypted. Put it behind your existing OpenResty/Nginx with a
certificate, forward to `hal:8084`, and set `WEB_TRUST_PROXY=true` so lockouts
use the real client IP and cookies are marked `Secure`:

```nginx
server {
    listen 443 ssl;
    server_name volvo.your_url.com;
    # ssl_certificate ... / ssl_certificate_key ...
    location / {
        proxy_pass http://192.168.1.55:8084;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Leave MQTT (1883) closed to the internet; only the dashboard port needs forwarding.

## Useful commands (on hal)

```bash
cd ~/volvo-mqtt
docker compose logs -f
docker compose restart volvo-mqtt     # after editing .env
docker compose up -d --build          # after code changes
```

The compose stack appears in Portainer as `volvo-mqtt`.
