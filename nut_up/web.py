from __future__ import annotations

import hmac
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .wake import WakeError, wake_machine

logger = logging.getLogger("nut_up")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

_CSP = "default-src 'self'"

_STATE_LABELS = {
    "ONLINE": "Online",
    "ON_BATTERY": "On Battery",
    "LOW_BATTERY": "Low Battery",
    "RESTORING": "Restoring",
    "UNKNOWN": "Unknown",
}


def create_web(app_state) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    _sessions: dict[str, float] = {}
    _SESSION_TTL = 86400

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = _CSP
        return response

    def _create_session() -> str:
        token = secrets.token_urlsafe(32)
        _sessions[token] = time.time() + _SESSION_TTL
        return token

    def _check_session(request: Request) -> Response | None:
        token = request.cookies.get("session", "")
        if _sessions.get(token, 0) > time.time():
            return None
        _sessions.pop(token, None)
        host = request.client.host if request.client else "unknown"
        logger.warning("Web session failure from %s", host)
        if request.headers.get("HX-Request"):
            r = Response(status_code=204)
            r.headers["HX-Redirect"] = "/login"
            return r
        return RedirectResponse("/login", status_code=303)

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
                    "state_label": _STATE_LABELS.get(us.state.name, us.state.name),
                    "raw_status": us.raw_status,
                    "restoring_countdown": restoring_countdown,
                    "battery_charge": us.battery_charge,
                    "battery_runtime": us.battery_runtime,
                    "ups_load": us.ups_load,
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
            "ups_list": ups_list,
            "machines": machines,
            "machine_states_updated": updated,
            "wake_in_progress": app_state.wake_in_progress,
            "wake_error": None,
        }

    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request):
        if _check_session(request) is None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    async def login_post(request: Request):
        body = await request.body()
        params = parse_qs(body.decode("utf-8", errors="replace"), strict_parsing=False)
        username = params.get("username", [""])[0]
        password = params.get("password", [""])[0]
        cfg = app_state.config.web
        if (
            hmac.compare_digest(username, cfg.username)
            and hmac.compare_digest(password, cfg.password or "")
        ):
            token = _create_session()
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie("session", token, httponly=True, samesite="strict", path="/")
            return resp
        host = request.client.host if request.client else "unknown"
        logger.warning("Web login failure from %s", host)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid credentials"}, status_code=401
        )

    @app.get("/logout")
    async def logout(request: Request):
        token = request.cookies.get("session", "")
        _sessions.pop(token, None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("session", path="/")
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if redir := _check_session(request):
            return redir
        return templates.TemplateResponse(request, "dashboard.html", _build_context(request))

    @app.get("/partials/status", response_class=HTMLResponse)
    async def partial_status(request: Request):
        if redir := _check_session(request):
            return redir
        return templates.TemplateResponse(request, "partials/status.html", _build_context(request))

    @app.post("/wake/all", response_class=HTMLResponse)
    async def wake_all(request: Request):
        if redir := _check_session(request):
            return redir
        errors = []
        for machine in app_state.config.machines:
            try:
                wake_machine(machine)
                logger.info("UI wake-all: %s", machine.name)
            except WakeError as e:
                logger.error("UI wake-all failed for %s: %s", machine.name, e)
                errors.append(f"{machine.name}: {e}")
        ctx = _build_context(request)
        status_html = templates.get_template("partials/status.html").render(**ctx)
        toast_html = templates.get_template("partials/toast.html").render(
            success=not errors,
            message="All machines woken" if not errors else "; ".join(errors),
        )
        return HTMLResponse(status_html + toast_html)

    @app.post("/wake/{name}", response_class=HTMLResponse)
    async def wake_one(name: str, request: Request):
        if redir := _check_session(request):
            return redir
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
            "machine": {
                "name": machine.name,
                "ip": machine.ip,
                "online": app_state.machine_states.get(machine.name, False),
                "wake_method": machine.wake_method,
            },
            "wake_error": wake_error,
        }
        card_html = templates.get_template("partials/machine_card.html").render(**ctx)
        toast_html = templates.get_template("partials/toast.html").render(
            success=(wake_error is None),
            message="Wake sent" if wake_error is None else wake_error,
        )
        return HTMLResponse(card_html + toast_html)

    return app
