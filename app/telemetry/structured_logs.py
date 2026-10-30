"""Portable request-summary logs. Never forward application log text or prompts."""

import os
import threading
from app.core.identity import scoped_auth_configured
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry._logs import SeverityNumber
from opentelemetry.trace import set_span_in_context

try:
    from azure.monitor.opentelemetry.exporter import AzureMonitorLogExporter
except ImportError:
    AzureMonitorLogExporter = None
from app.telemetry.destinations import (
    client_destinations,
    create_exporter,
    shared_destination,
    scope_destination,
)

_providers = {}
_lock = threading.Lock()


def emit_request_summary(handle, error=None):
    if os.getenv("TELEMETRY_ENABLED", "").lower() not in {"true", "1"} or os.getenv(
        "TELEMETRY_LOGS_ENABLED", ""
    ).lower() not in {"true", "1"}:
        return
    try:
        scope = getattr(handle, "scope", None)
        if scope is not None:
            destination = scope_destination(scope, "logs")
            isolated = True
            cache_key = ("credential", scope.scope_id)
        else:
            if scoped_auth_configured():
                return
            client = handle.correlation.get("client_id")
            destinations = client_destinations()
            isolated = client in destinations or client in {
                x.strip()
                for x in os.getenv("TELEMETRY_ISOLATED_CLIENT_IDS", "").split(",")
                if x.strip()
            }
            destination = destinations.get(client) if isolated else shared_destination()
            cache_key = client if isolated else None
        if not destination:
            return
        with _lock:
            if cache_key not in _providers:
                provider = LoggerProvider(
                    resource=Resource.create(
                        {
                            "service.name": os.getenv(
                                "TELEMETRY_SERVICE_NAME", "fetch"
                            ),
                            "deployment.environment": (
                                scope.environment
                                if scope
                                else (
                                    os.getenv("TELEMETRY_ENVIRONMENT")
                                    or os.getenv("ENV", "production")
                                )
                            ),
                            **(
                                {"fetch." + k: v for k, v in scope.attributes().items()}
                                if scope
                                else {}
                            ),
                        }
                    )
                )
                config = dict(destination)
                if not isolated and os.getenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS"):
                    config["headers_env"] = "OTEL_EXPORTER_OTLP_LOGS_HEADERS"
                exporter = create_exporter(
                    config,
                    signal="logs",
                    azure_factory=AzureMonitorLogExporter,
                    otlp_factory=OTLPLogExporter,
                )
                provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
                _providers[cache_key] = provider
            provider = _providers[cache_key]
        attrs = {
            "fetch." + k: str(v)
            for k, v in handle.correlation.items()
            if k
            in {
                "client_id",
                "environment",
                "api_key_id",
                "scope_id",
                "request_id",
                "conversation_id",
                "client_request_id",
                "user_id",
            }
            and v is not None
        }
        attrs["fetch.status"] = "error" if error else "completed"
        if error:
            attrs["error.type"] = type(error).__name__
        provider.get_logger("fetch.requests").emit(
            body="classification.request.completed",
            severity_number=SeverityNumber.ERROR if error else SeverityNumber.INFO,
            context=set_span_in_context(handle.span),
            attributes=attrs,
        )
    except Exception:
        # No destination fallback, credentials, error messages, or intake text in logs.
        return


def flush_logs(timeout_ms):
    with _lock:
        providers = list(_providers.values())
    for provider in providers:
        try:
            provider.force_flush(timeout_millis=timeout_ms)
        except Exception:
            pass
