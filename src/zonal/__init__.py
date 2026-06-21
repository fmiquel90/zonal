from . import imds
from .balancer import Balancer
from .config import DiscoveryConfig, HealthConfig, RegisterConfig
from .health import HealthChecker, resolve_service_id
from .log import configure_json_logging, get_logger
from .model import ZonalError, Host, NoHealthyHostError
from .register import deregister_instance, register_instance
from .routing import Router

__all__ = [
    "imds",
    "Balancer",
    "DiscoveryConfig",
    "HealthConfig",
    "RegisterConfig",
    "HealthChecker",
    "resolve_service_id",
    "configure_json_logging",
    "get_logger",
    "ZonalError",
    "Host",
    "NoHealthyHostError",
    "register_instance",
    "deregister_instance",
    "Router",
]


def __getattr__(name: str):
    # AsyncBalancer is resolved lazily (and kept out of __all__) so importing zonal — or a
    # `from zonal import *` — does not require the [aio] extra (aioboto3).
    if name == "AsyncBalancer":
        from .aio_balancer import AsyncBalancer

        return AsyncBalancer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
