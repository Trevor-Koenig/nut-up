# Plan: nut-up — NUT Wake Daemon

## Context

Trevor runs a Raspberry Pi as a NUT server (always-on, UPS-powered). When utility power fails,
NUT slaves (TrueNAS/Dell and Proxmox) shut themselves down via their own upsmon. When power
is restored, nothing currently wakes them. This tool monitors NUT for the power-restore event
and automatically wakes managed machines via WoL or IPMI, while exposing a REST API so Home
Assistant (already running, already monitoring NUT) can manually trigger wakes and display
machine status.

**Key prior art:** WOLNUT (`hardwarehaven/wolnut`) — Python, polls NUT, WoL-only, no API.
We build on the same concept, adding IPMI support + REST API + HA integration + web UI and
API machine management.

---

## Project Layout

```
nut-up/
├── Makefile                    # install / uninstall / update targets
├── pyproject.toml
├── deploy/
│   ├── nut-up.service          # systemd unit
│   └── config.example.yaml    # copy to /etc/nut-up/config.yaml
└── nut_up/
    ├── __init__.py
    ├── cli.py          # click entry point: daemon | discover | wake | status
    ├── config.py       # dataclasses + YAML load/save/validate
    ├── nut.py          # raw socket NUT protocol client
    ├── wake.py         # WoL (wakeonlan) + IPMI (ipmitool subprocess)
    ├── machine.py      # async ping-based online check
    ├── daemon.py       # UPS state machine + poll loop + wake orchestration
    ├── api.py          # FastAPI REST routes (/api/*)
    ├── web.py          # FastAPI web UI routes (/, /partials/*, /wake/*)
    └── templates/
        ├── base.html       # shared layout: Pico CSS + HTMX from CDN
        └── dashboard.html  # UPS status card + machine cards
```

---

## Deployment

### Target environment
- Raspberry Pi OS Bookworm (Debian 12, Python 3.11)
- Enforces PEP 668 — system-wide `pip install` is blocked
- Pi is the NUT master, always on, UPS-powered

### Approach: venv in /opt + Makefile + dedicated user

**Do NOT use:** pipx (user-scope, PATH not visible to systemd), Docker (overhead),
`--break-system-packages` (risks system conflicts), system Debian package (overkill).

**Install location:** `/opt/nut-up/` — venv lives here, binary is `ExecStart`'d directly.

**Dedicated service user:** `nut-up` (non-root).
- WoL: UDP broadcast, no root needed
- Ping: system `ping` is setuid, works as non-root
- ipmitool: network access only, no root needed
- Config: `/etc/nut-up/` owned by `nut-up:nut-up`, mode 750

### Makefile targets

Targets: `help`, `install`, `update`, `test`, `uninstall`, `purge`. See `Makefile` for current implementation.

The `nutup` shell script in the repo root wraps these targets and is installed to `/usr/local/bin/nutup` by `make install`, with the repo path baked in via `sed`. Users run `sudo ./nutup install` on first install, then `sudo nutup <target>` from anywhere thereafter.

### Install flow (user runs once)

```bash
git clone https://github.com/trevorkoenig4/nut-up
cd nut-up
sudo ./nutup install
sudo nano /etc/nut-up/config.yaml    # fill in NUT creds, machines, API key
sudo nutup test                      # verify NUT + IPMI connectivity before starting
sudo systemctl start nut-up
sudo journalctl -u nut-up -f
```

### Update flow

```bash
sudo nutup update   # git pull + reinstall + restart (from anywhere)
```

### systemd unit (`deploy/nut-up.service`)

```ini
[Unit]
Description=nut-up — NUT power-restore wake daemon
After=network.target nut-server.service
Wants=nut-server.service

[Service]
Type=simple
User=nut-up
Group=nut-up
ExecStart=/opt/nut-up/bin/nut-up daemon
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nut-up

[Install]
WantedBy=multi-user.target
```

---

## Dependencies (`pyproject.toml`)

