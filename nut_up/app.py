"""REST API (X-API-Key) and browser UI (session cookie) on one Starlette app."""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .wake import wake_machines

logger = logging.getLogger("nut_up")

_PKG = Path(__file__).parent
_SESSION_TTL = 86400
_MAX_LOGIN_BODY = 4096
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


async def _security(request: Request, call_next) -> Response:
    # CSRF: browsers label cross-site requests; "same-site" is rejected too, because
    # behind a reverse proxy every sibling subdomain counts as the same site.
    # Non-browser clients (Home Assistant, curl) send no Sec-Fetch-Site and pass.
    if request.method == "POST" and request.headers.get(
        "sec-fetch-site", "same-origin"
    ) not in ("same-origin", "none"):
        response = PlainTextResponse("Cross-site request blocked", 403)
    else:
        response = await call_next(request)
    response.headers.update(_SECURITY_HEADERS)
    if not request.scope["path"].startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _eq(a: str, b: str) -> bool:
    # bytes, not str: compare_digest raises TypeError on non-ASCII str input
    return hmac.compare_digest(a.encode(), b.encode())


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(request: Request, target: str) -> str:
    # Redirect relative to the request path rather than to "/<target>", so
    # the UI keeps working when a reverse proxy serves it under a subpath.
    depth = request.scope["path"].count("/") - 1
    return "../" * depth + target or "./"


