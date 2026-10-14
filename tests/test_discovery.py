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


def test_retries_default_to_zonal_bound_and_defer_to_the_operator(monkeypatch):
    from zonal import RegisterConfig
    from zonal.discovery import client_config

    for var in ("AWS_MAX_ATTEMPTS", "AWS_RETRY_MODE", "AWS_PROFILE", "AWS_CONFIG_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    caller = DiscoveryConfig(namespace="n", service="s")
    assert client_config(caller).retries == {"total_max_attempts": 2, "mode": "standard"}
    # registration has no loop behind it to retry a boot-time throttle
    reg = RegisterConfig(service_id="srv", port=80)
    assert client_config(reg).retries["total_max_attempts"] == 5

    monkeypatch.setenv("AWS_RETRY_MODE", "adaptive")
    assert client_config(caller).retries is None, "overrode the operator's AWS_RETRY_MODE"
    # an explicit config value still wins over the environment
    explicit = DiscoveryConfig(namespace="n", service="s", max_attempts=4)
    assert client_config(explicit).retries["total_max_attempts"] == 4
