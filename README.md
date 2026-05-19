# nut-up

A lightweight Python daemon that monitors a [NUT](https://networkupstools.org/) server and automatically wakes managed machines via Wake-on-LAN or IPMI after utility power is restored. It also exposes a REST API and browser dashboard for manual control and Home Assistant integration.

Runs on any systemd-based Linux system (Debian, Ubuntu, Fedora, Arch, Raspberry Pi OS, etc.).

---

## Quick Start

```bash
git clone https://github.com/Trevor-Koenig/nut-up.git && cd nut-up
sudo ./nutup install
sudo nano /etc/nut-up/config.yaml   # set NUT credentials, machines, and any interfaces you want enabled
sudo systemctl enable --now nut-up
curl http://localhost:8765/health   # should return {"status": "ok"}
```

After install, the `nutup` command is available system-wide. All commands auto-escalate to sudo if needed:

```
nutup              # show available commands
nutup update       # pull latest changes and restart
nutup test         # verify NUT and IPMI connectivity
nutup wake proxmox # manually wake a machine
nutup status       # show UPS and machine status
```

See [Installation](#installation) and [Configuration](#configuration) below for full details.

---

## How It Works

nut-up polls the NUT `upsd` daemon every 15 seconds and drives a per-UPS state machine:

```
UNKNOWN ──► ONLINE ──► ON_BATTERY ──► LOW_BATTERY
                ▲            │               │
                │            └───────────────┘
                │         (power restored, ≥10s elapsed)
                └────── RESTORING ◄────────────────────
```

- **Debounce:** the transition to `RESTORING` only fires if the UPS was on battery for at least 10 seconds, ignoring sub-10s flickers that don't cause machines to shut down.
- **Wake delay:** once power is restored, nut-up waits `wake_delay_seconds` (default 30) before sending wake signals, giving the UPS time to stabilize.
- **Wake trigger:** wake signals are sent only if `was_on_battery` is true, preventing spurious wakes on daemon restart.
- **Wake methods:** WoL magic packets via `wakeonlan` library, or IPMI power-on via `ipmitool` subprocess. Both can be mixed in the same config.

---

## Prerequisites

- **Any systemd-based Linux** (Debian, Ubuntu, Fedora, Arch, Raspberry Pi OS, etc.) with Python 3.10+
- **NUT already configured** — `upsd` running and reachable, slaves using `upsmon` to shut themselves down on battery. nut-up only handles the *restore* side.
- `python3` and `python3-venv` — install with your distro's package manager if missing (e.g. `sudo apt install python3-venv` on Debian/Ubuntu)
- `ipmitool` if any machines use IPMI wake (e.g. `sudo apt install ipmitool` on Debian/Ubuntu)
- **Network requirements:**
  - WoL: nut-up must be on the same L2 broadcast domain as the target machine, or you must configure a directed broadcast address. WoL packets do not route.
  - IPMI: the BMC/iDRAC IP must be reachable from the host.

---

## Installation

### Step 1 — Clone and install

```bash
git clone https://github.com/Trevor-Koenig/nut-up.git
cd nut-up
sudo ./nutup install
```

`nutup install` creates a `nut-up` system user, sets up a virtualenv at `/opt/nut-up/`, copies the example config to `/etc/nut-up/config.yaml`, installs the systemd unit, and makes `nutup` available system-wide at `/usr/local/bin/nutup`.

### Step 2 — Edit the config

```bash
sudo nano /etc/nut-up/config.yaml
```

At minimum, set:
- `nut.username` / `nut.password` — credentials from `/etc/nut/upsd.users`
- `machines` — the list of machines to wake (see [Configuration](#configuration) below)

Optionally set:
- `api.api_key` — enables the REST API (used by Home Assistant and the CLI). Leave blank to disable.
- `web.password` — enables the browser UI. Leave blank to disable.

### Step 3 — Enable and start

```bash
sudo systemctl enable --now nut-up
```

`enable` ensures the daemon starts automatically on boot — important since the whole point of nut-up is to respond to events after an unattended power outage.

### Step 4 — Verify

```bash
sudo systemctl status nut-up
curl http://localhost:8765/health   # → {"status": "ok"}
sudo journalctl -u nut-up -f       # watch live logs
```

### Updating

```bash
sudo nutup update   # pulls latest changes, reinstalls into the venv, and restarts the service
```

### Checking connectivity

After configuring, verify that nut-up can reach the NUT server and any IPMI BMCs before starting the daemon:

```bash
sudo nutup test
```

This connects to `upsd`, authenticates, reads the status of each configured UPS, and runs a read-only IPMI query against any IPMI-configured machines. It exits non-zero if anything fails.

### Uninstalling

```bash
sudo nutup uninstall   # stops/disables service, removes venv and unit; config is left in place
sudo nutup purge       # same as uninstall, plus removes /etc/nut-up/, the nut-up system user, and the repo directory
```

---

## Configuration

Config file: `/etc/nut-up/config.yaml`

```yaml
nut:
  host: localhost          # hostname or IP of the NUT server (upsd)
  port: 3493               # upsd port
  ups_names:
    - ups                  # UPS names as defined in /etc/nut/ups.conf
  username: monuser        # NUT username from /etc/nut/upsd.users
  password: secret

api:
  host: 0.0.0.0            # bind address; use 0.0.0.0 to listen on all interfaces
  port: 8765
  api_key:                 # set a secret string to enable the REST API; leave blank to disable
                           # used in X-API-Key header — required for Home Assistant integration
                           # generate one: openssl rand -base64 32

web:
  username: "admin"        # HTTP Basic Auth for the browser UI
  password:                # set a secret string to enable the browser UI; leave blank to disable
                           # generate one: openssl rand -base64 32
  # port: 8766             # optional — serve the web UI on a separate port from the API

wake_delay_seconds: 30     # wait this many seconds after power restore before waking machines

machines:
  - name: truenas
    mac: "AA:BB:CC:DD:EE:FF"
    ip: "192.168.1.10"
    broadcast: "192.168.1.255"   # optional; defaults to 255.255.255.255
    wake_method: wol
    # ups: ups                   # optional — tie to a specific UPS; omit to wake on any UPS event

  - name: proxmox
    mac: "11:22:33:44:55:66"
    ip: "192.168.1.20"
    wake_method: wol

  # IPMI example — requires: sudo apt install ipmitool
  # - name: dell-server
  #   ip: "192.168.1.30"
  #   mac: "AA:BB:CC:DD:EE:01"      # not used for ipmi wake; kept for inventory
  #   wake_method: ipmi
  #   ipmi_host: "192.168.1.31"     # iDRAC / BMC IP
  #   ipmi_user: "nut-up-power"     # Operator or Power User role — do NOT use root
  #   ipmi_pass: "secret"
```

### Full Config Reference

| Field | Type | Default | Description |
|---|---|---|---|
| `nut.host` | string | `localhost` | Hostname or IP of `upsd`. Prefer an IP — a hostname requires DNS, which may itself be unreachable just after a power event if the LAN DNS server was on the same circuit. |
| `nut.port` | integer | `3493` | `upsd` port |
| `nut.ups_names` | list | `[ups]` | UPS device names as defined in `ups.conf`; list multiple for multi-UPS setups |
| `nut.username` | string | `""` | NUT username from `upsd.users` |
| `nut.password` | string | `""` | NUT password |
| `api.host` | string | `0.0.0.0` | Bind address for the HTTP server |
| `api.port` | integer | `8765` | HTTP port |
| `api.api_key` | string | `null` | `X-API-Key` value for REST API auth. Omit or set to `null` to disable the REST API. Must not be `changeme`. |
| `web.username` | string | `admin` | HTTP Basic Auth username for the browser UI |
| `web.password` | string | `null` | HTTP Basic Auth password for the browser UI. Omit or set to `null` to disable the web UI. Must not be `changeme`. |
| `web.port` | integer | `null` | Port for the browser UI. When omitted, the web UI shares `api.port`. When set, a separate server is started on this port. Must differ from `api.port`. |
| `wake_delay_seconds` | integer | `30` | Seconds to wait after power restore before sending wake signals |
| `machines[].name` | string | — | **Required.** Identifier used in API URLs, logs, and the UI |
| `machines[].ip` | string | — | **Required.** IP address used for ping-based online checks |
| `machines[].mac` | string | — | MAC address for WoL (`AA:BB:CC:DD:EE:FF`). Required if `wake_method` is `wol`. |
| `machines[].broadcast` | string | `255.255.255.255` | Broadcast address for WoL magic packet. Set to subnet broadcast (e.g. `192.168.1.255`) if the default doesn't reach the machine. |
| `machines[].wake_method` | string | `wol` | `wol` or `ipmi` |
| `machines[].ups` | string | `null` | Tie this machine to a specific UPS name. If omitted, it wakes on any UPS power-restore event. |
| `machines[].ipmi_host` | string | — | BMC/iDRAC IP. Required if `wake_method` is `ipmi`. Prefer an IP for the same reason as `nut.host`. |
| `machines[].ipmi_user` | string | — | IPMI username. Required if `wake_method` is `ipmi`. |
| `machines[].ipmi_pass` | string | — | IPMI password. Required if `wake_method` is `ipmi`. |

Both `api.api_key` and `web.password` are optional. Omitting them (or setting to `null`) disables the respective interface — the daemon still runs the poll loop and wakes machines. The daemon refuses to start if either is set to `changeme`.

### Adding a machine

The quickest way to populate the `machines` list is the `discover` command. Run it while the machine is online and connected to NUT as a slave — it queries `upsd` for connected clients, looks up their MACs from the kernel ARP table, and prints ready-to-paste YAML:

```bash
nut-up discover
```

If you'd rather add a machine by hand, here's what you need:

**For WoL (`wake_method: wol`):**

| Field | Where to find it |
|---|---|
| `name` | Any identifier — used in API URLs, logs, and the UI. No spaces. |
| `ip` | The machine's LAN IP. Use a static IP or DHCP reservation so it doesn't change. |
| `mac` | Run `ip link show` on the target machine, or check your router's ARP/DHCP table. Format: `AA:BB:CC:DD:EE:FF`. |
| `broadcast` | Usually your subnet's broadcast address, e.g. `192.168.1.255`. Only needed if the default `255.255.255.255` doesn't work across VLANs. |
| `ups` | Optional. The name of a specific UPS from `nut.ups_names`. Omit if the machine should wake on any UPS restore event. |

WoL must also be enabled in the machine's BIOS/UEFI settings. Some NICs also require a per-boot OS-level configuration — if WoL works when triggered manually but not after a full power cut, check your NIC documentation.

**For IPMI (`wake_method: ipmi`):**

| Field | Where to find it |
|---|---|
| `ipmi_host` | The BMC/iDRAC/iLO IP address (separate from the host OS IP) |
| `ipmi_user` | A dedicated IPMI user with **Operator** or **Power User** role. Do not use `root`. |
| `ipmi_pass` | The IPMI user's password |

IPMI requires `ipmitool` to be installed on the nut-up host. Test the credentials manually before adding to config:

```bash
ipmitool -I lanplus -H <ipmi_host> -U <ipmi_user> -P <ipmi_pass> -L OPERATOR chassis power status
```

**After editing the config, restart the daemon to pick up the changes:**

```bash
sudo systemctl restart nut-up
```

---

## Web UI

The browser dashboard requires HTTP Basic Auth (the credentials from `web.username` / `web.password`).

It shows:
- Current state and raw NUT status for each configured UPS
- Online/offline status and wake method for each machine (auto-refreshes every 15 seconds via HTMX)
- A **Wake** button per machine that sends the wake signal immediately and updates the card in place

By default, the web UI and REST API share `api.port` (default `8765`). Set `web.port` in the config to run the web UI on its own port — when configured this way, a separate server is started and API routes are not accessible on the web port.

---

## CLI Reference (`nutup`)

After install, `nutup` is the single entry point for all nut-up operations. All commands auto-escalate to sudo if not already running as root.

### System commands

```bash
nutup install    # install from the cloned repo (first-time only)
nutup update     # git pull + reinstall package + restart service
nutup test       # check NUT connectivity, credentials, and IPMI BMC access
nutup lock       # regenerate requirements.lock from the installed venv (see Security Notes)
nutup uninstall  # stop service, remove venv/unit (config kept)
nutup purge      # remove everything including config and repo
```

### Runtime commands

```bash
# Wake a machine by name, or all machines at once
# Calls the daemon API if running; falls back to direct wake if not
nutup wake truenas
nutup wake all

# Show current UPS and machine status from the running daemon
nutup status

# Discover NUT slave clients via upsd, resolve their MACs from the ARP table,
# and print ready-to-paste YAML for the machines section of config.yaml
nutup discover

# Start the daemon (normally handled by systemd, not run directly)
nutup daemon [--config /etc/nut-up/config.yaml]
```

All runtime commands accept `--config <path>` to point at a non-default config file.

The `discover` command is useful during initial setup: it queries upsd for connected slave clients, looks up their MACs from the kernel ARP table, and does a reverse-DNS lookup for hostnames. Run it right after a slave machine has connected to upsd so its IP is in the ARP table.

---

## REST API

All REST endpoints are served on `api.port` (default `8765`). API docs are intentionally disabled; endpoints are documented here.

Authentication uses a static API key passed in the `X-API-Key` request header. The `/health` endpoint is unauthenticated.

### `GET /health`

Returns `200 OK` with no authentication required. Use this for uptime monitoring.

```bash
curl http://192.168.1.x:8765/health
```

```json
{"status": "ok"}
```

---

### `GET /api/status`

Returns the current state of all UPS devices and machines.

```bash
curl -H "X-API-Key: yourkey" http://192.168.1.x:8765/api/status
```

```json
{
  "ups": {
    "ups": {
      "state": "ONLINE",
      "raw_status": "OL",
      "last_transition": "2024-01-15T10:30:00Z"
    }
  },
  "machines": [
    {
      "name": "truenas",
      "ip": "192.168.1.10",
      "online": true,
      "wake_method": "wol"
    },
    {
      "name": "proxmox",
      "ip": "192.168.1.20",
      "online": false,
      "wake_method": "wol"
    }
  ],
  "machine_states_updated": "2024-01-15T10:30:12Z",
  "wake_in_progress": false,
  "last_wake_attempt": null
}
```

`state` is one of: `UNKNOWN`, `ONLINE`, `ON_BATTERY`, `LOW_BATTERY`, `RESTORING`.

`machine_states_updated` is the timestamp of the last successful ping sweep. `last_wake_attempt` is `null` if no wake has been attempted since daemon start.

---

### `POST /api/wake/{name}`

Sends a wake signal to the named machine immediately, regardless of UPS state.

```bash
curl -X POST -H "X-API-Key: yourkey" http://192.168.1.x:8765/api/wake/truenas
```

```json
{"status": "ok", "name": "truenas"}
```

Returns `404` if the machine name is not in the config, `500` if the wake attempt fails.

---

### `POST /api/wake/all`

Sends wake signals to all configured machines. Always returns `200`; per-machine errors are reported in the results array.

```bash
curl -X POST -H "X-API-Key: yourkey" http://192.168.1.x:8765/api/wake/all
```

```json
{
  "results": [
    {"name": "truenas", "status": "ok"},
    {"name": "proxmox", "status": "error", "message": "ipmitool exited with code 1: ..."}
  ]
}
```

---

## Home Assistant Integration

nut-up's REST API integrates cleanly with Home Assistant. If HA already has the [NUT integration](https://www.home-assistant.io/integrations/nut/) configured for UPS monitoring, nut-up adds machine online states and manual wake capability on top.

### Status Sensor

Add a `rest` sensor to pull machine and UPS state. Replace `192.168.1.x` and `your-api-key` with your values.

```yaml
# configuration.yaml
rest:
  - resource: "http://192.168.1.x:8765/api/status"
    headers:
      X-API-Key: "your-api-key"
    scan_interval: 30
    sensor:
      - name: "UPS State"
        unique_id: nut_up_ups_state
        value_template: "{{ value_json.ups.ups.state }}"

      - name: "TrueNAS Online"
        unique_id: nut_up_truenas_online
        value_template: >-
          {{ value_json.machines
             | selectattr('name', 'eq', 'truenas')
             | map(attribute='online')
             | first }}

      - name: "Proxmox Online"
        unique_id: nut_up_proxmox_online
        value_template: >-
          {{ value_json.machines
             | selectattr('name', 'eq', 'proxmox')
             | map(attribute='online')
             | first }}

      - name: "Wake In Progress"
        unique_id: nut_up_wake_in_progress
        value_template: "{{ value_json.wake_in_progress }}"
```

If you have multiple UPS devices, replace `value_json.ups.ups.state` with `value_json.ups.<ups-name>.state` using the name from your config.

### Wake Commands

Define REST commands for manual wake triggers:

```yaml
# configuration.yaml
rest_command:
  wake_truenas:
    url: "http://192.168.1.x:8765/api/wake/truenas"
    method: post
    headers:
      X-API-Key: "your-api-key"

  wake_proxmox:
    url: "http://192.168.1.x:8765/api/wake/proxmox"
    method: post
    headers:
      X-API-Key: "your-api-key"

  wake_all_machines:
    url: "http://192.168.1.x:8765/api/wake/all"
    method: post
    headers:
      X-API-Key: "your-api-key"
```

Add buttons to a dashboard:

```yaml
# Dashboard card
type: button
name: Wake TrueNAS
icon: mdi:server
tap_action:
  action: call-service
  service: rest_command.wake_truenas
```

### Notification Automation

Send a notification when power is restored and machines are being woken:

```yaml
automation:
  - alias: "Notify: UPS power restored, waking machines"
    trigger:
      - platform: state
        entity_id: sensor.ups_state
        to: "RESTORING"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Power Restored"
          message: "UPS is back on utility power. Machines will wake in {{ states('input_number.wake_delay') }} seconds."
```

Or trigger on UPS going to battery:

```yaml
automation:
  - alias: "Notify: UPS on battery"
    trigger:
      - platform: state
        entity_id: sensor.ups_state
        to: "ON_BATTERY"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Power Outage"
          message: "UPS is running on battery."
```

---

## Troubleshooting

### Connectivity pre-flight

Run `sudo nutup test` after configuring and before starting the daemon. It checks config validity, NUT authentication, UPS name resolution, and IPMI BMC access in one pass and reports pass/fail per check.

### Daemon won't start

| Symptom | Cause | Fix |
|---|---|---|
| `Config error: api.api_key must be changed` | `api_key` or `web.password` is set to `changeme` | Set a real value, or remove the field to disable that interface |
| `Config error: Config file not found` | Config doesn't exist | Run `sudo nutup install` or copy `deploy/config.example.yaml` to `/etc/nut-up/config.yaml` |
| `Config error: invalid MAC address` | Bad MAC in config | Verify MAC with `ip link show` or your router's ARP table |
| `Config error: invalid UPS name` | `nut.ups_names` contains whitespace or unusual characters | Use only letters, digits, `.`, `-`, `_` — these are the characters NUT itself accepts in `ups.conf` |
| `Config error: invalid ip address` / `invalid broadcast address` | `machines[].ip` or `machines[].broadcast` isn't a parseable IPv4/IPv6 address | Fix the value; the config loader validates with Python's `ipaddress` module |
| `Config error: invalid ipmi_host` | `machines[].ipmi_host` starts with `-` or contains unexpected characters | Use an IP address or a plain hostname for the BMC |

### WoL not working

| Symptom | Cause | Fix |
|---|---|---|
| Packet sent, machine doesn't wake | WoL disabled in BIOS | Enable "Wake on LAN" in BIOS/UEFI |
| WoL disabled in OS | `ethtool <iface>` shows `Wake-on: d` | Run `ethtool -s <iface> wol g`; persist via `post-up ethtool -s <iface> wol g` in `/etc/network/interfaces` |
| Works when run directly, not from Pi | Different subnet, broadcast blocked | Set `broadcast` to your subnet's broadcast address (e.g. `192.168.1.255`) in the machine config |
| Works on normal shutdown, not after full power loss | NIC loses standby power when AC is cut | WoL cannot work after a full power outage — use IPMI instead, or enable "Power On After AC Loss" in BIOS/UEFI |

### IPMI not working

| Symptom | Cause | Fix |
|---|---|---|
| `ipmitool not found` | Not installed | `sudo apt install ipmitool` |
| `ipmitool timed out` | UDP/623 blocked or BMC LAN disabled | Check firewall allows UDP/623; verify IPMI over LAN is enabled in BMC settings |
| `ipmitool exited with code 1` | Auth failure or wrong BMC IP | Verify `ipmi_host`, `ipmi_user`, `ipmi_pass`; test with `ipmitool -I lanplus -H <host> -U <user> -P <pass> -L OPERATOR chassis power status` |

### NUT connection errors

| Symptom | Cause | Fix |
|---|---|---|
| `NUT connection error` in logs | `upsd` not reachable | Verify `nut.host` and `nut.port`; check `systemctl status nut-server` |
| `NUT auth error` | Wrong credentials | Verify username/password match `/etc/nut/upsd.users` |
| Repeated warnings, no errors | Transient; nut-up escalates to ERROR after 5 consecutive failures | Check network stability |

### Viewing logs

```bash
sudo journalctl -u nut-up -f          # follow live
sudo journalctl -u nut-up --since today
sudo journalctl -u nut-up -n 100      # last 100 lines
```

---

## Security Notes

- **Use a randomly generated credential.** The daemon refuses to start if either `api.api_key` or `web.password` is set to `changeme`. Omit the field entirely to disable the interface instead. To generate a strong value:
  ```bash
  openssl rand -base64 32
  # or, with no extra dependencies:
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- The API key is sent in plaintext over HTTP. If nut-up is exposed beyond your local network, put it behind a reverse proxy with TLS.
- For IPMI machines, create a dedicated BMC user with **Operator** role — do not use the root/Administrator account. nut-up explicitly requests Operator-level sessions (`-L OPERATOR`), so Administrator privileges are neither required nor used. An Operator account can power on/off but cannot modify BMC configuration.
- By default the web UI and API share a port. Set `web.port` to a different value to run them on separate ports — this lets you firewall the web UI independently while keeping the API accessible to Home Assistant.
- API documentation endpoints (`/docs`, `/redoc`, `/openapi.json`) are disabled.
- **Supply chain.** Direct dependencies in `pyproject.toml` are capped with upper bounds so a future major-version release can't land on `nutup update` without a deliberate bump. For full transitive-dep pinning, run `sudo nutup lock` once on the install host — this writes `requirements.lock` from the installed venv. Commit the file, and both `install` and `update` will prefer it over a live PyPI resolution from then on. Frontend assets (Pico CSS, HTMX) are vendored under `nut_up/static/` and served same-origin, so the dashboard renders during an internet outage and there are no third-party CDNs in the page load path.
- **Config validation.** The config loader rejects UPS names containing whitespace or special characters, IP/broadcast values that aren't real IPs, and IPMI hostnames starting with `-`. This is defence-in-depth so that a malformed config can't smuggle extra arguments into the NUT protocol or into `ping`/`ipmitool` argv.
