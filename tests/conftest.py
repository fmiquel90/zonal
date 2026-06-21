import os
import uuid

import boto3
import pytest
from botocore.config import Config

ENDPOINT = os.environ.get("MINISTACK_ENDPOINT", "http://localhost:4566")
REGION = "eu-west-1"


@pytest.fixture(scope="session")
def endpoint():
    return ENDPOINT


@pytest.fixture(scope="session", autouse=True)
def _dummy_aws_credentials():
    # MiniStack accepts any credentials; the lib still needs them present to build a boto client.
    for key, value in {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        os.environ.setdefault(key, value)


@pytest.fixture(scope="session")
def sd(endpoint):
    client = boto3.client(
        "servicediscovery",
        endpoint_url=endpoint,
        region_name=REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(
            connect_timeout=1,
            read_timeout=2,
            retries={"max_attempts": 0},
            inject_host_prefix=False,  # DiscoverInstances data-plane prefix breaks custom endpoints
        ),
    )
    try:
        client.list_namespaces()
    except Exception:
        pytest.skip(f"MiniStack not reachable at {endpoint} (run: docker run -p 4566:4566 ministackorg/ministack)")
    return client


def _await_namespace(sd, resp):
    if "NamespaceId" in resp:  # emulator returns the id directly
        return resp["NamespaceId"]
    op_id = resp["OperationId"]
    for _ in range(50):
        op = sd.get_operation(OperationId=op_id)["Operation"]
        if op["Status"] == "SUCCESS":
            return op["Targets"]["NAMESPACE"]
        if op["Status"] == "FAIL":
            raise RuntimeError(op.get("ErrorMessage", "namespace creation failed"))
    raise TimeoutError("namespace creation timed out")


@pytest.fixture
def cloud_map_service(sd):
    """Create a throwaway private DNS namespace + service with custom health checks."""
    suffix = uuid.uuid4().hex[:8]
    ns_name = f"zonal-test-{suffix}.local"
    svc_name = f"svc-{suffix}"
    ns_id = _await_namespace(sd, sd.create_private_dns_namespace(Name=ns_name, Vpc="vpc-test"))
    svc = sd.create_service(
        Name=svc_name,
        NamespaceId=ns_id,
        DnsConfig={"DnsRecords": [{"Type": "A", "TTL": 10}]},
        HealthCheckCustomConfig={"FailureThreshold": 1},
    )
    service_id = svc["Service"]["Id"]
    yield {"namespace": ns_name, "service": svc_name, "service_id": service_id, "namespace_id": ns_id}
    try:
        for inst in sd.discover_instances(NamespaceName=ns_name, ServiceName=svc_name, HealthStatus="ALL").get("Instances", []):
            sd.deregister_instance(ServiceId=service_id, InstanceId=inst["InstanceId"])
        sd.delete_service(Id=service_id)
        sd.delete_namespace(Id=ns_id)
    except Exception:
        pass  # best-effort teardown
