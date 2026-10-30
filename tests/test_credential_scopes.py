"""Synthetic credentials and in-memory exporters; no model/cloud requests."""

import asyncio
import json
from unittest.mock import MagicMock
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk._logs.export import (
    SimpleLogRecordProcessor,
    InMemoryLogRecordExporter,
)

from app.main import app, classification_service
from app.models.api_models import ClassificationResponse
from app.core.identity import (
    CredentialScope,
    current_scope,
    http_scope_enforced,
    credential_registry,
)
from app.telemetry import opentelemetry_client as ot, structured_logs as logs

SCOPES = [
    CredentialScope("school", "production", "prod-one", True),
    CredentialScope("school", "production", "prod-two", True),
    CredentialScope("school", "test", "test-one", True),
    CredentialScope("other", "production", "other-one", True),
]


@pytest.fixture
def registry(monkeypatch):
    config = {}
    for i, scope in enumerate(SCOPES):
        monkeypatch.setenv("SYNTHETIC_TOKEN_" + str(i), "synthetic-secret-" + str(i))
        config[scope.key_id] = {
            "client_id": scope.client_id,
            "environment": scope.environment,
            "token_env": "SYNTHETIC_TOKEN_" + str(i),
            "capture_content": True,
        }
    monkeypatch.setenv("API_CREDENTIALS", json.dumps(config))
    monkeypatch.setenv("API_TOKEN_CLIENT_IDS", "legacy=legacy-secret")
    monkeypatch.setenv("API_TOKENS", "unmapped-secret")
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_LOGS_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_ENVIRONMENT", "production")
    monkeypatch.setenv("DASHBOARD_ENABLED", "false")
    return config


@pytest.fixture
def exporters(monkeypatch, registry):
    traces, summaries = {}, {}

    def trace_factory(**kwargs):
        route = kwargs.get("connection_string") or kwargs["endpoint"]
        traces[route] = InMemorySpanExporter()
        return traces[route]

    def log_factory(**kwargs):
        route = kwargs.get("connection_string") or kwargs["endpoint"]
        summaries[route] = InMemoryLogRecordExporter()
        return summaries[route]

    monkeypatch.setattr(ot, "_isolated_providers", {})
    monkeypatch.setattr(ot, "_isolated_tracers", {})
    monkeypatch.setattr(logs, "_providers", {})
    monkeypatch.setattr(ot, "BatchSpanProcessor", SimpleSpanProcessor)
    monkeypatch.setattr(logs, "BatchLogRecordProcessor", SimpleLogRecordProcessor)
    monkeypatch.setattr(ot, "AzureMonitorTraceExporter", trace_factory)
    monkeypatch.setattr(ot, "OTLPSpanExporter", trace_factory)
    monkeypatch.setattr(logs, "AzureMonitorLogExporter", log_factory)
    monkeypatch.setattr(logs, "OTLPLogExporter", log_factory)
    shared = MagicMock(side_effect=AssertionError("Shared fallback is forbidden"))
    monkeypatch.setattr(ot, "_get_tracer", shared)
    yield traces, summaries, shared
    for provider in ot._isolated_providers.values():
        provider.shutdown()
    for provider in logs._providers.values():
        provider.shutdown()


def routes(kind):
    return {
        scope.scope_id: (
            {"exporter": "azure", "connection_string": "azure-" + scope.key_id}
            if kind == "azure"
            else {"exporter": "otlp", "endpoint": "http://collector/" + scope.key_id}
        )
        for scope in SCOPES
    }


def result(client_id):
    return ClassificationResponse(
        client_id=client_id,
        request_id="synthetic-request",
        labels=[],
        follow_up_questions=[],
        likely_no_legal_problem={
            "value": False,
            "weighted_result": 0,
            "disagreement_score": 0,
        },
    )


