# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Start

At the start of every session, read [.claude/starting-plan.md](.claude/starting-plan.md) for module responsibilities, config schema, API contracts, and design rationale. The project is fully built; use it as a reference for intent and invariants, not build order. Where it conflicts with this file (it predates the FastAPI → Starlette move and the api.py/web.py → app.py merge), this file wins.

## File Generation Convention

All files you generate that are not part of the final deliverable (plans, scratch notes, design docs, checklists, task breakdowns, intermediate specs) must be saved to `.claude/`. Never create these files in the project root or any other directory.

## Project Overview

`nut-up` is a Python daemon (any systemd-based Linux, Python ≥3.10) that:
- Monitors a NUT server for UPS power-restore events
- Wakes managed machines via WoL or IPMI after power is restored
- Exposes a REST API (Starlette) for Home Assistant integration
- Serves a browser UI (Jinja2 + HTMX + Pico CSS, no build step)

## Development Commands

```bash
# Local dev (editable install)
python -m venv .venv && source .venv/bin/activate
pip install -e .
nut-up discover

# Lint / format
ruff check .
ruff format .

# Pi deployment
sudo ./nutup install       # first time (from repo dir)
nutup update               # git pull + reinstall + restart (auto-escalates to sudo)
nutup test                 # connectivity check: NUT auth, UPS names, IPMI BMCs
nutup wake truenas         # manually wake a machine (auto-escalates to sudo)
nutup status               # show UPS and machine status from running daemon
nutup lock                 # regenerate requirements.lock from the installed venv

# Smoke tests
curl http://localhost:8765/health
curl -H "X-API-Key: yourkey" http://localhost:8765/api/status
curl -X POST -H "X-API-Key: yourkey" http://localhost:8765/api/wake/truenas

# Logs
sudo journalctl -u nut-up -f
```

## Architecture

```
nutup            # unified CLI: all commands auto-escalate to sudo if not run as root
                 # system: install | update | test | lock | uninstall | purge | help (delegated to make)
                 # runtime: daemon | discover | wake <name|all> | status (delegated to nut-up venv binary)
                 # installed to /usr/local/bin/nutup with repo path baked in by make install
                 # unknown commands print help and exit non-zero
nut_up/
├── config.py    # dataclasses + strict YAML load/validate; raises ConfigError
├── nut.py       # sync raw-socket NUT protocol client; raises NutError / NutConnectionError
├── wake.py      # WoL (stdlib UDP) + IPMI (ipmitool subprocess); wake_machines(); raises WakeError
├── machine.py   # async ping-based online check via asyncio subprocesses
├── daemon.py    # 5-state UPS state machine + poll loop (15s) + wake orchestration + uvicorn setup
├── app.py       # Starlette app: REST (/health, /api/status, /api/wake/{name|all}; X-API-Key)
│                #   + browser UI (/, /login, /partials/status, /wake/{name|all}; session cookie)
├── templates/   # base.html, dashboard.html, login.html; partials/{status,machine_card,toast}.html
└── cli.py       # click entry point: daemon | discover | wake | status | check
deploy/
├── nut-up.service          # systemd unit (ExecStart=/opt/nut-up/bin/nut-up daemon)
└── config.example.yaml    # template for /etc/nut-up/config.yaml
```

