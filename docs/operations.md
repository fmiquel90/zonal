# Operations

## Before you deploy

- **🚨 Check the NAT Gateway first.** If this traffic traverses a NAT Gateway (`$0.045/GB` processed
  in `us-east-1`, `$0.048/GB` in `eu-west-1`, plus an hourly charge per gateway), *that* dominates
  the bill — roughly 2–5× the `$0.02/GB` round-trip cross-AZ charge. Make sure callers reach hosts on
  private IPs directly.
- **🧮 Capacity per AZ.** Same-AZ is only free when each AZ holds healthy capacity. If a host type
  isn't in every AZ, the fallback fires and you pay cross-AZ — weigh idle capacity against transfer
  saved.
- **🔁 Cap retries on large payloads.** After `report_failure` the next `pick()` routes elsewhere, so
  retry by re-picking rather than re-uploading to the same dead host.

## Required IAM

| Principal | Actions |
|---|---|
| Callers | `servicediscovery:DiscoverInstances` |
| Target hosts | `servicediscovery:RegisterInstance`, `servicediscovery:DeregisterInstance`, `servicediscovery:UpdateInstanceCustomHealthStatus` |
| Health service | `servicediscovery:DiscoverInstances`, `servicediscovery:UpdateInstanceCustomHealthStatus`, `servicediscovery:ListNamespaces`, `servicediscovery:ListServices` |

## Resilience characteristics

- **Cache-only hot path.** `pick()` never calls AWS; it reads the background-refreshed cache. A
  discovery failure keeps the last good host list (`discovery_refresh_failed` is logged) — stale but
  working beats an empty cache.
- **Starts-empty is safe.** `wait_ready()` returns once the first discovery completes, even if it
  found zero hosts; `pick()` then raises `NoHealthyHostError` rather than hanging.
- **Custom endpoints.** When `endpoint_url` is set (VPC endpoint, MiniStack, LocalStack), zonal
  disables botocore's `data-` host prefix automatically — `DiscoverInstances` is a data-plane call
  whose prefix points nowhere against a custom endpoint.
