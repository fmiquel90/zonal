import concurrent.futures as cf
import threading
from dataclasses import dataclass

import boto3
import requests

from ._base import poll_forever
from .config import HealthConfig
from .discovery import client_config, parse_instances
from .log import configure_json_logging, get_logger
from .model import Host

# Per-thread connection pools to cache, i.e. how many distinct hosts a probe thread can keep
# connections open to. Above this, requests' LRU evicts and those hosts reconnect each sweep.
_PROBE_POOL_HOSTS = 64


def resolve_service_id(sd, namespace_name: str, service_name: str) -> str:
    # generator + next(): paginate lazily, stopping at the first match
    ns_id = next(
        (
            ns["Id"]
            for page in sd.get_paginator("list_namespaces").paginate()
            for ns in page["Namespaces"]
            if ns["Name"] == namespace_name
        ),
        None,
    )
    if not ns_id:
        raise ValueError(f"namespace {namespace_name!r} not found")
    service_id = next(
        (
            svc["Id"]
            for page in sd.get_paginator("list_services").paginate(
                Filters=[{"Name": "NAMESPACE_ID", "Values": [ns_id], "Condition": "EQ"}]
            )
            for svc in page["Services"]
            if svc["Name"] == service_name
        ),
        None,
    )
    if not service_id:
        raise ValueError(f"service {service_name!r} not found in namespace {namespace_name!r}")
    return service_id


@dataclass
class _InstanceState:
    """Per-instance probe bookkeeping, kept in one record so it is pruned in one place."""

    pushed: str | None = None  # last status pushed to Cloud Map
    probed: str | None = None  # verdict of the most recent probe
    streak: int = 0  # consecutive probes agreeing with `probed`


