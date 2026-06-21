"""End-to-end local demo of zonal against MiniStack.

Spins up fake backend HTTP servers in two fake AZs, registers them in Cloud Map, runs the
health-check daemon, and drives a Balancer pinned to az1 — showing AZ affinity, a real request
through lease(), and the cross-AZ fallback when az1 goes unhealthy.

Run:
    docker run -d -p 4566:4566 ministackorg/ministack
    python examples/local_demo.py
"""

import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
import requests
from botocore.config import Config

from zonal import (
    Balancer,
    DiscoveryConfig,
    HealthChecker,
    HealthConfig,
    RegisterConfig,
    configure_json_logging,
    register_instance,
)

ENDPOINT = "http://localhost:4566"
REGION = "eu-west-1"


class FakeBackend:
    """A local HTTP server standing in for a backend host. `healthy` toggles /health."""

    def __init__(self, az: str):
        self.az = az
        self.healthy = True
        demo = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # /health
                self.send_response(200 if demo.healthy else 503)
                self.end_headers()

            def do_POST(self):  # /work
                self.send_response(200)
                self.end_headers()
                self.wfile.write(f"served by {demo.az}:{demo.port}".encode())

            def log_message(self, *_):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()


def sd_client():
    return boto3.client(
        "servicediscovery",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        # DiscoverInstances is a data-plane call; the "data-" host prefix breaks custom endpoints
        config=Config(inject_host_prefix=False, retries={"max_attempts": 2}),
    )


def create_service(sd):
    suffix = uuid.uuid4().hex[:8]
    ns_name, svc_name = f"demo-{suffix}.local", "backend"
    resp = sd.create_private_dns_namespace(Name=ns_name, Vpc="vpc-demo")
    ns_id = resp.get("NamespaceId")
    if ns_id is None:  # real-AWS-style async create: poll the operation
        for _ in range(50):
            op = sd.get_operation(OperationId=resp["OperationId"])["Operation"]
            if op["Status"] == "SUCCESS":
                ns_id = op["Targets"]["NAMESPACE"]
                break
            time.sleep(0.2)
    svc = sd.create_service(
        Name=svc_name,
        NamespaceId=ns_id,
        DnsConfig={"DnsRecords": [{"Type": "A", "TTL": 10}]},
        HealthCheckCustomConfig={"FailureThreshold": 1},
    )
    return ns_name, svc_name, svc["Service"]["Id"]


def picks(balancer, n=8):
    counts: dict[str, int] = {}
    for _ in range(n):
        h = balancer.pick()
        counts[f"{h.az}:{h.port}"] = counts.get(f"{h.az}:{h.port}", 0) + 1
    return counts


def wait_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def main():
    configure_json_logging()
    sd = sd_client()
    print("→ creating Cloud Map namespace + service")
    namespace, service, service_id = create_service(sd)

    # two hosts in az1, one in az2
    hosts = {
        "euw1-az1": [FakeBackend("euw1-az1"), FakeBackend("euw1-az1")],
        "euw1-az2": [FakeBackend("euw1-az2")],
    }
    for az, servers in hosts.items():
        for s in servers:
            register_instance(
                RegisterConfig(service_id=service_id, port=s.port, region=REGION),
                sd_client=sd,
                metadata={"ipv4": "127.0.0.1", "instance_id": f"i-{az}-{s.port}", "az_id": az,
                          "az_name": az, "region": REGION},
            )

    print("→ starting health-check daemon")
    checker = HealthChecker(
        HealthConfig(namespace=namespace, service=service, service_id=service_id, region=REGION,
                     interval=1.0, timeout=1.0, healthy_threshold=1, unhealthy_threshold=2),
        sd_client=sd,
    )
    threading.Thread(target=checker.run, daemon=True).start()

    print("→ starting balancer pinned to euw1-az1")
    balancer = Balancer(
        DiscoveryConfig(namespace=namespace, service=service, region=REGION, endpoint_url=ENDPOINT,
                        refresh_interval=1.0, breaker_cooldown=2.0),
        sd_client=sd,
        az_id="euw1-az1",
    ).start()

    assert balancer.wait_ready(timeout=15), "balancer never became ready"
    assert wait_until(lambda: all(h.az == "euw1-az1" for h in balancer.hosts())), "az1 hosts not isolated"

    print("\n[1] same-AZ affinity — all picks should be euw1-az1:")
    print("   ", picks(balancer))

    print("\n[2] real request through lease():")
    with balancer.lease() as host:
        body = requests.post(host.url("/work"), timeout=2).text
    print("    response:", body)

    print("\n[3] killing both euw1-az1 hosts -> expect fallback to euw1-az2 (watch cross_az_fallback log)")
    for s in hosts["euw1-az1"]:
        s.healthy = False
    assert wait_until(lambda: all(h.az == "euw1-az2" for h in balancer.hosts())), "fallback never happened"
    print("    picks after failure:", picks(balancer))

    print("\n→ cleanup")
    balancer.close()
    checker.stop()
    for servers in hosts.values():
        for s in servers:
            s.stop()
    for inst in sd.discover_instances(NamespaceName=namespace, ServiceName=service, HealthStatus="ALL").get("Instances", []):
        sd.deregister_instance(ServiceId=service_id, InstanceId=inst["InstanceId"])
    print("done.")


if __name__ == "__main__":
    main()
