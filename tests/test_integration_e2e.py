"""The three end-to-end promises the rest of the suite asserts only in pieces.

Everything here runs the whole chain against a live Cloud Map: real HTTP backends, the real
health daemon, and a running balancer with its background refresh loop. Nothing is stubbed.
"""
import time

import boto3
import pytest
from botocore.config import Config

from zonal import (
    Balancer,
    DiscoveryConfig,
    HealthChecker,
    HealthConfig,
    RegisterConfig,
    register_instance,
    deregister_instance,
)
from tests.conftest import ENDPOINT, REGION

pytestmark = pytest.mark.integration

# The refresh loop is the thing under test, so poll for its effect rather than sleeping a
# fixed amount: fast when it works, and it fails with a useful message when it doesn't.
SETTLE = 15.0


def until(predicate, timeout=SETTLE):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _register(sd, svc, backend, instance_id):
    register_instance(
        RegisterConfig(service_id=svc["service_id"], port=backend.port, region=REGION),
        sd_client=sd,
        metadata={"ipv4": "127.0.0.1", "instance_id": instance_id, "az_id": backend.az,
                  "az_name": backend.az, "region": REGION},
    )


def _discovery_cfg(svc, endpoint_url=ENDPOINT, **kw):
    return DiscoveryConfig(namespace=svc["namespace"], service=svc["service"], region=REGION,
                           endpoint_url=endpoint_url, refresh_interval=0.3, **kw)


def _ports(balancer):
    return {h.port for h in balancer.hosts()}


def _reaches(client) -> bool:
    try:
        client.list_namespaces()
        return True
    except Exception:
        return False


def test_unhealthy_host_leaves_a_running_balancer(sd, cloud_map_service, backends):
    """The product's one job, end to end: a backend stops answering /health, the daemon notices
    and pushes UNHEALTHY, and a balancer that is already running stops handing that host out.

    Every other test asserts one link of that chain; this one asserts the chain.
    """
    svc = cloud_map_service
    good, bad = backends("euw1-az1"), backends("euw1-az1")
    _register(sd, svc, good, "i-good")
    _register(sd, svc, bad, "i-bad")

    checker = HealthChecker(
        HealthConfig(namespace=svc["namespace"], service=svc["service"],
                     service_id=svc["service_id"], region=REGION, endpoint_url=ENDPOINT,
                     interval=0.3, timeout=1.0, healthy_threshold=1, unhealthy_threshold=1),
        sd_client=sd,
    )
    with checker, Balancer(_discovery_cfg(svc), sd_client=sd, az_id="euw1-az1") as balancer:
        checker.run_once()  # seed both as HEALTHY before the balancer's first discovery
        if not balancer.wait_ready(timeout=SETTLE):
            pytest.skip("MiniStack never completed a discovery")
        if not until(lambda: _ports(balancer) == {good.port, bad.port}):
            pytest.skip("MiniStack did not surface both HEALTHY instances")

        bad.healthy = False  # the host goes dark; only the daemon can notice
        assert until(lambda: checker.run_once() or _ports(balancer) == {good.port}), (
            f"unhealthy host never left the cache: {_ports(balancer)}"
        )
        # and the balancer keeps serving from the survivor rather than failing
        assert {balancer.pick().port for _ in range(6)} == {good.port}


def test_discovery_failure_keeps_the_stale_cache(sd, cloud_map_service, backends, breakable_endpoint):
    """operations.md promises a discovery failure keeps the last good host list — stale but
    working beats an empty cache. Cut the connection to Cloud Map for real and hold it to that.
    """
    svc = cloud_map_service
    backend = backends("euw1-az1")
    _register(sd, svc, backend, "i-stale")

    # Real botocore against the forwarder, but bounded: with the library's default client a cut
    # connection is retried with 60s timeouts, so the refresh would still be blocked in-flight and
    # the test would pass without the failure path ever running.
    failing_client = boto3.client(
        "servicediscovery", endpoint_url=breakable_endpoint.url, region_name=REGION,
        config=Config(inject_host_prefix=False, connect_timeout=1, read_timeout=1,
                      retries={"max_attempts": 0}),
    )
    cfg = _discovery_cfg(svc, endpoint_url=breakable_endpoint.url, refresh_jitter=0.0)
    with Balancer(cfg, sd_client=failing_client, az_id="euw1-az1") as balancer:
        if not balancer.wait_ready(timeout=SETTLE) or not until(lambda: _ports(balancer)):
            pytest.skip("MiniStack never surfaced a HEALTHY instance")
        warm = balancer.hosts()

        breakable_endpoint.cut()
        # prove discovery is actually failing before asserting anything about the cache
        assert until(lambda: not _reaches(failing_client), timeout=5), "the endpoint never went down"
        time.sleep(1.5)  # several refresh intervals, every one of them failing

        assert balancer.hosts() == warm, "stale cache was dropped on a discovery failure"
        assert balancer.pick() == warm[0], "pick() stopped working while discovery was down"

        breakable_endpoint.heal()
        assert until(lambda: balancer.hosts() == warm), "did not recover once discovery came back"


def test_deregistered_host_leaves_the_cache_and_comes_back(sd, cloud_map_service, backends):
    """Topology churn against the running refresh loop, and the only coverage
    `deregister_instance` has anywhere.
    """
    svc = cloud_map_service
    staying, leaving = backends("euw1-az1"), backends("euw1-az1")
    _register(sd, svc, staying, "i-staying")
    _register(sd, svc, leaving, "i-leaving")

    # a cooldown far longer than the test, so the ejection below can only be lifted by the prune
    # in Router.update — never by the clock
    cfg = _discovery_cfg(svc, breaker_cooldown=3600.0)
    with Balancer(cfg, sd_client=sd, az_id="euw1-az1") as balancer:
        if not balancer.wait_ready(timeout=SETTLE):
            pytest.skip("MiniStack never completed a discovery")
        if not until(lambda: _ports(balancer) == {staying.port, leaving.port}):
            pytest.skip("MiniStack did not surface both instances")

        # eject the host that is about to disappear, so we also prove Router.update prunes the
        # breaker entry rather than leaking it for a host that no longer exists
        balancer.report_failure(next(h for h in balancer.hosts() if h.port == leaving.port))

        deregister_instance(svc["service_id"], "i-leaving", sd_client=sd)
        assert until(lambda: _ports(balancer) == {staying.port}), (
            f"deregistered host never left the cache: {_ports(balancer)}"
        )
        assert balancer.pick().port == staying.port

        _register(sd, svc, leaving, "i-leaving")
        assert until(lambda: _ports(balancer) == {staying.port, leaving.port}), (
            "re-registered host never came back"
        )
        # the ejection must not have survived the host's absence: with a 1h cooldown, the only
        # thing that can hand `leaving` back out is Router.update having pruned its breaker entry
        assert until(lambda: {balancer.pick().port for _ in range(6)} == {staying.port, leaving.port}), (
            "breaker entry for a departed host was not pruned when it re-registered"
        )
