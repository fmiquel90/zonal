# Configuration

All configuration is plain dataclasses from `zonal.config`.

## `DiscoveryConfig` (caller)

| Field | Default | Purpose |
|---|---|---|
| `namespace`, `service` | — | Cloud Map namespace + service names |
| `region` | `None` | AWS region |
| `endpoint_url` | `None` | custom Cloud Map endpoint (VPC endpoint, or MiniStack in tests) |
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
| `az_attribute` | `AZID` | Cloud Map attribute key the AZ-ID is written under |
| `extra_attributes` | `{}` | additional Cloud Map instance attributes |

## `HealthConfig` (health daemon)

| Field | Default | Purpose |
|---|---|---|
| `namespace`, `service` | — | service to monitor |
| `service_id` | `None` | resolved from `namespace`+`service` if omitted |
| `health_path` / `scheme` | `/health` / `http` | probe target |
| `interval` / `timeout` | `10.0` / `2.0` | sweep cadence and per-probe timeout (seconds) |
| `healthy_threshold` / `unhealthy_threshold` | `2` / `3` | consecutive probes before flipping status |
| `concurrency` | `16` | parallel probes per sweep |

!!! tip "Same-AZ requires capacity in every AZ"
    Affinity is only free when each AZ holds healthy capacity. If a host type (e.g. GPU nodes) isn't
    present in every AZ, the cross-AZ fallback fires and you pay transfer. Weigh idle capacity cost
    against transfer saved.
