"""Telemetry helpers for emitting traces and opt-in structured request logs."""

from .opentelemetry_client import (
    start_request_trace,
    finalize_request_trace,
    record_telemetry_event,
    start_provider_generation,
    finalize_provider_generation,
    flush_telemetry,
    flush_telemetry_async,
)

__all__ = [
    "start_request_trace",
    "finalize_request_trace",
    "record_telemetry_event",
    "start_provider_generation",
    "finalize_provider_generation",
    "flush_telemetry",
    "flush_telemetry_async",
]
