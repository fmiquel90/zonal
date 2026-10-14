import concurrent.futures as cf
import threading
from dataclasses import dataclass

import boto3
import requests

from ._base import poll_forever
from .config import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT, HealthConfig
from .discovery import client_config, parse_instances
from .log import configure_json_logging, get_logger
from .model import Host, ServiceNotFoundError


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
        raise ServiceNotFoundError(f"namespace {namespace_name!r} not found")
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
        raise ServiceNotFoundError(
            f"service {service_name!r} not found in namespace {namespace_name!r}"
        )
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
        discovery_url = config.discovery_endpoint_url
        if sd_client is None and discovery_url and discovery_url != config.endpoint_url:
            self._discovery = boto3.client(
                "servicediscovery",
                region_name=config.region,
                endpoint_url=discovery_url,
                config=client_config(config, discovery_url),
            )
        else:
            self._discovery = self._sd
        # Not resolved here: that would be paginated network I/O in a constructor, so the object
        # could not be built without live AWS, and a throttle at boot would kill the process
        # instead of being absorbed by the run() loop that exists for exactly that. A missing
        # namespace or service is still fatal, see run(). "" (an unset env var) means resolve.
        self._service_id = config.service_id or None
        self._session = session
        self._local = threading.local()
        self._pool: cf.ThreadPoolExecutor | None = None  # created on first sweep, see _probe_all
        self._pool_lock = threading.Lock()  # close() can run on another thread mid-sweep
        self._stop = threading.Event()
        self._state: dict[str, _InstanceState] = {}
        self._log = get_logger("zonal.health").bind(
            namespace=config.namespace, target_service=config.service
        )
        if self._service_id is not None:
            self._log = self._log.bind(service_id=self._service_id)

    def service_id(self) -> str:
        """The Cloud Map service id, resolved from namespace+service on first use if not given."""
        if self._service_id is None:
            self._service_id = resolve_service_id(self._sd, self._cfg.namespace, self._cfg.service)
            self._log = self._log.bind(service_id=self._service_id)
            self._log.info("service_id_resolved")
        return self._service_id

    def _list_instances(self) -> list[Host]:
        resp = self._discovery.discover_instances(
            NamespaceName=self._cfg.namespace, ServiceName=self._cfg.service, HealthStatus="ALL"
        )
        return parse_instances(resp, self._cfg)

    def _probe_all(self, hosts: list[Host]):
        """Probe every host concurrently, reusing one pool across sweeps.

        The pool outlives a sweep so its threads (and their sessions) are not rebuilt every
        interval, and is created lazily so close() can release it and a later sweep still works.
        map() submits every probe before returning, so holding the lock across it is enough: a
        pool shut down after that still runs what was already queued.
        """
        with self._pool_lock:
            if self._pool is None:
                self._pool = cf.ThreadPoolExecutor(
                    max_workers=self._cfg.concurrency, thread_name_prefix="zonal-probe"
                )
            return self._pool.map(self._probe, hosts)

    def _http(self) -> requests.Session:
        """The session this thread probes with.

        Probes run concurrently and requests.Session is not guaranteed thread-safe, so the default
        path gives each pool thread its own, avoiding the per-call Session build that requests.get
        does. Probes do not keep connections alive: any thread may probe any host, so pooling
        would hold up to concurrency x hosts idle sockets open between sweeps, and a fresh
        connection is also the honest test of whether a host still accepts them.
        An injected session (tests) is used as-is.
        """
        if self._session is not None:
            return self._session
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
            session.headers["Connection"] = "close"
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
                ServiceId=self.service_id(), InstanceId=instance_id, Status=status
            )
        except self._sd.exceptions.InstanceNotFound:
            self._state.pop(instance_id, None)
            self._log.info("instance_gone", instance_id=instance_id)
            return False
        return True

    def run_once(self) -> None:
        self.service_id()  # resolve up front so a lookup failure is one clear error, not N
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
        """Sweep every `interval` until stop(). Transient failures are logged and retried; a
        namespace or service that does not exist raises ServiceNotFoundError, since no retry
        fixes it and a daemon that never pushes a status would leave hung hosts in rotation."""
        self._log.info("health_service_started", interval=self._cfg.interval)
        try:
            poll_forever(
                self._stop,
                self._cfg.interval,
                self.run_once,
                self._log,
                "health_sweep_failed",
                fatal=(ServiceNotFoundError,),
            )
        except ServiceNotFoundError:
            self._log.error("health_service_misconfigured", exc_info=True)
            raise
        finally:
            self.close()

    def stop(self) -> None:
        """Ask run() to finish its current sweep and return, and release the probe pool.
        Safe to call from another thread."""
        self._stop.set()
        self.close()

    def close(self) -> None:
        """Release the probe pool. run() and stop() do this; a later run_once() builds a new one."""
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)

    def __enter__(self) -> "HealthChecker":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def main(argv=None) -> None:
    import argparse

    p = argparse.ArgumentParser(description="zonal Cloud Map custom health checker")
    p.add_argument("--namespace", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--service-id")
    p.add_argument("--region")
    p.add_argument("--endpoint-url", help="custom Cloud Map endpoint (VPC endpoint, or an emulator)")
    p.add_argument(
        "--discovery-endpoint-url",
        help="custom endpoint for DiscoverInstances (the data-plane VPC endpoint); "
        "defaults to --endpoint-url",
    )
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
    p.add_argument("--aws-connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    p.add_argument("--aws-read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    p.add_argument(
        "--aws-max-attempts",
        type=int,
        help="total Cloud Map requests, retries included (default: AWS_MAX_ATTEMPTS or the "
        "profile if set, else 2)",
    )
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
        discovery_endpoint_url=args.discovery_endpoint_url,
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
