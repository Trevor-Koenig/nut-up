# nut-up

A lightweight Python daemon that monitors a [NUT](https://networkupstools.org/) server and automatically wakes managed machines via Wake-on-LAN or IPMI after utility power is restored. It also exposes a REST API and browser dashboard for manual control and Home Assistant integration.

Runs on any systemd-based Linux system (Debian, Ubuntu, Fedora, Arch, Raspberry Pi OS, etc.).

---

## Quick Start

```bash
git clone https://github.com/youruser/nut-up.git && cd nut-up
sudo make install
sudo nano /etc/nut-up/config.yaml   # set NUT credentials, machines, and any interfaces you want enabled
sudo systemctl enable --now nut-up
curl http://localhost:8765/health   # should return {"status": "ok"}
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
git clone https://github.com/youruser/nut-up.git
cd nut-up
sudo make install
```

`make install` creates a `nut-up` system user, sets up a virtualenv at `/opt/nut-up/`, copies the example config to `/etc/nut-up/config.yaml`, and installs the systemd unit.

### Step 2 — Edit the config

```bash
sudo nano /etc/nut-up/config.yaml
```

At minimum, set:
- `nut.username` / `nut.password` — credentials from `/etc/nut/upsd.users`
- `machines` — the list of machines to wake (see [Configuration](#configuration) below)

Optionally set:
- `api.api_key` — enables the REST API (used by Home Assistant and the CLI). Omit to disable.
- `web.password` — enables the browser UI. Omit to disable.

The daemon will refuse to start if either value is set to `changeme`.

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
git pull
sudo make update   # reinstalls into the venv and restarts the service
```

### Uninstalling

```bash
sudo make uninstall   # stops/disables service, removes venv and unit; config is left in place
sudo make purge       # same as uninstall, plus removes /etc/nut-up/ and the nut-up system user
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
  api_key: "your-secret"   # omit or set to null to disable the REST API entirely

web:
  username: "admin"        # HTTP Basic Auth for the browser UI
  password: "your-secret"  # omit or set to null to disable the web UI entirely

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
| `api.api_key` | string | `null` | `X-API-Key` value for REST API auth. Omit or set to `null` to disable the REST API. Must not be `changeme`. |
| `web.username` | string | `admin` | HTTP Basic Auth username for the browser UI |
| `web.password` | string | `null` | HTTP Basic Auth password for the browser UI. Omit or set to `null` to disable the web UI. Must not be `changeme`. |
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
ipmitool -I lanplus -H <ipmi_host> -U <ipmi_user> -P <ipmi_pass> power status
```

**After editing the config, restart the daemon to pick up the changes:**

```bash
sudo systemctl restart nut-up
```

---

## Web UI

The browser dashboard is served at `http://<host-ip>:8765/` and requires HTTP Basic Auth (the credentials from `web.username` / `web.password`).

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
| `Config error: api.api_key must be changed` | `api_key` or `web.password` is set to `changeme` | Set a real value, or remove the field to disable that interface |
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

- **Do not use `changeme` as a credential.** The daemon refuses to start if either `api.api_key` or `web.password` is set to `changeme`. Omit the field entirely to disable the interface instead.
- The API key is sent in plaintext over HTTP. If nut-up is exposed beyond your local network, put it behind a reverse proxy with TLS.
- For IPMI machines, create a dedicated BMC user with **Operator** or **Power User** role only — do not use the root/Administrator account. An Operator-role account can power on/off but cannot modify BMC configuration.
- The web UI and API share a port. There is no way to expose only the API without also exposing the UI; use firewall rules if you need to restrict browser access while allowing HA to reach the API.
- API documentation endpoints (`/docs`, `/redoc`, `/openapi.json`) are disabled.
