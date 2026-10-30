import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from app.telemetry import (
    destinations as d,
    opentelemetry_client as ot,
    structured_logs as logs,
)


@pytest.mark.parametrize("kind", ["azure", "otlp", "aws"])
@pytest.mark.parametrize("signal", ["traces", "logs"])
def test_exporter_factory_cloud_and_signal_parity(kind, signal, monkeypatch):
    monkeypatch.setenv("TEST_CONNECTION", "synthetic-connection")
    azure, otlp = MagicMock(), MagicMock()
    cfg = {
        "exporter": kind,
        "connection_string_env": "TEST_CONNECTION",
        "endpoint": "http://collector:4318",
    }
    result = d.create_exporter(
        cfg, signal=signal, azure_factory=azure, otlp_factory=otlp
    )
    if kind == "azure":
        azure.assert_called_once_with(connection_string="synthetic-connection")
        otlp.assert_not_called()
    else:
        otlp.assert_called_once_with(
            endpoint="http://collector:4318/v1/" + signal, headers=None, timeout=5
        )
        azure.assert_not_called()


def test_destination_maps_legacy_and_new(monkeypatch):
    monkeypatch.setenv("TELEMETRY_AZURE_CLIENT_CONNECTION_STRINGS", '{"old":"legacy"}')
    monkeypatch.setenv(
        "TELEMETRY_CLIENT_DESTINATIONS",
        '{"new":{"exporter":"otlp","endpoint":"http://collector:4318"}}',
    )
    assert set(d.client_destinations()) == {"old", "new"}
    monkeypatch.setenv("TELEMETRY_CLIENT_DESTINATIONS", "[]")
    with pytest.raises(ValueError):
        d.client_destinations()


@pytest.mark.parametrize("kind", ["azure", "otlp"])
def test_isolated_destination_routes_without_shared_fallback(kind, monkeypatch):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv(
        "TELEMETRY_CLIENT_DESTINATIONS",
        json.dumps(
            {
                "school": {
                    "exporter": kind,
                    "endpoint": "http://collector:4318",
                    "connection_string": "synthetic",
                }
            }
        ),
    )
    monkeypatch.setenv("TELEMETRY_AZURE_CLIENT_CONNECTION_STRINGS", "{}")
    monkeypatch.setenv("TELEMETRY_CONTENT_CLIENT_IDS", "school")
    monkeypatch.setattr(ot, "_isolated_tracers", {})
    monkeypatch.setattr(ot, "_isolated_providers", {})
    monkeypatch.setattr(ot, "BatchSpanProcessor", SimpleSpanProcessor)
    monkeypatch.setattr(ot, "AzureMonitorTraceExporter", lambda **kwargs: exporter)
    monkeypatch.setattr(ot, "OTLPSpanExporter", lambda **kwargs: exporter)

    def forbidden():
        raise AssertionError("Must not fall back to shared")

    monkeypatch.setattr(ot, "_get_tracer", forbidden)
    root = ot.start_request_trace(
        "request",
        {"problem_description": "synthetic"},
        metadata={"client_id": "school"},
    )
    child = ot.start_provider_generation(
        root, name="provider", model="synthetic", input_payload={}
    )
    ot.finalize_provider_generation(child)
    ot.finalize_request_trace(root)
    spans = exporter.get_finished_spans()
    assert len(spans) == 2 and spans[0].context.trace_id == spans[1].context.trace_id
    for provider in ot._isolated_providers.values():
        provider.shutdown()


def test_bad_generic_destination_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv(
        "TELEMETRY_CLIENT_DESTINATIONS",
        '{"school":{"exporter":"otlp","endpoint_env":"MISSING"}}',
    )
    monkeypatch.setattr(ot, "_isolated_tracers", {})
    shared = MagicMock()
    monkeypatch.setattr(ot, "_get_tracer", shared)
    assert ot._get_client_tracer("school") == (None, True)
    shared.assert_not_called()


def test_isolation_does_not_imply_content_permission(monkeypatch):
    monkeypatch.delenv("TELEMETRY_CONTENT_CLIENT_IDS", raising=False)
    tracer = MagicMock()
    monkeypatch.setattr(ot, "_get_client_tracer", lambda client: (tracer, True))
    handle = ot.start_request_trace(
        "PRIVATE", {"text": "PRIVATE"}, metadata={"client_id": "school"}
    )
    assert handle.capture_content is False
    assert "PRIVATE" not in str(tracer.mock_calls)


@pytest.mark.parametrize("kind", ["azure", "otlp"])
def test_structured_log_parity_and_redaction(kind, monkeypatch):
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_LOGS_ENABLED", "true")
    monkeypatch.setenv(
        "TELEMETRY_CLIENT_DESTINATIONS",
        json.dumps(
            {
                "school": {
                    "exporter": kind,
                    "endpoint": "http://collector:4318",
                    "connection_string": "synthetic",
                }
            }
        ),
    )
    monkeypatch.setattr(logs, "_providers", {})
    provider = MagicMock()
    monkeypatch.setattr(logs, "LoggerProvider", lambda **kw: provider)
    monkeypatch.setattr(logs, "BatchLogRecordProcessor", lambda exporter: exporter)
    monkeypatch.setattr(logs, "AzureMonitorLogExporter", lambda **kw: "azure")
    monkeypatch.setattr(logs, "OTLPLogExporter", lambda **kw: "otlp")
    monkeypatch.setattr(logs, "set_span_in_context", lambda span: None)
    handle = SimpleNamespace(
        correlation={
            "client_id": "school",
            "request_id": "synthetic",
            "untrusted": "PRIVATE",
        },
        span=None,
    )
    logs.emit_request_summary(handle, ValueError("PRIVATE exception message"))
    args = provider.get_logger.return_value.emit.call_args.kwargs
    assert args["attributes"]["fetch.client_id"] == "school"
    assert args["attributes"]["error.type"] == "ValueError"
    assert "PRIVATE" not in str(args)
    provider.add_log_record_processor.assert_called_once_with(kind)


def test_explicit_otlp_is_not_overridden_by_langfuse(monkeypatch):
    monkeypatch.setenv("TELEMETRY_EXPORTER", "otlp")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://collector:4318/v1/traces"
    )
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", raising=False)
    monkeypatch.setattr(
        ot,
        "_build_langfuse_otlp_config",
        lambda: ("https://other.example/v1/traces", {"Authorization": "private"}),
    )
    exporter = MagicMock()
    monkeypatch.setattr(ot, "OTLPSpanExporter", exporter)
    ot._build_span_exporter()
    assert exporter.call_args.kwargs["endpoint"] == "http://collector:4318/v1/traces"
    assert exporter.call_args.kwargs["headers"] is None
