"""Construction and shutdown: what blocks, what doesn't, and what surfaces where.

No network and no emulator — these pin the rules about *when* work happens, which is exactly the
kind of thing that regresses silently.
"""
import asyncio
import time

import pytest

import zonal.imds as imds_mod
from zonal import (
    Balancer,
    DiscoveryConfig,
    HealthChecker,
    HealthConfig,
    ImdsError,
    ZonalError,
)


class FakeSD:
    def discover_instances(self, **kwargs):
        return {"Instances": []}


class PaginatingSD(FakeSD):
    """Enough of a Cloud Map client for resolve_service_id, counting what it was asked for."""

    class exceptions:
        InstanceNotFound = Exception

    def __init__(self):
        self.paginators: list[str] = []

    def get_paginator(self, name):
        self.paginators.append(name)
        pages = (
            [{"Namespaces": [{"Name": "ns", "Id": "ns-1"}]}]
            if name == "list_namespaces"
            else [{"Services": [{"Name": "svc", "Id": "srv-9"}]}]
        )
        return type("P", (), {"paginate": lambda _self, **kw: pages})()


@pytest.fixture
def slow_imds(monkeypatch):
    """An IMDS lookup that takes a beat, so 'does this block?' is observable."""
    monkeypatch.setattr(imds_mod, "get_az_id", lambda timeout=2.0: (time.sleep(0.5), "euw1-az1")[1])


def test_imds_failure_is_a_zonal_error(monkeypatch):
    # A caller told to `except ZonalError` must not get a bare urllib error instead.
    monkeypatch.setattr(imds_mod, "BASE_URL", "http://127.0.0.1:1")  # nothing listens
    imds_mod.get_az_id.cache_clear()
    with pytest.raises(ImdsError) as excinfo:
        imds_mod.get_az_id(0.2)
    assert isinstance(excinfo.value, ZonalError)
    imds_mod.get_az_id.cache_clear()


def test_async_balancer_does_not_read_imds_in_a_running_loop(slow_imds):
    """Built inside a loop, the constructor must not block it — the README's own usage is
    `async with AsyncBalancer(cfg) as balancer`, i.e. inside a coroutine."""
    from zonal import AsyncBalancer

    cfg = DiscoveryConfig(namespace="n", service="s")

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)

        started = time.monotonic()
        balancer = AsyncBalancer(cfg, boto_session=object())
        assert time.monotonic() - started < 0.1, "constructor blocked on IMDS"
        assert balancer.az_id is None, "resolution should be deferred to start()"

        before = ticks
        await balancer.start()
        assert balancer.az_id == "euw1-az1"
        # the loop kept running while the blocking read happened off-thread
        assert ticks - before >= 5, f"event loop stalled: only {ticks - before} ticks"
        beat.cancel()
        await balancer.close()

    asyncio.run(scenario())


def test_async_balancer_resolves_eagerly_outside_a_loop(slow_imds):
    """Outside a loop there is nothing to stall, so behaviour matches the sync client: az_id is
    populated from construction."""
    from zonal import AsyncBalancer

    balancer = AsyncBalancer(DiscoveryConfig(namespace="n", service="s"), boto_session=object())
    assert balancer.az_id == "euw1-az1"


def test_explicit_az_id_never_touches_imds(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("IMDS must not be consulted when az_id is given")

    monkeypatch.setattr(imds_mod, "get_az_id", boom)
    assert Balancer(DiscoveryConfig(namespace="n", service="s"), sd_client=FakeSD(),
                    az_id="euw1-az1").az_id == "euw1-az1"


def test_health_checker_constructs_without_aws():
    """resolve_service_id is paginated network I/O; doing it in __init__ made the object
    unbuildable without live AWS and turned a boot-time throttle into a dead process."""
    sd = PaginatingSD()
    with HealthChecker(HealthConfig(namespace="ns", service="svc"), sd_client=sd) as checker:
        assert sd.paginators == [], "constructor hit the network"
        checker.run_once()
        assert checker.service_id() == "srv-9"
        checker.run_once()
        assert sd.paginators == ["list_namespaces", "list_services"], "resolved more than once"


def test_health_checker_skips_resolution_when_service_id_is_given():
    sd = PaginatingSD()
    cfg = HealthConfig(namespace="ns", service="svc", service_id="srv-x")
    with HealthChecker(cfg, sd_client=sd) as checker:
        checker.run_once()
    assert sd.paginators == []


def test_health_checker_resolution_failure_surfaces_in_the_sweep():
    """So the run() loop absorbs and retries it, instead of the constructor killing the process."""
    class Throttled(PaginatingSD):
        def get_paginator(self, name):
            raise RuntimeError("Throttled")

    checker = HealthChecker(HealthConfig(namespace="ns", service="svc"), sd_client=Throttled())
    with checker, pytest.raises(RuntimeError):
        checker.run_once()


def test_close_reports_whether_the_loop_actually_stopped():
    cfg = DiscoveryConfig(namespace="n", service="s", refresh_interval=30)
    balancer = Balancer(cfg, sd_client=FakeSD(), az_id="euw1-az1").start()
    assert balancer.wait_ready(timeout=5)
    assert balancer.close(timeout=5) is True

    # the default stays non-blocking: the thread is a daemon and never holds up process exit
    other = Balancer(cfg, sd_client=FakeSD(), az_id="euw1-az1").start()
    assert other.wait_ready(timeout=5)
    started = time.monotonic()
    other.close()
    assert time.monotonic() - started < 0.5
