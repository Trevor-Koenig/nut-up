# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Start

At the start of every session, read [.claude/starting-plan.md](.claude/starting-plan.md) for the full project plan, module responsibilities, config schema, API contracts, and build order. All decisions about architecture and implementation should be grounded in that file unless the user says otherwise.

## File Generation Convention

All files you generate that are not part of the final deliverable (plans, scratch notes, design docs, checklists, task breakdowns, intermediate specs) must be saved to `.claude/`. Never create these files in the project root or any other directory.

## Project Overview

`nut-up` is a Python daemon (Raspberry Pi OS Bookworm, Python 3.11) that:
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
sudo make install          # first time
sudo make update           # after git pull

# Smoke tests
curl http://localhost:8765/health
curl -H "X-API-Key: yourkey" http://localhost:8765/api/status
curl -X POST -H "X-API-Key: yourkey" http://localhost:8765/api/wake/truenas

# Logs
sudo journalctl -u nut-up -f
```

## Architecture

```
nut_up/
├── config.py    # dataclasses + YAML load/validate/save; raises ConfigError
├── nut.py       # sync raw-socket NUT protocol client; raises NutError hierarchy
├── wake.py      # WoL (wakeonlan) + IPMI (ipmitool subprocess); raises WakeError
├── machine.py   # async ping-based online check via asyncio subprocesses
├── daemon.py    # 5-state UPS state machine + poll loop (15s) + wake orchestration
├── api.py       # FastAPI REST: /health, /api/status, /api/wake/{name|all}; X-API-Key auth
├── web.py       # FastAPI browser UI: /, /partials/status, /wake/{name}; HTTP Basic Auth
├── templates/   # base.html + dashboard.html; Pico CSS + HTMX from CDN
└── cli.py       # click entry point: daemon | discover | add | wake | status
deploy/
├── nut-up.service          # systemd unit (ExecStart=/opt/nut-up/bin/nut-up daemon)
└── config.example.yaml    # template for /etc/nut-up/config.yaml
```

**Key design facts:**
- `AppState` dataclass is shared between daemon and API — safe because asyncio is single-threaded
- Wake fires only when `was_on_battery=True` and outage lasted ≥10s (debounce, hardcoded)
- NUT client is synchronous; bridged into asyncio via `asyncio.to_thread`
- No PyNUT, no Pydantic — minimal footprint for Pi
- `/opt/nut-up/` venv + dedicated `nut-up` system user (no root at runtime)

See [.claude/starting-plan.md](.claude/starting-plan.md) for full module specs, config schema, API response shapes, HTMX wiring, HA integration snippets, and the recommended build order.
