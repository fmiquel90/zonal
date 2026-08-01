from botocore.config import Config

from .config import DiscoveryConfig, HealthConfig, RegisterConfig
from .model import Host


# botocore defaults to a 60s connect and 60s read timeout with up to 5 attempts. Against an
# endpoint that accepts connections and then goes silent — a black-holed VPC endpoint, a security
# group change mid-flight — one DiscoverInstances call was measured blocking for over five minutes,
# on a loop that runs every few seconds. Bound it instead: the refresh loop is itself the retry, a
# failed refresh just keeps the stale cache, and a caller that needs more patience can raise these.
DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_READ_TIMEOUT = 3.0
DEFAULT_MAX_ATTEMPTS = 2  # total requests, first one included — not the number of retries


def _boto_config(
    endpoint_url: str | None,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Config:
    kwargs: dict = {
        "connect_timeout": connect_timeout,
        "read_timeout": read_timeout,
        "retries": {"total_max_attempts": max_attempts, "mode": "standard"},
    }
    # DiscoverInstances is a data-plane call: botocore prepends a "data-" host prefix
    # (data-servicediscovery.<region>...). Against a custom endpoint (VPC endpoint, MiniStack,
    # LocalStack) that prefix points nowhere, so disable it whenever an endpoint is overridden.
    if endpoint_url:
        kwargs["inject_host_prefix"] = False
    return Config(**kwargs)


def client_config(cfg: "DiscoveryConfig | RegisterConfig | HealthConfig") -> Config:
    """The botocore Config for any of zonal's config dataclasses."""
    return _boto_config(
        cfg.endpoint_url,
        connect_timeout=cfg.connect_timeout,
        read_timeout=cfg.read_timeout,
        max_attempts=cfg.max_attempts,
    )


def parse_instances(response: dict, cfg: DiscoveryConfig | HealthConfig) -> list[Host]:
    """Map a DiscoverInstances response onto Hosts, skipping instances missing an ip or port."""
    hosts: list[Host] = []
    for inst in response.get("Instances", []):
        attrs = inst.get("Attributes", {})
        ip = attrs.get(cfg.ip_attribute)
        port = attrs.get(cfg.port_attribute)
        if not ip or not port:
            continue
        hosts.append(
            Host(
                ip=ip,
                port=int(port),
                az=attrs.get(cfg.az_attribute),
                instance_id=inst.get("InstanceId"),
            )
        )
    return hosts


def select_hosts(hosts: list[Host], az_id: str | None, prefer_same_az: bool) -> tuple[list[Host], bool]:
    """Authoritative AZ selection, client-side: same-AZ hosts when any exist, else all (fallback).

    This does not rely on the backend honoring DiscoverInstances OptionalParameters. AWS documents
    those as opportunistic filters that fail open — when nothing matches, the filter is dropped and
    every instance is returned — and emulators may ignore them outright. Re-applying affinity here
    makes the behaviour identical everywhere and testable locally.
    Returns (effective_hosts, is_cross_az_fallback).
    """
    if not prefer_same_az or not az_id:
        return hosts, False
    same_az = [h for h in hosts if h.az == az_id]
    if same_az:
        return same_az, False
    return hosts, True


def discover_kwargs(cfg: DiscoveryConfig, az_id: str | None) -> dict:
    kwargs: dict = {
        "NamespaceName": cfg.namespace,
        "ServiceName": cfg.service,
        "HealthStatus": "HEALTHY",
        "MaxResults": cfg.max_results,
    }
    # OptionalParameters narrows the result to same-AZ hosts server-side — a bandwidth optimization,
    # never a correctness guarantee: AWS applies these filters opportunistically and returns every
    # instance when none match. select_hosts re-applies affinity client-side, so the outcome is the
    # same whether the backend honours the filter, fails it open, or ignores it entirely.
    if cfg.prefer_same_az and az_id:
        kwargs["OptionalParameters"] = {cfg.az_attribute: az_id}
    return kwargs
