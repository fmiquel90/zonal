from botocore.config import Config

from .config import DiscoveryConfig
from .model import Host


def _boto_config(endpoint_url: str | None) -> Config | None:
    # DiscoverInstances is a data-plane call: botocore prepends a "data-" host prefix
    # (data-servicediscovery.<region>...). Against a custom endpoint (VPC endpoint, MiniStack,
    # LocalStack) that prefix points nowhere, so disable it whenever an endpoint is overridden.
    if endpoint_url:
        return Config(inject_host_prefix=False)
    return None


def parse_instances(response: dict, cfg: DiscoveryConfig) -> list[Host]:
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

    This does not rely on the backend honoring DiscoverInstances OptionalParameters (real AWS does;
    emulators like MiniStack/LocalStack do not), so affinity behaves identically everywhere and is
    testable locally. Returns (effective_hosts, is_cross_az_fallback).
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
    # OptionalParameters narrows the result to same-AZ hosts server-side (a bandwidth optimization
    # in real AWS). It is NOT relied upon for correctness — select_hosts re-applies affinity
    # client-side — so behavior is identical when the backend ignores it (e.g. emulators).
    if cfg.prefer_same_az and az_id:
        kwargs["OptionalParameters"] = {cfg.az_attribute: az_id}
    return kwargs
