"""Bounded token buckets for authenticated API calls; no request content retained."""

from collections import OrderedDict
from dataclasses import dataclass
import asyncio
import hashlib
import math
import os
from threading import Lock
import time

from fastapi import HTTPException


@dataclass(frozen=True)
class Budget:
    rpm: int
    burst: int

    @classmethod
    def load(cls, name: str, rpm: int, burst: int):
        rate = int(os.getenv(f"FETCH_RATE_{name}_RPM", str(rpm)))
        capacity = int(os.getenv(f"FETCH_RATE_{name}_BURST", str(burst)))
        if not 1 <= capacity <= rate <= 100000:
            raise ValueError("Rate budgets require 1 <= burst <= rpm <= 100000")
        return cls(rate, capacity)


class MemoryBuckets:
    """Atomic multi-bucket admission, at most 10,000 identities per process."""

    def __init__(self, clock=time.monotonic, max_entries=10000):
        self.clock = clock
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.lock = Lock()

    async def consume(self, buckets):
        with self.lock:
            now = self.clock()
            # Every configured bucket is completely refilled after 60 idle seconds.
            while self.entries and next(iter(self.entries.values()))[1] <= now - 60:
                self.entries.popitem(last=False)
            new = sum(key not in self.entries for key, _ in buckets)
            if len(self.entries) + new > self.max_entries:
                raise RuntimeError("Rate limiter capacity reached")
            balances = []
            retry = 0.0
            for key, budget in buckets:
                tokens, updated = self.entries.get(key, (budget.burst, now))
                tokens = min(
                    budget.burst, tokens + max(0, now - updated) * budget.rpm / 60
                )
                balances.append((key, tokens))
                retry = max(retry, (1 - tokens) * 60 / budget.rpm)
            # A denied request does not spend either bucket's remaining capacity.
            if retry > 0:
                return math.ceil(retry)
            for key, tokens in balances:
                self.entries[key] = (tokens - 1, now)
                self.entries.move_to_end(key)
            return 0

    async def close(self):
        pass


# Redis server time avoids clock skew between API hosts. All keys share a hash tag.
# Admission across API-key and consumer buckets is one atomic operation.
_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local balances = {}
local retry = 0
for i, key in ipairs(KEYS) do
    local rate = tonumber(ARGV[(i-1)*2+1]) / 60
    local burst = tonumber(ARGV[(i-1)*2+2])
    local old = redis.call('HMGET', key, 'tokens', 'updated')
    local tokens = tonumber(old[1]) or burst
    local updated = tonumber(old[2]) or now
    tokens = math.min(burst, tokens + math.max(0, now-updated)*rate)
    balances[i] = tokens
    retry = math.max(retry, (1-tokens)/rate)
end
if retry > 0 then return math.ceil(retry) end
for i, key in ipairs(KEYS) do
    redis.call('HSET', key, 'tokens', balances[i]-1, 'updated', now)
    redis.call('EXPIRE', key, 60)
end
return 0
"""


class RedisBuckets:
    def __init__(self, url):
        from redis.asyncio import Redis
        from redis.backoff import NoBackoff
        from redis.asyncio.retry import Retry

        self.client = Redis.from_url(
            url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            max_connections=50,
            retry=Retry(NoBackoff(), 0),
        )

    async def consume(self, buckets):
        keys = [key for key, _ in buckets]
        args = [value for _, budget in buckets for value in (budget.rpm, budget.burst)]
        return int(await self.client.eval(_SCRIPT, len(keys), *keys, *args))

    async def close(self):
        await self.client.aclose()


def bucket_key(*parts):
    # No raw tokens, consumer identifiers, or IP addresses in backend keys.
    import json

    digest = hashlib.sha256(json.dumps(parts).encode()).hexdigest()
    return "fetch-rate:{fetch-rate}:" + digest


class RateLimiter:
    def __init__(self):
        backend = os.getenv("FETCH_RATE_LIMIT_BACKEND", "memory")
        if backend not in {"memory", "redis", "disabled"}:
            raise ValueError("Invalid FETCH_RATE_LIMIT_BACKEND")
        self.key = Budget.load("KEY", 60, 10)
        self.consumer = Budget.load("CONSUMER", 120, 20)
        self.events = Budget.load("EVENT", 120, 30)
        self.auth = Budget.load("AUTH", 30, 10)
        self.backend = None
        if backend == "memory":
            self.backend = MemoryBuckets()
        elif backend == "redis":
            url = os.getenv("FETCH_RATE_REDIS_URL", "")
            if not url.startswith(("redis://", "rediss://")):
                raise ValueError("FETCH_RATE_REDIS_URL is required for Redis")
            self.backend = RedisBuckets(url)

    async def enforce(self, buckets):
        if self.backend is None:
            return
        try:
            async with asyncio.timeout(1):
                retry = await self.backend.consume(buckets)
        except Exception:
            # No unbounded local fallback if the shared limiter is unavailable.
            raise HTTPException(
                503,
                "Rate limiting is temporarily unavailable",
                headers={"Retry-After": "1", "Cache-Control": "no-store"},
            ) from None
        if retry:
            raise HTTPException(
                429,
                "Rate limit exceeded",
                headers={"Retry-After": str(retry), "Cache-Control": "no-store"},
            )

    async def failed_auth(self, peer):
        await self.enforce([(bucket_key("auth", peer), self.auth)])

    async def admitted(self, scope, client, token, peer, path):
        event = path == "/api/telemetry/referral-completed"
        group = "events" if event else "classify"
        if scope:
            key_identity = ("scoped", scope.scope_id)
            consumer_identity = ("scoped", scope.client_id, scope.environment)
        elif token:
            key_identity = ("legacy", hashlib.sha256(token.encode()).hexdigest())
            consumer_identity = ("legacy", client) if client else key_identity
        else:
            key_identity = consumer_identity = ("anonymous-dev", peer)
        buckets = [
            (
                bucket_key(group, "key", *key_identity),
                self.events if event else self.key,
            ),
            (
                bucket_key(group, "consumer", *consumer_identity),
                self.events if event else self.consumer,
            ),
        ]
        await self.enforce(buckets)

    async def close(self):
        if self.backend:
            await self.backend.close()
