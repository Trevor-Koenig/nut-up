# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Start

At the start of every session, read [.claude/starting-plan.md](.claude/starting-plan.md) for module responsibilities, config schema, API contracts, and design rationale. The project is fully built; use it as a reference for intent and invariants, not build order.

## File Generation Convention

All files you generate that are not part of the final deliverable (plans, scratch notes, design docs, checklists, task breakdowns, intermediate specs) must be saved to `.claude/`. Never create these files in the project root or any other directory.

## Project Overview

`nut-up` is a Python daemon (any systemd-based Linux, Python ≥3.10) that:
- Monitors a NUT server for UPS power-restore events
- Wakes managed machines via WoL or IPMI after power is restored
- Exposes a REST API (FastAPI) for Home Assistant integration
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
sudo nutup update          # git pull + reinstall + restart (from anywhere after install)
sudo nutup test            # connectivity check: NUT auth, UPS names, IPMI BMCs

# Smoke tests
curl http://localhost:8765/health
curl -H "X-API-Key: yourkey" http://localhost:8765/api/status
curl -X POST -H "X-API-Key: yourkey" http://localhost:8765/api/wake/truenas

# Logs
sudo journalctl -u nut-up -f
```

## Architecture

```
nutup            # management CLI: install | update | test | uninstall | purge | help
                 # installed to /usr/local/bin/nutup with repo path baked in by make install
nut_up/
├── config.py    # dataclasses + YAML load/validate/save; raises ConfigError
├── nut.py       # sync raw-socket NUT protocol client; raises NutError hierarchy
├── wake.py      # WoL (wakeonlan) + IPMI (ipmitool subprocess); raises WakeError
├── machine.py   # async ping-based online check via asyncio subprocesses
├── daemon.py    # 5-state UPS state machine + poll loop (15s) + wake orchestration
├── api.py       # FastAPI REST: /health, /api/status, /api/wake/{name|all}; X-API-Key auth
├── web.py       # FastAPI browser UI: /, /partials/status, /wake/{name}; HTTP Basic Auth
├── templates/   # base.html, dashboard.html; partials/status.html, partials/machine_card.html
└── cli.py       # click entry point: daemon | discover | wake | status | check
deploy/
├── nut-up.service          # systemd unit (ExecStart=/opt/nut-up/bin/nut-up daemon)
└── config.example.yaml    # template for /etc/nut-up/config.yaml
```

**Key design facts:**
- `AppState` dataclass is shared between daemon and API — safe because asyncio is single-threaded
- Wake fires only when `was_on_battery=True` and outage lasted ≥10s (debounce, hardcoded)
- NUT client is synchronous; bridged into asyncio via `asyncio.to_thread`
- No PyNUT, no Pydantic — minimal footprint
- `/opt/nut-up/` venv + dedicated `nut-up` system user (no root at runtime)
- Web app (`create_web`) is mounted onto the API app at `/`; both share one uvicorn server
- Security headers (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy) added as middleware in both `api.py` and `web.py`; CSP allowlists only the Pico CSS and HTMX CDN URLs
- `AppState.nut_consecutive_failures` counts back-to-back NUT errors; escalates from WARNING to ERROR at 5
- Web context includes `restoring_countdown` (seconds until wake fires, computed from `wake_delay_seconds - elapsed`)
- `config.py` exposes `save_config()` for writing back to YAML (comments not preserved)
- `api.api_key = None` disables REST API; `web.password = None` disables browser UI — both checked in `run_daemon`

See [.claude/starting-plan.md](.claude/starting-plan.md) for full module specs, config schema, API response shapes, and HTMX wiring.
