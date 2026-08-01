"""Internals shared by the sync client, the async client, and the health daemon.

Nothing here is public API. BalancerBase holds the discovery policy both balancers apply, so it is
written once rather than per transport. poll_forever holds the daemon-loop recovery rule for the
two threaded loops (Balancer and HealthChecker); AsyncBalancer keeps its own loop, because it has
to hold an `async with` client open across iterations and stops by cancellation rather than by an
Event — sharing it would mean parameterising both, for one caller.
"""

import random
import threading
from collections.abc import Callable

from . import imds
from .config import DiscoveryConfig
from .discovery import discover_kwargs, parse_instances, select_hosts
from .log import get_logger
from .model import Host
from .routing import Router


def poll_forever(
    stop: threading.Event,
    interval: float,
    step: Callable[[], None],
    log,
    failure_event: str,
    *,
    jitter: float = 0.0,
) -> None:
    """Run `step` every `interval` seconds until `stop` is set, absorbing any exception it raises.

    Failures are logged and retried on the next tick rather than killing the daemon — for both
    callers a transient AWS error must not take the process down.
    """
    while not stop.is_set():
        try:
            step()
        except Exception:
            log.warning(failure_event, exc_info=True)
        stop.wait(interval + random.uniform(0, jitter))


class BalancerBase:
    """Discovery policy and breaker feedback, shared by Balancer and AsyncBalancer.

    Everything here is synchronous and transport-free. Subclasses own only the transport (boto3 vs
    aioboto3) and the concurrency primitives; `_ready` is a threading.Event or an asyncio.Event,
    both of which expose the same synchronous is_set()/set() used below.
    """

    _ready: threading.Event  # or asyncio.Event — only is_set()/set() are used

    def __init__(self, config: DiscoveryConfig, az_id: str | None, logger_name: str):
        self._cfg = config
        self._az_id = az_id
        self._router = Router(config.breaker_cooldown)
        self._log = get_logger(logger_name).bind(
            namespace=config.namespace, target_service=config.service
        )
        if az_id is not None:
            self._set_az_id(az_id)

    def _set_az_id(self, az_id: str) -> None:
        self._az_id = az_id
        self._log = self._log.bind(az=az_id)

    def _resolve_az_id(self) -> str:
        """Read the AZ-ID from IMDS. Blocking — subclasses decide where it is safe to call."""
        self._set_az_id(imds.get_az_id(self._cfg.imds_timeout))
        return self._az_id

    @property
    def az_id(self) -> str | None:
        """The caller's AZ-ID.

        `None` only on an AsyncBalancer built inside a running event loop without an explicit
        `az_id` and not yet started — see AsyncBalancer.start().
        """
        return self._az_id

    def _discover_kwargs(self) -> dict:
        return discover_kwargs(self._cfg, self._az_id)

    def _apply(self, response: dict) -> None:
        """Fold a DiscoverInstances response into the router. Called from both refresh loops."""
        hosts = parse_instances(response, self._cfg)
        if hosts:
            effective, fallback = select_hosts(hosts, self._az_id, self._cfg.prefer_same_az)
            previous = self._router.snapshot()
            self._router.update(effective)
            if fallback:
                # serving cross-AZ: no healthy same-AZ host, so this traffic is billable
                self._log.warning("cross_az_fallback", host_count=len(effective))
            # the set diff only feeds the log line below, so don't build it before we're ready
            if self._ready.is_set() and set(effective) != set(previous):
                self._log.info("discovery_changed", host_count=len(effective))
        # Signal readiness after the first completed refresh even if it found nothing — else
        # wait_ready() hangs forever on a service that starts empty. An empty *later* refresh
        # keeps the stale cache (handled above); pick() surfaces NoHealthyHostError if empty.
        if not self._ready.is_set():
            self._log.info("discovery_ready", host_count=len(self._router.snapshot()))
            self._ready.set()

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
