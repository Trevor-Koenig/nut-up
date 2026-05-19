from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request

from .wake import WakeError, wake_machine

logger = logging.getLogger("nut_up")

_PICO_URL = "https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css"
_HTMX_URL = "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"

_CSP = (
    "default-src 'self'; "
    f"script-src {_HTMX_URL}; "
    f"style-src {_PICO_URL}"
)


def _utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_api(app_state, *, api_enabled: bool = True) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = _CSP
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    if api_enabled:
        def _check_api_key(request: Request) -> None:
            key = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(key, app_state.config.api.api_key):
                host = request.client.host if request.client else "unknown"
                logger.warning("API auth failure from %s", host)
                raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

        @app.get("/api/status")
        async def status(request: Request):
            _check_api_key(request)
            ups_data = {}
            for name, us in app_state.ups_states.items():
                ups_data[name] = {
                    "state": us.state.name,
                    "raw_status": us.raw_status,
                    "last_transition": _utc_iso(us.last_transition),
                }
            machines = [
                {
                    "name": m.name,
                    "ip": m.ip,
                    "online": app_state.machine_states.get(m.name, False),
                    "wake_method": m.wake_method,
                }
                for m in app_state.config.machines
            ]
            return {
                "ups": ups_data,
                "machines": machines,
                "machine_states_updated": _utc_iso(app_state.machine_states_updated),
                "wake_in_progress": app_state.wake_in_progress,
                "last_wake_attempt": _utc_iso(app_state.last_wake_attempt),
            }

        @app.post("/api/wake/all")
        async def wake_all(request: Request):
            _check_api_key(request)
            results = []
            for machine in app_state.config.machines:
                try:
                    wake_machine(machine)
                    logger.info("Manual wake (all): %s", machine.name)
                    results.append({"name": machine.name, "status": "ok"})
                except WakeError as e:
                    logger.error("Manual wake (all) failed for %s: %s", machine.name, e)
                    results.append({"name": machine.name, "status": "error", "message": str(e)})
            return {"results": results}

        @app.post("/api/wake/{name}")
        async def wake_one(name: str, request: Request):
            _check_api_key(request)
            machine = next((m for m in app_state.config.machines if m.name == name), None)
            if machine is None:
                raise HTTPException(status_code=404, detail=f"Unknown machine: {name!r}")
            try:
                wake_machine(machine)
                logger.info("Manual wake: %s", machine.name)
                return {"status": "ok", "name": name}
            except WakeError as e:
                logger.error("Manual wake failed for %s: %s", machine.name, e)
                raise HTTPException(status_code=500, detail=str(e))

    return app
