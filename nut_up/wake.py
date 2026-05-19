from __future__ import annotations

import os
import subprocess

import wakeonlan

from .config import MachineConfig


class WakeError(RuntimeError):
    pass


def wake_machine(machine: MachineConfig) -> None:
    if machine.wake_method == "wol":
        _wake_wol(machine)
    elif machine.wake_method == "ipmi":
        _wake_ipmi(machine)
    else:
        raise WakeError(f"Unknown wake_method: {machine.wake_method!r}")


def _wake_wol(machine: MachineConfig) -> None:
    broadcast = machine.broadcast or "255.255.255.255"
    try:
        wakeonlan.send_magic_packet(machine.mac, ip_address=broadcast)
    except Exception as e:
        raise WakeError(f"WoL failed for {machine.name}: {e}") from e


def _wake_ipmi(machine: MachineConfig) -> None:
    cmd = [
        "ipmitool", "-I", "lanplus",
        "-H", machine.ipmi_host,
        "-U", machine.ipmi_user,
        "-E",
        "-L", "OPERATOR",
        "chassis", "power", "on",
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