**Key design facts:**
- `AppState` dataclass is shared between daemon and API — safe because asyncio is single-threaded
- State machine: any poll seeing `OB` → ON_BATTERY/LOW_BATTERY; the next poll without it → RESTORING, which starts a per-UPS `_wake_sequence` task. After `wake_delay_seconds` it wakes that UPS's machines (plus `ups: null` ones) only if the UPS is still in the *same* RESTORING episode (`last_transition` unchanged) — a re-failure during the delay cancels it. Tasks are held in `daemon._tasks` (asyncio keeps only weak refs). `AppState.wake_in_progress` is derived: any UPS in RESTORING
- `wake_machine()` passes `-L OPERATOR` to ipmitool — OPERATOR is the required privilege level; ADMINISTRATOR is neither needed nor requested
- `nutup wake` always wakes directly (no daemon round-trip); every wake path (UPS, API, UI, CLI) goes through `wake.wake_machines()`, which logs and returns `{name: error|None}`. HTTP handlers and the daemon call it via `asyncio.to_thread` because ipmitool can block 15s
- NUT client is synchronous; bridged into asyncio via `asyncio.to_thread`
- Minimal footprint: no FastAPI/Pydantic (plain Starlette), no PyNUT, no `wakeonlan` (magic packet is 3 lines in `wake.py`), plain `uvicorn` without `[standard]` extras
- `/opt/nut-up/` venv + dedicated `nut-up` system user (no root at runtime)
- `create_app(state, api=, web=)` builds one Starlette app; by default API + UI share one uvicorn server on `api.port`. When `web.port` is set, `run_daemon` runs two servers: `create_app(api=True, web=False)` and `create_app(api=False, web=True)`. `/health` is always present
- `app._security` middleware: CSP `default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'` (so no inline `style=`/scripts — put styles in `static/app.css`), X-Frame-Options, nosniff, Referrer-Policy, `Cache-Control: no-store` on everything except `/static/`. It also rejects POSTs whose `Sec-Fetch-Site` is not `same-origin`/`none` (CSRF; `same-site` is rejected too since sibling subdomains behind a proxy are same-site; non-browser clients send no header and pass). Pico CSS and HTMX are vendored under `nut_up/static/`; HTMX eval/script-tag execution/history cache are disabled via the `htmx-config` meta tag in `base.html`
- Credentials are compared with `hmac.compare_digest` on **bytes** (`app._eq`) — on str it raises TypeError for non-ASCII input. Login body is capped at 4 KB
- `AppState.nut_consecutive_failures` counts back-to-back NUT errors; escalates from WARNING to ERROR at 5
- Web context includes `restoring_countdown` (seconds until wake fires, computed from `wake_delay_seconds - elapsed`)
- The daemon never writes its config: `/etc/nut-up` (config + TLS key) is `root:nut-up`, group-readable only
- `api.api_key = None` disables REST API; `web.password = None` disables browser UI — both checked in `run_daemon`
- `web.port` (optional) runs the browser UI on a separate port from the API; validation rejects values outside 1–65535 and equal to `api.port`
- `config._section()` builds each dataclass from YAML, rejecting unknown keys (typos would otherwise silently disable things) and wrong types (checked against the field annotation strings — keep annotations to `str`, `int`, `list[str]`, `Optional[...]` of those); blank values fall back to defaults, so `api_key:` means disabled. `_validate` strict-checks UPS and machine names (`^[A-Za-z0-9_.-]+$`), machine `ip`/`broadcast` via `ipaddress.ip_address`, `ups` references, and `ipmi_host` against a hostname regex that forbids a leading `-`; rationale is to keep these values un-injectable into the line-based NUT protocol, argv for `ping`/`ipmitool`, and URL paths/HTML ids
- `machine.py` passes `--` before `ip` to `ping` as defence-in-depth against argv-flag injection
- Supply chain: `pyproject.toml` caps direct deps with upper bounds and sets lower bounds past known CVEs (`h11` is listed explicitly for that reason — `pip install .` won't upgrade a transitive dep that already satisfies its parent); `make install` / `make update` prefer `requirements.lock` (committed) over PyPI resolution when present; `make lock` (a.k.a. `nutup lock`) regenerates it from the installed venv. Pico CSS and HTMX are vendored in [nut_up/static/](nut_up/static/) — to bump either, fetch the npm tarball, check it against the registry's `dist.integrity` sha512, and copy the file in place; no SRI hash to recompute (the asset is same-origin so SRI is omitted)
- Reverse-proxy friendly with zero config: `daemon._TRUSTED_PROXY_NETS` (private ranges) is passed to uvicorn as `forwarded_allow_ips` (CIDR needs uvicorn ≥0.31), so `request.client` / `request.url.scheme` reflect `X-Forwarded-For` / `-Proto`; the `FORWARDED_ALLOW_IPS` env var overrides. The session cookie's `Secure` flag follows the scheme. Templates and redirects use relative URLs (`app._rel`, based on the raw `scope["path"]`, not `request.url`) so prefix-stripping subpath proxies work — don't add root-absolute `/...` URLs to templates
- `nut.py._cmd` echoes only the protocol verb (not the full command) in `NutError`, so a non-auth `ERR` after `PASSWORD <secret>` cannot leak the secret into the journal
- systemd unit is sandboxed (`systemd-analyze security` ≈ 3.1). Those options imply NoNewPrivileges, which makes the kernel ignore ping's `cap_net_raw` file capability, so the unit grants `CAP_NET_RAW` as its only ambient capability — don't drop it or every machine shows offline
- `make _tls` (run by install and update) regenerates the self-signed cert (EC P-256, SAN incl. localhost) when missing or within 30 days of expiry. `nutup status` pins it: the CLI's SSL context trusts only `api.tls_cert`, never the system CA store, because hostname checking is off

See [.claude/starting-plan.md](.claude/starting-plan.md) for full module specs, config schema, API response shapes, and HTMX wiring.
