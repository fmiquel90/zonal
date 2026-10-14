from dataclasses import dataclass, field

# botocore defaults to a 60s connect and 60s read timeout with up to 5 attempts. Against an
# endpoint that accepts connections and then goes silent — a black-holed VPC endpoint, a security
# group change mid-flight — one DiscoverInstances call was measured blocking for over five minutes,
# on a loop that runs every few seconds. Bound it instead: the refresh loop is itself the retry, a
# failed refresh just keeps the stale cache, and a caller that needs more patience can raise these.
DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_READ_TIMEOUT = 3.0
DEFAULT_MAX_ATTEMPTS = 2  # total requests, first one included — not the number of retries
# Registration runs once at boot with no loop behind it to retry, and a fleet scale-out sends
# every new host at the low-TPS control plane at once: throttling is expected, not exceptional.
REGISTER_MAX_ATTEMPTS = 5
DEFAULT_IMDS_TIMEOUT = 2.0


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
    # Bounds on every Cloud Map call, see DEFAULT_CONNECT_TIMEOUT. max_attempts counts total
    # requests, the first one included. Left at None it defers to AWS_MAX_ATTEMPTS /
    # AWS_RETRY_MODE or the profile when the operator set either, else falls back to
    # DEFAULT_MAX_ATTEMPTS.
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    max_attempts: int | None = None
    imds_timeout: float = DEFAULT_IMDS_TIMEOUT  # per-request bound on each IMDS lookup


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
    endpoint_url: str | None = None  # custom Cloud Map endpoint (VPC endpoint, or an emulator)
    # Bounds on every Cloud Map call, see DEFAULT_CONNECT_TIMEOUT. max_attempts counts total
    # requests, the first one included. Left at None it defers to AWS_MAX_ATTEMPTS /
    # AWS_RETRY_MODE or the profile when the operator set either, else falls back to
    # REGISTER_MAX_ATTEMPTS.
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    max_attempts: int | None = None
    imds_timeout: float = DEFAULT_IMDS_TIMEOUT  # per-request bound on each IMDS lookup


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
    # The daemon calls both Cloud Map planes, and AWS serves them from separate VPC endpoint
    # services (servicediscovery and data-servicediscovery). endpoint_url takes the control-plane
    # calls (status pushes, service-id lookup); discovery_endpoint_url takes DiscoverInstances and
    # defaults to endpoint_url, which suits an emulator that serves both on one host.
    endpoint_url: str | None = None
    discovery_endpoint_url: str | None = None
    # Bounds on every Cloud Map call, see DEFAULT_CONNECT_TIMEOUT. max_attempts counts total
    # requests, the first one included. Left at None it defers to AWS_MAX_ATTEMPTS /
    # AWS_RETRY_MODE or the profile when the operator set either, else falls back to
    # DEFAULT_MAX_ATTEMPTS.
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        # The probe pool is built on the first sweep, where the run() loop would absorb this and
        # retry it forever; a config that can never probe must fail at startup instead.
        if self.concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {self.concurrency}")
