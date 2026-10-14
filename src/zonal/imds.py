import http.client
import urllib.request

from .config import DEFAULT_IMDS_TIMEOUT
from .model import ZonalError

BASE_URL = "http://169.254.169.254/latest"
DEFAULT_TIMEOUT = DEFAULT_IMDS_TIMEOUT


class ImdsError(ZonalError):
    """The instance metadata service could not be reached, or did not answer in time.

    Raised instead of the raw urllib error so callers can catch ZonalError. Off EC2 — ECS, EKS,
    a laptop — don't reach for IMDS at all: pass `az_id=` to a balancer, or `metadata=` to
    register_instance.
    """


def _fetch(url: str, timeout: float, *, method: str = "GET", headers: dict | None = None) -> str:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    # URLError and socket.timeout derive from OSError; a connection dropped mid-body raises an
    # HTTPException (IncompleteRead, LineTooLong...), which does not.
    except (OSError, http.client.HTTPException) as exc:
        raise ImdsError(f"IMDS request to {url} failed: {exc}") from exc


def _token(timeout: float = DEFAULT_TIMEOUT, ttl: int = 60) -> str:
    return _fetch(
        f"{BASE_URL}/api/token",
        timeout,
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": str(ttl)},
    )


def _get(path: str, token: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    return _fetch(
        f"{BASE_URL}/meta-data/{path}", timeout, headers={"X-aws-ec2-metadata-token": token}
    )


# Process-wide, keyed by name only: lru_cache would key on timeout too, so two callers with
# different timeouts (or get_az_id() vs get_az_id(2.0)) would keep evicting each other. A failed
# lookup is never stored, so it is retried on the next call.
_cache: dict = {}


def get_az_id(timeout: float = DEFAULT_TIMEOUT) -> str:
    """The caller's AZ-ID, over IMDSv2. Raises ImdsError if the metadata service is unreachable.

    AZ-ID (euw1-az1), not AZ-name: the name is randomized per account, the ID is stable and
    physically consistent, which is what intra-AZ routing must key on.

    Immutable for the instance's lifetime, so cached: an app with one balancer per downstream
    service would otherwise pay two blocking round trips per balancer at startup.
    """
    if "az_id" not in _cache:
        _cache["az_id"] = _get("placement/availability-zone-id", _token(timeout), timeout)
    return _cache["az_id"]


def metadata(timeout: float = DEFAULT_TIMEOUT) -> dict[str, str]:
    """Everything register_instance needs, over one IMDSv2 token.

    Cached for the same reason as get_az_id, and so the two never disagree about az_id. The dict
    is shared between callers — treat it as read-only.
    """
    if "metadata" not in _cache:
        token = _token(timeout)
        md = {
            "az_id": _cache.get("az_id") or _get("placement/availability-zone-id", token, timeout),
            "az_name": _get("placement/availability-zone", token, timeout),
            "instance_id": _get("instance-id", token, timeout),
            "ipv4": _get("local-ipv4", token, timeout),
            "region": _get("placement/region", token, timeout),
        }
        _cache.setdefault("az_id", md["az_id"])
        _cache["metadata"] = md
    return _cache["metadata"]
