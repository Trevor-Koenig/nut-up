from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import uvicorn

from .config import Config
from .machine import check_all
from .nut import NutClient, NutConnectionError
from .wake import wake_machines

logger = logging.getLogger("nut_up")

# Honour X-Forwarded-For / X-Forwarded-Proto from any private-network peer so a
# reverse proxy on another LAN host (NPM, Traefik, Caddy, ...) works with no
# config. Client IP is only used for logging and scheme only for the cookie's
# Secure flag, so a LAN host forging these headers can only affect itself.
# FORWARDED_ALLOW_IPS (uvicorn's env var) overrides, e.g. for a public proxy.
_TRUSTED_PROXY_NETS = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",  # CGNAT / Tailscale
    "::1/128",
    "fc00::/7",
]

_POLL_INTERVAL = 15

# Strong references to running wake sequences — asyncio only keeps weak ones,
# so an unreferenced task can be garbage-collected mid-sleep.
_tasks: set[asyncio.Task] = set()


class UpsStateEnum(Enum):
    UNKNOWN = auto()
    ONLINE = auto()
    ON_BATTERY = auto()
    LOW_BATTERY = auto()
    RESTORING = auto()


@dataclass
class UpsState:
    state: UpsStateEnum = UpsStateEnum.UNKNOWN
    raw_status: str = ""
    last_transition: float = field(default_factory=time.time)
    battery_charge: Optional[int] = None
    battery_runtime: Optional[int] = None
    ups_load: Optional[int] = None


@dataclass
class AppState:
    ups_states: dict[str, UpsState]
    config: Config
    machine_states: dict[str, bool] = field(default_factory=dict)
    machine_states_updated: Optional[float] = None
    last_wake_attempt: Optional[float] = None
    nut_consecutive_failures: int = 0

    @property
    def wake_in_progress(self) -> bool:
        return any(us.state is UpsStateEnum.RESTORING for us in self.ups_states.values())


def _next_state(cur: UpsStateEnum, raw: str) -> UpsStateEnum:
    """Map a raw NUT status onto the state machine.

    Any poll that sees OB counts as an outage; the next poll without it enters
    RESTORING, which stays until its wake sequence finishes (or OB returns).
    """
    flags = raw.upper().split()
    if "OB" in flags:
        return UpsStateEnum.LOW_BATTERY if "LB" in flags else UpsStateEnum.ON_BATTERY
    if cur in (UpsStateEnum.ON_BATTERY, UpsStateEnum.LOW_BATTERY, UpsStateEnum.RESTORING):
        return UpsStateEnum.RESTORING
    return UpsStateEnum.ONLINE


async def _wake_sequence(app: AppState, ups_name: str) -> None:
    us = app.ups_states[ups_name]
    entered = us.last_transition
    delay = app.config.wake_delay_seconds
    logger.info("Power restored on UPS %r — waking machines in %ds", ups_name, delay)
    await asyncio.sleep(delay)

    # Power failed again during the delay (possibly restoring since): this run is stale
    if us.state is not UpsStateEnum.RESTORING or us.last_transition != entered:
        logger.warning("UPS %r changed state during wake delay — wake cancelled", ups_name)
        return
    app.last_wake_attempt = time.time()
    machines = [m for m in app.config.machines if m.ups in (None, ups_name)]
    try:
        await asyncio.to_thread(wake_machines, machines, f"UPS {ups_name}")
    finally:
        if us.state is UpsStateEnum.RESTORING and us.last_transition == entered:
            us.state = UpsStateEnum.ONLINE
            us.last_transition = time.time()


def _query_nut(cfg: Config) -> dict[str, dict]:
    """Blocking NUT query — called via asyncio.to_thread."""
    with NutClient(cfg.nut) as client:
        return client.get_all_statuses()


async def _poll_loop(app: AppState) -> None:
    while True:
        try:
            statuses = await asyncio.to_thread(_query_nut, app.config)
            app.nut_consecutive_failures = 0
        except Exception as e:  # anything — the poll loop must never die
            app.nut_consecutive_failures += 1
            transient = isinstance(e, NutConnectionError) and app.nut_consecutive_failures < 5
            logger.log(
                logging.WARNING if transient else logging.ERROR,
                "NUT query failed (attempt %d): %s",
                app.nut_consecutive_failures,
                e,
            )
            statuses = {}

        for ups_name, data in statuses.items():
            us = app.ups_states.setdefault(ups_name, UpsState())
            us.raw_status = data["status"]
            us.battery_charge = data["battery_charge"]
            us.battery_runtime = data["battery_runtime"]
            us.ups_load = data["ups_load"]
            new_state = _next_state(us.state, us.raw_status)
            if new_state is not us.state:
                logger.info(
                    "UPS %s: %s → %s (raw: %r)",
                    ups_name, us.state.name, new_state.name, us.raw_status,
                )
                us.state = new_state
                us.last_transition = time.time()
                if new_state is UpsStateEnum.RESTORING:
                    task = asyncio.create_task(_wake_sequence(app, ups_name))
                    _tasks.add(task)
                    task.add_done_callback(_tasks.discard)

        try:
            app.machine_states = await check_all(app.config.machines)
            app.machine_states_updated = time.time()
        except Exception as e:
            logger.warning("Machine ping check failed: %s", e)

        await asyncio.sleep(_POLL_INTERVAL)


def run_daemon(cfg: Config) -> None:
    app_state = AppState(ups_states={n: UpsState() for n in cfg.nut.ups_names}, config=cfg)

    api_enabled = cfg.api.api_key is not None
    web_enabled = cfg.web.password is not None
    if not api_enabled:
        logger.info("REST API disabled — set api.api_key in config to enable")
    if not web_enabled:
        logger.info("Web UI disabled — set web.password in config to enable")

    from .app import create_app

    separate_web_port = web_enabled and cfg.web.port is not None
    apps = [
        (cfg.api.port, create_app(app_state, api=api_enabled,
                                  web=web_enabled and not separate_web_port)),
    ]
    if separate_web_port:
        apps.append((cfg.web.port, create_app(app_state, api=False, web=True)))
        logger.info("Web UI on port %d (API on port %d)", cfg.web.port, cfg.api.port)

    server_kwargs: dict = {
        "forwarded_allow_ips": os.environ.get("FORWARDED_ALLOW_IPS") or _TRUSTED_PROXY_NETS,
        "server_header": False,
    }
    if cfg.api.tls_cert:
        server_kwargs["ssl_certfile"] = cfg.api.tls_cert
        server_kwargs["ssl_keyfile"] = cfg.api.tls_key
        logger.info("TLS enabled — serving HTTPS")

    servers = [
        uvicorn.Server(uvicorn.Config(
            app, host=cfg.api.host, port=port, log_level="info", access_log=True, **server_kwargs,
        ))
        for port, app in apps
    ]

    async def _run() -> None:
        await asyncio.gather(_poll_loop(app_state), *(s.serve() for s in servers))

    asyncio.run(_run())