```toml
[project]
name = "nut-up"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.29",
    "jinja2>=3.1",          # web UI templates
    "click>=8.1",
    "pyyaml>=6.0",
    "wakeonlan>=3.1",
]

[project.scripts]
nut-up = "nut_up.cli:main"
```

No PyNUT, no Pydantic — minimal footprint on Pi.
Optional OS package: `apt install ipmitool` (only needed if any machine uses `wake_method: ipmi`).
Frontend libs loaded from CDN (no npm, no build step): Pico CSS, HTMX.

---

## Config File (`/etc/nut-up/config.yaml`)

```yaml
nut:
  host: localhost
  port: 3493
  ups_names:             # list — single-item is the common case; multi-UPS planned
    - ups
  username: monuser
  password: secret

api:
  host: 0.0.0.0
  port: 8765
  api_key: "changeme"          # HA uses this in X-API-Key header

web:
  username: "admin"
  password: "changeme"         # HTTP Basic Auth for the browser UI

wake_delay_seconds: 30         # wait after power restored before waking

machines:
  - name: truenas
    mac: "AA:BB:CC:DD:EE:FF"
    ip: "192.168.1.10"
    broadcast: "192.168.1.255" # omit to use 255.255.255.255
    wake_method: wol
    ups: ups              # optional — which UPS wakes this machine; omit to wake on any
  - name: proxmox
    mac: "11:22:33:44:55:66"
    ip: "192.168.1.20"
    wake_method: wol
    # Future iDRAC:
    # wake_method: ipmi
    # ipmi_host: 192.168.1.21
    # ipmi_user: root        # use a dedicated IPMI user with Operator/Power User role, not root
    # ipmi_pass: secret
```

---

## Module Responsibilities

### `config.py`
- Python dataclasses: `NutConfig`, `ApiConfig`, `MachineConfig`, `Config`
  - `NutConfig.ups_names: list[str]` — one or more UPS device names on the same upsd
  - `MachineConfig.ups: str | None = None` — if set, machine only wakes when that UPS restores;
    if `None`, machine wakes when any configured UPS restores
- `load_config(path) -> Config` — validates MAC format, `wake_method` enum values,
  IPMI field co-presence (`ipmi_host`/`ipmi_user`/`ipmi_pass` all required if method is ipmi)
- `save_config(config, path)` — available for future tooling; YAML comments are lost on write
- Raises `ConfigError(str)` with field name and message on invalid input

### `nut.py`
- `NutClient(host, port, username, password, ups_names: list[str])` — synchronous context manager
- Raw TCP socket + `socket.makefile()` for line-by-line reads
- `get_all_statuses() -> dict[str, str]` — queries each name in `ups_names`, returns
  `{ups_name: raw_status}` e.g. `{"ups": "OL CHRG", "ups2": "OB LB"}`
- `list_clients() -> list[str]` — IPs of currently connected upsmon slaves (uses `LIST CLIENT`)
- Error hierarchy: `NutError > NutAuthError | NutConnectionError | NutProtocolError`
- NUT protocol: connect → `USERNAME` → `PASSWORD` → `LOGIN` → query → `LOGOUT`

### `wake.py`
- `wake_machine(machine: MachineConfig)` — dispatches on `wake_method`
- WoL: `wakeonlan.send_magic_packet(mac, ip_address=broadcast)`
- IPMI: `subprocess.run(["ipmitool", "-I", "lanplus", "-H", host, "-U", user, "-P", pass,
  "chassis", "power", "on"])` — wraps `FileNotFoundError` into
  `WakeError("ipmitool not found — run: apt install ipmitool")`
- Raises `WakeError(RuntimeError)` on any failure

### `machine.py`
- `async def is_online(ip, count=1, timeout=2) -> bool`
  — `asyncio.create_subprocess_exec("ping", "-c", count, "-W", timeout, ip)`
- `async def check_all(machines) -> dict[str, bool]`
  — `asyncio.gather` over all machines concurrently

### `daemon.py`

