from dataclasses import dataclass, field


@dataclass
class DiscoveryConfig:
    namespace: str
    service: str
    region: str | None = None
    endpoint_url: str | None = None  # custom Cloud Map endpoint (VPC endpoint, or MiniStack in tests)
    refresh_interval: float = 5.0
    refresh_jitter: float = 1.0
    breaker_cooldown: float = 10.0
    prefer_same_az: bool = True
    max_results: int = 100
    az_attribute: str = "AZID"
    ip_attribute: str = "AWS_INSTANCE_IPV4"
    port_attribute: str = "AWS_INSTANCE_PORT"


@dataclass
class RegisterConfig:
    service_id: str
    port: int
    region: str | None = None
    az_attribute: str = "AZID"
    extra_attributes: dict[str, str] = field(default_factory=dict)
    # must match DiscoveryConfig's, or the balancer discovers nothing: one side writes the
    # attributes the other side reads.
    ip_attribute: str = "AWS_INSTANCE_IPV4"
    port_attribute: str = "AWS_INSTANCE_PORT"


@dataclass
class HealthConfig:
    namespace: str
    service: str
    service_id: str | None = None  # resolved from namespace+service if omitted
    region: str | None = None
    health_path: str = "/health"
    scheme: str = "http"
    interval: float = 10.0
    timeout: float = 2.0
    healthy_threshold: int = 2
    unhealthy_threshold: int = 3
    concurrency: int = 16
    # ip/port must match DiscoveryConfig's, or the daemon parses no instance out of the listing and
    # silently sweeps nothing. az_attribute only labels the parsed host — the daemon does not route
    # on AZ — but a wrong value here is harmless, not a probe failure.
    az_attribute: str = "AZID"
    ip_attribute: str = "AWS_INSTANCE_IPV4"
    port_attribute: str = "AWS_INSTANCE_PORT"
