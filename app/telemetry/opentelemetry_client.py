from __future__ import annotations
from urllib.parse import unquote

import asyncio
import base64
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, set_span_in_context

from app.telemetry.structured_logs import emit_request_summary, flush_logs
from app.telemetry.destinations import (
    client_destinations,
    create_exporter,
    scope_destination,
)
from app.core.identity import (
    CredentialScope,
    current_scope,
    http_scope_enforced,
    scoped_auth_configured,
)
from app.utils.logging import get_logger
from app.core.config import TELEMETRY_FLUSH_TIMEOUT_MS

try:
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
except ImportError:  # pragma: no cover - optional dependency at runtime
    AzureMonitorTraceExporter = None

if os.getenv("ENV") != "production":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

logger = get_logger(__name__)

_tracer = None
_tracer_provider: Optional[TracerProvider] = None
_client_disabled = False
_flush_lock = threading.Lock()
_isolated_lock = threading.Lock()
_isolated_tracers: Dict[Any, Any] = {}
_isolated_providers: Dict[Any, TracerProvider] = {}


@dataclass
class TelemetrySpanHandle:
    span: Span
    kind: str
    capture_content: bool = True
    correlation: Dict[str, Any] = field(default_factory=dict)
    tracer: Optional[Any] = None
    scope: Optional[CredentialScope] = None


def _content_capture_allowed(client_id: Optional[str]) -> bool:
    """Only explicitly authorized clients may send content to the shared sink."""
    allowed = {
        value.strip()
        for value in os.getenv("TELEMETRY_CONTENT_CLIENT_IDS", "").split(",")
        if value.strip()
    }
    return bool(client_id and client_id in allowed)


def _set_correlation_attributes(span: Span, correlation: Dict[str, Any]) -> None:
    for key, value in correlation.items():
        if value is not None:
            span.set_attribute(f"fetch.{key}", str(value))


def _isolated_connections():
    try:
        return client_destinations()
    except (ValueError, TypeError):
        logger.error("Invalid client telemetry destinations; export skipped.")
        return None


def _isolated_client_ids() -> set[str]:
    return {
        value.strip()
        for value in os.getenv("TELEMETRY_ISOLATED_CLIENT_IDS", "").split(",")
        if value.strip()
    }


