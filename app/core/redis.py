# app/core/redis.py
"""One shared async Redis client.

Redis is optional in development: when REDIS_URL is not set, ``get_redis`` returns
None and callers fall back to their local behaviour. In production REDIS_URL is
always set. The client is created lazily on first use so importing this module never
opens a connection.
"""
import asyncio
import logging
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_client: Optional[Any] = None


def build_client() -> Any:
    """Create a new client from the settings. Split out so tests can replace it."""
    from redis.asyncio import Redis  # imported here so the app still starts without it in dev

    assert settings.redis_url is not None
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        max_connections=settings.redis_max_connections,
        health_check_interval=settings.redis_health_check_interval_seconds,
        retry_on_timeout=False,
    )


def get_redis() -> Optional[Any]:
    """The shared client, or None when Redis is not configured."""
    global _client
    if not settings.redis_configured:
        return None
    if _client is None:
        _client = build_client()
    return _client


def key(*parts: str) -> str:
    """A namespaced Redis key, e.g. key("rl", "login", "1.2.3.4") -> "hubbly:rl:login:1.2.3.4"."""
    return ":".join((settings.redis_key_prefix, *parts))


async def check_redis() -> bool:
    """True when Redis answers PING in time. False when it is down or not configured."""
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(
            await asyncio.wait_for(client.ping(), timeout=settings.health_timeout_seconds)
        )
    except Exception:
        logger.warning("Redis health check failed", exc_info=True)
        return False


async def close_redis() -> None:
    global _client
    client, _client = _client, None
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        logger.warning("Redis connection did not close cleanly", exc_info=True)