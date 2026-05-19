from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    api_key: str = "changeme"


@dataclass
class WebConfig:
    username: str = "admin"
    password: str = "changeme"


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


def _validate(cfg: Config) -> None:
    if not cfg.nut.ups_names:
        raise ConfigError("nut.ups_names must not be empty")

    for i, m in enumerate(cfg.machines):
        loc = f"machines[{i}] ({m.name!r})"
        if not m.name:
            raise ConfigError(f"{loc}: name is required")
        if m.wake_method not in ("wol", "ipmi"):
            raise ConfigError(f"{loc}: wake_method must be 'wol' or 'ipmi'")
        if m.wake_method == "wol":
            if not _MAC_RE.match(m.mac or ""):
                raise ConfigError(f"{loc}: invalid MAC address {m.mac!r}")
        if m.wake_method == "ipmi":
            missing = [
                f for f in ("ipmi_host", "ipmi_user", "ipmi_pass") if not getattr(m, f)
            ]
            if missing:
                raise ConfigError(
                    f"{loc}: ipmi wake_method requires {', '.join(missing)}"
                )

    # A05 — default credential guard; checked last after structural validation
    if cfg.api.api_key == "changeme":
        raise ConfigError("api.api_key must be changed from the default 'changeme'")
    if cfg.web.password == "changeme":
        raise ConfigError("web.password must be changed from the default 'changeme'")


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}")
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error: {e}")

    if not isinstance(raw, dict):
        raw = {}

    def _get(d: dict, key: str, default=None):
        return d.get(key, default) if isinstance(d, dict) else default

    nut_raw = raw.get("nut") or {}
    api_raw = raw.get("api") or {}
    web_raw = raw.get("web") or {}

    nut = NutConfig(
        host=_get(nut_raw, "host", "localhost"),
        port=int(_get(nut_raw, "port", 3493)),
        ups_names=_get(nut_raw, "ups_names", ["ups"]),
        username=_get(nut_raw, "username", ""),
        password=_get(nut_raw, "password", ""),
    )
    api = ApiConfig(
        host=_get(api_raw, "host", "0.0.0.0"),
        port=int(_get(api_raw, "port", 8765)),
        api_key=_get(api_raw, "api_key", "changeme"),
    )
    web = WebConfig(
        username=_get(web_raw, "username", "admin"),
        password=_get(web_raw, "password", "changeme"),
    )

    machines = []
    for m_raw in raw.get("machines") or []:
        if not isinstance(m_raw, dict):
            continue
        machines.append(
            MachineConfig(
                name=m_raw.get("name", ""),
                mac=m_raw.get("mac", ""),
                ip=m_raw.get("ip", ""),
                broadcast=m_raw.get("broadcast", "255.255.255.255"),
                wake_method=m_raw.get("wake_method", "wol"),
                ups=m_raw.get("ups"),
                ipmi_host=m_raw.get("ipmi_host"),
                ipmi_user=m_raw.get("ipmi_user"),
                ipmi_pass=m_raw.get("ipmi_pass"),
            )
        )

    cfg = Config(
        nut=nut,
        api=api,
        web=web,
        wake_delay_seconds=int(raw.get("wake_delay_seconds", 30)),
        machines=machines,
    )
    _validate(cfg)
    return cfg


def save_config(config: Config, path: str | Path) -> None:
    """Write config back to YAML. YAML comments are not preserved."""
    path = Path(path)
    data: dict = {
        "nut": {
            "host": config.nut.host,
            "port": config.nut.port,
            "ups_names": config.nut.ups_names,
            "username": config.nut.username,
            "password": config.nut.password,
        },
        "api": {
            "host": config.api.host,
            "port": config.api.port,
            "api_key": config.api.api_key,
        },
        "web": {
            "username": config.web.username,
            "password": config.web.password,
        },
        "wake_delay_seconds": config.wake_delay_seconds,
        "machines": [
            {
                "name": m.name,
                "mac": m.mac,
                "ip": m.ip,
                "broadcast": m.broadcast,
                "wake_method": m.wake_method,
                **({"ups": m.ups} if m.ups else {}),
                **(
                    {
                        "ipmi_host": m.ipmi_host,
                        "ipmi_user": m.ipmi_user,
                        "ipmi_pass": m.ipmi_pass,
                    }
                    if m.wake_method == "ipmi"
                    else {}
                ),
            }
            for m in config.machines
        ],
    }
    path.write_text(yaml.dump(data, default_flow_style=False))
