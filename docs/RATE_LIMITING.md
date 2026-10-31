# API rate limiting

FETCH limits authenticated requests before classification/model calls. Limits are enabled by default and apply even when telemetry is disabled. Rejections return HTTP 429 with an integer `Retry-After` in seconds and `Cache-Control: no-store`. Clients should wait at least that long, add jitter, and avoid immediate retries.

| Budget | Sustained requests/minute | Initial burst | Environment variables |
| --- | ---: | ---: | --- |
| Classification per API key | 60 | 10 | `FETCH_RATE_KEY_RPM`, `FETCH_RATE_KEY_BURST` |
| Classification per consumer/environment, across keys | 120 | 20 | `FETCH_RATE_CONSUMER_RPM`, `FETCH_RATE_CONSUMER_BURST` |
| Telemetry-event reporting, both per key and per consumer/environment | 120 | 30 | `FETCH_RATE_EVENT_RPM`, `FETCH_RATE_EVENT_BURST` |
| Failed authentication per network peer | 30 | 10 | `FETCH_RATE_AUTH_RPM`, `FETCH_RATE_AUTH_BURST` |

These are token buckets, not fixed calendar-minute quotas: capacity refills continuously at RPM/60 per second, up to the burst size. A key initially permits ten classifications immediately, then refills one request per second. Each admitted request must fit both key and consumer budgets atomically. Follow-up classifications, cache hits, and admitted requests that later fail validation/provider execution still count. Rejected requests do not spend either bucket. Require `1 <= burst <= rpm <= 100000`; malformed settings fail startup. Restart after configuration changes.

Keys derive from authenticated `API_CREDENTIALS` scope, never body fields, user/conversation IDs, or caller-provided headers. Test and production use independent budgets. Rotating the secret under the same structured key ID retains its shared Redis budget; issuing another key does not bypass the consumer limit. Legacy mapped keys share a consumer budget but cannot provide test/production isolation; unmapped legacy keys are limited individually. Anonymous development calls use network-peer buckets. Stored keys are SHA-256 digests; raw tokens, IPs, and submitted content are not stored or logged by this limiter.

## Single API process

`FETCH_RATE_LIMIT_BACKEND=memory` adds no network/storage dependency. It uses an atomic, bounded in-process store (10,000 bucket entries), expiring fully replenished idle entries after 60 seconds. If capacity is exhausted, requests fail with 503 rather than evicting active budgets and letting clients bypass limits. Restarting the process resets these limits.

Memory budgets are **per process**, not per deployment. Multiple Uvicorn workers, Fly Machines, ECS tasks, or overlapping old/new replicas each have their own capacity. Use Redis for shared enforcement; the memory setting is appropriate only when this limitation is acceptable.

## Multiple processes/hosts

```dotenv
FETCH_RATE_LIMIT_BACKEND=redis
FETCH_RATE_REDIS_URL=rediss://USER:PASSWORD@HOST:6379/0
```

Use a low-latency Redis/Valkey service reachable from all API instances, preferably through private networking and TLS. Inject the URL as a secret. Both budgets are checked in one atomic Lua operation using Redis server time, with 60-second key expiry. All keys use one Redis Cluster hash tag; use a normal Redis endpoint/proxy supported by redis-py (automatic Redis Cluster topology discovery is not configured).

Redis adds one network round trip per admitted/failed-auth check. It does not store traces. Checks have a one-second overall timeout; unavailable/full backends return a generic 503 with `Retry-After: 1`. There is no fallback to independent memory limits. Deployments sharing this Redis database and prefix intentionally share budgets; use separate Redis databases/instances for unrelated FETCH deployments. Configure capacity/eviction so active limiter keys are not silently evicted; Redis restarts or data loss reset budgets. An administrator must provision Redis separately; no new paid service is deployed by this change.

## Boundaries

The limits protect model-call volume, not a dollar spend ceiling or an in-flight concurrency guarantee. One classification can invoke multiple models and auxiliary calls. Direct Python service calls bypass HTTP admission, which is useful for offline tests/evaluations. `/health` and documentation endpoints are not limited. The separate conversation viewer has independent authentication and is not covered by these API limits.

Failed-auth throttling uses the ASGI network peer. This code does not parse forwarded-IP headers. Configure the web server to trust forwarded headers only from known proxies; an overly broad proxy trust setting can still allow a forged ASGI peer. Invalid payloads can be rejected by FastAPI before authentication runs. Use hosting/edge controls for volumetric traffic, body-size limits, and broader IP abuse protection; this application limiter is not a DDoS firewall.