async def instrumented(request, **kwargs):
    # Even metadata from a misbehaving call site cannot override auth identity.
    root = ot.start_request_trace(
        "synthetic",
        {"client_id": "spoofed", "environment": "production"},
        metadata={
            "client_id": "spoofed",
            "environment": "production",
            "api_key_id": "spoofed",
            "scope_id": "spoofed",
            "conversation_id": "same-session",
            "request_id": "synthetic-request",
        },
    )
    await asyncio.sleep(0)
    child = ot.start_provider_generation(
        root, name="model", model="synthetic", input_payload={}
    )
    await asyncio.sleep(0)
    ot.finalize_provider_generation(child)
    ot.finalize_request_trace(root)
    return result(kwargs["client_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["azure", "otlp"])
async def test_concurrent_http_scopes_isolate_roots_children_logs(
    kind, monkeypatch, exporters
):
    traces, summaries, shared = exporters
    monkeypatch.setenv("TELEMETRY_SCOPE_DESTINATIONS", json.dumps(routes(kind)))
    monkeypatch.setattr(
        "app.main.classification_service",
        SimpleNamespace(allowed_providers=None, classify=instrumented),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/api/classify",
                    headers={"Authorization": f"Bearer synthetic-secret-{i}"},
                    json={
                        "text": "Synthetic",
                        "client_id": "spoofed",
                        "environment": "production",
                        "api_key_id": "spoofed",
                        "conversation_id": "same-session",
                    },
                )
                for i in range(4)
            ]
        )
    assert all(r.status_code == 200 for r in responses)
    for i, scope in enumerate(SCOPES):
        route = (
            "azure-" + scope.key_id
            if kind == "azure"
            else "http://collector/" + scope.key_id + "/v1/"
        )
        spans = traces[
            route if kind == "azure" else route + "traces"
        ].get_finished_spans()
        records = summaries[
            route if kind == "azure" else route + "logs"
        ].get_finished_logs()
        assert len(spans) == 2 and len(records) == 1
        child, root = spans
        assert (
            child.context.trace_id == root.context.trace_id
            and child.parent.span_id == root.context.span_id
        )
        assert responses[i].json()["client_id"] == scope.client_id
        for span in spans:
            assert span.attributes["fetch.scope_id"] == scope.scope_id
            assert span.attributes["fetch.api_key_id"] == scope.key_id
            assert (
                span.resource.attributes["deployment.environment"] == scope.environment
            )
            assert "synthetic-secret" not in str(dict(span.attributes))
        log = records[0].log_record
        assert log.attributes["fetch.scope_id"] == scope.scope_id
        assert log.trace_id == root.context.trace_id
        assert (
            records[0].resource.attributes["deployment.environment"]
            == scope.environment
        )
    shared.assert_not_called()
    assert current_scope.get() is None and http_scope_enforced.get() is False


@pytest.mark.parametrize(
    "failure", ["missing", "invalid", "duplicate", "trace-init", "log-init"]
)
def test_missing_or_failed_destination_never_falls_back(
    failure, monkeypatch, exporters
):
    traces, summaries, shared = exporters
    mapping = routes("otlp")
    if failure == "missing":
        mapping.pop(SCOPES[2].scope_id)
    if failure == "invalid":
        mapping[SCOPES[2].scope_id] = {"exporter": "unsupported"}
    if failure == "duplicate":
        mapping[SCOPES[2].scope_id] = mapping[SCOPES[0].scope_id]
    if failure == "trace-init":
        monkeypatch.setattr(
            ot, "OTLPSpanExporter", MagicMock(side_effect=ValueError("secret config"))
        )
    if failure == "log-init":
        monkeypatch.setattr(
            logs, "OTLPLogExporter", MagicMock(side_effect=ValueError("secret config"))
        )
    monkeypatch.setenv("TELEMETRY_SCOPE_DESTINATIONS", json.dumps(mapping))
    monkeypatch.setattr(
        "app.main.classification_service",
        SimpleNamespace(allowed_providers=None, classify=instrumented),
    )
    response = TestClient(app).post(
        "/api/classify",
        headers={"Authorization": "Bearer synthetic-secret-2"},
        json={"text": "Synthetic"},
    )
    assert response.status_code == 200
    assert not summaries
    if failure != "log-init":
        assert not traces
    shared.assert_not_called()


@pytest.mark.parametrize("token", ["legacy-secret", "unmapped-secret", None])
def test_unscoped_http_never_uses_shared_telemetry(token, monkeypatch, exporters):
    traces, summaries, shared = exporters
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv(
        "TELEMETRY_CLIENT_DESTINATIONS",
        '{"legacy":{"exporter":"azure","connection_string":"old"}}',
    )
    monkeypatch.setattr(
        "app.main.classification_service",
        SimpleNamespace(allowed_providers=None, classify=instrumented),
    )
    if token is None:
        # Anonymous dev mode is allowed only when no credentials are configured.
        monkeypatch.setenv("API_CREDENTIALS", "{}")
        monkeypatch.delenv("API_TOKEN_CLIENT_IDS")
        monkeypatch.delenv("API_TOKENS")
    response = TestClient(app).post(
        "/api/classify",
        headers={"Authorization": "Bearer " + token} if token else {},
        json={"text": "Synthetic"},
    )
    assert response.status_code == 200
    assert not traces and not summaries
    shared.assert_not_called()


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate-secret",
        "missing-secret",
        "missing-environment",
        "legacy-conflict",
        "bad-json",
    ],
)
def test_ambiguous_credentials_fail_closed(problem, registry, monkeypatch):
    config = registry
    if problem == "duplicate-secret":
        config["prod-two"]["token_env"] = "SYNTHETIC_TOKEN_0"
    if problem == "missing-secret":
        config["prod-two"]["token_env"] = "DOES_NOT_EXIST"
    if problem == "missing-environment":
        config["prod-two"].pop("environment")
    if problem == "legacy-conflict":
        monkeypatch.setenv("API_TOKEN_CLIENT_IDS", "other=synthetic-secret-0")
    monkeypatch.setenv(
        "API_CREDENTIALS",
        "invalid-json" if problem == "bad-json" else json.dumps(config),
    )
    response = TestClient(app).post(
        "/api/classify",
        headers={"Authorization": "Bearer synthetic-secret-0"},
        json={"text": "Synthetic"},
    )
    assert response.status_code == 503
    assert "synthetic-secret" not in response.text


