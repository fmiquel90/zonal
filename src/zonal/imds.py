import urllib.request

_BASE = "http://169.254.169.254/latest"


def _token(ttl: int = 60) -> str:
    req = urllib.request.Request(
        f"{_BASE}/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": str(ttl)},
    )
    return urllib.request.urlopen(req, timeout=2).read().decode()


def _get(path: str, token: str) -> str:
    req = urllib.request.Request(
        f"{_BASE}/meta-data/{path}",
        headers={"X-aws-ec2-metadata-token": token},
    )
    return urllib.request.urlopen(req, timeout=2).read().decode()


def get_az_id() -> str:
    # AZ-ID (euw1-az1), not AZ-name: the name is randomized per account, the ID is stable
    # and physically consistent, which is what intra-AZ routing must key on.
    return _get("placement/availability-zone-id", _token())


def metadata() -> dict[str, str]:
    token = _token()
    return {
        "az_id": _get("placement/availability-zone-id", token),
        "az_name": _get("placement/availability-zone", token),
        "instance_id": _get("instance-id", token),
        "ipv4": _get("local-ipv4", token),
        "region": _get("placement/region", token),
    }
