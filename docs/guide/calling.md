# Calling a service

zonal hands you a host; **you make the call** with whatever HTTP client you already use. There are
two styles: the `lease()` context manager (automatic breaker feedback) or explicit `pick()` +
`report_*` (you decide what counts as a failure).

## Sync caller

### With `lease()`

`lease()` picks a host and feeds the circuit breaker automatically — an exception inside the block
ejects that host and re-raises; a clean exit marks it healthy.

```python
import requests
from zonal import Balancer, DiscoveryConfig

cfg = DiscoveryConfig(namespace="services.internal", service="backend", region="eu-west-1")
session = requests.Session()

with Balancer(cfg) as balancer:
    balancer.wait_ready(timeout=5)
    with balancer.lease() as host:
        resp = session.post(host.url("/work"), data=payload, timeout=(3, 60))
        resp.raise_for_status()                    # raise here -> host ejected
```

### With explicit `pick()` + `report_*`

Prefer explicit control over what counts as a failure (e.g. treat a `5xx` as an ejection)?

```python
host = balancer.pick()
try:
    resp = session.post(host.url("/work"), data=payload)
    if resp.status_code >= 500:
        balancer.report_failure(host)              # 5xx ejection, at your discretion
except (requests.Timeout, requests.ConnectionError):
    balancer.report_failure(host)
    raise
```

## Async caller

`AsyncBalancer` mirrors the sync API; `start` / `wait_ready` / `lease` are async, while `pick`,
`report_*`, and `hosts` stay synchronous (they are cheap and lock-free under asyncio).

```python
import httpx
from zonal import AsyncBalancer, DiscoveryConfig

cfg = DiscoveryConfig(namespace="services.internal", service="backend", region="eu-west-1")
async with AsyncBalancer(cfg) as balancer, httpx.AsyncClient() as http:
    await balancer.wait_ready()
    async with balancer.lease() as host:
        resp = await http.post(host.url("/work"), content=payload)
        resp.raise_for_status()
```

## The circuit breaker

The breaker is **local to each caller** and complements Cloud Map's shared health status:

- `report_failure(host)` ejects the host for `breaker_cooldown` seconds; subsequent `pick()` calls
  skip it.
- `report_success(host)` clears an ejection immediately.
- If *every* host is in cooldown, `pick()` still returns one — a possibly-degraded host beats
  refusing all traffic.

!!! tip "Retries on large payloads"
    After `report_failure`, the next `pick()` routes elsewhere — retry by **re-picking** rather than
    re-uploading megabytes to the same dead host. Cap retries so a bad sweep doesn't amplify load.

## AZ affinity & fallback

`pick()` only ever returns hosts from the effective set the refresh loop maintains: same-AZ hosts
when any are healthy, otherwise **all** healthy hosts (a cross-AZ fallback, logged as
`cross_az_fallback`). Affinity is applied client-side, so it behaves the same on real AWS and on
emulators. Disable it with `prefer_same_az=False` in [`DiscoveryConfig`](../configuration.md).
