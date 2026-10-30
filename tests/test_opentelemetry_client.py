import pytest
import asyncio
import json
import threading

from app.telemetry import opentelemetry_client


def test_record_telemetry_event_redacts_query_from_input_payload(monkeypatch):
    class FakeSpan:
        def __init__(self):
            self.attributes = {}
            self.ended = False

        def set_attribute(self, key, value):
            self.attributes[key] = value

        def end(self):
            self.ended = True

    class FakeTracer:
        def __init__(self):
            self.span = FakeSpan()

        def start_span(self, **kwargs):
            return self.span

    monkeypatch.setenv("TELEMETRY_CONTENT_CLIENT_IDS", "osbar-referral")
    tracer = FakeTracer()
    monkeypatch.setattr(opentelemetry_client, "_get_tracer", lambda: tracer)
    monkeypatch.setenv("TELEMETRY_CAPTURE_FULL_QUERY", "true")

    opentelemetry_client.record_telemetry_event(
        name="osbar.interaction.completed",
        event_payload={
            "event_type": "osbar.interaction.completed",
            "query": "private housing facts",
            "followup_questions_answers": [{"question": "Q", "answer": "a" * 12000}],
        },
        client_id="osbar-referral",
    )

    trace_input = json.loads(tracer.span.attributes["langfuse.trace.input"])
    assert trace_input["query"].startswith("[redacted;")
    assert (
        tracer.span.attributes["fetch.user_query.chunk_01"] == "private housing facts"
    )
    assert tracer.span.ended is True
    chunks = [
        value
        for key, value in sorted(tracer.span.attributes.items())
        if key.startswith("fetch.event.chunk_")
    ]
    assert len(chunks) >= 3
    assert all(len(value) <= 6000 for value in chunks)
    assert json.loads("".join(chunks)) == trace_input


def test_telemetry_disabled_is_per_call_gate(monkeypatch):
    monkeypatch.setattr(opentelemetry_client, "_tracer", None)
    monkeypatch.setattr(opentelemetry_client, "_tracer_provider", None)
    monkeypatch.setattr(opentelemetry_client, "_client_disabled", False)
    monkeypatch.setenv("TELEMETRY_ENABLED", "0")
    monkeypatch.setenv("AZURE_MONITOR_CONNECTION_STRING", "InstrumentationKey=test")

    assert opentelemetry_client._get_tracer() is None
    assert opentelemetry_client._client_disabled is False


def test_build_langfuse_otlp_config(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")

    endpoint, headers = opentelemetry_client._build_langfuse_otlp_config()

    assert endpoint == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    assert headers["Authorization"].startswith("Basic ")
    assert headers["x-langfuse-ingestion-version"] == "4"


def test_flush_telemetry_async_swallows_flush_errors(monkeypatch):
    def fail_flush():
        raise RuntimeError("exporter failed")

    monkeypatch.setattr(opentelemetry_client, "flush_telemetry", fail_flush)

    asyncio.run(opentelemetry_client.flush_telemetry_async())


def test_flush_telemetry_async_times_out(monkeypatch):
    def slow_flush():
        import time

        time.sleep(0.1)

    monkeypatch.setattr(opentelemetry_client, "TELEMETRY_FLUSH_TIMEOUT_MS", 1)
    monkeypatch.setattr(opentelemetry_client, "flush_telemetry", slow_flush)

    asyncio.run(opentelemetry_client.flush_telemetry_async())


def test_flush_telemetry_skips_when_flush_already_running(monkeypatch):
    class Provider:
        def force_flush(self, timeout_millis):
            raise AssertionError("force_flush should not be called")

    locked = threading.Lock()
    locked.acquire()
    monkeypatch.setattr(opentelemetry_client, "_tracer_provider", Provider())
    monkeypatch.setattr(opentelemetry_client, "_flush_lock", locked)

    try:
        opentelemetry_client.flush_telemetry()
    finally:
        locked.release()


class RecordingSpan:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.ended = False

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status

    def record_exception(self, error):
        self.attributes["exception"] = str(error)

    def end(self):
        self.ended = True


class RecordingTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, **kwargs):
        span = RecordingSpan(name)
        self.spans.append(span)
        return span


