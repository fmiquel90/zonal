# Configuration

All configuration is plain dataclasses from `zonal.config`.

## `DiscoveryConfig` (caller)

| Field | Default | Purpose |
|---|---|---|
| `namespace`, `service` | — | Cloud Map namespace + service names |
| `region` | `None` | AWS region |
| `endpoint_url` | `None` | custom Cloud Map endpoint (VPC endpoint, or MiniStack in tests) |
| `connect_timeout` / `read_timeout` | `2.0` / `3.0` | per-request bounds on every Cloud Map call (seconds) |
| `max_attempts` | `2` | total Cloud Map requests per call, the first one included |
| `imds_timeout` | `2.0` | bound on the IMDS lookup behind an auto-detected AZ-ID (seconds) |
| `refresh_interval` / `refresh_jitter` | `5.0` / `1.0` | background discovery cadence (seconds) |
| `breaker_cooldown` | `10.0` | local eject duration after `report_failure` (seconds) |
| `prefer_same_az` | `True` | toggle AZ affinity |
| `max_results` | `100` | max instances per `DiscoverInstances` call |
| `az_attribute` | `AZID` | Cloud Map attribute holding the AZ-ID |
| `ip_attribute` | `AWS_INSTANCE_IPV4` | Cloud Map attribute holding the host IP |
| `port_attribute` | `AWS_INSTANCE_PORT` | Cloud Map attribute holding the host port |

## `RegisterConfig` (target host)

| Field | Default | Purpose |
|---|---|---|
| `service_id` | — | Cloud Map service id to register into |
| `port` | — | port the host's server listens on |
| `region` | `None` | AWS region |
| `endpoint_url` | `None` | custom Cloud Map endpoint (VPC endpoint, or an emulator) |
| `connect_timeout` / `read_timeout` | `2.0` / `3.0` | per-request bounds on every Cloud Map call (seconds) |
| `max_attempts` | `2` | total Cloud Map requests per call, the first one included |
| `imds_timeout` | `2.0` | bound on the IMDS lookup behind an auto-detected AZ-ID (seconds) |
| `az_attribute` | `AZID` | Cloud Map attribute key the AZ-ID is written under |
| `extra_attributes` | `{}` | additional Cloud Map instance attributes |
| `ip_attribute` | `AWS_INSTANCE_IPV4` | Cloud Map attribute key the host IP is written under |
| `port_attribute` | `AWS_INSTANCE_PORT` | Cloud Map attribute key the host port is written under |

## `HealthConfig` (health daemon)

| Field | Default | Purpose |
|---|---|---|
| `namespace`, `service` | — | service to monitor |
| `service_id` | `None` | resolved from `namespace`+`service` if omitted |
| `region` | `None` | AWS region |
| `endpoint_url` | `None` | custom Cloud Map endpoint (VPC endpoint, or an emulator) |
| `connect_timeout` / `read_timeout` | `2.0` / `3.0` | per-request bounds on every Cloud Map call (seconds) |
| `max_attempts` | `2` | total Cloud Map requests per call, the first one included |
| `health_path` / `scheme` | `/health` / `http` | probe target |
| `interval` / `timeout` | `10.0` / `2.0` | sweep cadence and per-probe timeout (seconds) |
| `healthy_threshold` / `unhealthy_threshold` | `2` / `3` | consecutive probes before flipping status |
| `concurrency` | `16` | parallel probes per sweep |
| `ip_attribute` | `AWS_INSTANCE_IPV4` | Cloud Map attribute holding the host IP |
| `port_attribute` | `AWS_INSTANCE_PORT` | Cloud Map attribute holding the host port |
| `az_attribute` | `AZID` | attribute parsed onto `Host.az`; the daemon does not route on AZ, so a wrong value here is inert |

All of these are CLI flags too: `--endpoint-url`, `--ip-attribute`, `--port-attribute`,
`--az-attribute`, and — prefixed so they don't read as variants of `--timeout`, which bounds the
`/health` probe — `--aws-connect-timeout`, `--aws-read-timeout`, `--aws-max-attempts`.

!!! warning "Don't unbound the Cloud Map calls"
    botocore's own defaults are a 60s connect and 60s read with up to 5 attempts. Against an
    endpoint that accepts a connection and then goes silent — a black-holed VPC endpoint, a
    security group changed under you — a single `DiscoverInstances` was measured blocking for
    **over five minutes**, on a loop that runs every few seconds. zonal bounds it to roughly ten.
    Raise these if your link is genuinely slow; do not remove them.

!!! warning "Keep the attribute keys consistent"
    The `ip_attribute` / `port_attribute` keys describe one schema written by `RegisterConfig` and
    read by both `DiscoveryConfig` and `HealthConfig`. Override a key in one place and you must
    override it in all three — otherwise the balancer discovers nothing, or the health daemon lists
    instances it cannot parse and silently sweeps zero hosts.

!!! tip "Same-AZ requires capacity in every AZ"
    Affinity is only free when each AZ holds healthy capacity. If a host type (e.g. GPU nodes) isn't
    present in every AZ, the cross-AZ fallback fires and you pay transfer. Weigh idle capacity cost
    against transfer saved.
