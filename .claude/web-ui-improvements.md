# Plan: Web UI Improvements

## Context
The current nut-up dashboard is functional but minimal: HTTP Basic Auth (browser native dialog), no status colors, no UPS metrics, and no feedback on wake actions. The user wants a proper login page, visual polish, UPS battery/load/runtime metrics, toast notifications on wake, and a Wake All button.

---

## Files to Create

### `nut_up/static/app.css`
Custom styles layered on top of Pico CSS:
- Status color CSS vars: `--nu-online`, `--nu-offline`, `--nu-battery`, `--nu-low-battery`, `--nu-restoring`
- `.nu-online`, `.nu-offline`, etc. color classes used in templates
- `#toast` fixed-position container + `@keyframes toast-in` / `toast-out` animations (auto-dismiss at 3s, no JS needed)
- `.login-card` centering for login page
- Progress bar styles for battery % display
- Metric pill styles (`.nu-metric`)

### `nut_up/templates/login.html`
Extends `base.html`. A centered card with:
- Username + password `<input>` fields
- Submit button "Log in"
- `{% if error %}<small>{{ error }}</small>{% endif %}` error display
- Form POSTs to `/login` (application/x-www-form-urlencoded)

### `nut_up/templates/partials/toast.html`
OOB partial for HTMX:
```html
<div id="toast" hx-swap-oob="true">
  <article class="nu-toast {% if success %}nu-toast-ok{% else %}nu-toast-err{% endif %}">
    {{ message }}
  </article>
</div>
```

---

## Files to Modify

### `nut_up/nut.py`
Add private helper and extend `get_all_statuses` to also fetch optional metrics in the **same connection**, keeping one NUT session per poll cycle.

```python
def _get_optional_var(self, ups_name: str, var_name: str) -> str | None:
    """Fetch a single var; return None on ERR (var not supported)."""
    self._send(f"GET VAR {ups_name} {var_name}")
    resp = self._readline()
    if resp.startswith("ERR "):
        return None
    parts = resp.split('"', 2)
    return parts[1] if len(parts) >= 2 else None

def get_all_statuses(self) -> dict[str, dict]:
    """Returns {ups_name: {status, battery_charge, battery_runtime, ups_load}}."""
    result: dict[str, dict] = {}
    for name in self._ups_names:
        self._send(f"GET VAR {name} ups.status")
        resp = self._readline()
        if resp.startswith("ERR "):
            raise NutProtocolError(f"GET VAR {name} ups.status: {resp[4:]}")
        parts = resp.split('"', 2)
        raw_status = parts[1] if len(parts) >= 2 else resp
        result[name] = {
            "status": raw_status,
            "battery_charge": _safe_int(self._get_optional_var(name, "battery.charge")),
            "battery_runtime": _safe_int(self._get_optional_var(name, "battery.runtime")),
            "ups_load": _safe_int(self._get_optional_var(name, "ups.load")),
        }
    return result
```

Add module-level helper:
```python
def _safe_int(val: str | None) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None
```

Return type of `get_all_statuses` changes from `dict[str, str]` → `dict[str, dict]`. Update the docstring accordingly.

### `nut_up/daemon.py`
**`UpsState` dataclass** — add three optional metric fields:
```python
battery_charge: Optional[int] = None   # percent, 0-100
battery_runtime: Optional[int] = None  # seconds
ups_load: Optional[int] = None         # percent, 0-100
```

**`_poll_loop`** — update the status loop from `for ups_name, raw in statuses.items()` to:
```python
for ups_name, data in statuses.items():
    raw = data["status"]
    us.raw_status = raw
    us.battery_charge = data.get("battery_charge")
    us.battery_runtime = data.get("battery_runtime")
    us.ups_load = data.get("ups_load")
    # existing state machine logic unchanged below...
```

### `nut_up/web.py`
Major restructure — remove Basic Auth, add session system, new routes.

**Imports to add:** `secrets`, `RedirectResponse`, `Response` (from starlette.responses)  
**Remove:** `base64` import (no longer needed)

**Session store** (inside `create_web` closure):
```python
_sessions: dict[str, float] = {}  # token → expiry timestamp
_SESSION_TTL = 86400  # 24h
```

