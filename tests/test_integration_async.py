"""AsyncBalancer against MiniStack: the async refresh loop over real aioboto3.

The sync client is covered by test_integration_ministack.py; this pins the async path, which
shares BalancerBase with it but has its own transport, event loop and shutdown.
"""
import asyncio

import pytest

from zonal import DiscoveryConfig, RegisterConfig, register_instance
from tests.conftest import ENDPOINT, REGION

pytest.importorskip("aioboto3", reason="async client needs the [aio] extra")
from zonal import AsyncBalancer  # noqa: E402  (after importorskip)

pytestmark = pytest.mark.integration


def _register(sd, service_id, port, az, instance_id):
    register_instance(
        RegisterConfig(service_id=service_id, port=port, region=REGION),
        sd_client=sd,
        metadata={"ipv4": "10.0.0.1", "instance_id": instance_id, "az_id": az,
                  "az_name": az, "region": REGION},
    )


async def _settled(balancer, predicate, timeout=10.0):
    """Wait for the refresh loop to reach a steady state, not merely to become ready."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate(balancer.hosts()):
            return True
        await asyncio.sleep(0.2)
    return False


def test_async_balancer_discovers_and_prefers_same_az(sd, cloud_map_service):
    svc = cloud_map_service
    _register(sd, svc["service_id"], 8080, "euw1-az1", "i-async-az1")
    _register(sd, svc["service_id"], 8081, "euw1-az2", "i-async-az2")

    cfg = DiscoveryConfig(
        namespace=svc["namespace"], service=svc["service"], region=REGION,
        endpoint_url=ENDPOINT, refresh_interval=0.3, breaker_cooldown=30.0,
    )

    async def scenario():
        async with AsyncBalancer(cfg, az_id="euw1-az1") as balancer:
            if not await balancer.wait_ready(timeout=10):
                pytest.skip("MiniStack never completed a discovery")
            if not await _settled(balancer, lambda h: bool(h) and all(x.az == "euw1-az1" for x in h)):
                pytest.skip("MiniStack returned no HEALTHY az1 instance (health/AZ filter fidelity)")

            hosts = balancer.hosts()
            assert [h.port for h in hosts] == [8080]
            assert balancer.az_id == "euw1-az1"

            # breaker: the only host is ejected, but pick() still hands it back rather than
            # refusing traffic, and report_success clears the ejection.
            host = balancer.pick()
            balancer.report_failure(host)
            assert balancer.pick() == host
            balancer.report_success(host)

            # lease() must eject on a raised exception and re-raise it
            with pytest.raises(RuntimeError):
                async with balancer.lease() as leased:
                    assert leased.az == "euw1-az1"
                    raise RuntimeError("boom")

    asyncio.run(scenario())


def test_async_balancer_falls_back_cross_az(sd, cloud_map_service):
    svc = cloud_map_service
    _register(sd, svc["service_id"], 8081, "euw1-az2", "i-async-only-az2")

    cfg = DiscoveryConfig(
        namespace=svc["namespace"], service=svc["service"], region=REGION,
        endpoint_url=ENDPOINT, refresh_interval=0.3,
    )

    async def scenario():
        # no host in az1 at all -> the effective set is every healthy host, cross-AZ
        async with AsyncBalancer(cfg, az_id="euw1-az1") as balancer:
            if not await balancer.wait_ready(timeout=10):
                pytest.skip("MiniStack never completed a discovery")
            if not await _settled(balancer, bool):
                pytest.skip("MiniStack returned no HEALTHY instances")
            assert all(h.az == "euw1-az2" for h in balancer.hosts())
            assert balancer.pick().az == "euw1-az2"

    asyncio.run(scenario())
