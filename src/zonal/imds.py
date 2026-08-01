import urllib.request
from functools import lru_cache

from .model import ZonalError

BASE_URL = "http://169.254.169.254/latest"
DEFAULT_TIMEOUT = 2.0


class ImdsError(ZonalError):
    """The instance metadata service could not be reached, or did not answer in time.

    Raised instead of the raw urllib error so callers can catch ZonalError. Off EC2 — ECS, EKS,
    a laptop — don't reach for IMDS at all: pass `az_id=` to a balancer, or `metadata=` to
    register_instance.
    """


def _fetch(url: str, timeout: float, *, method: str = "GET", headers: dict | None = None) -> str:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=timeout).read().decode()
    except OSError as exc:  # URLError, socket.timeout and friends all derive from OSError
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


@lru_cache(maxsize=1)
def get_az_id(timeout: float = DEFAULT_TIMEOUT) -> str:
    """The caller's AZ-ID, over IMDSv2. Raises ImdsError if the metadata service is unreachable.

    AZ-ID (euw1-az1), not AZ-name: the name is randomized per account, the ID is stable and
    physically consistent, which is what intra-AZ routing must key on.

    Immutable for the instance's lifetime, so cached: an app with one balancer per downstream
    service would otherwise pay two blocking round trips per balancer at startup. lru_cache does
    not cache exceptions, so a failed lookup is still retried.
    """
    return _get("placement/availability-zone-id", _token(timeout), timeout)


@lru_cache(maxsize=1)
def metadata(timeout: float = DEFAULT_TIMEOUT) -> dict[str, str]:
    """Everything register_instance needs, over one IMDSv2 token.

    Cached for the same reason as get_az_id, and so the two never disagree about az_id. The dict
    is shared between callers — treat it as read-only.
    """
    token = _token(timeout)
    return {
        "az_id": _get("placement/availability-zone-id", token, timeout),
        "az_name": _get("placement/availability-zone", token, timeout),
        "instance_id": _get("instance-id", token, timeout),
        "ipv4": _get("local-ipv4", token, timeout),
        "region": _get("placement/region", token, timeout),
    }
