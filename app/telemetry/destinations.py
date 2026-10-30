"""Destination configuration shared by trace and structured-log exporters."""

import json
import os
from urllib.parse import urlsplit, unquote


def client_destinations():
    """Resolve named clients without tying tenant isolation to one cloud vendor.

    Legacy Azure mappings remain supported. New generic entries take precedence.
    Malformed configuration raises so callers can fail closed.
    """
    legacy = json.loads(os.getenv("TELEMETRY_AZURE_CLIENT_CONNECTION_STRINGS", "{}"))
    generic = json.loads(os.getenv("TELEMETRY_CLIENT_DESTINATIONS", "{}"))
    if not isinstance(legacy, dict) or not isinstance(generic, dict):
        raise ValueError("Expected destination maps")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in legacy.items()):
        raise ValueError("Invalid legacy destinations")
    if not all(isinstance(k, str) and isinstance(v, dict) for k, v in generic.items()):
        raise ValueError("Invalid destinations")
    return {
        **{k: {"exporter": "azure", "connection_string": v} for k, v in legacy.items()},
        **generic,
    }


def value(config, key):
    direct = config.get(key, "")
    env_name = config.get(key + "_env")
    if env_name:
        if not isinstance(env_name, str):
            raise ValueError("Invalid environment reference")
        direct = os.getenv(env_name, "")
    if not isinstance(direct, str):
        raise ValueError("Invalid destination value")
    return direct.strip()


def resolved(config, signal="traces"):
    kind = value(config, "exporter").lower()
    if kind in {"azure", "azure_monitor", "applicationinsights"}:
        connection = value(config, "connection_string")
        if not connection:
            raise ValueError("Azure connection string required")
        return "azure", {"connection_string": connection}
    if kind in {"otlp", "aws", "adot"}:
        endpoint = value(config, signal + "_endpoint")
        if not endpoint:
            base = value(config, "endpoint").rstrip("/")
            if base:
                endpoint = base + "/v1/" + signal
        if not endpoint or urlsplit(endpoint).scheme not in {"http", "https"}:
            raise ValueError("OTLP HTTP endpoint required")
        headers = config.get("headers", {})
        if config.get("headers_env"):
            headers = {}
            for entry in os.getenv(config["headers_env"], "").split(","):
                key, sep, val = entry.partition("=")
                if sep and key.strip():
                    headers[key.strip()] = unquote(val.strip())
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError("Invalid OTLP headers")
        return "otlp", {"endpoint": endpoint, "headers": headers or None, "timeout": 5}
    raise ValueError("Unknown telemetry exporter")


def create_exporter(
    config, *, signal="traces", provider=None, azure_factory=None, otlp_factory=None
):
    kind, kwargs = resolved(config, signal)
    factory = azure_factory if kind == "azure" else otlp_factory
    if factory is None:
        raise ValueError("Exporter dependency unavailable")
    if kind == "azure" and provider is not None and signal == "traces":
        kwargs["tracer_provider"] = provider
    return factory(**kwargs)


def shared_destination():
    kind = os.getenv("TELEMETRY_EXPORTER", "").strip().lower()
    connection = os.getenv("AZURE_MONITOR_CONNECTION_STRING") or os.getenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING", ""
    )
    if kind in {"azure", "azure_monitor", "applicationinsights"} or (
        not kind and connection
    ):
        return {"exporter": "azure", "connection_string": connection}
    return {
        "exporter": kind or "otlp",
        "endpoint": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        "traces_endpoint": os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""),
        "logs_endpoint": os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", ""),
        "headers_env": "OTEL_EXPORTER_OTLP_HEADERS",
    }


def scope_destination(scope, signal="traces"):
    """Exact credential-scope routing. Never use a consumer/shared fallback."""
    routes = json.loads(os.getenv("TELEMETRY_SCOPE_DESTINATIONS", "{}"))
    if not isinstance(routes, dict) or not all(
        isinstance(k, str) and isinstance(v, dict) for k, v in routes.items()
    ):
        raise ValueError("Invalid scoped telemetry destinations")
    destination = routes.get(scope.scope_id)
    if destination is None:
        return None
    fingerprint = resolved(destination, signal)
    # Reject obviously shared destinations across scopes. A common collector must
    # use distinct authenticated routes/headers and enforce downstream separation.
    for scope_id, other in routes.items():
        if scope_id == scope.scope_id:
            continue
        try:
            other_fingerprint = resolved(other, signal)
        except (ValueError, TypeError):
            continue
        if other_fingerprint == fingerprint:
            raise ValueError("Telemetry destination is reused across credential scopes")
    return destination
