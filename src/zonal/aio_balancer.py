import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aioboto3

from ._base import BalancerBase
from .config import DiscoveryConfig
from .discovery import client_config
from .model import Host


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class AsyncBalancer(BalancerBase):
    """Asyncio counterpart of Balancer. Selection and breaker feedback are sync (cheap); only
    discovery and lease() are async."""

    def __init__(self, config: DiscoveryConfig, *, boto_session: aioboto3.Session | None = None, az_id: str | None = None):
        super().__init__(config, az_id, "zonal.aiobalancer")
        if self._az_id is None and not _in_event_loop():
            # Constructed outside a loop, so a blocking read stalls nothing: resolve now and keep
            # az_id populated from construction, as the sync client does. Inside a loop it is
            # deferred to start(), which reads IMDS off-thread rather than freezing the loop for
            # up to two round trips.
            self._resolve_az_id()
        self._boto = boto_session or aioboto3.Session()
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._closing = False

    async def start(self) -> "AsyncBalancer":
        if self._az_id is None:
            # imds.get_az_id blocks on a socket; run_in_executor keeps the loop serving while it does
            await asyncio.get_running_loop().run_in_executor(None, self._resolve_az_id)
        self._task = asyncio.create_task(self._loop(), name="zonal-refresh")
        return self

    async def wait_ready(self, timeout: float | None = None) -> bool:
        # Not folded into wait_for(aw, None): since 3.12 that routes through asyncio.timeout(),
        # which raises RuntimeError when awaited outside a Task. A bare await has no such
        # requirement, and the untimed case never needs a timer anyway.
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
            config=client_config(self._cfg),
        ) as sd:
            while not self._closing:
                try:
                    # keep the stale cache on failure: stale-but-working beats an empty cache
                    self._apply(await sd.discover_instances(**self._discover_kwargs()))
                except Exception:
                    self._log.warning("discovery_refresh_failed", exc_info=True)
                await asyncio.sleep(
                    self._cfg.refresh_interval + random.uniform(0, self._cfg.refresh_jitter)
                )

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[Host]:
        """Pick a host; on a raised exception eject it from the breaker and re-raise, else mark success."""
        host = self.pick()
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
