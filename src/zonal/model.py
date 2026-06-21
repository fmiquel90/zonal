from dataclasses import dataclass


class ZonalError(Exception):
    """Base class for all zonal errors."""


class NoHealthyHostError(ZonalError):
    """Raised when the discovery cache holds no host to route to."""


@dataclass(frozen=True, slots=True)
class Host:
    ip: str
    port: int
    az: str | None = None
    instance_id: str | None = None

    def url(self, path: str = "", *, scheme: str = "http") -> str:
        return f"{scheme}://{self.ip}:{self.port}{path}"
