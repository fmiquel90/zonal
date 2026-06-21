# Health service

The `zonal-healthcheck` daemon is an **out-of-band** health checker: one per service is enough. It
discovers every registered host, probes `/health` concurrently, and pushes `HEALTHY`/`UNHEALTHY` back
to Cloud Map. Unlike a self-reporting host, it can detect a *hung* instance that can no longer report
on its own behalf.

```bash
zonal-healthcheck --namespace services.internal --service backend \
    --region eu-west-1 --health-path /health --interval 10 --log-level INFO
```

Tune hysteresis and probe fan-out when the defaults don't fit:

```bash
zonal-healthcheck --namespace services.internal --service backend --region eu-west-1 \
    --healthy-threshold 2 --unhealthy-threshold 3 --concurrency 16 --timeout 2
```

## What a sweep does

1. **Discover** every instance for the service (`HealthStatus=ALL`).
2. **Probe** each host's `health_path` concurrently. A probe counts as healthy only on a **`2xx`** —
   a `4xx` (e.g. a misrouted path) or `5xx` is a failure.
3. **Apply hysteresis** — a status only flips after `healthy_threshold` (default 2) or
   `unhealthy_threshold` (default 3) *consecutive* identical results, absorbing transient blips.
4. **Push** the new status to Cloud Map only when it changes; bookkeeping for instances Cloud Map no
   longer lists is dropped so memory doesn't grow across instance churn.

## Two-layer health model

| Layer | Owned by | Speed | Purpose |
|---|---|---|---|
| Cloud Map custom status | `zonal-healthcheck` | slow (seconds, hysteresis) | shared, authoritative liveness for discovery |
| Local circuit breaker | each caller | fast (per request) | eject a host *this* caller just saw fail |

The balancer only ever discovers `HEALTHY` hosts; the breaker then reacts faster than the daemon can
for failures a caller observes directly. See [calling a service](calling.md#the-circuit-breaker).

!!! note "Run it as its own process"
    The CLI configures JSON logging itself (it owns its process). Run one daemon per service, e.g. as
    a systemd unit or a small ECS/Fargate task.
