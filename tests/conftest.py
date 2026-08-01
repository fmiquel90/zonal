import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

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
        time.sleep(0.2)  # else the 50 attempts burn through in milliseconds and never wait
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


class _BackendHandler(BaseHTTPRequestHandler):
    """Defined once at module scope; its backend is reached through `self.server`."""

    def do_GET(self):  # /health
        self.send_response(200 if self.server.backend.healthy else 503)
        self.end_headers()

    def do_POST(self):  # /work
        backend = self.server.backend
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f"served by {backend.az}:{backend.port}".encode())

    def log_message(self, *_):
        pass


class FakeBackend:
    """A real local HTTP server standing in for a target host. `healthy` toggles /health."""

    def __init__(self, az: str = "euw1-az1"):
        self.az = az
        self.healthy = True
        self._server = HTTPServer(("127.0.0.1", 0), _BackendHandler)
        self._server.backend = self
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()


@pytest.fixture
def backends():
    """Factory for real backend servers, all stopped at teardown."""
    started = []

    def make(az="euw1-az1"):
        b = FakeBackend(az)
        started.append(b)
        return b

    yield make
    for b in started:
        b.stop()


class BreakableEndpoint:
    """A TCP forwarder in front of Cloud Map that can be cut on demand.

    Lets a test make discovery fail for real — a socket error raised out of botocore — without
    touching the emulator other tests share.
    """

    def __init__(self, target: str):
        parsed = urlparse(target)
        self._target = (parsed.hostname, parsed.port or 80)
        self._live: list[socket.socket] = []
        self._lock = threading.Lock()
        self.up = True
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self.url = f"http://127.0.0.1:{self._srv.getsockname()[1]}"
        self._stopped = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stopped:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            if not self.up:
                conn.close()
                continue
            threading.Thread(target=self._pipe, args=(conn,), daemon=True).start()

    def _pipe(self, conn):
        try:
            upstream = socket.create_connection(self._target, timeout=5)
        except OSError:
            conn.close()
            return
        with self._lock:
            self._live += [conn, upstream]
        for src, dst in ((conn, upstream), (upstream, conn)):
            threading.Thread(target=self._copy, args=(src, dst), daemon=True).start()

    @staticmethod
    def _copy(src, dst):
        try:
            while chunk := src.recv(65536):
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass

    def cut(self):
        """Refuse new connections and drop the ones already open (including keep-alive)."""
        self.up = False
        with self._lock:
            live, self._live = self._live, []
        for s in live:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def heal(self):
        self.up = True

    def stop(self):
        self._stopped = True
        self.cut()
        try:
            self._srv.close()
        except OSError:
            pass


@pytest.fixture
def breakable_endpoint(sd):
    """A Cloud Map endpoint that a test can cut mid-flight. Depends on `sd` so it inherits the
    skip when the emulator is not running."""
    fwd = BreakableEndpoint(ENDPOINT)
    yield fwd
    fwd.stop()