def _get_scoped_tracer(scope: CredentialScope):
    if not _env_flag_enabled("TELEMETRY_ENABLED", default=False):
        return None, True
    try:
        destination = scope_destination(scope)
        if destination is None:
            return None, True
        cache_key = ("credential", scope.scope_id)
        with _isolated_lock:
            if cache_key in _isolated_tracers:
                return _isolated_tracers[cache_key], True
            attributes = {"fetch." + k: v for k, v in scope.attributes().items()}
            attributes.update(
                {
                    "service.name": os.getenv("TELEMETRY_SERVICE_NAME", "fetch"),
                    "deployment.environment": scope.environment,
                    "deployment.environment.name": scope.environment,
                }
            )
            provider = TracerProvider(resource=Resource.create(attributes))
            exporter = create_exporter(
                destination,
                provider=provider,
                azure_factory=AzureMonitorTraceExporter,
                otlp_factory=OTLPSpanExporter,
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            tracer = provider.get_tracer("app.telemetry")
            _isolated_providers[cache_key] = provider
            _isolated_tracers[cache_key] = tracer
            return tracer, True
    except Exception:
        logger.warning("Scoped telemetry destination unavailable; export skipped.")
        return None, True


def _get_client_tracer(client_id: Optional[str]) -> Tuple[Optional[Any], bool]:
    """Select a destination from authenticated identity; isolated routes fail closed."""
    scope = current_scope.get()
    if scope is not None:
        return _get_scoped_tracer(scope)
    if http_scope_enforced.get() or scoped_auth_configured():
        # Anonymous development and legacy unscoped credentials never export.
        return None, True
    connections = _isolated_connections()
    if connections is None:
        return None, False
    isolated = client_id in _isolated_client_ids() or client_id in connections
    if not isolated:
        return _get_tracer(), False
    if not _env_flag_enabled("TELEMETRY_ENABLED", default=False):
        return None, True
    destination = connections.get(client_id)
    if not destination:
        logger.warning(
            "Isolated telemetry destination unavailable for client %s; export skipped.",
            client_id,
        )
        return None, True
    with _isolated_lock:
        if client_id in _isolated_tracers:
            return _isolated_tracers[client_id], True
        try:
            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": os.getenv(
                            "TELEMETRY_SERVICE_NAME", "fetch"
                        ).strip()
                        or "fetch",
                        "deployment.environment": os.getenv("TELEMETRY_ENVIRONMENT")
                        or os.getenv("ENV")
                        or "prod",
                        "fetch.client_id": client_id,
                    }
                )
            )
            exporter = create_exporter(
                destination,
                provider=provider,
                azure_factory=AzureMonitorTraceExporter,
                otlp_factory=OTLPSpanExporter,
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            tracer = provider.get_tracer("app.telemetry")
            # Do not install isolated providers globally: child spans use this exact tracer.
            _isolated_providers[client_id] = provider
            _isolated_tracers[client_id] = tracer
            return tracer, True
        except Exception:
            logger.warning(
                "Unable to initialize isolated telemetry for client %s; export skipped.",
                client_id,
            )
            return None, True


def _env_flag_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _serialize_value(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=True, sort_keys=True)


def _set_large_text_attributes(
    span: Span,
    *,
    key_prefix: str,
    value: Optional[str],
    chunk_size: int = 7000,
    max_chunks: int = 8,
) -> None:
    if not value:
        return
    text = str(value)
    span.set_attribute(f"{key_prefix}.length", len(text))
    span.set_attribute(
        f"{key_prefix}.is_truncated", len(text) > chunk_size * max_chunks
    )
    chunks = [
        text[i : i + chunk_size]
        for i in range(0, min(len(text), chunk_size * max_chunks), chunk_size)
    ]
    for idx, chunk in enumerate(chunks, start=1):
        span.set_attribute(f"{key_prefix}.chunk_{idx:02d}", chunk)


def _capture_full_query_enabled() -> bool:
    return _env_flag_enabled("TELEMETRY_CAPTURE_FULL_QUERY", default=True)


def _sanitize_metadata_key(key: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in key)


def _normalize_otlp_traces_endpoint(endpoint: str) -> str:
    normalized = endpoint.strip().rstrip("/")
    if normalized.endswith("/v1/traces"):
        return normalized
    return f"{normalized}/v1/traces"


def _parse_otlp_headers(value: str) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for entry in value.split(","):
        segment = entry.strip()
        if not segment or "=" not in segment:
            continue
        key, header_value = segment.split("=", 1)
        key = key.strip()
        header_value = header_value.strip()
        if key and header_value:
            headers[key] = unquote(header_value)
    return headers


def _build_langfuse_otlp_config() -> Optional[Tuple[str, Dict[str, str]]]:
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    base_url = os.getenv("LANGFUSE_BASE_URL", "").strip()
    if not public_key or not secret_key or not base_url:
        return None

    auth_string = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode(
        "ascii"
    )
    headers = {
        "Authorization": f"Basic {auth_string}",
        "x-langfuse-ingestion-version": os.getenv(
            "LANGFUSE_OTLP_INGESTION_VERSION", "4"
        ).strip()
        or "4",
    }
    endpoint = _normalize_otlp_traces_endpoint(
        f"{base_url.rstrip('/')}/api/public/otel"
    )
    return endpoint, headers


def _build_span_exporter() -> Optional[Any]:
    exporter_name = os.getenv("TELEMETRY_EXPORTER", "").strip().lower()
    azure_connection_string = (
        os.getenv("AZURE_MONITOR_CONNECTION_STRING")
        or os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
        or ""
    ).strip()

    explicit_traces_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""
    ).strip()
    explicit_base_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    explicit_headers = (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS")
        or os.getenv("OTEL_EXPORTER_OTLP_HEADERS")
        or ""
    ).strip()

    legacy_langfuse = _build_langfuse_otlp_config()

    if exporter_name in {"azure", "azure_monitor", "applicationinsights"} or (
        not exporter_name and azure_connection_string
    ):
        if not azure_connection_string:
            logger.info(
                "Telemetry disabled: AZURE_MONITOR_CONNECTION_STRING or APPLICATIONINSIGHTS_CONNECTION_STRING is required for Azure Monitor exporter."
            )
            return None
        if AzureMonitorTraceExporter is None:
            logger.warning(
                "Telemetry disabled: azure-monitor-opentelemetry-exporter is not installed."
            )
            return None
        return create_exporter(
            {"exporter": "azure", "connection_string": azure_connection_string},
            azure_factory=AzureMonitorTraceExporter,
            otlp_factory=OTLPSpanExporter,
        )

    if exporter_name in {"otlp", "langfuse", "aws", "adot"} or (
        not exporter_name
        and (explicit_traces_endpoint or explicit_base_endpoint or legacy_langfuse)
    ):
        endpoint = explicit_traces_endpoint
        headers: Dict[str, str] = {}
        if not endpoint and explicit_base_endpoint:
            endpoint = _normalize_otlp_traces_endpoint(explicit_base_endpoint)
        if explicit_headers:
            headers = _parse_otlp_headers(explicit_headers)
        elif legacy_langfuse and not endpoint:
            endpoint, headers = legacy_langfuse

        if not endpoint:
            logger.info(
                "Telemetry disabled: OTLP exporter requires OTEL_EXPORTER_OTLP_TRACES_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT."
            )
            return None

        return create_exporter(
            {"exporter": "otlp", "traces_endpoint": endpoint, "headers": headers},
            azure_factory=AzureMonitorTraceExporter,
            otlp_factory=OTLPSpanExporter,
        )

    logger.info(
        "Telemetry disabled: set TELEMETRY_EXPORTER plus exporter credentials, or provide Azure Monitor/OTLP telemetry environment variables."
    )
    return None


