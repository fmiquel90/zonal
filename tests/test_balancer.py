import pytest

from zonal import Balancer, DiscoveryConfig, NoHealthyHostError


class FakeSD:
    def __init__(self, instances):
        self._instances = instances

    def discover_instances(self, **kwargs):
        return {"Instances": self._instances}


def _inst(ip, az="euw1-az1"):
    return {
        "InstanceId": f"i-{ip}",
        "Attributes": {"AWS_INSTANCE_IPV4": ip, "AWS_INSTANCE_PORT": "80", "AZID": az},
    }


def _balancer(instances):
    # az_id injected -> no IMDS; thread not started -> refresh driven manually
    bal = Balancer(
        DiscoveryConfig(namespace="n", service="s"),
        sd_client=FakeSD(instances),
        az_id="euw1-az1",
    )
    bal._refresh_once()
    return bal


def test_pick_raises_when_empty():
    bal = Balancer(DiscoveryConfig(namespace="n", service="s"), sd_client=FakeSD([]), az_id="euw1-az1")
    bal._refresh_once()
    with pytest.raises(NoHealthyHostError):
        bal.pick()


def test_lease_ejects_host_on_exception():
    bal = _balancer([_inst("10.0.0.1"), _inst("10.0.0.2")])
    with pytest.raises(RuntimeError):
        with bal.lease() as host:
            failed = host
            raise RuntimeError("boom")
    assert all(bal.pick() != failed for _ in range(4))  # ejected for the cooldown window


def test_report_success_clears_ejection():
    bal = _balancer([_inst("10.0.0.1")])
    only = bal.pick()
    bal.report_failure(only)
    bal.report_success(only)
    assert bal.pick() == only


def test_az_id_exposed():
    assert _balancer([_inst("10.0.0.1")]).az_id == "euw1-az1"
