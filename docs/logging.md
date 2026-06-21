# Logging

zonal logs through [`structlog`](https://www.structlog.org) and **never configures logging on
import** — a library must not hijack the host app's logging. Call `configure_json_logging()` once at
startup to emit JSON on stdout:

```python
from zonal import configure_json_logging

configure_json_logging()          # or configure_json_logging(level="DEBUG")
```

A balancer event then looks like:

```json
{"namespace": "services.internal", "target_service": "backend", "az": "euw1-az1", "host_count": 4, "service": "zonal", "env": "prod", "message": "discovery_ready", "level": "info", "timestamp": "2026-06-15T10:00:00Z"}
```

Every line carries the Observability RFC v1 required fields: `timestamp`, `level`, `service`, `env`,
`message`, and a correlation/trace id when one is available.

## Levels

`DEBUG` is **off by default**. Enable it with `LOG_DEBUG=true`, a granular `LOG_LEVEL`, or an explicit
`configure_json_logging(level="DEBUG")` (the argument wins over both env vars). An invalid level
raises a clear `ValueError` at startup rather than failing obscurely later.

## Identity & correlation

`service`, `env`, and `version` are read once from `DD_SERVICE`/`SERVICE_NAME`,
`DD_ENV`/`ENV`, and `DD_VERSION`/`VERSION`. Correlation is resolved per event: an active **ddtrace**
or **OpenTelemetry** span wins; otherwise the `zonal.log.correlation_id` context var is used if set.

## Events

| Source | Events |
|---|---|
| Balancers | `discovery_ready`, `discovery_changed`, `discovery_refresh_failed`, `cross_az_fallback`, `host_ejected` |
| Hosts | `registered`, `deregistered` |
| Health service | `health_service_started`, `health_status_changed`, `instance_gone`, `health_sweep_failed`, `health_sweep` (at `debug`) |

The `zonal-healthcheck` daemon owns its process, so it calls `configure_json_logging()` itself.