def _get_tracer():
    global _tracer, _tracer_provider, _client_disabled

    if not _env_flag_enabled("TELEMETRY_ENABLED", default=False):
        return None

    if _client_disabled:
        return None
    if _tracer is not None:
        return _tracer

    try:
        exporter = _build_span_exporter()
    except Exception:
        logger.warning("Unable to initialize telemetry exporter; export skipped.")
        return None
    if exporter is None:
        return None

    try:
        resource = Resource.create(
            {
                "service.name": os.getenv("TELEMETRY_SERVICE_NAME", "fetch").strip()
                or "fetch",
                "deployment.environment": (
                    os.getenv("TELEMETRY_ENVIRONMENT") or os.getenv("ENV") or "prod"
                ).strip(),
            }
        )
        _tracer_provider = TracerProvider(resource=resource)
        _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        # Ensure exporters that inspect the global tracer provider can access resource attrs.
        try:
            trace.set_tracer_provider(_tracer_provider)
        except Exception:
            pass
        _tracer = _tracer_provider.get_tracer("app.telemetry")
    except Exception as exc:  # pragma: no cover - defensive
        _client_disabled = True
        logger.warning(
            "Failed to initialize OpenTelemetry client: %s", type(exc).__name__
        )
        return None

    return _tracer


def _set_metadata_attributes(
    span: Span,
    metadata: Optional[Mapping[str, Any]],
    *,
    trace_level: bool,
    observation_level: bool,
) -> None:
    if not metadata:
        return

    for key, value in metadata.items():
        if value is None:
            continue
        sanitized_key = _sanitize_metadata_key(str(key))
        serialized = _serialize_value(value)
        if trace_level:
            span.set_attribute(f"langfuse.trace.metadata.{sanitized_key}", serialized)
        if observation_level:
            span.set_attribute(
                f"langfuse.observation.metadata.{sanitized_key}", serialized
            )


def _annotate_error(span: Span, error: BaseException) -> None:
    # SDK exception bodies may contain credentials, even when content capture is on.
    error_type = type(error).__name__
    span.set_attribute("error.type", error_type)
    span.set_status(Status(StatusCode.ERROR, error_type))
    span.set_attribute("langfuse.observation.level", "ERROR")
    span.set_attribute("langfuse.observation.status_message", error_type)


