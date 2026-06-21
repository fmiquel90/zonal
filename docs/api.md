# API reference

## `Balancer`

`Balancer(config, *, sd_client=None, az_id=None)` — the AZ-ID is auto-detected via IMDS unless you
pass `az_id`; `sd_client` injects a preconfigured boto3 client (tests, custom endpoints).

| Member | Description |
|---|---|
| `.start()` / `with Balancer(...) as b` | start the background refresh loop |
| `.wait_ready(timeout=None) -> bool` | block until the first discovery completes |
| `.pick() -> Host` | a healthy same-AZ host (raises `NoHealthyHostError` if the cache is empty) |
| `.lease()` (context manager) | pick + auto `report_failure` on exception, else `report_success` |
| `.report_failure(host)` / `.report_success(host)` | feed the local circuit breaker |
| `.hosts() -> tuple[Host, ...]` | current cached (effective) host set |
| `.az_id` | the caller's resolved AZ-ID |
| `.close()` | stop the refresh loop |

## `AsyncBalancer`

`AsyncBalancer(config, *, boto_session=None, az_id=None)` mirrors `Balancer`. `start` / `wait_ready` /
`lease` are async (`await` / `async with`); `pick`, `report_*`, and `hosts` stay synchronous. It
takes an `aioboto3.Session` (`boto_session`) rather than a ready-made client, since the async client
is an `async with` resource owned by the refresh loop.

`AsyncBalancer` is resolved lazily and kept out of `from zonal import *`, so importing `zonal`
doesn't require the `[aio]` extra:

```python
from zonal import AsyncBalancer
```

## `Host`

A frozen dataclass: `ip`, `port`, `az`, `instance_id`, plus `host.url(path="", *, scheme="http")`.

## Registration

- `register_instance(config, *, sd_client=None, metadata=None) -> str`
- `deregister_instance(service_id, instance_id, *, sd_client=None, region=None) -> None`

## Health

- `HealthChecker(config, *, sd_client=None, session=None)` — `.run()`, `.run_once()`, `.stop()`.
- `resolve_service_id(sd, namespace_name, service_name) -> str`

## Logging

- `configure_json_logging(level=None)`
- `get_logger(name="zonal")`

## Errors

- `ZonalError` — base class.
- `NoHealthyHostError` — raised by `pick()` when the cache is empty.