def test_scope_content_policy_is_not_broadened_by_client_allowlist(
    registry, exporters, monkeypatch
):
    traces, _, _ = exporters
    registry["test-one"]["capture_content"] = False
    monkeypatch.setenv("API_CREDENTIALS", json.dumps(registry))
    monkeypatch.setenv("TELEMETRY_CONTENT_CLIENT_IDS", "school")
    monkeypatch.setenv("TELEMETRY_SCOPE_DESTINATIONS", json.dumps(routes("otlp")))
    monkeypatch.setattr(
        "app.main.classification_service",
        SimpleNamespace(allowed_providers=None, classify=instrumented),
    )
    assert (
        TestClient(app)
        .post(
            "/api/classify",
            headers={"Authorization": "Bearer synthetic-secret-2"},
            json={"text": "Synthetic"},
        )
        .status_code
        == 200
    )
    spans = traces["http://collector/test-one/v1/traces"].get_finished_spans()
    assert len(spans) == 1 and spans[0].attributes["fetch.content_captured"] is False


def test_dashboard_test_and_prod_partition_separately(registry, monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_STORAGE", "sqlite")
    monkeypatch.setenv("DASHBOARD_SQLITE_PATH", str(tmp_path / "synthetic.db"))
    monkeypatch.setenv(
        "DASHBOARD_TOKEN_CLIENT_IDS",
        SCOPES[2].scope_id
        + "=review-test,"
        + SCOPES[0].scope_id
        + "=review-prod,school=legacy-review",
    )
    with TestClient(app, base_url="https://testserver") as client:
        for i in [0, 1, 2]:
            assert (
                client.post(
                    "/api/activity/outcome",
                    headers={"Authorization": f"Bearer synthetic-secret-{i}"},
                    json={"conversation_id": "same-session", "outcome": "in_progress"},
                ).status_code
                == 202
            )
        client.post("/dashboard/login", json={"token": "review-test"})
        records = client.get("/dashboard/api/activity").json()["events"]
        assert len(records) == 1 and records[0]["client_id"] == SCOPES[2].scope_id
        client.post("/dashboard/login", json={"token": "legacy-review"})
        assert client.get("/dashboard/api/activity").json()["events"] == []


def test_unknown_key_has_401_with_registry_only(registry, monkeypatch):
    monkeypatch.delenv("API_TOKENS", raising=False)
    monkeypatch.delenv("API_TOKEN_CLIENT_IDS", raising=False)
    response = TestClient(app).post(
        "/api/classify",
        headers={"Authorization": "Bearer invalid"},
        json={"text": "Synthetic"},
    )
    assert response.status_code == 401


def test_credential_context_reset_after_exception(registry, monkeypatch):
    async def fail(*args, **kwargs):
        assert current_scope.get().environment == "test"
        raise RuntimeError("Synthetic failure")

    monkeypatch.setattr(
        "app.main.classification_service",
        SimpleNamespace(allowed_providers=None, classify=fail),
    )
    client = TestClient(app, raise_server_exceptions=False)
    assert (
        client.post(
            "/api/classify",
            headers={"Authorization": "Bearer synthetic-secret-2"},
            json={"text": "Synthetic"},
        ).status_code
        == 500
    )
    assert current_scope.get() is None and http_scope_enforced.get() is False


def test_unscoped_internal_eval_cannot_use_shared_destination(registry, exporters):
    traces, summaries, shared = exporters
    assert (
        ot.start_request_trace(
            "Synthetic test",
            {},
            metadata={"client_id": "school", "environment": "production"},
        )
        is None
    )
    assert not traces and not summaries
    shared.assert_not_called()


def test_real_classification_uses_authenticated_environment(
    registry, exporters, monkeypatch
):
    traces, summaries, _ = exporters
    monkeypatch.setenv("TELEMETRY_SCOPE_DESTINATIONS", json.dumps(routes("otlp")))
    response = TestClient(app).post(
        "/api/classify",
        headers={"Authorization": "Bearer synthetic-secret-2"},
        json={
            "text": "I need help establishing paternity.",
            "conversation_id": "synthetic-session",
            "skip_followups": True,
            "enable_screening_protocols": False,
            "client_id": "other",
            "environment": "production",
            "api_key_id": "prod-one",
        },
    )
    assert response.status_code == 200
    assert response.json()["client_id"] == "school"
    spans = traces["http://collector/test-one/v1/traces"].get_finished_spans()
    assert spans and all(
        s.attributes["fetch.scope_id"] == SCOPES[2].scope_id for s in spans
    )
    assert len(summaries["http://collector/test-one/v1/logs"].get_finished_logs()) == 1
