"""Env-gated OpenTelemetry bootstrap for lab repos (canonical copy).

No-op unless TELEMETRY_ENABLED=1 (or true/yes). When enabled, initializes a
tracer provider with an OTLP/HTTP span exporter configured from the standard
OTEL_EXPORTER_* environment variables and registers an atexit flush.

Vendored per repo (see ~/.hermes/scripts/otel/sync-shim.sh). Dependencies live
behind the optional extra `telemetry`; missing packages are tolerated when
disabled, warned when enabled. Never raises into application code.
"""
from __future__ import annotations

import atexit
import logging
import os
from contextlib import contextmanager

log = logging.getLogger(__name__)

_ENABLED = os.getenv("TELEMETRY_ENABLED", "").strip().lower() in ("1", "true", "yes")
_provider = None
_active = False


def enabled() -> bool:
    """True when TELEMETRY_ENABLED requested telemetry (regardless of init success)."""
    return _ENABLED


def init(service_name: str | None = None) -> bool:
    """Initialize telemetry if enabled. Returns True when a provider is active."""
    global _provider, _active
    if not _ENABLED or _active:
        return _active
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        _provider = TracerProvider(resource=Resource.create({
            "service.name": service_name or _default_service(),
        }))
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(_provider)
        _active = True
        atexit.register(flush)
        log.info("telemetry enabled (service=%s)", service_name)
    except ImportError:
        if _ENABLED:
            log.warning(
                "telemetry enabled but opentelemetry packages are missing; "
                "install the project's 'telemetry' extra"
            )
    except Exception:  # noqa: BLE001 - telemetry must never break the app
        log.exception("telemetry init failed; continuing without it")
    return _active


def _default_service() -> str:
    return os.getenv("OTEL_SERVICE_NAME") or "unknown"


def get_tracer(name: str = "app"):
    """Return the tracer when active, else None (callers guard on None)."""
    if not _active:
        return None
    from opentelemetry import trace

    return trace.get_tracer(name)


@contextmanager
def span(name: str, attrs: dict | None = None):
    """Start a span when active; no-op context manager otherwise.

    Yields the span (or None) so callers can set_attribute / record exceptions.
    """
    tracer = get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name, attributes=attrs or {}) as current:
        yield current


def flush() -> None:
    """Force-pend queued spans to the exporter (also registered at exit)."""
    if _active and _provider is not None:
        try:
            _provider.force_flush()
        except Exception:  # noqa: BLE001
            log.debug("telemetry flush failed", exc_info=True)
