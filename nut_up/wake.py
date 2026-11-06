from __future__ import annotations

import ipaddress
import logging
import os
import socket
import subprocess

from .config import MachineConfig

logger = logging.getLogger("nut_up")


class WakeError(RuntimeError):
    pass


def ipmitool(machine: MachineConfig, *args: str) -> str:
    """Run an ipmitool lanplus command against the machine's BMC; return stdout."""
    cmd = [
        "ipmitool", "-I", "lanplus",
        "-H", machine.ipmi_host,
        "-U", machine.ipmi_user,
        "-E",  # password from IPMI_PASSWORD, never argv
        "-L", "OPERATOR",
        *args,
    ]
    env = {**os.environ, "IPMI_PASSWORD": machine.ipmi_pass}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    except FileNotFoundError:
        raise WakeError("ipmitool not found — run: apt install ipmitool")
    except subprocess.TimeoutExpired:
        raise WakeError(f"ipmitool timed out for {machine.name}")
    except OSError as e:
        raise WakeError(f"ipmitool error for {machine.name}: {e}") from e
    if result.returncode != 0:
        raise WakeError(
            f"ipmitool failed for {machine.name} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def wake_machine(machine: MachineConfig) -> None:
    if machine.wake_method == "ipmi":
        ipmitool(machine, "chassis", "power", "on")
        return
    # WoL magic packet: 6 x 0xFF then the MAC 16 times, as a UDP broadcast to port 9
    packet = b"\xff" * 6 + bytes.fromhex(machine.mac.replace(":", "")) * 16
    v6 = ipaddress.ip_address(machine.broadcast).version == 6
    try:
        with socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, (machine.broadcast, 9))
    except OSError as e:
        raise WakeError(f"WoL failed for {machine.name}: {e}") from e


def wake_machines(machines: list[MachineConfig], source: str) -> dict[str, str | None]:
    """Wake each machine, logging the outcome. Returns {name: error message or None}."""
    results: dict[str, str | None] = {}
    for m in machines:
        try:
            wake_machine(m)
            logger.info("%s wake: %s via %s", source, m.name, m.wake_method)
            results[m.name] = None
        except WakeError as e:
            logger.error("%s wake failed for %s: %s", source, m.name, e)
            results[m.name] = str(e)
    return results