class HealthChecker:
    """Out-of-band health service: probes every registered host and pushes the result to Cloud Map.

    Runs as its own daemon (one per service is enough). Unlike a self-reporting host, it can detect
    a hung instance that can no longer report on its own behalf.
    """

    def __init__(self, config: HealthConfig, *, sd_client=None, session: requests.Session | None = None):
        self._cfg = config
        self._sd = sd_client or boto3.client(
            "servicediscovery",
            region_name=config.region,
            endpoint_url=config.endpoint_url,
            config=client_config(config),
        )
        self._service_id = config.service_id or resolve_service_id(
            self._sd, config.namespace, config.service
        )
        self._session = session
        self._local = threading.local()
        self._pool: cf.ThreadPoolExecutor | None = None  # created on first sweep, see _probe_all
        self._stop = threading.Event()
        self._state: dict[str, _InstanceState] = {}
        self._log = get_logger("zonal.health").bind(
            namespace=config.namespace, target_service=config.service, service_id=self._service_id
        )

    def _list_instances(self) -> list[Host]:
        resp = self._sd.discover_instances(
            NamespaceName=self._cfg.namespace, ServiceName=self._cfg.service, HealthStatus="ALL"
        )
        return parse_instances(resp, self._cfg)

    def _probe_all(self, hosts: list[Host]):
        """Probe every host concurrently, reusing one pool across sweeps.

        The pool outlives a sweep so its threads (and their sessions) are not rebuilt every
        interval, and is created lazily so close() can release it and a later sweep still works.
        """
        if self._pool is None:
            self._pool = cf.ThreadPoolExecutor(
                max_workers=self._cfg.concurrency, thread_name_prefix="zonal-probe"
            )
        return self._pool.map(self._probe, hosts)

    def _http(self) -> requests.Session:
        """The session this thread probes with.

        Probes run concurrently and requests.Session is not guaranteed thread-safe, so the default
        path gives each pool thread its own, avoiding the per-call Session build that requests.get
        does. Connections also survive between sweeps, though only for the last `pool_connections`
        distinct hosts a given thread touched — a fleet much larger than that still reconnects.
        An injected session (tests) is used as-is.
        """
        if self._session is not None:
            return self._session
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=_PROBE_POOL_HOSTS, pool_maxsize=2)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        return session

    def _probe(self, host: Host) -> bool:
        # Only a 2xx counts as healthy — a 4xx (e.g. a misrouted health_path) is a failure, not a pass.
        url = host.url(self._cfg.health_path, scheme=self._cfg.scheme)
        try:
            return 200 <= self._http().get(url, timeout=self._cfg.timeout).status_code < 300
        except requests.RequestException:
            return False

    def _evaluate(self, instance_id: str, ok: bool) -> None:
        desired = "HEALTHY" if ok else "UNHEALTHY"
        state = self._state.setdefault(instance_id, _InstanceState())
        state.streak = state.streak + 1 if state.probed == desired else 1
        state.probed = desired
        previous = state.pushed
        if desired == previous:
            return
        # hysteresis: require N consecutive identical probes before flipping, to absorb blips
        threshold = self._cfg.healthy_threshold if ok else self._cfg.unhealthy_threshold
        if state.streak >= threshold and self._push(instance_id, desired):
            state.pushed = desired
            self._log.info(
                "health_status_changed", instance_id=instance_id, status=desired, previous=previous
            )

    def _push(self, instance_id: str, status: str) -> bool:
        """Push a status to Cloud Map. False if the instance has since been deregistered."""
        try:
            self._sd.update_instance_custom_health_status(
                ServiceId=self._service_id, InstanceId=instance_id, Status=status
            )
        except self._sd.exceptions.InstanceNotFound:
            self._state.pop(instance_id, None)
            self._log.info("instance_gone", instance_id=instance_id)
            return False
        return True

    def run_once(self) -> None:
        # An instance with no InstanceId cannot be pushed a status, and would otherwise be keyed
        # under None in _state and sent to Cloud Map as InstanceId=None.
        hosts = [h for h in self._list_instances() if h.instance_id]
        # drop bookkeeping for instances Cloud Map no longer lists, else _state grows unbounded
        # across instance churn (it is only otherwise pruned when a push finds one gone).
        live = {h.instance_id for h in hosts}
        self._state = {k: v for k, v in self._state.items() if k in live}
        total = healthy = 0
        for host, ok in zip(hosts, self._probe_all(hosts)):
            total += 1
            healthy += ok
            self._evaluate(host.instance_id, ok)
        self._log.debug("health_sweep", total=total, healthy=healthy, unhealthy=total - healthy)

    def run(self) -> None:
        self._log.info("health_service_started", interval=self._cfg.interval)
        try:
            poll_forever(
                self._stop, self._cfg.interval, self.run_once, self._log, "health_sweep_failed"
            )
        finally:
            self.close()

    def stop(self) -> None:
        """Ask run() to finish its current sweep and return. Safe to call from another thread."""
        self._stop.set()

    def close(self) -> None:
        """Release the probe pool. run() does this on exit; call it yourself if you only use
        run_once(), or use the checker as a context manager."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)

    def __enter__(self) -> "HealthChecker":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
        self.close()


def main(argv=None) -> None:
    import argparse

    p = argparse.ArgumentParser(description="zonal Cloud Map custom health checker")
    p.add_argument("--namespace", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--service-id")
    p.add_argument("--region")
    p.add_argument("--endpoint-url", help="custom Cloud Map endpoint (VPC endpoint, or an emulator)")
    p.add_argument("--health-path", default="/health")
    p.add_argument("--scheme", default="http")
    p.add_argument("--interval", type=float, default=10.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--healthy-threshold", type=int, default=2)
    p.add_argument("--unhealthy-threshold", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=16)
    # Must match whatever RegisterConfig wrote and DiscoveryConfig reads, or the daemon lists
    # instances it cannot parse and silently sweeps nothing.
    # `aws-` prefixed so they don't read as variants of --timeout, which bounds the /health probe
    p.add_argument("--aws-connect-timeout", type=float, default=2.0)
    p.add_argument("--aws-read-timeout", type=float, default=3.0)
    p.add_argument("--aws-max-attempts", type=int, default=2, help="total Cloud Map requests, retries included")
    p.add_argument("--az-attribute", default="AZID")
    p.add_argument("--ip-attribute", default="AWS_INSTANCE_IPV4")
    p.add_argument("--port-attribute", default="AWS_INSTANCE_PORT")
    # No default: defer to the LOG_DEBUG/LOG_LEVEL env opt-in unless explicitly overridden.
    p.add_argument("--log-level")
    args = p.parse_args(argv)
    configure_json_logging(level=args.log_level)
    cfg = HealthConfig(
        namespace=args.namespace,
        service=args.service,
        service_id=args.service_id,
        region=args.region,
        endpoint_url=args.endpoint_url,
        health_path=args.health_path,
        scheme=args.scheme,
        interval=args.interval,
        timeout=args.timeout,
        healthy_threshold=args.healthy_threshold,
        unhealthy_threshold=args.unhealthy_threshold,
        concurrency=args.concurrency,
        az_attribute=args.az_attribute,
        ip_attribute=args.ip_attribute,
        port_attribute=args.port_attribute,
        connect_timeout=args.aws_connect_timeout,
        read_timeout=args.aws_read_timeout,
        max_attempts=args.aws_max_attempts,
    )
    with HealthChecker(cfg) as checker:
        checker.run()
