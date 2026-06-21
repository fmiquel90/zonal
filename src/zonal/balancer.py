import random
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import boto3

from . import imds
from .config import DiscoveryConfig
from .discovery import _boto_config, discover_kwargs, parse_instances, select_hosts
from .log import get_logger
from .model import Host
from .routing import Router


class Balancer:
    """AZ-affine host balancer over AWS Cloud Map.

    Hands out a healthy host in the caller's AZ, falling back to other AZs only when no same-AZ
    host is healthy. Per-host liveness is tracked by a local circuit breaker that you feed via
    report_failure / report_success (or the lease() context manager). It does not perform the
    request — you own the transport.
    """

    def __init__(self, config: DiscoveryConfig, *, sd_client=None, az_id: str | None = None):
        self._cfg = config
        self._az_id = az_id or imds.get_az_id()
        self._sd = sd_client or boto3.client(
            "servicediscovery",
            region_name=config.region,
            endpoint_url=config.endpoint_url,
            config=_boto_config(config.endpoint_url),
        )
        self._router = Router(config.breaker_cooldown)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._log = get_logger("zonal.balancer").bind(
            namespace=config.namespace, target_service=config.service, az=self._az_id
        )
        self._thread = threading.Thread(target=self._loop, name="zonal-refresh", daemon=True)

    @property
    def az_id(self) -> str:
        return self._az_id

    def start(self) -> "Balancer":
        self._thread.start()
        return self

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def _refresh_once(self) -> None:
        resp = self._sd.discover_instances(**discover_kwargs(self._cfg, self._az_id))
        hosts = parse_instances(resp, self._cfg)
        if hosts:
            effective, fallback = select_hosts(hosts, self._az_id, self._cfg.prefer_same_az)
            changed = set(effective) != set(self._router.snapshot())
            self._router.update(effective)
            if fallback:
                # serving cross-AZ: no healthy same-AZ host, so this traffic is billable
                self._log.warning("cross_az_fallback", host_count=len(effective))
            if self._ready.is_set() and changed:
                self._log.info("discovery_changed", host_count=len(effective))
        # Signal readiness after the first completed refresh even if it found nothing — else
        # wait_ready() hangs forever on a service that starts empty. An empty *later* refresh
        # keeps the stale cache (handled above); pick() surfaces NoHealthyHostError if empty.
        if not self._ready.is_set():
            self._log.info("discovery_ready", host_count=len(self._router.snapshot()))
            self._ready.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_once()
            except Exception:
                # keep the stale cache: stale-but-working beats an empty cache
                self._log.warning("discovery_refresh_failed", exc_info=True)
            self._stop.wait(self._cfg.refresh_interval + random.uniform(0, self._cfg.refresh_jitter))

    def pick(self) -> Host:
        """Return a healthy host (same-AZ preferred). Raises NoHealthyHostError if the cache is empty."""
        return self._router.pick()

    def hosts(self) -> tuple[Host, ...]:
        return self._router.snapshot()

    def report_failure(self, host: Host) -> None:
        self._router.mark_down(host)
        self._log.warning("host_ejected", host=host.ip, host_az=host.az)

    def report_success(self, host: Host) -> None:
        self._router.clear(host)

    @contextmanager
    def lease(self) -> Iterator[Host]:
        """Pick a host; on a raised exception eject it from the breaker and re-raise, else mark success."""
        host = self._router.pick()
        try:
            yield host
        except Exception:
            self.report_failure(host)
            raise
        else:
            self.report_success(host)

    def close(self) -> None:
        self._stop.set()

    def __enter__(self) -> "Balancer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()
