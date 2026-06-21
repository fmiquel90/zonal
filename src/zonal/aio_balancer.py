import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aioboto3

from . import imds
from .config import DiscoveryConfig
from .discovery import _boto_config, discover_kwargs, parse_instances, select_hosts
from .log import get_logger
from .model import Host
from .routing import Router


class AsyncBalancer:
    """Asyncio counterpart of Balancer. Selection and breaker feedback are sync (cheap); only
    discovery and lease() are async."""

    def __init__(self, config: DiscoveryConfig, *, boto_session: aioboto3.Session | None = None, az_id: str | None = None):
        self._cfg = config
        self._az_id = az_id or imds.get_az_id()
        self._router = Router(config.breaker_cooldown)
        self._boto = boto_session or aioboto3.Session()
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._closing = False
        self._log = get_logger("zonal.aiobalancer").bind(
            namespace=config.namespace, target_service=config.service, az=self._az_id
        )

    @property
    def az_id(self) -> str:
        return self._az_id

    async def start(self) -> "AsyncBalancer":
        self._task = asyncio.create_task(self._loop(), name="zonal-refresh")
        return self

    async def wait_ready(self, timeout: float | None = None) -> bool:
        if timeout is None:
            await self._ready.wait()
            return True
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _loop(self) -> None:
        async with self._boto.client(
            "servicediscovery",
            region_name=self._cfg.region,
            endpoint_url=self._cfg.endpoint_url,
            config=_boto_config(self._cfg.endpoint_url),
        ) as sd:
            while not self._closing:
                try:
                    resp = await sd.discover_instances(**discover_kwargs(self._cfg, self._az_id))
                    hosts = parse_instances(resp, self._cfg)
                    if hosts:
                        effective, fallback = select_hosts(hosts, self._az_id, self._cfg.prefer_same_az)
                        changed = set(effective) != set(self._router.snapshot())
                        self._router.update(effective)
                        if fallback:
                            self._log.warning("cross_az_fallback", host_count=len(effective))
                        if self._ready.is_set() and changed:
                            self._log.info("discovery_changed", host_count=len(effective))
                    # Signal readiness after the first successful refresh even if empty — else
                    # wait_ready() hangs forever on a service that starts with no instances. A
                    # later empty refresh keeps the stale cache; pick() raises if truly empty.
                    if not self._ready.is_set():
                        self._log.info("discovery_ready", host_count=len(self._router.snapshot()))
                        self._ready.set()
                except Exception:
                    self._log.warning("discovery_refresh_failed", exc_info=True)
                await asyncio.sleep(
                    self._cfg.refresh_interval + random.uniform(0, self._cfg.refresh_jitter)
                )

    def pick(self) -> Host:
        return self._router.pick()

    def hosts(self) -> tuple[Host, ...]:
        return self._router.snapshot()

    def report_failure(self, host: Host) -> None:
        self._router.mark_down(host)
        self._log.warning("host_ejected", host=host.ip, host_az=host.az)

    def report_success(self, host: Host) -> None:
        self._router.clear(host)

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[Host]:
        host = self._router.pick()
        try:
            yield host
        except Exception:
            self.report_failure(host)
            raise
        else:
            self.report_success(host)

    async def close(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def __aenter__(self) -> "AsyncBalancer":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.close()
