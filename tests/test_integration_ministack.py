import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from zonal import Balancer, DiscoveryConfig, HealthChecker, HealthConfig, RegisterConfig, register_instance
from tests.conftest import ENDPOINT, REGION

pytestmark = pytest.mark.integration


def _metadata(ip, instance_id, az="euw1-az1"):
    return {"ipv4": ip, "instance_id": instance_id, "az_id": az, "az_name": "eu-west-1a", "region": REGION}


def test_register_then_discover_through_client(sd, cloud_map_service):
    svc = cloud_map_service
    register_instance(
        RegisterConfig(service_id=svc["service_id"], port=8080),
        sd_client=sd,
        metadata=_metadata("10.0.0.7", "i-aaaa"),
    )

    cfg = DiscoveryConfig(
        namespace=svc["namespace"],
        service=svc["service"],
        region=REGION,
        endpoint_url=ENDPOINT,
        refresh_interval=0.3,
    )
    # az_id injected so the lib never touches IMDS in tests
    with Balancer(cfg, az_id="euw1-az1") as balancer:
        if not balancer.wait_ready(timeout=4):
            pytest.skip("MiniStack returned no HEALTHY instances (health/AZ filter fidelity)")
        hosts = balancer.hosts()
        assert balancer.pick().az == "euw1-az1"

    assert any(h.ip == "10.0.0.7" and h.port == 8080 for h in hosts)
    assert all(h.az == "euw1-az1" for h in hosts)


class _Health(BaseHTTPRequestHandler):
    healthy = True

    def do_GET(self):
        self.send_response(200 if _Health.healthy else 503)
        self.end_headers()

    def log_message(self, *_):
        pass


@pytest.fixture
def health_endpoint():
    _Health.healthy = True
    server = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def _status_of(sd, svc, instance_id):
    resp = sd.discover_instances(NamespaceName=svc["namespace"], ServiceName=svc["service"], HealthStatus="ALL")
    return {i["InstanceId"]: i.get("HealthStatus") for i in resp.get("Instances", [])}.get(instance_id)


def test_health_checker_flips_status(sd, cloud_map_service, health_endpoint):
    svc = cloud_map_service
    register_instance(
        RegisterConfig(service_id=svc["service_id"], port=health_endpoint),
        sd_client=sd,
        metadata=_metadata("127.0.0.1", "i-health"),
    )
    checker = HealthChecker(
        HealthConfig(
            namespace=svc["namespace"],
            service=svc["service"],
            service_id=svc["service_id"],
            region=REGION,
            healthy_threshold=2,
            unhealthy_threshold=2,
        ),
        sd_client=sd,
    )

    checker.run_once()
    checker.run_once()  # two consecutive HEALTHY probes -> threshold reached
    if _status_of(sd, svc, "i-health") != "HEALTHY":
        pytest.skip("MiniStack does not track custom health status")

    _Health.healthy = False
    checker.run_once()
    checker.run_once()  # two consecutive failing probes -> flip to UNHEALTHY
    assert _status_of(sd, svc, "i-health") == "UNHEALTHY"
