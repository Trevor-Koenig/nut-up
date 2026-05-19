from __future__ import annotations

import base64
import hmac
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .wake import WakeError, wake_machine

logger = logging.getLogger("nut_up")

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_PICO_URL = "https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css"
_HTMX_URL = "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"

_CSP = (
    "default-src 'self'; "
    f"script-src {_HTMX_URL}; "
    f"style-src {_PICO_URL}"
)


def create_web(app_state) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = _CSP
        return response

    def _check_basic_auth(request: Request) -> None:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                username, _, password = decoded.partition(":")
                if (
                    hmac.compare_digest(username, app_state.config.web.username)
                    and hmac.compare_digest(password, app_state.config.web.password)
                ):
                    return
            except Exception:
                pass
        host = request.client.host if request.client else "unknown"
        logger.warning("Web auth failure from %s", host)
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="nut-up"'},
        )

    def _build_context(request: Request) -> dict:
        now = time.time()
        ups_list = []
        for name, us in app_state.ups_states.items():
            restoring_countdown = None
            if us.state.name == "RESTORING":
                elapsed = now - us.last_transition
                restoring_countdown = max(
                    0, int(app_state.config.wake_delay_seconds - elapsed)
                )
            ups_list.append(
                {
                    "name": name,
                    "state": us.state.name,
                    "raw_status": us.raw_status,
                    "restoring_countdown": restoring_countdown,
                }
            )

        machines = [
            {
                "name": m.name,
                "ip": m.ip,
                "online": app_state.machine_states.get(m.name, False),
                "wake_method": m.wake_method,
            }
            for m in app_state.config.machines
        ]

        updated = None
        if app_state.machine_states_updated:
            updated = datetime.fromtimestamp(
                app_state.machine_states_updated, tz=timezone.utc
            ).strftime("%H:%M:%S UTC")

        return {
            "request": request,
            "ups_list": ups_list,
            "machines": machines,
            "machine_states_updated": updated,
            "wake_in_progress": app_state.wake_in_progress,
            "wake_error": None,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        _check_basic_auth(request)
        return templates.TemplateResponse("dashboard.html", _build_context(request))

    @app.get("/partials/status", response_class=HTMLResponse)
    async def partial_status(request: Request):
        _check_basic_auth(request)
        return templates.TemplateResponse("partials/status.html", _build_context(request))

    @app.post("/wake/{name}", response_class=HTMLResponse)
    async def wake_one(name: str, request: Request):
        _check_basic_auth(request)
        machine = next((m for m in app_state.config.machines if m.name == name), None)
        if machine is None:
            raise HTTPException(status_code=404, detail=f"Unknown machine: {name!r}")

        wake_error = None
        try:
            wake_machine(machine)
            logger.info("UI wake: %s", machine.name)
        except WakeError as e:
            logger.error("UI wake failed for %s: %s", machine.name, e)
            wake_error = str(e)

        ctx = {
            "request": request,
            "machine": {
                "name": machine.name,
                "ip": machine.ip,
                "online": app_state.machine_states.get(machine.name, False),
                "wake_method": machine.wake_method,
            },
            "wake_error": wake_error,
        }
        return templates.TemplateResponse("partials/machine_card.html", ctx)

    return app
