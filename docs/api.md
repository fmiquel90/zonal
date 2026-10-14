# API reference

## `Balancer`

`Balancer(config, *, sd_client=None, az_id=None)` — the AZ-ID is auto-detected via IMDS unless you
pass `az_id`; `sd_client` injects a preconfigured boto3 client (tests, custom endpoints).

| Member | Description |
|---|---|
| `.start()` / `with Balancer(...) as b` | start the background refresh loop |
| `.wait_ready(timeout=None) -> bool` | block until the first discovery completes (even if it found no hosts); `False` on timeout |
| `.pick() -> Host` | a healthy same-AZ host (raises `NoHealthyHostError` if the cache is empty) |
| `.lease()` (context manager) | pick + auto `report_failure` on exception, else `report_success` |
| `.report_failure(host)` / `.report_success(host)` | feed the local circuit breaker |
| `.hosts() -> tuple[Host, ...]` | current cached (effective) host set |
| `.az_id` | the caller's resolved AZ-ID |
| `.close()` | stop the refresh loop. Non-blocking: the thread is a daemon and never holds up process exit |
| `.wait_closed(timeout=None) -> bool` | block until the loop has stopped after `close()`; `False` on timeout |

## `AsyncBalancer`

`AsyncBalancer(config, *, boto_session=None, az_id=None)` mirrors `Balancer`. `start` / `wait_ready` /
`lease` are async (`await` / `async with`); `pick`, `report_*`, and `hosts` stay synchronous. It
takes an `aioboto3.Session` (`boto_session`) rather than a ready-made client, since the async client
is an `async with` resource owned by the refresh loop.

One deliberate difference: constructed **inside a running event loop** without an explicit `az_id`,
it does not read IMDS in `__init__` — that would freeze the loop for up to two blocking round
trips, and the documented usage (`async with AsyncBalancer(cfg) as balancer`) is exactly that case.
Resolution moves to `start()`, which runs it off-thread, so `.az_id` is `None` in between.
Constructed outside a loop there is nothing to stall, so it resolves eagerly like the sync client.

`AsyncBalancer` is resolved lazily and kept out of `from zonal import *`, so importing `zonal`
doesn't require the `[aio]` extra:

```python
from zonal import AsyncBalancer
```

## `Host`

A frozen dataclass: `ip`, `port`, `az`, `instance_id`, plus `host.url(path="", *, scheme="http")`.

## Registration

- `register_instance(config, *, sd_client=None, metadata=None) -> str`
- `deregister_instance(service_id, instance_id, *, sd_client=None, region=None, endpoint_url=None) -> None`

## Health

`HealthChecker(config, *, sd_client=None, session=None)` — `.run()`, `.run_once()`, `.stop()`,
`.close()`, `.service_id()`.

| Concern | Behaviour |
|---|---|
| Construction | No I/O. When `service_id` is omitted (or empty) it is resolved from `namespace`+`service` on the first sweep, so a throttle at boot is retried by the `run()` loop instead of killing the process. A namespace or service that does not exist is not retried: `run()` raises `ServiceNotFoundError` |
| Stopping | `stop()` asks `run()` to finish its current sweep and return, and releases the probe pool; safe from another thread |
| Threads | Probes share one pool across sweeps. `run()`, `stop()`, `close()` and the context manager all release it; a later `run_once()` builds a new one |

- `resolve_service_id(sd, namespace_name, service_name) -> str`

## Logging

- `configure_json_logging(level=None)`
- `get_logger(name="zonal")`

## Errors

- `ZonalError` — base class.
- `NoHealthyHostError` — raised by `pick()` when the cache is empty.
- `ServiceNotFoundError` — the Cloud Map namespace or service does not exist (also a `ValueError`).
- `ImdsError` — the metadata service was unreachable or too slow (bounded by `imds_timeout`).

`botocore` exceptions from the AWS calls still surface as-is, so `except ZonalError` does **not**
catch those.

## Also exported

- `Router(breaker_cooldown=10.0, clock=time.monotonic)` — the host cache, round-robin picker and
  circuit breaker each balancer wraps. Exported for direct use and testing.
- `imds` — `get_az_id(timeout=2.0)` and `metadata(timeout=2.0)`, over IMDSv2. Both are cached for
  the process whatever `timeout` they are called with (the values are immutable for the instance's lifetime, and caching both keeps them
  from disagreeing); the dict `metadata()` returns is shared, so treat it as read-only. Transport
  failures raise `ImdsError`. Off EC2, don't reach for IMDS at all — pass `az_id=` to a balancer or
  `metadata=` to `register_instance`.
