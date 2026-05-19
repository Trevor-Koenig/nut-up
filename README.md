# nut-up

A lightweight Python daemon for Raspberry Pi that monitors a [NUT](https://networkupstools.org/) server and automatically wakes managed machines via Wake-on-LAN or IPMI after utility power is restored. It also exposes a REST API and browser dashboard for manual control and Home Assistant integration.

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

- **Raspberry Pi OS Bookworm** (Debian 12, Python 3.11). Other systemd-based Linux distributions should work but are untested.
- **NUT already configured** — `upsd` running on the Pi, slaves using `upsmon` to shut themselves down on battery. nut-up only handles the *restore* side.
- `python3` and `python3-venv` available (`sudo apt install python3-venv`).
- `ipmitool` if any machines use IPMI wake: `sudo apt install ipmitool`.
- **Network requirements:**
  - WoL: nut-up must be on the same L2 broadcast domain as the target machine, or you must configure a directed broadcast address. WoL packets do not route.
  - IPMI: the BMC/iDRAC IP must be reachable from the Pi.

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/youruser/nut-up.git
cd nut-up

# 2. Install (creates venv at /opt/nut-up, system user, systemd unit)
sudo make install

# 3. Edit the config — at minimum change api_key, web.password, nut credentials, and machines
sudo nano /etc/nut-up/config.yaml

# 4. Start the daemon
sudo systemctl start nut-up

# 5. Verify
sudo systemctl status nut-up
curl http://localhost:8765/health
```

The `make install` target:
- Creates a `nut-up` system user (no login shell, no home directory)
- Creates a virtualenv at `/opt/nut-up/` and installs the package
- Copies `deploy/config.example.yaml` to `/etc/nut-up/config.yaml` (only if it doesn't already exist)
- Sets config ownership to `nut-up:nut-up` with mode `640`
- Installs and enables the systemd unit

**Updating after a `git pull`:**

```bash
sudo make update   # reinstalls into the venv and restarts the service
```

**Uninstalling:**

```bash
sudo make uninstall   # stops/disables service, removes venv and unit; config is left in place
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
  api_key: "changeme"      # REQUIRED: change this — used in X-API-Key header

web:
  username: "admin"        # HTTP Basic Auth for the browser UI
  password: "changeme"     # REQUIRED: change this

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
  #   wake_method: ipmi
  #   ipmi_host: "192.168.1.31"     # iDRAC / BMC IP
  #   ipmi_user: "nut-up-power"     # Operator or Power User role — do NOT use root
  #   ipmi_pass: "secret"
```

### Full Config Reference

| Field | Type | Default | Description |
|---|---|---|---|
| `nut.host` | string | `localhost` | Hostname or IP of `upsd` |
| `nut.port` | integer | `3493` | `upsd` port |
| `nut.ups_names` | list | `[ups]` | UPS device names as defined in `ups.conf`; list multiple for multi-UPS setups |
| `nut.username` | string | `""` | NUT username from `upsd.users` |
| `nut.password` | string | `""` | NUT password |
| `api.host` | string | `0.0.0.0` | Bind address for the HTTP server |
| `api.port` | integer | `8765` | HTTP port |
| `api.api_key` | string | — | **Required.** `X-API-Key` value for REST API auth. Must not be `changeme`. |
| `web.username` | string | `admin` | HTTP Basic Auth username for the browser UI |
| `web.password` | string | — | **Required.** HTTP Basic Auth password. Must not be `changeme`. |
| `wake_delay_seconds` | integer | `30` | Seconds to wait after power restore before sending wake signals |
| `machines[].name` | string | — | **Required.** Identifier used in API URLs, logs, and the UI |
| `machines[].ip` | string | — | **Required.** IP address used for ping-based online checks |
| `machines[].mac` | string | — | MAC address for WoL (`AA:BB:CC:DD:EE:FF`). Required if `wake_method` is `wol`. |
| `machines[].broadcast` | string | `255.255.255.255` | Broadcast address for WoL magic packet. Set to subnet broadcast (e.g. `192.168.1.255`) if the default doesn't reach the machine. |
| `machines[].wake_method` | string | `wol` | `wol` or `ipmi` |
| `machines[].ups` | string | `null` | Tie this machine to a specific UPS name. If omitted, it wakes on any UPS power-restore event. |
| `machines[].ipmi_host` | string | — | BMC/iDRAC IP. Required if `wake_method` is `ipmi`. |
| `machines[].ipmi_user` | string | — | IPMI username. Required if `wake_method` is `ipmi`. |
| `machines[].ipmi_pass` | string | — | IPMI password. Required if `wake_method` is `ipmi`. |

The daemon refuses to start if `api.api_key` or `web.password` is left as `changeme`.

---

## Web UI

The browser dashboard is served at `http://<pi-ip>:8765/` and requires HTTP Basic Auth (the credentials from `web.username` / `web.password`).

It shows:
- Current state and raw NUT status for each configured UPS
- Online/offline status and wake method for each machine (auto-refreshes every 15 seconds via HTMX)
- A **Wake** button per machine that sends the wake signal immediately and updates the card in place

The UI and the REST API are served from the same port. API routes (`/health`, `/api/*`) take priority; the browser UI catches everything else.

---

## CLI Reference

```bash
# Start the daemon (normally handled by systemd)
nut-up daemon [--config /etc/nut-up/config.yaml]

# Discover NUT slave clients via upsd, resolve their MACs from the ARP table,
# and print ready-to-paste YAML for the machines section of config.yaml
nut-up discover

# Wake a machine by name, or wake all machines
# Calls the daemon API; falls back to direct wake if the daemon is unreachable
nut-up wake truenas
nut-up wake all

# Show current UPS and machine status from the running daemon
nut-up status
```

All commands accept `--config <path>` to point at a non-default config file.

The `discover` command is useful during initial setup: it queries upsd for connected slave clients, looks up their MACs from the kernel ARP table, and does a reverse-DNS lookup for hostnames. Run it right after a slave machine has connected to upsd so its IP is in the ARP table.

---

## REST API

All REST endpoints are served on the same port as the web UI (default `8765`). API docs are intentionally disabled; endpoints are documented here.

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

### Daemon won't start

| Symptom | Cause | Fix |
|---|---|---|
| `Config error: api.api_key must be changed` | Default credentials still set | Edit `/etc/nut-up/config.yaml` and set a real `api_key` and `web.password` |
| `Config error: Config file not found` | Config doesn't exist | Run `sudo make install` or copy `deploy/config.example.yaml` to `/etc/nut-up/config.yaml` |
| `Config error: invalid MAC address` | Bad MAC in config | Verify MAC with `ip link show` or your router's ARP table |

### WoL not working

| Symptom | Cause | Fix |
|---|---|---|
| Packet sent, machine doesn't wake | WoL disabled in BIOS | Enable "Wake on LAN" in BIOS/UEFI |
| Works when run directly, not from Pi | Different subnet, broadcast blocked | Set `broadcast` to your subnet's broadcast address (e.g. `192.168.1.255`) in the machine config |
| Works manually, not after outage | Machine NIC loses WoL capability when powered off too long | Some NICs require the host to configure WoL each boot; check NIC documentation |

### IPMI not working

| Symptom | Cause | Fix |
|---|---|---|
| `ipmitool not found` | Not installed | `sudo apt install ipmitool` |
| `ipmitool exited with code 1` | Auth failure or wrong BMC IP | Verify `ipmi_host`, `ipmi_user`, `ipmi_pass`; test with `ipmitool -I lanplus -H <host> -U <user> -P <pass> power status` |
| Permission denied | IPMI user lacks Power User role | Assign at minimum **Operator** role in the BMC user settings |

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

- **Change both `api.api_key` and `web.password` before starting the daemon.** The daemon will refuse to start with the default `changeme` value.
- The API key is sent in plaintext over HTTP. If nut-up is exposed beyond your local network, put it behind a reverse proxy with TLS.
- For IPMI machines, create a dedicated BMC user with **Operator** or **Power User** role only — do not use the root/Administrator account. An Operator-role account can power on/off but cannot modify BMC configuration.
- The web UI and API share a port. There is no way to expose only the API without also exposing the UI; use firewall rules if you need to restrict browser access while allowing HA to reach the API.
- API documentation endpoints (`/docs`, `/redoc`, `/openapi.json`) are disabled.
