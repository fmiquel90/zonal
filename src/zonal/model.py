from dataclasses import dataclass


class ZonalError(Exception):
    """Base class for all zonal errors."""


class NoHealthyHostError(ZonalError):
    """Raised when the discovery cache holds no host to route to."""


class ServiceNotFoundError(ZonalError, ValueError):
    """The Cloud Map namespace or service does not exist.

    A misconfiguration rather than a transient failure, so the health daemon exits on it instead of
    retrying forever. Also a ValueError, which resolve_service_id raised before this existed.
    """


@dataclass(frozen=True, slots=True)
class Host:
    ip: str
    port: int
    az: str | None = None
    instance_id: str | None = None

    def url(self, path: str = "", *, scheme: str = "http") -> str:
        return f"{scheme}://{self.ip}:{self.port}{path}"
