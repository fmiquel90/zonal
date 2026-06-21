# Getting started

## Install

=== "Sync only"

    ```bash
    pip install "zonal @ git+ssh://git@github.com/<org>/zonal.git@v0.1.0"
    ```

=== "With async (aioboto3)"

    ```bash
    pip install "zonal[aio] @ git+ssh://git@github.com/<org>/zonal.git@v0.1.0"
    ```

zonal targets **Python 3.10+** and depends on `boto3`, `requests`, and `structlog`. The async
balancer additionally needs `aioboto3` (the `[aio]` extra).

## Prerequisites

zonal routes over an existing **AWS Cloud Map** service. The service must be created (via your infra
tooling) with `HealthCheckCustomConfig` — Route 53 health checks only work on public IPs, not your
private hosts. See [required IAM](../operations.md#required-iam).

## Your first call

```python
import requests
from zonal import Balancer, DiscoveryConfig, configure_json_logging

configure_json_logging()  # optional: JSON logs on stdout
session = requests.Session()

cfg = DiscoveryConfig(namespace="services.internal", service="backend", region="eu-west-1")
with Balancer(cfg) as balancer:
    balancer.wait_ready(timeout=5)

    with balancer.lease() as host:                 # same-AZ host, fallback if none healthy
        resp = session.post(host.url("/work"), data=payload, timeout=(3, 60))
        resp.raise_for_status()                    # raise here -> host ejected from the breaker
```

That is the whole loop: the balancer keeps a background-refreshed cache of healthy same-AZ hosts,
`lease()` hands you one and feeds the circuit breaker, and **you** make the HTTP call.

!!! note "The AZ-ID is auto-detected"
    On EC2, the caller's AZ-ID is read from IMDS. Off-EC2 (or in tests) pass `az_id=...` explicitly,
    otherwise construction fails trying to reach the metadata endpoint.

## Try it locally

`examples/local_demo.py` runs the whole chain against [MiniStack](https://ministack.org) — fake
backends in two AZs, registration, the health daemon, and a balancer pinned to `az1` showing affinity
and cross-AZ fallback:

```bash
docker run -d -p 4566:4566 ministackorg/ministack
python examples/local_demo.py
```

Next: [calling a service](calling.md).
