"""Redis client factory. Connection settings come from the environment so a
worker on another machine points at the hub with REDIS_HOST alone."""

from __future__ import annotations

import os

import redis


def get_client(
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    username: str | None = None,
) -> redis.Redis:
    return redis.Redis(
        host=host or os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=port or int(os.environ.get("REDIS_PORT", "6379")),
        password=password or os.environ.get("REDIS_PASSWORD") or None,
        username=username or os.environ.get("REDIS_USERNAME") or None,
        decode_responses=True,
    )
