import concurrent.futures as cf
import threading

import boto3
import requests

from .config import HealthConfig
from .log import configure_json_logging, get_logger


def resolve_service_id(sd, namespace_name: str, service_name: str) -> str:
    ns_id = None
    for page in sd.get_paginator("list_namespaces").paginate():
        for ns in page["Namespaces"]:
            if ns["Name"] == namespace_name:
                ns_id = ns["Id"]
                break
        if ns_id:
            break
    if not ns_id:
        raise ValueError(f"namespace {namespace_name!r} not found")
    for page in sd.get_paginator("list_services").paginate(
        Filters=[{"Name": "NAMESPACE_ID", "Values": [ns_id], "Condition": "EQ"}]
    ):
        for svc in page["Services"]:
            if svc["Name"] == service_name:
                return svc["Id"]
    raise ValueError(f"service {service_name!r} not found in namespace {namespace_name!r}")


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
        self._http = session  # None -> each probe uses requests.get (see _probe)
        self._stop = threading.Event()
        self._status: dict[str, str] = {}  # instance_id -> last pushed status
        self._streak: dict[str, tuple[str, int]] = {}  # instance_id -> (probe result, count)
        self._log = get_logger("zonal.health").bind(
            namespace=config.namespace, target_service=config.service, service_id=self._service_id
        )

    def _list_instances(self) -> list[tuple[str, str, int]]:
        resp = self._sd.discover_instances(
            NamespaceName=self._cfg.namespace, ServiceName=self._cfg.service, HealthStatus="ALL"
        )
        out = []
        for inst in resp.get("Instances", []):
            a = inst.get("Attributes", {})
            ip, port = a.get("AWS_INSTANCE_IPV4"), a.get("AWS_INSTANCE_PORT")
            if ip and port:
                out.append((inst["InstanceId"], ip, int(port)))
        return out

    def _probe(self, ip: str, port: int) -> bool:
        url = f"{self._cfg.scheme}://{ip}:{port}{self._cfg.health_path}"
        # Probes run concurrently in a thread pool; requests.Session is not guaranteed
        # thread-safe, so the default path uses requests.get (a self-contained session per
        # call). An injected session (tests) is used as-is. Only a 2xx counts as healthy —
        # a 4xx (e.g. a misrouted health_path) is a failure, not a pass.
        get = self._http.get if self._http is not None else requests.get
        try:
            return 200 <= get(url, timeout=self._cfg.timeout).status_code < 300
        except requests.RequestException:
            return False

    def _evaluate(self, instance_id: str, ok: bool) -> None:
        desired = "HEALTHY" if ok else "UNHEALTHY"
        value, count = self._streak.get(instance_id, (None, 0))
        count = count + 1 if value == desired else 1
        self._streak[instance_id] = (desired, count)
        if desired == self._status.get(instance_id):
            return
        # hysteresis: require N consecutive identical probes before flipping, to absorb blips
        threshold = self._cfg.healthy_threshold if ok else self._cfg.unhealthy_threshold
        if count >= threshold:
            previous = self._status.get(instance_id)
            self._push(instance_id, desired)
            self._status[instance_id] = desired
            self._log.info(
                "health_status_changed", instance_id=instance_id, status=desired, previous=previous
            )

    def _push(self, instance_id: str, status: str) -> None:
        try:
            self._sd.update_instance_custom_health_status(
                ServiceId=self._service_id, InstanceId=instance_id, Status=status
            )
        except self._sd.exceptions.InstanceNotFound:
            self._status.pop(instance_id, None)
            self._streak.pop(instance_id, None)
            self._log.info("instance_gone", instance_id=instance_id)

    def run_once(self) -> None:
        instances = self._list_instances()
        # drop bookkeeping for instances Cloud Map no longer lists, else _status/_streak grow
        # unbounded across instance churn (they are only otherwise pruned on a status flip).
        live = {i[0] for i in instances}
        self._status = {k: v for k, v in self._status.items() if k in live}
        self._streak = {k: v for k, v in self._streak.items() if k in live}
        with cf.ThreadPoolExecutor(max_workers=self._cfg.concurrency) as ex:
            results = list(ex.map(lambda i: (i[0], self._probe(i[1], i[2])), instances))
        for instance_id, ok in results:
            self._evaluate(instance_id, ok)
        self._log.debug(
            "health_sweep",
            total=len(results),
            healthy=sum(1 for _, ok in results if ok),
            unhealthy=sum(1 for _, ok in results if not ok),
        )

    def run(self) -> None:
        self._log.info("health_service_started", interval=self._cfg.interval)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                self._log.warning("health_sweep_failed", exc_info=True)
            self._stop.wait(self._cfg.interval)

    def stop(self) -> None:
        self._stop.set()


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
