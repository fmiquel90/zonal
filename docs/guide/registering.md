# Registering a host

Each target host self-registers in Cloud Map at boot, advertising its IP, port, and **AZ-ID**. The
AZ-ID is what callers key on for affinity.

```python
from zonal import RegisterConfig, register_instance

register_instance(RegisterConfig(service_id="srv-xxxx", port=8080, region="eu-west-1"))
```

`register_instance`:

1. reads the instance's IP, instance-id, and AZ-ID from IMDS (or accepts an explicit `metadata`
   dict — used in tests and the demo);
2. writes them to Cloud Map as `AWS_INSTANCE_IPV4`, `AWS_INSTANCE_PORT`, and the AZ attribute
   (`AZID` by default), plus any `extra_attributes`;
3. pushes an initial `HEALTHY` custom status so the host serves immediately, then lets the
   [health service](health-service.md) own its status from there.

It returns the registered instance id.

!!! warning "Custom health checks required"
    The Cloud Map service must be created with `HealthCheckCustomConfig`. If it uses Route 53 health
    checks instead, the initial `HEALTHY` push is skipped (`CustomHealthNotFound` is swallowed) — but
    Route 53 checks can't reach private hosts, so custom health is the intended setup.

## Deregistering

On graceful shutdown, remove the instance so callers stop discovering it:

```python
from zonal import deregister_instance

deregister_instance(service_id="srv-xxxx", instance_id="i-0abc...", region="eu-west-1")
```

See [configuration](../configuration.md#registerconfig-target-host) for all `RegisterConfig` fields
and [required IAM](../operations.md#required-iam).