def test_non_osb_contents_never_exported(monkeypatch):
    tracer = RecordingTracer()
    monkeypatch.setattr(opentelemetry_client, "_get_tracer", lambda: tracer)
    monkeypatch.setenv("TELEMETRY_CONTENT_CLIENT_IDS", "osbar-referral")
    monkeypatch.setenv("TELEMETRY_CAPTURE_FULL_QUERY", "true")
    for client_id in ("other-client", None):
        handle = opentelemetry_client.start_request_trace(
            name="PRIVATE query",
            input_payload={"problem_description": "PRIVATE input"},
            full_query_text="PRIVATE full query",
            metadata={
                "client_id": client_id,
                "request_id": "r1",
                "conversation_id": "s1",
                "client_request_id": "c1",
                "user_id": "u1",
                "unexpected": "PRIVATE metadata",
            },
        )
        assert (
            opentelemetry_client.start_provider_generation(
                handle,
                name="semantic_merge",
                model="test",
                input_payload={"prompt": "PRIVATE prompt"},
            )
            is None
        )
        opentelemetry_client.finalize_request_trace(
            handle,
            output_payload={"answer": "PRIVATE output"},
            error=ValueError("PRIVATE error"),
        )
        span = tracer.spans[-1]
        assert span.name == "classification.request"
        assert span.ended
        assert span.attributes["fetch.content_captured"] is False
        assert span.attributes["fetch.request_id"] == "r1"
        assert span.attributes["fetch.client_request_id"] == "c1"
        assert span.attributes["fetch.user_id"] == "u1"
        assert "PRIVATE" not in json.dumps(span.attributes)
    assert len(tracer.spans) == 2
    opentelemetry_client.record_telemetry_event(
        name="osbar.interaction.completed",
        event_payload={"query": "PRIVATE"},
        client_id="wisconsin",
    )
    assert len(tracer.spans) == 2


def test_osb_keeps_content_and_children_inherit_identity(monkeypatch):
    tracer = RecordingTracer()
    monkeypatch.setattr(opentelemetry_client, "_get_tracer", lambda: tracer)
    monkeypatch.setenv("TELEMETRY_CONTENT_CLIENT_IDS", "osbar-referral,wisconsin")
    monkeypatch.setattr(opentelemetry_client, "set_span_in_context", lambda span: None)
    handle = opentelemetry_client.start_request_trace(
        name="OSB query",
        input_payload={"problem_description": "OSB input"},
        metadata={
            "client_id": "osbar-referral",
            "request_id": "r1",
            "conversation_id": "s1",
        },
    )
    child = opentelemetry_client.start_provider_generation(
        handle,
        name="provider",
        model="test",
        input_payload={"prompt": "OSB prompt"},
    )
    opentelemetry_client.finalize_provider_generation(
        child, output_payload={"answer": "OSB output"}
    )
    opentelemetry_client.finalize_request_trace(
        handle, output_payload={"answer": "OSB result"}
    )
    assert len(tracer.spans) == 2
    for span in tracer.spans:
        assert span.attributes["fetch.client_id"] == "osbar-referral"
        assert span.attributes["fetch.request_id"] == "r1"
        assert span.attributes["fetch.conversation_id"] == "s1"
        assert span.ended
    assert "OSB input" in tracer.spans[0].attributes["langfuse.trace.input"]
    assert "OSB output" in tracer.spans[1].attributes["langfuse.observation.output"]


def test_empty_content_allowlist_disables_content(monkeypatch):
    monkeypatch.setenv("TELEMETRY_CONTENT_CLIENT_IDS", "")
    assert not opentelemetry_client._content_capture_allowed("osbar-referral")


def test_isolated_destination_missing_or_invalid_never_falls_back(monkeypatch):
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_ISOLATED_CLIENT_IDS", "wisconsin")
    shared = RecordingTracer()
    monkeypatch.setattr(opentelemetry_client, "_get_tracer", lambda: shared)
    for config in ("{}", '{"wisconsin":""}', "invalid-json", "[]"):
        monkeypatch.setenv("TELEMETRY_AZURE_CLIENT_CONNECTION_STRINGS", config)
        assert (
            opentelemetry_client.start_request_trace(
                name="Private",
                input_payload={"problem_description": "Private"},
                metadata={"client_id": "wisconsin"},
            )
            is None
        )
    assert shared.spans == []


