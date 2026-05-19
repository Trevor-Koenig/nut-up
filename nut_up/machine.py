from __future__ import annotations

import asyncio

from .config import MachineConfig


async def is_online(ip: str, count: int = 1, timeout: int = 2) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", str(count), "-W", str(timeout), "--", ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception:
        return False


async def check_all(machines: list[MachineConfig]) -> dict[str, bool]:
    results = await asyncio.gather(
        *(is_online(m.ip) for m in machines),
        return_exceptions=True,
    )
    return {
        m.name: (r if isinstance(r, bool) else False)
        for m, r in zip(machines, results)
    }
