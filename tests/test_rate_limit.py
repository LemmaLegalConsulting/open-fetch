import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.main import app
from app.core.identity import CredentialScope
from app.core.rate_limit import Budget, MemoryBuckets, RateLimiter
from app.models.api_models import ClassificationResponse


@pytest.mark.asyncio
async def test_atomic_bursts_refill_and_consumer_budget():
    clock = [0.0]
    store = MemoryBuckets(clock=lambda: clock[0])
    budget = Budget(60, 2)
    calls = await asyncio.gather(
        *(store.consume([("key", budget), ("consumer", budget)]) for _ in range(12))
    )
    assert calls.count(0) == 2
    assert calls.count(1) == 10
    clock[0] = 0.5
    assert await store.consume([("key", budget), ("consumer", budget)]) == 1
    clock[0] = 1
    assert await store.consume([("key", budget), ("consumer", budget)]) == 0
    # Denial by one bucket does not spend the other bucket.
    assert await store.consume([("new-key", budget), ("consumer", budget)]) == 1
    assert "new-key" not in store.entries


@pytest.mark.asyncio
async def test_memory_cap_never_evicts_active_budgets():
    clock = [0.0]
    store = MemoryBuckets(clock=lambda: clock[0], max_entries=1)
    assert await store.consume([("one", Budget(60, 1))]) == 0
    with pytest.raises(RuntimeError):
        await store.consume([("two", Budget(60, 1))])
    assert await store.consume([("one", Budget(60, 1))]) == 1
    clock[0] = 61
    assert await store.consume([("two", Budget(60, 1))]) == 0
    assert len(store.entries) == 1


@pytest.mark.asyncio
async def test_scope_aggregation_and_environment_separation(monkeypatch):
    monkeypatch.setenv("FETCH_RATE_CONSUMER_BURST", "1")
    limiter = RateLimiter()
    a = CredentialScope("school", "production", "a")
    b = CredentialScope("school", "production", "b")
    await limiter.admitted(a, "school", "secret-a", "peer", "/api/classify")
    with pytest.raises(HTTPException) as exc:
        await limiter.admitted(b, "school", "secret-b", "peer", "/api/classify")
    assert exc.value.status_code == 429
    await limiter.admitted(
        CredentialScope("school", "test", "b"),
        "school",
        "secret",
        "peer",
        "/api/classify",
    )
    await limiter.admitted(
        CredentialScope("other", "production", "b"),
        "other",
        "secret",
        "peer",
        "/api/classify",
    )
    # Event reporting has its own budget; it cannot exhaust model-call capacity.
    await limiter.admitted(
        a, "school", "secret-a", "peer", "/api/telemetry/referral-completed"
    )
    assert all(
        "secret" not in key and "school" not in key for key in limiter.backend.entries
    )


def test_http_limit_prevents_model_calls_and_ignores_claimed_identity(monkeypatch):
    monkeypatch.setenv("API_TOKENS", "synthetic-one,synthetic-two")
    monkeypatch.setenv("FETCH_RATE_KEY_BURST", "1")
    app.state.rate_limiter = RateLimiter()
    classify = AsyncMock(
        return_value=ClassificationResponse(
            labels=[],
            follow_up_questions=[],
            likely_no_legal_problem={
                "value": False,
                "weighted_result": 0,
                "disagreement_score": 0,
            },
        )
    )
    monkeypatch.setattr(
        "app.main.classification_service",
        SimpleNamespace(classify=classify, allowed_providers=None),
    )
    client = TestClient(app)
    response = client.post(
        "/api/classify",
        headers={"Authorization": "Bearer synthetic-one"},
        json={"text": "Synthetic"},
    )
    assert response.status_code == 200
    denied = client.post(
        "/api/classify",
        headers={
            "Authorization": "Bearer synthetic-one",
            "X-Forwarded-For": "different",
        },
        json={
            "text": "Synthetic",
            "client_id": "different",
            "conversation_id": "different",
        },
    )
    assert denied.status_code == 429
    assert int(denied.headers["Retry-After"]) >= 1
    assert denied.headers["Cache-Control"] == "no-store"
    assert classify.await_count == 1
    assert (
        client.post(
            "/api/classify",
            headers={"Authorization": "Bearer synthetic-two"},
            json={"text": "Synthetic"},
        ).status_code
        == 200
    )
    assert client.get("/health").status_code == 200


def test_failed_auth_uses_peer_not_supplied_token_or_forwarded_header(monkeypatch):
    monkeypatch.setenv("API_TOKENS", "valid-secret")
    monkeypatch.setenv("FETCH_RATE_AUTH_BURST", "1")
    app.state.rate_limiter = RateLimiter()
    client = TestClient(app)
    for i, status in enumerate([401, 429]):
        response = client.post(
            "/api/classify",
            headers={"Authorization": f"Bearer invalid-{i}", "X-Forwarded-For": str(i)},
            json={"text": "Synthetic"},
        )
        assert response.status_code == status
    assert len(app.state.rate_limiter.backend.entries) == 1


@pytest.mark.asyncio
async def test_backend_failure_fails_closed_without_error_details():
    limiter = RateLimiter()
    limiter.backend.consume = AsyncMock(side_effect=RuntimeError("redis://secret@host"))
    with pytest.raises(HTTPException) as exc:
        await limiter.enforce([("key", Budget(60, 1))])
    assert exc.value.status_code == 503
    assert "secret" not in str(exc.value.detail)


@pytest.mark.parametrize(
    "setting,value",
    [
        ("FETCH_RATE_LIMIT_BACKEND", "typo"),
        ("FETCH_RATE_KEY_RPM", "0"),
        ("FETCH_RATE_KEY_BURST", "61"),
        ("FETCH_RATE_LIMIT_BACKEND", "redis"),
    ],
)
def test_bad_configuration_rejected(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError):
        RateLimiter()


@pytest.mark.asyncio
async def test_disabling_limiter_is_explicit(monkeypatch):
    monkeypatch.setenv("FETCH_RATE_LIMIT_BACKEND", "disabled")
    limiter = RateLimiter()
    assert limiter.backend is None
    await limiter.admitted(None, None, None, "peer", "/api/classify")


@pytest.mark.asyncio
async def test_redis_lua_shared_atomic_admission_and_expiry():
    import fakeredis.aioredis
    from app.core.rate_limit import RedisBuckets, bucket_key

    server = fakeredis.FakeServer()
    stores = [RedisBuckets("redis://localhost") for _ in range(2)]
    for store in stores:
        await store.close()
        store.client = fakeredis.aioredis.FakeRedis(server=server)
    key, consumer = bucket_key("key"), bucket_key("consumer")
    budgets = [(key, Budget(1, 1)), (consumer, Budget(1, 1))]
    try:
        results = await asyncio.gather(
            *(stores[i % 2].consume(budgets) for i in range(20))
        )
        assert results.count(0) == 1
        assert all(1 <= r <= 60 for r in results if r)
        assert 1 <= await stores[0].client.ttl(key) <= 60
        assert (
            await stores[1].consume([("fresh", Budget(1, 1)), (consumer, Budget(1, 1))])
            > 0
        )
        assert not await stores[0].client.exists("fresh")
        # Use server time to simulate a fully replenished bucket without sleeping.
        seconds, micros = await stores[0].client.time()
        for identity in [key, consumer]:
            await stores[0].client.hset(
                identity, "updated", seconds + micros / 1e6 - 61
            )
        assert await stores[1].consume(budgets) == 0
    finally:
        for store in stores:
            await store.close()