**Helpers:**
```python
def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + _SESSION_TTL
    return token

def _check_session(request: Request) -> Response | None:
    """Return a redirect Response if not authenticated, else None."""
    token = request.cookies.get("session", "")
    if _sessions.get(token, 0) > time.time():
        return None  # valid
    _sessions.pop(token, None)  # clean expired
    host = request.client.host if request.client else "unknown"
    logger.warning("Web session failure from %s", host)
    if request.headers.get("HX-Request"):
        r = Response(status_code=204)
        r.headers["HX-Redirect"] = "/login"
        return r
    return RedirectResponse("/login", status_code=303)
```

**Replace all** `_check_basic_auth(request)` calls with:
```python
if redir := _check_session(request):
    return redir
```

**New routes:**
```python
@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if _check_session(request) is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})

@app.post("/login")
async def login_post(request: Request):
    body = await request.body()
    from urllib.parse import parse_qs
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
```

**`/wake/all` route** (register BEFORE `/wake/{name}` to avoid path conflict):
```python
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
```

**`/wake/{name}` route** — after wake attempt, append toast OOB:
```python
card_html = templates.get_template("partials/machine_card.html").render(**ctx)
toast_html = templates.get_template("partials/toast.html").render(
    success=(wake_error is None),
    message="Wake sent" if wake_error is None else wake_error,
)
return HTMLResponse(card_html + toast_html)
```

**`_build_context`** — add metrics + state label to each UPS dict:
```python
_STATE_LABELS = {
    "ONLINE": "Online", "ON_BATTERY": "On Battery",
    "LOW_BATTERY": "Low Battery", "RESTORING": "Restoring", "UNKNOWN": "Unknown",
}
# In ups_list.append:
ups_list.append({
    "name": name,
    "state": us.state.name,
    "state_label": _STATE_LABELS.get(us.state.name, us.state.name),
    "raw_status": us.raw_status,
    "restoring_countdown": restoring_countdown,
    "battery_charge": us.battery_charge,
    "battery_runtime": us.battery_runtime,
    "ups_load": us.ups_load,
})
```

### `nut_up/templates/base.html`
- Add `<link rel="stylesheet" href="/static/app.css">` after pico.min.css
- Add logout link to nav (right side): `<ul><li><a href="/logout">Log out</a></li></ul>`
- Add `<div id="toast"></div>` before `</body>`

### `nut_up/templates/dashboard.html`
Add a "Wake All" button above the `#status` div:
```html
<div style="margin-bottom:1rem">
  <button hx-post="/wake/all" hx-target="#status">Wake All</button>
</div>
```

### `nut_up/templates/partials/status.html`
**UPS card**: Replace plain text state with color-coded span + metric pills:
```html
<span class="nu-{{ ups.state|lower|replace('_','-') }}">● {{ ups.state_label }}</span>
{% if ups.battery_charge is not none %}<span class="nu-metric">{{ ups.battery_charge }}% battery</span>{% endif %}
{% if ups.ups_load is not none %}<span class="nu-metric">{{ ups.ups_load }}% load</span>{% endif %}
{% if ups.battery_runtime is not none %}<span class="nu-metric">{{ (ups.battery_runtime // 60) }}m remaining</span>{% endif %}
```
Remove the `<code>{{ ups.raw_status }}</code>` bullet.

### `nut_up/templates/partials/machine_card.html`
Replace inline `style=` on status indicator with CSS class:
```html
<span class="{% if machine.online %}nu-online{% else %}nu-offline{% endif %}">
  {% if machine.online %}● Online{% else %}○ Offline{% endif %}
</span>
```
Remove `style="margin:0"` from Wake button (handled by app.css).

---

## Verification

```bash
# 1. Browser hits http://<host>:8765 — redirects to /login (not Basic Auth dialog)
# 2. Login with wrong creds — shows "Invalid credentials" error inline
# 3. Login with correct creds — redirects to dashboard
# 4. Dashboard shows color-coded UPS state + battery/load/runtime if NUT exposes them
# 5. Click Wake on a machine — card updates + toast appears and fades
# 6. Click Wake All — all machine cards refresh + toast summarizes result
# 7. Click Log out — redirects back to /login, session cookie cleared
# 8. HTMX poll (15s) while logged out — full-page redirect to /login via HX-Redirect

sudo journalctl -u nut-up -f
curl -H "X-API-Key: yourkey" http://localhost:8765/api/status  # API unaffected
```

**Future:** Per-machine IPMI shutdown (`ipmitool chassis power soft`). WoL shutdown would require SSH config.

No new Python dependencies. Form parsing uses stdlib `urllib.parse`; session tokens use stdlib `secrets`.
