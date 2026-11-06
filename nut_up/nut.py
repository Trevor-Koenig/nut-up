from __future__ import annotations

import socket

from .config import NutConfig


def _safe_int(val: str | None) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


class NutError(Exception):
    pass


class NutConnectionError(NutError):
    pass


class NutClient:
    """Synchronous raw-socket NUT protocol client. Use as a context manager."""

    def __init__(self, cfg: NutConfig) -> None:
        self._cfg = cfg
        self._sock: socket.socket | None = None
        self._file = None

    def __enter__(self) -> "NutClient":
        cfg = self._cfg
        try:
            self._sock = socket.create_connection((cfg.host, cfg.port), timeout=5)
        except OSError as e:
            raise NutConnectionError(f"Cannot connect to upsd at {cfg.host}:{cfg.port}: {e}") from e
        # errors="replace": a misbehaving upsd must not crash the poll loop
        self._file = self._sock.makefile("r", encoding="utf-8", errors="replace")
        if cfg.username:
            self._cmd(f"USERNAME {cfg.username}")
        if cfg.password:
            self._cmd(f"PASSWORD {cfg.password}")
        self._cmd(f"LOGIN {cfg.ups_names[0]}")
        return self

    def __exit__(self, *_) -> None:
        try:
            if self._sock:
                self._send("LOGOUT")
                self._readline()
        except Exception:
            pass
        finally:
            if self._file:
                self._file.close()
            if self._sock:
                self._sock.close()
            self._file = None
            self._sock = None

    def _send(self, line: str) -> None:
        try:
            self._sock.sendall((line + "\n").encode())
        except OSError as e:
            raise NutConnectionError(f"Send error: {e}") from e

    def _readline(self) -> str:
        try:
            line = self._file.readline()
        except OSError as e:
            raise NutConnectionError(f"Read error: {e}") from e
        if not line:
            raise NutConnectionError("Connection closed by server")
        return line.rstrip("\n")

    def _cmd(self, command: str) -> str:
        self._send(command)
        resp = self._readline()
        if resp.startswith("ERR "):
            # Only echo the verb — arguments may contain the NUT password.
            verb = command.split(" ", 1)[0]
            raise NutError(f"NUT error for {verb}: {resp[4:]}")
        return resp

    def _get_var(self, ups_name: str, var_name: str, required: bool = False) -> str | None:
        """Return the variable's value; on ERR raise if required, else return None."""
        self._send(f"GET VAR {ups_name} {var_name}")
        resp = self._readline()
        if resp.startswith("ERR "):
            if required:
                raise NutError(f"GET VAR {ups_name} {var_name}: {resp[4:]}")
            return None
        parts = resp.split('"', 2)
        return parts[1] if len(parts) >= 2 else resp

    def get_all_statuses(self) -> dict[str, dict]:
        """Query ups.status + optional metrics for each UPS. Returns {ups_name: {status, battery_charge, battery_runtime, ups_load}}."""
        result: dict[str, dict] = {}
        for name in self._cfg.ups_names:
            result[name] = {
                "status": self._get_var(name, "ups.status", required=True),
                "battery_charge": _safe_int(self._get_var(name, "battery.charge")),
                "battery_runtime": _safe_int(self._get_var(name, "battery.runtime")),
                "ups_load": _safe_int(self._get_var(name, "ups.load")),
            }
        return result

    def list_clients(self) -> list[str]:
        """Return IPs of currently connected upsmon slaves across all configured UPS devices."""
        seen: dict[str, None] = {}
        for name in self._cfg.ups_names:
            self._cmd(f"LIST CLIENT {name}")
            while True:
                line = self._readline()
                if line.startswith("END LIST CLIENT"):
                    break
                # CLIENT <ups> <ip>
                parts = line.split()
                if len(parts) >= 3 and parts[0] == "CLIENT":
                    seen[parts[2]] = None
        return list(seen)
