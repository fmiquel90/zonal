import boto3

from . import imds
from .config import RegisterConfig
from .log import get_logger

log = get_logger("zonal.register")


def register_instance(config: RegisterConfig, *, sd_client=None, metadata: dict | None = None) -> str:
    """Self-register the current EC2 instance in Cloud Map with its AZ-ID attribute.

    Call this at boot on each target host. Returns the registered instance id.
    """
    sd = sd_client or boto3.client("servicediscovery", region_name=config.region)
    md = metadata or imds.metadata()
    attrs = {
        "AWS_INSTANCE_IPV4": md["ipv4"],
        "AWS_INSTANCE_PORT": str(config.port),
        config.az_attribute: md["az_id"],
        **config.extra_attributes,
    }
    sd.register_instance(
        ServiceId=config.service_id,
        InstanceId=md["instance_id"],
        Attributes=attrs,
    )
    # With custom health checks an instance is not routable until a status is pushed; mark it
    # HEALTHY on boot so it serves immediately, then let the health service own it from there.
    try:
        sd.update_instance_custom_health_status(
            ServiceId=config.service_id, InstanceId=md["instance_id"], Status="HEALTHY"
        )
    except sd.exceptions.CustomHealthNotFound:
        pass  # service uses Route 53 health checks instead of custom ones
    log.info(
        "registered",
        service_id=config.service_id,
        instance_id=md["instance_id"],
        az=md["az_id"],
        ip=md["ipv4"],
        port=config.port,
    )
    return md["instance_id"]


def deregister_instance(service_id: str, instance_id: str, *, sd_client=None, region: str | None = None) -> None:
    sd = sd_client or boto3.client("servicediscovery", region_name=region)
    sd.deregister_instance(ServiceId=service_id, InstanceId=instance_id)
    log.info("deregistered", service_id=service_id, instance_id=instance_id)