**UPS state machine — 5 states (one instance per UPS in `ups_states`):**
```
UNKNOWN → ONLINE         (first poll OL, no wake — suppresses startup false positive)
UNKNOWN → ON_BATTERY     (first poll OB, record, no wake)
ONLINE  → ON_BATTERY     (OB seen, record transition time)
ON_BATTERY → LOW_BATTERY (LB added to status)
ON_BATTERY/LOW_BATTERY → RESTORING  (OL returns after ≥10s on battery)
RESTORING → ONLINE       (after wake_sequence completes)
```

**Key correctness rules:**
- Wake only fires when `was_on_battery = True` (state passed through OB/LB)
- Minimum outage debounce: 10 seconds in OB/LB before restore triggers wakes
  (hardcoded — prevents 1-second glitch from waking everything)
- `wake_in_progress` flag prevents double-wake if power flickers during delay window

**`AppState` dataclass** — single shared object (daemon writes, API reads — safe: asyncio
is single-threaded):
```python
@dataclass
class AppState:
    ups_states: dict[str, UpsState]   # keyed by ups_name; one entry per configured UPS
    config: Config
    machine_states: dict[str, bool] = field(default_factory=dict)  # cached ping results
    machine_states_updated: float | None = None  # unix timestamp of last check_all()
    last_wake_attempt: float | None = None
    wake_in_progress: bool = False
    nut_consecutive_failures: int = 0
```

**Poll loop:** every 15 seconds:
1. `asyncio.to_thread(query_nut, ...)` — calls `get_all_statuses()`, updates `ups_states`
   per UPS. `NutConnectionError` → log WARNING (ERROR after 5 consecutive), hold last state.
2. `check_all(config.machines)` — concurrent pings via `asyncio.gather`, writes result into
   `AppState.machine_states` and `machine_states_updated`. Never crashes on ping failure.

Wake fires when any UPS a machine is associated with transitions to RESTORING.