def start_request_trace(
    name: str,
    input_payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    full_query_text: Optional[str] = None,
) -> Optional[TelemetrySpanHandle]:
    """Start a root request trace span backed by OpenTelemetry."""
    metadata = dict(metadata or {})
    scope = current_scope.get()
    if scope is not None:
        metadata.update(scope.attributes())
        input_payload = {**input_payload, **scope.attributes()}
    tracer, isolated = _get_client_tracer(metadata.get("client_id"))
    if tracer is None:
        return None
    capture_content = (
        scope.capture_content
        if scope
        else _content_capture_allowed(metadata.get("client_id"))
    )
    correlation = {
        key: metadata[key]
        for key in (
            "client_id",
            "environment",
            "api_key_id",
            "scope_id",
            "request_id",
            "client_request_id",
            "conversation_id",
            "user_id",
        )
        if metadata.get(key) is not None
    }
    if not capture_content:
        name = "classification.request"
        input_payload = {}
        full_query_text = None
        metadata = correlation
    try:
        span = tracer.start_span(name=name, kind=SpanKind.SERVER)
        span.set_attribute("langfuse.trace.name", name)
        span.set_attribute("langfuse.observation.type", "span")
        span.set_attribute("langfuse.trace.input", _serialize_value(input_payload))
        span.set_attribute(
            "langfuse.observation.input", _serialize_value(input_payload)
        )
        span.set_attribute("fetch.content_captured", capture_content)
        _set_correlation_attributes(span, correlation)
        if capture_content and _capture_full_query_enabled():
            _set_large_text_attributes(
                span,
                key_prefix="fetch.user_query",
                value=full_query_text or input_payload.get("problem_description"),
            )
        if metadata and metadata.get("conversation_id"):
            span.set_attribute("langfuse.session.id", str(metadata["conversation_id"]))
        _set_metadata_attributes(
            span, metadata, trace_level=True, observation_level=True
        )
        return TelemetrySpanHandle(
            span=span,
            kind="request",
            capture_content=capture_content,
            correlation=correlation,
            tracer=tracer,
            scope=scope,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Unable to start telemetry request trace '%s': %s",
            "redacted",
            type(exc).__name__,
        )
        return None


def finalize_request_trace(
    span: Optional[TelemetrySpanHandle],
    output_payload: Optional[Dict[str, Any]] = None,
    error: Optional[BaseException] = None,
) -> None:
    """Update and close the request trace span."""
    if not span:
        return
    try:
        if output_payload is not None and span.capture_content:
            span.span.set_attribute(
                "langfuse.trace.output", _serialize_value(output_payload)
            )
            span.span.set_attribute(
                "langfuse.observation.output", _serialize_value(output_payload)
            )
        if error is not None:
            if span.capture_content:
                _annotate_error(span.span, error)
            else:
                span.span.set_status(Status(StatusCode.ERROR))
                span.span.set_attribute("error.type", type(error).__name__)
        emit_request_summary(span, error)
        span.span.end()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Failed to finalize telemetry request trace: %s", type(exc).__name__
        )


def record_telemetry_event(
    *,
    name: str,
    event_payload: Dict[str, Any],
    client_id: str,
    full_query_text: Optional[str] = None,
) -> None:
    """Record a completed application event in the shared telemetry backend."""
    scope = current_scope.get()
    if scope is not None:
        client_id = scope.client_id
        event_payload = {
            **event_payload,
            **scope.attributes(),
            "is_test": scope.environment == "test",
        }
    if not (scope.capture_content if scope else _content_capture_allowed(client_id)):
        return
    tracer, _ = _get_client_tracer(client_id)
    if tracer is None:
        return
    span = None
    try:
        span = tracer.start_span(name=name, kind=SpanKind.SERVER)
        span.set_attribute("fetch.content_captured", True)
        trace_payload = dict(event_payload)
        if "query" in trace_payload:
            trace_payload["query"] = "[redacted; see fetch.user_query.* attributes]"
        span.set_attribute("langfuse.trace.name", name)
        span.set_attribute("langfuse.observation.type", "span")
        span.set_attribute(
            "fetch.event.type", str(event_payload.get("event_type", name))
        )
        span.set_attribute("fetch.client_id", client_id)
        if scope is not None:
            _set_correlation_attributes(span, scope.attributes())
        span.set_attribute(
            "fetch.environment", str(event_payload.get("environment", "unknown"))
        )
        span.set_attribute("fetch.is_test", bool(event_payload.get("is_test", False)))
        # Azure limits individual property lengths. Preserve the full structured
        # event across bounded chunks, independently of the user query chunks.
        _set_large_text_attributes(
            span,
            key_prefix="fetch.event",
            value=_serialize_value(trace_payload),
            chunk_size=6000,
            max_chunks=16,
        )
        span.set_attribute("langfuse.trace.input", _serialize_value(trace_payload))
        span.set_attribute(
            "langfuse.observation.input", _serialize_value(trace_payload)
        )
        if _capture_full_query_enabled():
            _set_large_text_attributes(
                span,
                key_prefix="fetch.user_query",
                value=full_query_text or event_payload.get("query"),
            )
        if event_payload.get("conversation_id"):
            span.set_attribute(
                "langfuse.session.id", str(event_payload["conversation_id"])
            )
        span.end()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Failed to record telemetry event '%s': %s", "redacted", type(exc).__name__
        )
        if span is not None:
            try:
                span.end()
            except Exception:
                pass


