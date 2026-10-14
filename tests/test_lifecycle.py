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
    ServiceNotFoundError,
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


class FakeAioSession:
    """An aioboto3.Session whose client is an async context manager over FakeSD."""

    def __init__(self, fail_with: Exception | None = None):
        self.fail_with = fail_with
        self.closed = False

    def client(self, *args, **kwargs):
        session = self

        class _Client:
            async def __aenter__(self):
                if session.fail_with:
                    raise session.fail_with
                return self

            async def __aexit__(self, *exc):
                session.closed = True

            async def discover_instances(self, **kw):
                return {"Instances": []}

        return _Client()


@pytest.fixture
def slow_imds(monkeypatch):
    """An IMDS lookup that takes a beat, so 'does this block?' is observable."""
    monkeypatch.setattr(imds_mod, "get_az_id", lambda timeout=2.0: (time.sleep(0.5), "euw1-az1")[1])


def test_imds_failure_is_a_zonal_error(monkeypatch):
    # A caller told to `except ZonalError` must not get a bare urllib error instead.
    monkeypatch.setattr(imds_mod, "BASE_URL", "http://127.0.0.1:1")  # nothing listens
    imds_mod._cache.clear()
    with pytest.raises(ImdsError) as excinfo:
        imds_mod.get_az_id(0.2)
    assert isinstance(excinfo.value, ZonalError)
    imds_mod._cache.clear()


def test_async_balancer_does_not_read_imds_in_a_running_loop(slow_imds):
    """Built inside a loop, the constructor must not block it — the README's own usage is
    `async with AsyncBalancer(cfg) as balancer`, i.e. inside a coroutine."""
    pytest.importorskip("aioboto3")
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
        balancer = AsyncBalancer(cfg, boto_session=FakeAioSession())
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
    pytest.importorskip("aioboto3")
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


def test_wait_closed_reports_whether_the_loop_actually_stopped():
    cfg = DiscoveryConfig(namespace="n", service="s", refresh_interval=30)
    balancer = Balancer(cfg, sd_client=FakeSD(), az_id="euw1-az1").start()
    assert balancer.wait_ready(timeout=5)
    balancer.close()
    assert balancer.wait_closed(timeout=5) is True

    # the default stays non-blocking: the thread is a daemon and never holds up process exit
    other = Balancer(cfg, sd_client=FakeSD(), az_id="euw1-az1").start()
    assert other.wait_ready(timeout=5)
    started = time.monotonic()
    other.close()
    assert time.monotonic() - started < 0.5


def test_async_balancer_bad_client_config_raises_from_start():
    """A client that cannot be built (no region, max_attempts=0) used to die inside the refresh
    task without a log line, leaving an untimed wait_ready() pending forever."""
    pytest.importorskip("aioboto3")
    from zonal import AsyncBalancer

    cfg = DiscoveryConfig(namespace="n", service="s")

    async def scenario():
        balancer = AsyncBalancer(cfg, boto_session=FakeAioSession(RuntimeError("no region")),
                                 az_id="euw1-az1")
        with pytest.raises(RuntimeError, match="no region"):
            await balancer.start()

        session = FakeAioSession()
        async with AsyncBalancer(cfg, boto_session=session, az_id="euw1-az1") as ok:
            assert await ok.wait_ready(timeout=5)
        assert session.closed, "close() did not release the client"

    asyncio.run(scenario())


def test_empty_az_id_means_auto_detect(monkeypatch):
    """`az_id=os.environ.get("AZ_ID", "")` must not silently turn AZ affinity off."""
    monkeypatch.setattr(imds_mod, "get_az_id", lambda timeout=2.0: "euw1-az2")
    balancer = Balancer(DiscoveryConfig(namespace="n", service="s"), sd_client=FakeSD(), az_id="")
    assert balancer.az_id == "euw1-az2"


def test_empty_service_id_is_resolved():
    sd = PaginatingSD()
    cfg = HealthConfig(namespace="ns", service="svc", service_id="")
    with HealthChecker(cfg, sd_client=sd) as checker:
        assert checker.service_id() == "srv-9"


def test_zero_concurrency_fails_at_startup():
    with pytest.raises(ValueError, match="concurrency"):
        HealthConfig(namespace="ns", service="svc", concurrency=0)


def test_missing_service_stops_the_daemon_instead_of_retrying_forever():
    """A typo'd namespace or service can never succeed; retrying it every sweep left a RUNNING
    daemon that never pushed a status, so hung hosts stayed in rotation."""
    class Elsewhere(PaginatingSD):
        def get_paginator(self, name):
            pages = [{"Namespaces": [{"Name": "other", "Id": "ns-2"}]}]
            return type("P", (), {"paginate": lambda _self, **kw: pages})()

    checker = HealthChecker(HealthConfig(namespace="ns", service="svc", interval=0.01),
                            sd_client=Elsewhere())
    with pytest.raises(ServiceNotFoundError):
        checker.run()
    assert isinstance(ServiceNotFoundError("x"), ValueError), "resolve_service_id raised ValueError"


def test_stop_releases_the_probe_pool():
    sd = PaginatingSD()
    checker = HealthChecker(HealthConfig(namespace="ns", service="svc", service_id="srv-x"),
                            sd_client=sd)
    checker._probe_all([])
    assert checker._pool is not None
    checker.stop()
    assert checker._pool is None


def test_imds_cache_ignores_the_timeout(monkeypatch):
    """lru_cache keyed on timeout, so get_az_id() and get_az_id(2.0) evicted each other."""
    calls = []
    monkeypatch.setattr(imds_mod, "_token", lambda timeout=2.0, ttl=60: "t")
    monkeypatch.setattr(imds_mod, "_get", lambda path, token, timeout=2.0: calls.append(path) or "v")
    imds_mod._cache.clear()
    try:
        for args in [(), (2.0,), (), (5.0,)]:
            imds_mod.get_az_id(*args)
        assert calls == ["placement/availability-zone-id"]
        imds_mod.metadata()
        imds_mod.metadata(1.0)
        assert calls.count("placement/availability-zone-id") == 1, "metadata re-read the AZ-ID"
        assert calls.count("instance-id") == 1
    finally:
        imds_mod._cache.clear()


def test_imds_truncated_response_is_a_zonal_error(monkeypatch):
    import http.client

    def truncated(*a, **kw):
        raise http.client.IncompleteRead(b"euw1")

    monkeypatch.setattr(imds_mod.urllib.request, "urlopen", truncated)
    with pytest.raises(ImdsError):
        imds_mod._fetch("http://169.254.169.254/latest/api/token", 0.1)