**`wake_sequence`:** `asyncio.sleep(wake_delay_seconds)` → iterate machines → `wake_machine()`
→ catch `WakeError` per-machine (log and continue, don't abort remaining machines).

**`run_daemon(cfg)`:** starts two asyncio tasks in one event loop:
1. `poll_loop` (the state machine)
2. `uvicorn.Server` (programmatic start via `uvicorn.Config`)

### `api.py` — REST API (for Home Assistant / external callers)
- All routes mounted under `/api` prefix
- `X-API-Key` header dependency on all routes except `/health`
- Returns JSON only

**Endpoints:**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | none | `{"status": "ok"}` — for reverse proxy health checks |
| `GET` | `/api/status` | key | UPS states + per-machine cached online/offline + wake_in_progress |
| `POST` | `/api/wake/all` | key | Wake all machines — registered before `/{name}` to avoid collision |
| `POST` | `/api/wake/{name}` | key | Wake one machine; 404 if unknown, 500 on WakeError |

**`GET /api/status` response:**
```json
{
  "ups": {
    "ups": {"state": "ONLINE", "raw_status": "OL CHRG", "last_transition": "2026-05-18T21:30:00Z"}
  },
  "machines": [
    {"name": "truenas", "ip": "192.168.1.10", "online": true,  "wake_method": "wol"},
    {"name": "proxmox", "ip": "192.168.1.20", "online": false, "wake_method": "wol"}
  ],
  "machine_states_updated": "2026-05-18T21:30:10Z",
  "wake_in_progress": false,
  "last_wake_attempt": null
}
```

`/api/status` reads cached `AppState.machine_states` — no live ping per call. HA gets the
state from the last poll cycle (at most 15s stale).

**`POST /api/wake/all` response:**
```json
{"results": [{"name": "truenas", "status": "ok"}, {"name": "proxmox", "status": "error", "message": "..."}]}
```

---

### `web.py` — Browser UI (Jinja2 + HTMX)
- HTTP Basic Auth dependency on all routes (username/password from `config.web`)
- Serves the dashboard and HTMX partials that update in-place

**Stack:**
- **Pico CSS** (CDN) — classless semantic HTML, looks good with no class names, mobile-friendly
- **HTMX** (CDN) — `hx-post` on wake buttons, `hx-get` polling for status updates
- No JS framework, no build step

**Endpoints:**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | basic | Full dashboard (renders `dashboard.html`) |
| `GET` | `/partials/status` | basic | HTMX partial — UPS card + all machine cards, polled every 15s |
| `POST` | `/wake/{name}` | basic | Wake action from UI button; returns updated machine card HTML |

**Dashboard layout (`dashboard.html`):**
```
┌─────────────────────────────────────┐
│  nut-up                             │
├─────────────────────────────────────┤
│  UPS Status                         │
│  ● ONLINE  •  OL CHRG               │
│  Battery: 100%  Runtime: 2h 15m     │
├─────────────────────────────────────┤
│  truenas      192.168.1.10          │
│  ● Online                [Wake]     │
├─────────────────────────────────────┤
│  proxmox      192.168.1.20          │
│  ○ Offline               [Wake]     │
│  Last woken: 2026-05-18 21:31       │
└─────────────────────────────────────┘
```

**HTMX behavior:**
- Whole status block (`<div id="status">`) has `hx-get="/partials/status" hx-trigger="every 15s"` — auto-refreshes without page reload
- Wake buttons: `hx-post="/wake/truenas" hx-target="#machine-truenas"` — replaces just that machine's card on response
- During `RESTORING` state: UPS card shows "Wake in progress — machines waking in ~Xs" (computed from `last_transition + wake_delay_seconds - now`)
- No confirmation dialog (minimal); wake is intentional if user is on the page

### `cli.py` — click subcommands

| Command | Description |
|---------|-------------|
| `nut-up daemon [--config PATH]` | Start daemon (poll loop + API server) |
| `nut-up discover` | Query upsd LIST CLIENT + `/proc/net/arp` → print ready-to-paste YAML |
| `nut-up wake <name\|all>` | Call `POST /wake/{name}` on running daemon; fall back to direct wake if daemon unreachable |
| `nut-up status` | Call `GET /status`, pretty-print table |

**`discover` detail:**
1. Connect to NUT, call `list_clients()` → list of slave IPs
2. Parse `/proc/net/arp` → `{ip: mac}` map (note: only hosts the Pi has recently communicated
   with appear here — offline machines may be absent)
3. `socket.gethostbyaddr(ip)` → hostname (fall back to `machine-<last-octet>`)
4. Print YAML block; mark `mac: "UNKNOWN"` with comment if not in ARP table

Machines are managed by editing `/etc/nut-up/config.yaml` directly and restarting the daemon.

---

## Home Assistant Integration (documented in README, optional)

```yaml
# configuration.yaml
rest_command:
  wake_truenas:
    url: "http://pi-ip:8765/api/wake/truenas"
    method: POST
    headers:
      X-API-Key: "your-api-key"
  wake_all:
    url: "http://pi-ip:8765/api/wake/all"
    method: POST
    headers:
      X-API-Key: "your-api-key"

sensor:
  - platform: rest
    name: "NUT-UP Status"
    resource: "http://pi-ip:8765/api/status"
    headers:
      X-API-Key: "your-api-key"
    scan_interval: 60
    json_attributes: [ups, machines, wake_in_progress]
    value_template: "{{ value_json.ups.state }}"
```

---

## Security Requirements (OWASP)

### A05 — Default credential guard (`config.py` + `daemon.py`)
`load_config()` must raise `ConfigError` if `api.api_key == "changeme"` or
`web.password == "changeme"`. The daemon exits code 1 before binding any port.
This is the last validation step in `load_config`, after structural checks.

Note: all API write endpoints have been removed. The API is read + wake-only, which
eliminates the config-mutation attack surface entirely.

### A05 — Disable FastAPI auto-docs (`api.py`, `web.py`)
Both FastAPI app instances must be created with:
```python
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
```

### A08 — Subresource Integrity for CDN assets (`templates/base.html`)
HTMX and Pico CSS must be loaded with pinned versions and `integrity` attributes:
```html
<link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css"
      integrity="sha384-<hash>"
      crossorigin="anonymous">
<script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"
        integrity="sha384-<hash>"
        crossorigin="anonymous"></script>
```
Compute hashes at implementation time:
```bash
curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A
```

### A09 — Auth failure logging (`api.py`, `web.py`)
- Failed `X-API-Key` checks: `logger.warning("API auth failure from %s", request.client.host)`
- Failed HTTP Basic Auth: `logger.warning("Web auth failure from %s", request.client.host)`
- Both log at WARNING level; no lockout (local-network service).

### Security response headers (middleware on both `api.py` and `web.py`)
Add a middleware that sets on every response:
```python
response.headers["X-Frame-Options"] = "DENY"
response.headers["X-Content-Type-Options"] = "nosniff"
response.headers["Referrer-Policy"] = "no-referrer"
response.headers["Content-Security-Policy"] = (
    "default-src 'self'; "
    "script-src https://unpkg.com; "   # HTMX CDN — tighten to exact version URL at impl time
    "style-src https://cdn.jsdelivr.net"  # Pico CSS CDN
)
```
The CSP `script-src`/`style-src` values should be tightened to the exact pinned CDN URLs once
versions are chosen, consistent with the SRI hash work.

### Config file permissions (`Makefile`)
After copying the config file, set mode 640 so credentials are not world-readable:
```makefile
chmod 640 $(CONFFILE)
chown nut-up:nut-up $(CONFFILE)
```

### IPMI least privilege (`deploy/config.example.yaml` comment)
Add a comment on `ipmi_user` warning against using `root`; recommend a dedicated IPMI user
with `Operator` or `Power User` role (power-on only, no firmware/KVM access).

### HA API key (`README` / HA integration section)
Document using `!secret nut_up_api_key` in HA's `secrets.yaml` rather than inlining the key
in `configuration.yaml`.

### CSRF non-applicability (no code needed)
HTTP Basic Auth does not auto-send cross-origin like cookies, so CSRF is not a concern with
the current auth model. If auth is ever changed to session cookies, add CSRF tokens.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| upsd unreachable | Log WARNING; hold last state; retry every 15s; escalate to ERROR after 5 failures |
| WoL failure | Log ERROR per-machine; continue waking remaining machines |
| ipmitool missing | `WakeError` with install instructions; logged, API returns 500 |
| Config missing/malformed | `ConfigError` printed to stderr; daemon exits code 1 |
| Default credentials | `ConfigError` printed to stderr; daemon exits code 1 (see Security Requirements) |
| API port in use | uvicorn raises; systemd restarts daemon (RestartSec=10) |
| Machine ping unreachable | Not an error — `"online": false` in `/status` |

---

## Build Order (each step independently testable)

1. `config.py` — no internal deps
2. `nut.py` — test against live upsd with `nc localhost 3493`
3. `wake.py` — test with a real machine on the LAN
4. `machine.py` — standalone ping test
5. `api.py` — test with hardcoded `AppState` + `httpx` test client
6. `web.py` + `templates/` — test with hardcoded `AppState`, verify HTMX partials render correctly
7. `daemon.py` — integrates everything; test against real NUT
8. `cli.py` — thin wrappers; test last
9. Makefile + systemd — final integration on Pi

---

## Verification

```bash
# Dev machine: install editable, test modules
pip install -e .
nut-up discover

# Pi: full install
sudo ./nutup install
sudo nano /etc/nut-up/config.yaml
sudo nutup test
sudo systemctl start nut-up
sudo journalctl -u nut-up -f

# Smoke test REST API
curl http://localhost:8765/health
curl -H "X-API-Key: yourkey" http://localhost:8765/api/status
curl -X POST -H "X-API-Key: yourkey" http://localhost:8765/api/wake/truenas

# Test web UI: open http://pi-ip:8765/ in browser, log in with Basic Auth
# Verify machine cards show correct online/offline state
# Click Wake button, verify HTMX updates card in-place without page reload
# Wait 15 seconds, verify auto-refresh updates status

# Simulate power event: cut UPS input briefly, watch logs for state transition
# Verify machines wake after wake_delay_seconds
# Verify web UI shows "Wake in progress" during RESTORING state

# HA (optional): add rest_command and sensor, trigger wake from HA dashboard
```
