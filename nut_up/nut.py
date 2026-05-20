from __future__ import annotations

import socket


def _safe_int(val: str | None) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


class NutError(Exception):
    pass


class NutAuthError(NutError):
    pass


class NutConnectionError(NutError):
    pass


class NutProtocolError(NutError):
    pass


class NutClient:
    """Synchronous raw-socket NUT protocol client. Use as a context manager."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        ups_names: list[str],
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._ups_names = ups_names
        self._sock: socket.socket | None = None
        self._file = None

    def __enter__(self) -> "NutClient":
        try:
            self._sock = socket.create_connection((self._host, self._port), timeout=5)
        except OSError as e:
            raise NutConnectionError(
                f"Cannot connect to upsd at {self._host}:{self._port}: {e}"
            ) from e
        self._file = self._sock.makefile("r")
        self._authenticate()
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
            err = resp[4:]
            if any(k in err for k in ("ACCESS-DENIED", "USERNAME", "PASSWORD")):
                raise NutAuthError(f"Auth error: {err}")
            # Only echo the verb — arguments may contain the NUT password.
            verb = command.split(" ", 1)[0]
            raise NutProtocolError(f"NUT error for {verb}: {err}")
        return resp

    def _authenticate(self) -> None:
        if self._username:
            self._cmd(f"USERNAME {self._username}")
        if self._password:
            self._cmd(f"PASSWORD {self._password}")
        if self._ups_names:
            self._cmd(f"LOGIN {self._ups_names[0]}")

    def _get_optional_var(self, ups_name: str, var_name: str) -> str | None:
        self._send(f"GET VAR {ups_name} {var_name}")
        resp = self._readline()
        if resp.startswith("ERR "):
            return None
        parts = resp.split('"', 2)
        return parts[1] if len(parts) >= 2 else None

    def get_all_statuses(self) -> dict[str, dict]:
        """Query ups.status + optional metrics for each UPS. Returns {ups_name: {status, battery_charge, battery_runtime, ups_load}}."""
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

    def list_clients(self) -> list[str]:
        """Return IPs of currently connected upsmon slaves across all configured UPS devices."""
        seen: dict[str, None] = {}
        for name in self._ups_names:
            self._send(f"LIST CLIENT {name}")
            header = self._readline()
            if header.startswith("ERR "):
                raise NutProtocolError(f"LIST CLIENT {name}: {header[4:]}")
            while True:
                line = self._readline()
                if line.startswith("END LIST CLIENT"):
                    break
                # CLIENT <ups> <ip>
                parts = line.split()
                if len(parts) >= 3 and parts[0] == "CLIENT":
                    seen[parts[2]] = None
        return list(seen)
