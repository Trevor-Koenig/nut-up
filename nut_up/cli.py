from __future__ import annotations

import json
import logging
import socket
import ssl
import sys
import urllib.request

import click

from .config import Config, ConfigError, load_config

DEFAULT_CONFIG = "/etc/nut-up/config.yaml"

config_option = click.option(
    "--config", default=DEFAULT_CONFIG, show_default=True, help="Path to config file"
)


def _load(path: str) -> Config:
    try:
        return load_config(path)
    except ConfigError as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)


@click.group()
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@main.command()
@config_option
def daemon(config: str) -> None:
    """Start the poll loop and API/web server."""
    from .daemon import run_daemon

    run_daemon(_load(config))


@main.command()
@config_option
def discover(config: str) -> None:
    """Discover NUT slave clients and print ready-to-paste YAML for config machines section."""
    cfg = _load(config)
    from .nut import NutClient, NutError

    try:
        with NutClient(cfg.nut) as client:
            client_ips = client.list_clients()
    except NutError as e:
        click.echo(f"NUT error: {e}", err=True)
        sys.exit(1)

    # Build IP → MAC map from the kernel ARP table
    arp: dict[str, str] = {}
    try:
        with open("/proc/net/arp") as f:
            next(f)  # skip header line
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                    arp[parts[0]] = parts[3].upper()
    except OSError:
        pass

    if not client_ips:
        click.echo("# No NUT slave clients found.", err=True)
        return

    click.echo("machines:")
    for ip in client_ips:
        mac = arp.get(ip, "UNKNOWN")
        try:
            hostname = socket.gethostbyaddr(ip)[0].split(".")[0]
        except OSError:
            hostname = f"machine-{ip.split('.')[-1]}"

        mac_note = "  # WARNING: not in ARP table — fill in manually" if mac == "UNKNOWN" else ""
        click.echo(f"  - name: {hostname}")
        click.echo(f"    mac: \"{mac}\"{mac_note}")
        click.echo(f"    ip: \"{ip}\"")
        click.echo("    wake_method: wol")
        click.echo("    # ups: ups  # tie to a specific UPS name; omit to wake on any")
        click.echo()


@main.command()
@click.argument("name")
@config_option
def wake(name: str, config: str) -> None:
    """Wake a machine by NAME (or 'all')."""
    cfg = _load(config)
    from .wake import wake_machines

    machines = cfg.machines if name == "all" else [m for m in cfg.machines if m.name == name]
    if not machines:
        click.echo(f"Unknown machine: {name!r}", err=True)
        sys.exit(1)
    if any(wake_machines(machines, "CLI").values()):
        sys.exit(1)


@main.command()
@config_option
def status(config: str) -> None:
    """Show current UPS and machine status from the running daemon."""
    cfg = _load(config)
    if cfg.api.api_key is None:
        click.echo("REST API is disabled — set api.api_key to use this command", err=True)
        sys.exit(1)

    host = "localhost" if cfg.api.host == "0.0.0.0" else cfg.api.host
    scheme = "https" if cfg.api.tls_cert else "http"
    url = f"{scheme}://{host}:{cfg.api.port}/api/status"
    ssl_ctx = None
    if cfg.api.tls_cert:
        # Trust only the daemon's own certificate (not the system CA store): with
        # hostname checks off, any publicly-trusted cert would otherwise pass.
        ssl_ctx = ssl.create_default_context(cafile=cfg.api.tls_cert)
        ssl_ctx.check_hostname = False

    try:
        req = urllib.request.Request(url, headers={"X-API-Key": cfg.api.api_key})
        with urllib.request.urlopen(req, timeout=5, context=ssl_ctx) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        click.echo(f"Cannot reach daemon at {url}: {e}", err=True)
        sys.exit(1)

    click.echo("\n=== UPS Status ===")
    for ups_name, us in data.get("ups", {}).items():
        click.echo(
            f"  {ups_name}: {us['state']}  raw={us['raw_status']!r}"
            f"  last_transition={us['last_transition']}"
        )

    click.echo("\n=== Machines ===")
    for m in data.get("machines", []):
        state = "Online " if m["online"] else "Offline"
        click.echo(f"  {m['name']:<20} {m['ip']:<18} {state}  [{m['wake_method']}]")

    if data.get("wake_in_progress"):
        click.echo("\n  *** Wake in progress ***")
    if data.get("last_wake_attempt"):
        click.echo(f"  Last wake attempt: {data['last_wake_attempt']}")

    updated = data.get("machine_states_updated") or "N/A"
    click.echo(f"\nMachine states as of: {updated}")


@main.command()
@config_option
def check(config: str) -> None:
    """Check NUT server connectivity, credentials, and IPMI BMC access."""
    click.echo("Checking configuration...")
    cfg = _load(config)
    click.echo(f"  {config}  OK")
    all_ok = True

    click.echo(f"\nChecking NUT server ({cfg.nut.host}:{cfg.nut.port})...")
    from .nut import NutClient, NutError

    try:
        with NutClient(cfg.nut) as client:
            statuses = client.get_all_statuses()
        for ups_name, data in statuses.items():
            click.echo(f"  {ups_name}: {data['status']!r}  OK")
    except NutError as e:
        click.echo(f"  {e}  FAIL", err=True)
        all_ok = False

    if cfg.machines:
        from .wake import WakeError, ipmitool

        click.echo("\nChecking machines...")
        for m in cfg.machines:
            if m.wake_method != "ipmi":
                click.echo(f"  {m.name} (wol) — WoL is fire-and-forget, no pre-flight check")
                continue
            click.echo(f"  {m.name} (ipmi) — querying chassis power status...")
            try:
                click.echo(f"    {ipmitool(m, 'chassis', 'power', 'status')}  OK")
            except WakeError as e:
                click.echo(f"    {e}  FAIL", err=True)
                all_ok = False

    click.echo()
    if not all_ok:
        click.echo("Some checks failed.", err=True)
        sys.exit(1)
    click.echo("All checks passed.")