def test_isolated_traces_and_children_export_only_to_their_destination(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    shared_exporter = InMemorySpanExporter()
    isolated_exporter = InMemorySpanExporter()
    shared_provider = TracerProvider()
    shared_provider.add_span_processor(SimpleSpanProcessor(shared_exporter))
    monkeypatch.setattr(
        opentelemetry_client, "_get_tracer", lambda: shared_provider.get_tracer("test")
    )
    monkeypatch.setattr(opentelemetry_client, "_tracer_provider", shared_provider)
    monkeypatch.setattr(opentelemetry_client, "_isolated_tracers", {})
    monkeypatch.setattr(opentelemetry_client, "_isolated_providers", {})
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv(
        "TELEMETRY_AZURE_CLIENT_CONNECTION_STRINGS",
        '{"wisconsin":"isolated-connection"}',
    )
    monkeypatch.setenv("TELEMETRY_ISOLATED_CLIENT_IDS", "wisconsin")
    monkeypatch.setenv("TELEMETRY_CONTENT_CLIENT_IDS", "osbar-referral,wisconsin")
    monkeypatch.setattr(opentelemetry_client, "BatchSpanProcessor", SimpleSpanProcessor)
    connections = []

    def exporter(**kwargs):
        connections.append(kwargs["connection_string"])
        assert (
            kwargs["tracer_provider"].resource.attributes["fetch.client_id"]
            == "wisconsin"
        )
        return isolated_exporter

    monkeypatch.setattr(opentelemetry_client, "AzureMonitorTraceExporter", exporter)
    handles = {}
    # Interleave organizations to catch routing through process-global state.
    for client_id in ("wisconsin", "osbar-referral", "wisconsin"):
        root = opentelemetry_client.start_request_trace(
            name=client_id + " query",
            input_payload={"problem_description": client_id + " input"},
            metadata={
                "client_id": client_id,
                "request_id": client_id + "-r1",
                "conversation_id": "same-session",
                "client_request_id": "c1",
                "user_id": "u1",
            },
        )
        handles[client_id] = root
        child = opentelemetry_client.start_provider_generation(
            root,
            name="semantic_merge",
            model="test",
            input_payload={"prompt": client_id + " prompt"},
        )
        opentelemetry_client.finalize_provider_generation(
            child, output_payload={"answer": client_id + " output"}
        )
        opentelemetry_client.finalize_request_trace(
            root, output_payload={"answer": client_id + " result"}
        )
    opentelemetry_client.flush_telemetry()
    assert connections == ["isolated-connection"]
    assert len(shared_exporter.get_finished_spans()) == 2
    assert len(isolated_exporter.get_finished_spans()) == 4
    for exporter, client_id in (
        (shared_exporter, "osbar-referral"),
        (isolated_exporter, "wisconsin"),
    ):
        spans = exporter.get_finished_spans()
        for span in spans:
            assert span.attributes["fetch.client_id"] == client_id
            assert span.attributes["fetch.request_id"] == client_id + "-r1"
            assert span.attributes["fetch.client_request_id"] == "c1"
        child, root = spans[:2]
        assert child.context.trace_id == root.context.trace_id
        assert child.parent.span_id == root.context.span_id
        assert client_id + " prompt" in child.attributes["langfuse.observation.input"]
        assert client_id + " result" in root.attributes["langfuse.trace.output"]
    shared_provider.shutdown()
    for provider in opentelemetry_client._isolated_providers.values():
        provider.shutdown()


def test_isolated_exporter_failure_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv(
        "TELEMETRY_AZURE_CLIENT_CONNECTION_STRINGS", '{"wisconsin":"invalid"}'
    )
    monkeypatch.setattr(opentelemetry_client, "_isolated_tracers", {})
    shared = RecordingTracer()
    monkeypatch.setattr(opentelemetry_client, "_get_tracer", lambda: shared)

    def fail(**kwargs):
        raise ValueError("invalid exporter credentials")

    monkeypatch.setattr(opentelemetry_client, "AzureMonitorTraceExporter", fail)
    assert (
        opentelemetry_client.start_request_trace(
            name="Private",
            input_payload={"text": "Private"},
            metadata={"client_id": "wisconsin"},
        )
        is None
    )
    assert shared.spans == []
