"""Local Redis lifecycle for `relay up`.

If REDIS_HOST points elsewhere, we never touch it (the hub is someone else's
job). For the local default we start redis-server with AOF on — the doctor
refuses RDB-only for a reason — data dir ~/.relay/redis, pidfile alongside.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import redis as redis_lib

from relay.bus.client import get_client


def is_local() -> bool:
    return os.environ.get("REDIS_HOST", "127.0.0.1") in ("127.0.0.1", "localhost")


def reachable() -> bool:
    try:
        get_client().ping()
        return True
    except redis_lib.RedisError:
        return False


def ensure_aof(client: redis_lib.Redis) -> bool:
    """Turn AOF on for a local instance that has it off. Returns True if on."""
    if client.config_get("appendonly").get("appendonly") == "yes":
        return True
    if not is_local():
        return False  # never reconfigure someone else's hub
    client.config_set("appendonly", "yes")
    try:
        client.config_rewrite()  # persist across restarts when a config file exists
    except redis_lib.RedisError:
        pass  # no config file — the CONFIG SET still holds for this instance
    return client.config_get("appendonly").get("appendonly") == "yes"


def ensure_running(state_root: Path) -> str:
    """Returns a short description of what happened. Raises on failure."""
    if reachable():
        if ensure_aof(get_client()):
            return "redis already running (AOF on)"
        raise RuntimeError(
            "redis is reachable but AOF is off and it is not local — "
            "enable appendonly on the hub"
        )
    if not is_local():
        raise RuntimeError(
            f"REDIS_HOST={os.environ.get('REDIS_HOST')} is not reachable — "
            "remote redis must be started on its own host"
        )
    if shutil.which("redis-server") is None:
        raise RuntimeError("redis-server not installed (brew install redis)")

    datadir = state_root / "redis"
    datadir.mkdir(parents=True, exist_ok=True)
    port = os.environ.get("REDIS_PORT", "6379")
    subprocess.Popen(
        ["redis-server", "--port", port, "--appendonly", "yes",
         "--dir", str(datadir), "--save", "", "--bind", "127.0.0.1",
         "--pidfile", str(datadir / "redis.pid")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(100):
        if reachable():
            return f"started redis-server (AOF on, data: {datadir})"
        time.sleep(0.05)
    raise RuntimeError("redis-server did not come up within 5s")