def start_provider_generation(
    parent_span: Optional[TelemetrySpanHandle],
    *,
    name: str,
    model: Optional[str],
    input_payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[TelemetrySpanHandle]:
    """Start a child generation span for a provider/model invocation."""
    if parent_span is None or not parent_span.capture_content:
        return None
    tracer = parent_span.tracer
    if tracer is None or parent_span is None:
        return None
    try:
        span = tracer.start_span(
            name=name,
            kind=SpanKind.CLIENT,
            context=set_span_in_context(parent_span.span),
        )
        span.set_attribute("langfuse.observation.type", "generation")
        _set_correlation_attributes(span, parent_span.correlation)
        span.set_attribute(
            "langfuse.observation.input", _serialize_value(input_payload)
        )
        if model:
            span.set_attribute("langfuse.observation.model.name", model)
            span.set_attribute("gen_ai.request.model", model)
        if metadata and metadata.get("conversation_id"):
            span.set_attribute("langfuse.session.id", str(metadata["conversation_id"]))
        _set_metadata_attributes(
            span, metadata, trace_level=False, observation_level=True
        )
        return TelemetrySpanHandle(
            span=span,
            kind="generation",
            correlation=parent_span.correlation,
            tracer=tracer,
            scope=parent_span.scope,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Unable to start telemetry generation for '%s': %s",
            "redacted",
            type(exc).__name__,
        )
        return None


def finalize_provider_generation(
    generation: Optional[TelemetrySpanHandle],
    *,
    output_payload: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    usage_details: Optional[Dict[str, int]] = None,
    cost_details: Optional[Dict[str, float]] = None,
    error: Optional[BaseException] = None,
) -> None:
    """Update and close a provider generation span."""
    if not generation:
        return
    try:
        if output_payload is not None:
            generation.span.set_attribute(
                "langfuse.observation.output", _serialize_value(output_payload)
            )
        _set_metadata_attributes(
            generation.span,
            metadata,
            trace_level=False,
            observation_level=True,
        )
        if usage_details:
            generation.span.set_attribute(
                "langfuse.observation.usage_details",
                _serialize_value(usage_details),
            )
            for key, value in usage_details.items():
                if isinstance(value, (int, float)):
                    generation.span.set_attribute(f"gen_ai.usage.{key}", int(value))
        if cost_details:
            generation.span.set_attribute(
                "langfuse.observation.cost_details",
                _serialize_value(cost_details),
            )
        if error is not None:
            _annotate_error(generation.span, error)
        generation.span.end()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Failed to finalize telemetry generation: %s", type(exc).__name__
        )


def flush_telemetry() -> None:
    """Force-flush buffered OpenTelemetry spans."""
    with _isolated_lock:
        providers = list(_isolated_providers.values())
    if _tracer_provider is not None:
        providers.append(_tracer_provider)
    if not _flush_lock.acquire(blocking=False):
        logger.debug("Telemetry flush already in progress; skipping.")
        return
    try:
        flush_logs(TELEMETRY_FLUSH_TIMEOUT_MS)
        for provider in providers:
            try:
                provider.force_flush(timeout_millis=TELEMETRY_FLUSH_TIMEOUT_MS)
            except Exception:
                logger.warning("Telemetry provider flush failed.")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Telemetry flush failed: %s", type(exc).__name__)
    finally:
        _flush_lock.release()


async def flush_telemetry_async() -> None:
    """Bounded flush for shutdown or explicit evaluation use; not the request path."""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(flush_telemetry),
            timeout=TELEMETRY_FLUSH_TIMEOUT_MS / 1000.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Telemetry flush exceeded %sms; continuing without waiting.",
            TELEMETRY_FLUSH_TIMEOUT_MS,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Telemetry async flush failed: %s", type(exc).__name__)
