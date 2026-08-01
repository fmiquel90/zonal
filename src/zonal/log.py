import os
import sys
from contextvars import ContextVar

import structlog

# Set per request/consumer when no tracer is active; _add_correlation reads it.
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

SERVICE = os.getenv("DD_SERVICE") or os.getenv("SERVICE_NAME", "zonal")
ENV = os.getenv("DD_ENV") or os.getenv("ENV", "unknown")
VERSION = os.getenv("DD_VERSION") or os.getenv("VERSION")


def get_logger(name: str = "zonal"):
    return structlog.get_logger(name)


def _add_static_fields(_, __, event_dict):
    # setdefault: an explicit per-event bind (e.g. tests) still wins.
    event_dict.setdefault("service", SERVICE)
    event_dict.setdefault("env", ENV)
    if VERSION:
        event_dict.setdefault("version", VERSION)
    return event_dict


def _add_correlation(_, __, event_dict):
    """Prefer an active distributed trace; fall back to a request-scoped id.

    Consults only tracers the application has already imported. `import` costs a full sys.path walk
    on every miss — Python does not negative-cache failed imports — which, once per log record,
    dominated the cost of emitting the line; a sys.modules lookup is a dict hit. It also stays live:
    a tracer imported later in the process is picked up on the next record. Every read is guarded,
    so a shut-down tracer or a version skew degrades to a line without a trace id, never an
    exception out of the logging path.
    """
    ddtrace = sys.modules.get("ddtrace")
    if ddtrace is not None:
        try:
            span = ddtrace.tracer.current_span()
            if span is not None:
                event_dict["dd.trace_id"] = str(span.trace_id)
                event_dict["dd.span_id"] = str(span.span_id)
                event_dict.setdefault("trace_id", str(span.trace_id))
                return event_dict
        except Exception:
            pass

    otel = sys.modules.get("opentelemetry.trace")
    if otel is not None:
        try:
            ctx = otel.get_current_span().get_span_context()
            if ctx.is_valid:
                event_dict["trace_id"] = format(ctx.trace_id, "032x")
                event_dict["span_id"] = format(ctx.span_id, "016x")
                return event_dict
        except Exception:
            pass

    cid = correlation_id.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_level(level: str | None) -> str:
    if level:
        return level.upper()
    # DEBUG is opt-in only: off unless LOG_DEBUG is explicitly truthy.
    if _truthy(os.getenv("LOG_DEBUG")):
        return "DEBUG"
    return os.getenv("LOG_LEVEL", "INFO").upper()


def configure_json_logging(level: str | None = None) -> None:
    """Opt-in JSON logging setup. Apps call this once at startup.

    A library must not configure logging on import (it would hijack the host app's setup), so
    zonal only emits through structlog.get_logger and leaves rendering to the app — except the
    zonal-healthcheck daemon, which owns its process and calls this itself.

    Emits the Observability RFC v1 required fields (timestamp, level, service, env, message, and
    a correlation/trace id). DEBUG is off by default; enable it with LOG_DEBUG=true (or a granular
    LOG_LEVEL). An explicit `level` argument overrides both env vars.
    """
    log_level = _resolve_level(level)
    if log_level.lower() not in structlog.processors.NAME_TO_LEVEL:
        valid = ", ".join(sorted(structlog.processors.NAME_TO_LEVEL))
        raise ValueError(f"invalid log level {log_level!r}; choose from: {valid}")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _add_static_fields,
            _add_correlation,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.processors.format_exc_info,
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            structlog.processors.NAME_TO_LEVEL[log_level.lower()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
