from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import uvicorn

from .config import Config
from .machine import check_all
from .nut import NutClient, NutConnectionError, NutError
from .wake import WakeError, wake_machine

logger = logging.getLogger("nut_up")


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
    was_on_battery: bool = False
    battery_start: Optional[float] = None
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
    wake_in_progress: bool = False
    nut_consecutive_failures: int = 0


def _parse_nut_flags(raw: str) -> tuple[bool, bool]:
    """Return (on_battery, low_battery) from a raw NUT status string."""
    flags = raw.upper().split()
    return "OB" in flags, "LB" in flags


def _next_state(us: UpsState, raw: str) -> UpsStateEnum | None:
    """Return the next UPS state or None to stay in the current state."""
    on_battery, low_battery = _parse_nut_flags(raw)
    cur = us.state

    if cur == UpsStateEnum.UNKNOWN:
        return UpsStateEnum.ON_BATTERY if on_battery else UpsStateEnum.ONLINE

    if cur == UpsStateEnum.ONLINE:
        return UpsStateEnum.ON_BATTERY if on_battery else None

    if cur == UpsStateEnum.ON_BATTERY:
        if low_battery:
            return UpsStateEnum.LOW_BATTERY
        if not on_battery:
            elapsed = time.time() - (us.battery_start or us.last_transition)
            if elapsed >= 10:
                return UpsStateEnum.RESTORING
        return None

    if cur == UpsStateEnum.LOW_BATTERY:
        if not on_battery:
            elapsed = time.time() - (us.battery_start or us.last_transition)
            if elapsed >= 10:
                return UpsStateEnum.RESTORING
        return None

    # RESTORING transitions back to ONLINE externally after wake_sequence completes
    return None


async def _wake_sequence(app: AppState, triggered_by: str) -> None:
    app.wake_in_progress = True
    app.last_wake_attempt = time.time()
    delay = app.config.wake_delay_seconds

    logger.info(
        "Wake sequence triggered by UPS %r — waiting %ds before waking machines",
        triggered_by,
        delay,
    )
    await asyncio.sleep(delay)

    for machine in app.config.machines:
        if machine.ups is not None and machine.ups != triggered_by:
            continue
        try:
            wake_machine(machine)
            logger.info("Woke %s (%s) via %s", machine.name, machine.ip, machine.wake_method)
        except WakeError as e:
            logger.error("Failed to wake %s: %s", machine.name, e)

    app.wake_in_progress = False

    # Transition RESTORING → ONLINE for the triggering UPS
    us = app.ups_states.get(triggered_by)
    if us is not None:
        us.state = UpsStateEnum.ONLINE
        us.last_transition = time.time()
        us.was_on_battery = False
        us.battery_start = None


def _query_nut(cfg: Config) -> dict[str, str]:
    """Blocking NUT query — called via asyncio.to_thread."""
    with NutClient(
        cfg.nut.host,
        cfg.nut.port,
        cfg.nut.username,
        cfg.nut.password,
        cfg.nut.ups_names,
    ) as client:
        return client.get_all_statuses()


async def _poll_loop(app: AppState) -> None:
    while True:
        # 1. Query NUT
        try:
            statuses = await asyncio.to_thread(_query_nut, app.config)
            app.nut_consecutive_failures = 0
        except NutConnectionError as e:
            app.nut_consecutive_failures += 1
            level = (
                logging.ERROR if app.nut_consecutive_failures >= 5 else logging.WARNING
            )
            logger.log(
                level,
                "NUT connection error (attempt %d): %s",
                app.nut_consecutive_failures,
                e,
            )
            statuses = {}
        except NutError as e:
            app.nut_consecutive_failures += 1
            logger.error("NUT error: %s", e)
            statuses = {}

        # 2. Update UPS state machine
        for ups_name, data in statuses.items():
            if ups_name not in app.ups_states:
                app.ups_states[ups_name] = UpsState()

            us = app.ups_states[ups_name]
            raw = data["status"]
            us.raw_status = raw
            us.battery_charge = data.get("battery_charge")
            us.battery_runtime = data.get("battery_runtime")
            us.ups_load = data.get("ups_load")
            new_state = _next_state(us, raw)

            if new_state is not None and new_state != us.state:
                logger.info(
                    "UPS %s: %s → %s (raw: %r)",
                    ups_name, us.state.name, new_state.name, raw,
                )
                old_state = us.state
                us.state = new_state
                us.last_transition = time.time()

                if new_state in (UpsStateEnum.ON_BATTERY, UpsStateEnum.LOW_BATTERY):
                    us.was_on_battery = True
                    if old_state in (UpsStateEnum.ONLINE, UpsStateEnum.UNKNOWN):
                        us.battery_start = time.time()

                if (
                    new_state == UpsStateEnum.RESTORING
                    and us.was_on_battery
                    and not app.wake_in_progress
                ):
                    asyncio.create_task(_wake_sequence(app, ups_name))

        # 3. Ping all machines concurrently
        try:
            app.machine_states = await check_all(app.config.machines)
            app.machine_states_updated = time.time()
        except Exception as e:
            logger.warning("Machine ping check failed: %s", e)

        await asyncio.sleep(15)


def run_daemon(cfg: Config) -> None:
    ups_states = {name: UpsState() for name in cfg.nut.ups_names}
    app_state = AppState(ups_states=ups_states, config=cfg)

    api_enabled = cfg.api.api_key is not None
    web_enabled = cfg.web.password is not None

    if not api_enabled:
        logger.info("REST API disabled — set api.api_key in config to enable")
    if not web_enabled:
        logger.info("Web UI disabled — set web.password in config to enable")

    from .api import create_api
    from .web import create_web

    api_app = create_api(app_state, api_enabled=api_enabled)

    separate_web_port = web_enabled and cfg.web.port is not None

    if web_enabled and not separate_web_port:
        web_app = create_web(app_state)
        api_app.mount("/", web_app)

    tls_kwargs: dict = {}
    if cfg.api.tls_cert:
        tls_kwargs = {"ssl_certfile": cfg.api.tls_cert, "ssl_keyfile": cfg.api.tls_key}
        logger.info("TLS enabled — serving HTTPS on port %d", cfg.api.port)

    uv_cfg = uvicorn.Config(
        api_app,
        host=cfg.api.host,
        port=cfg.api.port,
        log_level="info",
        access_log=True,
        **tls_kwargs,
    )
    server = uvicorn.Server(uv_cfg)

    servers = [server]

    if separate_web_port:
        web_app = create_web(app_state)
        web_uv_cfg = uvicorn.Config(
            web_app,
            host=cfg.api.host,
            port=cfg.web.port,
            log_level="info",
            access_log=True,
            **tls_kwargs,
        )
        servers.append(uvicorn.Server(web_uv_cfg))
        logger.info(
            "Web UI running on %s:%d (separate from API on port %d)",
            cfg.api.host,
            cfg.web.port,
            cfg.api.port,
        )

    async def _run() -> None:
        await asyncio.gather(
            _poll_loop(app_state),
            *[s.serve() for s in servers],
        )

    asyncio.run(_run())
