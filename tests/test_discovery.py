from zonal.config import DiscoveryConfig
from zonal.discovery import discover_kwargs, parse_instances, select_hosts
from zonal.model import Host


def _cfg(**kw):
    return DiscoveryConfig(namespace="ns", service="svc", **kw)


def test_parse_instances_maps_attributes():
    resp = {
        "Instances": [
            {
                "InstanceId": "i-1",
                "Attributes": {
                    "AWS_INSTANCE_IPV4": "10.0.0.1",
                    "AWS_INSTANCE_PORT": "8080",
                    "AZID": "euw1-az1",
                },
            }
        ]
    }
    assert parse_instances(resp, _cfg()) == [Host("10.0.0.1", 8080, "euw1-az1", "i-1")]


def test_parse_instances_skips_incomplete():
    resp = {"Instances": [{"InstanceId": "i-1", "Attributes": {"AWS_INSTANCE_IPV4": "10.0.0.1"}}]}
    assert parse_instances(resp, _cfg()) == []


def test_discover_kwargs_uses_optional_param_for_az_affinity():
    kw = discover_kwargs(_cfg(), "euw1-az1")
    assert kw["HealthStatus"] == "HEALTHY"
    assert kw["OptionalParameters"] == {"AZID": "euw1-az1"}


def test_discover_kwargs_omits_az_when_disabled():
    assert "OptionalParameters" not in discover_kwargs(_cfg(prefer_same_az=False), "euw1-az1")


_A = Host("10.0.0.1", 80, "euw1-az1")
_B = Host("10.0.0.2", 80, "euw1-az1")
_C = Host("10.0.0.3", 80, "euw1-az2")


def test_select_prefers_same_az():
    assert select_hosts([_A, _B, _C], "euw1-az1", True) == ([_A, _B], False)


def test_select_falls_back_to_all_when_no_same_az():
    assert select_hosts([_C], "euw1-az1", True) == ([_C], True)


def test_select_no_affinity_returns_all():
    assert select_hosts([_A, _C], "euw1-az1", False) == ([_A, _C], False)
