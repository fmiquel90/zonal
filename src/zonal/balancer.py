import threading
from collections.abc import Iterator
from contextlib import contextmanager

import boto3

from ._base import BalancerBase, poll_forever
from .config import DiscoveryConfig
from .discovery import _boto_config
from .model import Host


class Balancer(BalancerBase):
    """AZ-affine host balancer over AWS Cloud Map.

    Hands out a healthy host in the caller's AZ, falling back to other AZs only when no same-AZ
    host is healthy. Per-host liveness is tracked by a local circuit breaker that you feed via
    report_failure / report_success (or the lease() context manager). It does not perform the
    request — you own the transport.
    """

    def __init__(self, config: DiscoveryConfig, *, sd_client=None, az_id: str | None = None):
        super().__init__(config, az_id, "zonal.balancer")
        self._sd = sd_client or boto3.client(
            "servicediscovery",
            region_name=config.region,
            endpoint_url=config.endpoint_url,
            config=_boto_config(config.endpoint_url),
        )
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="zonal-refresh", daemon=True)

    def start(self) -> "Balancer":
        self._thread.start()
        return self

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def _refresh_once(self) -> None:
        self._apply(self._sd.discover_instances(**self._discover_kwargs()))

    def _loop(self) -> None:
        # on failure poll_forever keeps the stale cache: stale-but-working beats an empty cache
        poll_forever(
            self._stop,
            self._cfg.refresh_interval,
            self._refresh_once,
            self._log,
            "discovery_refresh_failed",
            jitter=self._cfg.refresh_jitter,
        )

    @contextmanager
    def lease(self) -> Iterator[Host]:
        """Pick a host; on a raised exception eject it from the breaker and re-raise, else mark success."""
        host = self.pick()
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
