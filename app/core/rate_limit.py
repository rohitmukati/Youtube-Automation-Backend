# app/core/rate_limit.py
"""Fixed-window rate limiting backed by Redis, with a safe in-memory fallback.

Design rules:
* Limits are named rules in ``settings.rate_limits`` so they can be tuned in one place.
* Redis is the source of truth when configured (all API instances share the counters).
* If Redis is missing or down the limiter NEVER blocks users because of that. It falls
  back to per-process counters and tries Redis again after a short cooldown.
* The counter and its expiry are set in one atomic Lua script, so a crash can never
  leave a counter without an expiry (which would lock a user out for good).
"""
import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.config import settings
from app.core.exceptions import RateLimitedError
from app.core.redis import get_redis, key

logger = logging.getLogger(__name__)

# Increment, and (re)apply the expiry when the key is new or has none. Returns {count, ttl}.
_LUA_HIT = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if count == 1 or ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""

_MEMORY_MAX_KEYS = 10_000


@dataclass(frozen=True)
class RateRule:
    name: str
    limit: int
    window_seconds: int


@dataclass(frozen=True)
class RateResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


def get_rule(name: str) -> RateRule:
    try:
        limit, window = settings.rate_limits[name]
    except KeyError as exc:
        raise KeyError(f"Unknown rate limit rule: {name}") from exc
    if int(limit) < 1 or int(window) < 1:
        raise ValueError(f"Rate limit rule {name} must have a positive limit and window")
    return RateRule(name=name, limit=int(limit), window_seconds=int(window))


class _MemoryCounters:
    """Per-process fixed-window counters. Used only when Redis is unavailable."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, tuple[int, float]] = {}

    def hit(self, name: str, window: int) -> tuple[int, int]:
        now = self._clock()
        with self._lock:
            count, reset_at = self._items.get(name, (0, 0.0))
            if reset_at <= now:
                count, reset_at = 0, now + window
            count += 1
            self._items[name] = (count, reset_at)
            if len(self._items) > _MEMORY_MAX_KEYS:
                self._prune(now)
            return count, max(1, int(reset_at - now + 0.999))

    def _prune(self, now: float) -> None:
        expired = [k for k, (_, reset_at) in self._items.items() if reset_at <= now]
        for k in expired:
            del self._items[k]
        # Still too big (a flood of distinct keys inside one window): drop the oldest half
        # rather than growing without bound.
        if len(self._items) > _MEMORY_MAX_KEYS:
            ordered = sorted(self._items.items(), key=lambda item: item[1][1])
            for k, _ in ordered[: len(ordered) // 2]:
                del self._items[k]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class RateLimiter:
    def __init__(
        self,
        redis_getter: Callable[[], Optional[Any]] = get_redis,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis_getter = redis_getter
        self._clock = clock
        self._memory = _MemoryCounters(clock)
        self._redis_retry_at = 0.0
        self._last_warning = 0.0

    async def hit(self, rule: RateRule, identity: str) -> RateResult:
        if not settings.rate_limit_enabled:
            return RateResult(True, rule.limit, rule.limit, 0)

        counter_key = key("rl", rule.name, identity)
        count_ttl = await self._hit_redis(counter_key, rule)
        if count_ttl is None:
            count_ttl = self._memory.hit(counter_key, rule.window_seconds)

        count, ttl = count_ttl
        allowed = count <= rule.limit
        return RateResult(
            allowed=allowed,
            limit=rule.limit,
            remaining=max(rule.limit - count, 0),
            retry_after=0 if allowed else max(1, ttl),
        )

    async def _hit_redis(self, counter_key: str, rule: RateRule) -> Optional[tuple[int, int]]:
        now = self._clock()
        if now < self._redis_retry_at:
            return None
        client = self._redis_getter()
        if client is None:
            return None
        try:
            raw = await client.eval(_LUA_HIT, 1, counter_key, rule.window_seconds)
            return int(raw[0]), int(raw[1])
        except Exception:
            self._redis_retry_at = now + settings.redis_failure_cooldown_seconds
            if now - self._last_warning > 60:
                self._last_warning = now
                logger.warning(
                    "Redis is not reachable, rate limits use local memory for %ss",
                    settings.redis_failure_cooldown_seconds,
                    exc_info=True,
                )
            return None

    def reset_for_tests(self) -> None:
        self._memory.clear()
        self._redis_retry_at = 0.0


limiter = RateLimiter()


def client_ip(forwarded_for: Optional[str], peer: Optional[str], trusted_hops: int) -> str:
    """The real client address.

    ``X-Forwarded-For`` can be written by the client, so only the entries added by OUR
    proxies are trusted: with N trusted proxies the real client is the Nth entry from the
    right. With 0 trusted proxies the header is ignored completely.
    IPv6 clients are grouped by their /64 network, so one user cannot dodge a limit by
    rotating through addresses in their own block.
    """
    candidate = peer or ""
    if trusted_hops > 0 and forwarded_for:
        parts = [part.strip() for part in forwarded_for.split(",") if part.strip()]
        if len(parts) >= trusted_hops:
            candidate = parts[-trusted_hops]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return "unknown"
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False).network_address)
    return str(address)


async def enforce(rule_name: str, identity: str, *, instance: Optional[RateLimiter] = None) -> None:
    result = await (instance or limiter).hit(get_rule(rule_name), identity)
    if not result.allowed:
        logger.info("Rate limit %s hit by %s", rule_name, identity)
        raise RateLimitedError(result.retry_after)


def rate_limit(rule_name: str, *, scope: str = "ip") -> Callable[..., Any]:
    """A FastAPI dependency. Use it as ``dependencies=[Depends(rate_limit("auth_login"))]``.

    scope="ip"   counts per client address (for endpoints used before login)
    scope="user" counts per logged-in user
    """
    get_rule(rule_name)  # fail at import time for a typo, not on the first request
    if scope not in ("ip", "user"):
        raise ValueError("scope must be 'ip' or 'user'")

    from fastapi import Request  # local import keeps the core logic importable without FastAPI

    if scope == "ip":

        async def by_ip(request: Request) -> None:
            peer = request.client.host if request.client else None
            identity = client_ip(
                request.headers.get("x-forwarded-for"), peer, settings.trusted_proxy_hops
            )
            await enforce(rule_name, identity)

        return by_ip

    from app.core.dependencies import CurrentUser

    async def by_user(current_user: CurrentUser) -> None:
        await enforce(rule_name, str(current_user.id))

    return by_user