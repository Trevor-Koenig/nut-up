from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import urllib.error
import urllib.request

import click

from .config import ConfigError, load_config

DEFAULT_CONFIG = "/etc/nut-up/config.yaml"


@click.group()
def main() -> None:
    pass


@main.command()
@click.option("--config", default=DEFAULT_CONFIG, show_default=True, help="Path to config file")
def daemon(config: str) -> None:
    """Start the poll loop and API/web server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(config)
    except ConfigError as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    from .daemon import run_daemon
    run_daemon(cfg)


@main.command()
@click.option("--config", default=DEFAULT_CONFIG, show_default=True, help="Path to config file")
def discover(config: str) -> None:
    """Discover NUT slave clients and print ready-to-paste YAML for config machines section."""
    try:
        cfg = load_config(config)
    except ConfigError as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    from .nut import NutClient, NutError

    try:
        with NutClient(
            cfg.nut.host,
            cfg.nut.port,
            cfg.nut.username,
            cfg.nut.password,
            cfg.nut.ups_names,
        ) as client:
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
        except Exception:
            octets = ip.split(".")
            hostname = f"machine-{octets[-1]}" if octets else ip

        mac_note = "  # WARNING: not in ARP table — fill in manually" if mac == "UNKNOWN" else ""
        click.echo(f"  - name: {hostname}")
        click.echo(f"    mac: \"{mac}\"{mac_note}")
        click.echo(f"    ip: \"{ip}\"")
        click.echo(f"    wake_method: wol")
        click.echo(f"    # ups: ups  # tie to a specific UPS name; omit to wake on any")
        click.echo()


@main.command()
@click.argument("name")
@click.option("--config", default=DEFAULT_CONFIG, show_default=True, help="Path to config file")
def wake(name: str, config: str) -> None:
    """Wake a machine by NAME (or 'all'). Calls daemon API; falls back to direct wake."""
    try:
        cfg = load_config(config)
    except ConfigError as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    host = "localhost" if cfg.api.host == "0.0.0.0" else cfg.api.host
    base_url = f"http://{host}:{cfg.api.port}"
    path = "/api/wake/all" if name == "all" else f"/api/wake/{name}"

    try:
        req = urllib.request.Request(
            f"{base_url}{path}",
            method="POST",
            headers={"X-API-Key": cfg.api.api_key},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        click.echo(f"Wake sent via daemon: {data}")
        return
    except Exception as e:
        click.echo(f"Daemon unreachable ({e}), trying direct wake...", err=True)

    from .wake import WakeError, wake_machine

    machines = (
        cfg.machines if name == "all" else [m for m in cfg.machines if m.name == name]
    )
    if not machines:
        click.echo(f"Unknown machine: {name!r}", err=True)
        sys.exit(1)

    for machine in machines:
        try:
            wake_machine(machine)
            click.echo(f"Woke {machine.name} via {machine.wake_method}")
        except WakeError as e:
            click.echo(f"Failed to wake {machine.name}: {e}", err=True)


@main.command()
@click.option("--config", default=DEFAULT_CONFIG, show_default=True, help="Path to config file")
def status(config: str) -> None:
    """Show current UPS and machine status from the running daemon."""
    try:
        cfg = load_config(config)
    except ConfigError as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    host = "localhost" if cfg.api.host == "0.0.0.0" else cfg.api.host
    base_url = f"http://{host}:{cfg.api.port}"

    try:
        req = urllib.request.Request(
            f"{base_url}/api/status",
            headers={"X-API-Key": cfg.api.api_key},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        click.echo(f"Cannot reach daemon at {base_url}: {e}", err=True)
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
@click.option("--config", default=DEFAULT_CONFIG, show_default=True, help="Path to config file")
def check(config: str) -> None:
    """Check NUT server connectivity, credentials, and IPMI BMC access."""
    all_ok = True

    click.echo("Checking configuration...")
    try:
        cfg = load_config(config)
        click.echo(f"  {config}  OK")
    except ConfigError as e:
        click.echo(f"  {e}  FAIL", err=True)
        sys.exit(1)

    click.echo(f"\nChecking NUT server ({cfg.nut.host}:{cfg.nut.port})...")
    from .nut import NutAuthError, NutClient, NutConnectionError, NutError

    try:
        with NutClient(
            cfg.nut.host,
            cfg.nut.port,
            cfg.nut.username,
            cfg.nut.password,
            cfg.nut.ups_names,
        ) as client:
            statuses = client.get_all_statuses()
        for ups_name, raw_status in statuses.items():
            click.echo(f"  {ups_name}: {raw_status!r}  OK")
    except NutAuthError as e:
        click.echo(f"  Auth failed: {e}  FAIL", err=True)
        all_ok = False
    except NutConnectionError as e:
        click.echo(f"  Connection failed: {e}  FAIL", err=True)
        all_ok = False
    except NutError as e:
        click.echo(f"  NUT error: {e}  FAIL", err=True)
        all_ok = False

    if cfg.machines:
        click.echo("\nChecking machines...")
        for m in cfg.machines:
            if m.wake_method == "ipmi":
                click.echo(f"  {m.name} (ipmi) — querying chassis power status...")
                cmd = [
                    "ipmitool", "-I", "lanplus",
                    "-H", m.ipmi_host,
                    "-U", m.ipmi_user,
                    "-P", m.ipmi_pass,
                    "chassis", "power", "status",
                ]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                    if result.returncode == 0:
                        click.echo(f"    {result.stdout.strip()}  OK")
                    else:
                        click.echo(
                            f"    exit {result.returncode}: {result.stderr.strip()}  FAIL",
                            err=True,
                        )
                        all_ok = False
                except FileNotFoundError:
                    click.echo(
                        "    ipmitool not found — run: apt install ipmitool  FAIL", err=True
                    )
                    all_ok = False
                except subprocess.TimeoutExpired:
                    click.echo("    ipmitool timed out  FAIL", err=True)
                    all_ok = False
            else:
                click.echo(f"  {m.name} (wol) — WoL is fire-and-forget, no pre-flight check")

    click.echo()
    if all_ok:
        click.echo("All checks passed.")
    else:
        click.echo("Some checks failed.", err=True)
        sys.exit(1)