def create_app(app_state, *, api: bool, web: bool) -> Starlette:
    cfg = app_state.config
    templates = Jinja2Templates(directory=str(_PKG / "templates"))
    sessions: dict[str, float] = {}

    def machine_view(m) -> dict:
        return {
            "name": m.name,
            "ip": m.ip,
            "online": app_state.machine_states.get(m.name, False),
            "wake_method": m.wake_method,
        }

    def find_machine(name: str):
        return next((m for m in cfg.machines if m.name == name), None)

    async def wake(machines, source: str) -> dict[str, str | None]:
        # ipmitool can block for up to 15s per machine — keep the event loop free
        return await asyncio.to_thread(wake_machines, machines, source)

    # --- REST API ---------------------------------------------------------

    def api_route(handler):
        async def checked(request: Request) -> Response:
            if not _eq(request.headers.get("x-api-key", ""), cfg.api.api_key):
                logger.warning("API auth failure from %s", _client(request))
                return JSONResponse({"detail": "Invalid or missing X-API-Key"}, 401)
            return await handler(request)
        return checked

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @api_route
    async def api_status(request: Request) -> Response:
        return JSONResponse({
            "ups": {
                name: {
                    "state": us.state.name,
                    "raw_status": us.raw_status,
                    "last_transition": _utc_iso(us.last_transition),
                }
                for name, us in app_state.ups_states.items()
            },
            "machines": [machine_view(m) for m in cfg.machines],
            "machine_states_updated": _utc_iso(app_state.machine_states_updated),
            "wake_in_progress": app_state.wake_in_progress,
            "last_wake_attempt": _utc_iso(app_state.last_wake_attempt),
        })

    @api_route
    async def api_wake_all(request: Request) -> Response:
        results = await wake(cfg.machines, "API")
        return JSONResponse({"results": [
            {"name": n, "status": "ok"} if err is None
            else {"name": n, "status": "error", "message": err}
            for n, err in results.items()
        ]})

    @api_route
    async def api_wake_one(request: Request) -> Response:
        name = request.path_params["name"]
        machine = find_machine(name)
        if machine is None:
            return JSONResponse({"detail": f"Unknown machine: {name!r}"}, 404)
        err = (await wake([machine], "API"))[name]
        if err is not None:
            return JSONResponse({"detail": err}, 500)
        return JSONResponse({"status": "ok", "name": name})

    # --- Browser UI -------------------------------------------------------

    def logged_in(request: Request) -> bool:
        token = request.cookies.get("session", "")
        if sessions.get(token, 0) > time.time():
            return True
        if token:
            sessions.pop(token, None)
            logger.warning("Web session failure from %s", _client(request))
        return False

    def web_route(handler):
        async def checked(request: Request) -> Response:
            if logged_in(request):
                return await handler(request)
            if request.headers.get("HX-Request"):
                # HTMX resolves HX-Redirect against the page URL (the dashboard)
                return Response(status_code=204, headers={"HX-Redirect": "login"})
            return RedirectResponse(_rel(request, "login"), status_code=303)
        return checked

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def fragment(name: str, **ctx) -> str:
        return templates.get_template(name).render(**ctx)

    def status_context() -> dict:
        now = time.time()
        ups_list = []
        for name, us in app_state.ups_states.items():
            countdown = None
            if us.state.name == "RESTORING":
                countdown = max(0, int(cfg.wake_delay_seconds - (now - us.last_transition)))
            ups_list.append({
                "name": name,
                "state": us.state.name,
                "restoring_countdown": countdown,
                "battery_charge": us.battery_charge,
                "battery_runtime": us.battery_runtime,
                "ups_load": us.ups_load,
            })
        updated = None
        if app_state.machine_states_updated:
            updated = datetime.fromtimestamp(
                app_state.machine_states_updated
            ).astimezone().strftime("%H:%M:%S %Z")
        return {
            "ups_list": ups_list,
            "machines": [machine_view(m) for m in cfg.machines],
            "machine_states_updated": updated,
        }

    async def login(request: Request) -> Response:
        if request.method == "GET":
            if logged_in(request):
                return RedirectResponse(_rel(request, ""), status_code=303)
            return render(request, "login.html", error=None)

        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > _MAX_LOGIN_BODY:
                return PlainTextResponse("Request too large", 413)
        params = parse_qs(body.decode("utf-8", errors="replace"))
        username = params.get("username", [""])[0]
        password = params.get("password", [""])[0]
        # & (not `and`) so a wrong username takes as long as a wrong password
        if _eq(username, cfg.web.username) & _eq(password, cfg.web.password):
            now = time.time()
            for t in [t for t, exp in sessions.items() if exp <= now]:
                del sessions[t]
            token = secrets.token_urlsafe(32)
            sessions[token] = now + _SESSION_TTL
            logger.info("Web login from %s", _client(request))
            resp = RedirectResponse(_rel(request, ""), status_code=303)
            # scheme reflects X-Forwarded-Proto when behind a trusted proxy
            resp.set_cookie(
                "session",
                token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                path="/",
            )
            return resp
        logger.warning("Web login failure from %s", _client(request))
        return render(request, "login.html", 401, error="Invalid credentials")

    async def logout(request: Request) -> Response:
        sessions.pop(request.cookies.get("session", ""), None)
        resp = RedirectResponse(_rel(request, "login"), status_code=303)
        resp.delete_cookie("session", path="/")
        return resp

    @web_route
    async def dashboard(request: Request) -> Response:
        return render(request, "dashboard.html", **status_context())

    @web_route
    async def partial_status(request: Request) -> Response:
        return render(request, "partials/status.html", **status_context())

    @web_route
    async def web_wake_all(request: Request) -> Response:
        errors = [f"{n}: {e}" for n, e in (await wake(cfg.machines, "UI")).items() if e]
        return HTMLResponse(
            fragment("partials/status.html", **status_context())
            + fragment("partials/toast.html", success=not errors,
                       message="; ".join(errors) or "All machines woken")
        )

    @web_route
    async def web_wake_one(request: Request) -> Response:
        machine = find_machine(request.path_params["name"])
        if machine is None:
            return PlainTextResponse("Unknown machine", 404)
        err = (await wake([machine], "UI"))[machine.name]
        return HTMLResponse(
            fragment("partials/machine_card.html", machine=machine_view(machine), wake_error=err)
            + fragment("partials/toast.html", success=err is None, message=err or "Wake sent")
        )

    routes = [Route("/health", health)]
    if api:
        routes += [
            Route("/api/status", api_status),
            Route("/api/wake/all", api_wake_all, methods=["POST"]),
            Route("/api/wake/{name}", api_wake_one, methods=["POST"]),
        ]
    if web:
        routes += [
            Route("/", dashboard),
            Route("/login", login, methods=["GET", "POST"]),
            Route("/logout", logout),
            Route("/partials/status", partial_status),
            Route("/wake/all", web_wake_all, methods=["POST"]),
            Route("/wake/{name}", web_wake_one, methods=["POST"]),
            Mount("/static", StaticFiles(directory=str(_PKG / "static")), name="static"),
        ]
    return Starlette(routes=routes, middleware=[Middleware(BaseHTTPMiddleware, dispatch=_security)])
