from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import yaml


class ConfigError(Exception):
    pass


@dataclass
class NutConfig:
    host: str = "localhost"
    port: int = 3493
    ups_names: list[str] = field(default_factory=lambda: ["ups"])
    username: str = ""
    password: str = ""


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    api_key: Optional[str] = None  # None = REST API disabled
    tls_cert: Optional[str] = None  # path to PEM cert; None = plain HTTP
    tls_key: Optional[str] = None   # path to PEM private key


@dataclass
class WebConfig:
    username: str = "admin"
    password: Optional[str] = None  # None = web UI disabled
    port: Optional[int] = None  # None = share server with API; set to run on a separate port


@dataclass
class MachineConfig:
    name: str = ""
    mac: str = ""
    ip: str = ""
    broadcast: str = "255.255.255.255"
    wake_method: str = "wol"
    ups: Optional[str] = None
    ipmi_host: Optional[str] = None
    ipmi_user: Optional[str] = None
    ipmi_pass: Optional[str] = None


@dataclass
class Config:
    nut: NutConfig = field(default_factory=NutConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    web: WebConfig = field(default_factory=WebConfig)
    wake_delay_seconds: int = 30
    machines: list[MachineConfig] = field(default_factory=list)


_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
# UPS and machine names: letters, digits, dots, dashes, underscores. Keeps
# line-based NUT protocol commands free of injectable whitespace/newlines, and
# machine names safe in URL paths and HTML ids.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# Hostnames per RFC 952/1123: letters/digits/dot/dash, must not start with a dash
# (so the value can never be misread as a CLI flag by ipmitool).
_HOSTNAME_RE = re.compile(r"^(?!-)[A-Za-z0-9.-]+$")


def _section(cls, raw, loc: str):
    """Build a config dataclass from a YAML mapping, rejecting unknown keys and wrong types.

    Blank values fall back to the dataclass default, so `api_key:` with nothing
    after it means "disabled" rather than "empty key".
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{loc}: expected a mapping")
    types = {f.name: f.type for f in fields(cls)}
    unknown = sorted(str(k) for k in raw if k not in types)
    if unknown:
        raise ConfigError(f"{loc}: unknown key(s): {', '.join(unknown)}")
    values = {k: v for k, v in raw.items() if v not in (None, "")}
    for key, value in values.items():
        want = types[key]
        if want.startswith("Optional["):
            want = want[len("Optional["):-1]
        if want == "list[str]":
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = type(value) is {"str": str, "int": int}[want]
        if not ok:
            # e.g. an unquoted MAC like 11:22:33:44:55:66 is parsed by YAML as an int
            raise ConfigError(f"{loc}.{key}: expected {want}, got {value!r}")
    return cls(**values)


def _validate(cfg: Config) -> None:
    if not cfg.nut.ups_names:
        raise ConfigError("nut.ups_names must not be empty")
    for name in cfg.nut.ups_names:
        if not _NAME_RE.match(name):
            raise ConfigError(f"nut.ups_names: invalid UPS name {name!r}")

    for i, m in enumerate(cfg.machines):
        loc = f"machines[{i}] ({m.name!r})"
        if not _NAME_RE.match(m.name):
            raise ConfigError(f"{loc}: name is required (letters, digits, '.', '-', '_')")
        if m.wake_method not in ("wol", "ipmi"):
            raise ConfigError(f"{loc}: wake_method must be 'wol' or 'ipmi'")
        if m.ups is not None and m.ups not in cfg.nut.ups_names:
            raise ConfigError(f"{loc}: ups {m.ups!r} is not in nut.ups_names")
        for key in ("ip", "broadcast"):
            value = getattr(m, key)
            if value:
                try:
                    ipaddress.ip_address(value)
                except ValueError:
                    raise ConfigError(f"{loc}: invalid {key} address {value!r}")
        if m.wake_method == "wol" and not _MAC_RE.match(m.mac):
            raise ConfigError(f"{loc}: invalid MAC address {m.mac!r}")
        if m.wake_method == "ipmi":
            missing = [f for f in ("ipmi_host", "ipmi_user", "ipmi_pass") if not getattr(m, f)]
            if missing:
                raise ConfigError(f"{loc}: ipmi wake_method requires {', '.join(missing)}")
            if not _HOSTNAME_RE.match(m.ipmi_host):
                raise ConfigError(f"{loc}: invalid ipmi_host {m.ipmi_host!r}")

    for loc, port in (("nut.port", cfg.nut.port), ("api.port", cfg.api.port),
                      ("web.port", cfg.web.port)):
        if port is not None and not 1 <= port <= 65535:
            raise ConfigError(f"{loc} must be 1-65535, got {port}")
    if cfg.web.port == cfg.api.port:
        raise ConfigError("web.port must differ from api.port when set explicitly")
    if cfg.wake_delay_seconds < 0:
        raise ConfigError("wake_delay_seconds must not be negative")

    if bool(cfg.api.tls_cert) != bool(cfg.api.tls_key):
        raise ConfigError("api.tls_cert and api.tls_key must both be set or both be omitted")
    for loc, path in (("api.tls_cert", cfg.api.tls_cert), ("api.tls_key", cfg.api.tls_key)):
        if path and not Path(path).is_file():
            raise ConfigError(f"{loc} not found: {path}")

    # A07 — default credential guard
    if cfg.api.api_key == "changeme":
        raise ConfigError("api.api_key must be changed from the default 'changeme'")
    if cfg.web.password == "changeme":
        raise ConfigError("web.password must be changed from the default 'changeme'")


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}")
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error: {e}")
    if not isinstance(raw, dict):
        raise ConfigError("config: expected a mapping at the top level")

    unknown = sorted(str(k) for k in raw if k not in {f.name for f in fields(Config)})
    if unknown:
        raise ConfigError(f"config: unknown key(s): {', '.join(unknown)}")
    machines = raw.get("machines") or []
    if not isinstance(machines, list):
        raise ConfigError("machines: expected a list")
    delay = raw.get("wake_delay_seconds")
    if delay is None:
        delay = 30
    if type(delay) is not int:
        raise ConfigError(f"wake_delay_seconds: expected int, got {delay!r}")

    cfg = Config(
        nut=_section(NutConfig, raw.get("nut"), "nut"),
        api=_section(ApiConfig, raw.get("api"), "api"),
        web=_section(WebConfig, raw.get("web"), "web"),
        wake_delay_seconds=delay,
        machines=[_section(MachineConfig, m, f"machines[{i}]") for i, m in enumerate(machines)],
    )
    _validate(cfg)
    return cfg
