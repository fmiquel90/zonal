import concurrent.futures as cf
import threading
from dataclasses import dataclass

import boto3
import requests

from ._base import poll_forever
from .config import HealthConfig
from .discovery import parse_instances
from .log import configure_json_logging, get_logger
from .model import Host


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
        self._sd = sd_client or boto3.client("servicediscovery", region_name=config.region)
        self._service_id = config.service_id or resolve_service_id(
            self._sd, config.namespace, config.service
        )
        self._session = session
        self._local = threading.local()
        self._pool = cf.ThreadPoolExecutor(
            max_workers=config.concurrency, thread_name_prefix="zonal-probe"
        )
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

    def _http(self) -> requests.Session:
        """The session this thread probes with.

        Probes run concurrently and requests.Session is not guaranteed thread-safe, so the default
        path gives each pool thread its own — which, unlike a session per call, keeps connections
        alive across sweeps. An injected session (tests) is used as-is.
        """
        if self._session is not None:
            return self._session
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
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
        hosts = self._list_instances()
        # drop bookkeeping for instances Cloud Map no longer lists, else _state grows unbounded
        # across instance churn (it is only otherwise pruned when a push finds one gone).
        live = {h.instance_id for h in hosts}
        self._state = {k: v for k, v in self._state.items() if k in live}
        total = healthy = 0
        for host, ok in zip(hosts, self._pool.map(self._probe, hosts)):
            total += 1
            healthy += ok
            self._evaluate(host.instance_id, ok)
        self._log.debug("health_sweep", total=total, healthy=healthy, unhealthy=total - healthy)

    def run(self) -> None:
        self._log.info("health_service_started", interval=self._cfg.interval)
        poll_forever(self._stop, self._cfg.interval, self.run_once, self._log, "health_sweep_failed")

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)


def main(argv=None) -> None:
    import argparse

    p = argparse.ArgumentParser(description="zonal Cloud Map custom health checker")
    p.add_argument("--namespace", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--service-id")
    p.add_argument("--region")
    p.add_argument("--health-path", default="/health")
    p.add_argument("--scheme", default="http")
    p.add_argument("--interval", type=float, default=10.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--healthy-threshold", type=int, default=2)
    p.add_argument("--unhealthy-threshold", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=16)
    # No default: defer to the LOG_DEBUG/LOG_LEVEL env opt-in unless explicitly overridden.
    p.add_argument("--log-level")
    args = p.parse_args(argv)
    configure_json_logging(level=args.log_level)
    cfg = HealthConfig(
        namespace=args.namespace,
        service=args.service,
        service_id=args.service_id,
        region=args.region,
        health_path=args.health_path,
        scheme=args.scheme,
        interval=args.interval,
        timeout=args.timeout,
        healthy_threshold=args.healthy_threshold,
        unhealthy_threshold=args.unhealthy_threshold,
        concurrency=args.concurrency,
    )
    HealthChecker(cfg).run()
