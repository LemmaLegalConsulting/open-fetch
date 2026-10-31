"""The API exports asynchronously and never hosts a trace viewer."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models.api_models import ClassificationRequest
from app.services.classification_service import ClassificationService


def test_dashboard_routes_absent_even_with_legacy_flag(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ENABLED", "true")
    client = TestClient(app)
    for path in ["/dashboard/", "/dashboard/api/activity", "/dashboard/api/export"]:
        assert client.get(path).status_code == 404
    assert client.post("/api/activity/outcome", json={}).status_code == 404


@pytest.mark.asyncio
async def test_classification_does_not_force_flush(monkeypatch):
    flush = AsyncMock(side_effect=AssertionError("Request waited for export"))
    monkeypatch.setattr("app.telemetry.flush_telemetry_async", flush)
    monkeypatch.setattr(
        "app.services.classification_service.flush_telemetry_async",
        flush,
        raising=False,
    )
    service = ClassificationService()
    try:
        await service.classify(
            ClassificationRequest(text="A synthetic divorce", skip_followups=True)
        )
        flush.assert_not_awaited()
    finally:
        await service.cleanup()


def test_shutdown_flushes_telemetry(monkeypatch):
    flush = AsyncMock()
    monkeypatch.setattr("app.telemetry.flush_telemetry_async", flush)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        flush.assert_not_awaited()
    flush.assert_awaited_once()
