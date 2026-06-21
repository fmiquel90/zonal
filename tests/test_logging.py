import json

from zonal.log import configure_json_logging, get_logger


def test_configure_json_logging_emits_json(capsys):
    configure_json_logging()
    get_logger("zonal.test").bind(service="backend").info("discovery_ready", host_count=3)
    line = capsys.readouterr().out.strip()
    record = json.loads(line)
    assert record["message"] == "discovery_ready"
    assert record["host_count"] == 3
    assert record["service"] == "backend"
    assert record["level"] == "info"
    assert "timestamp" in record
    assert "env" in record


def test_required_fields_present_without_binding(capsys, monkeypatch):
    monkeypatch.setenv("DD_SERVICE", "zonal-healthcheck")
    monkeypatch.setenv("DD_ENV", "staging")
    # SERVICE/ENV are resolved at import; reload so the env override takes effect.
    import importlib

    import zonal.log as log_mod

    importlib.reload(log_mod)
    log_mod.configure_json_logging()
    log_mod.get_logger("zonal.test").info("health_service_started", interval=10.0)
    record = json.loads(capsys.readouterr().out.strip())
    assert record["service"] == "zonal-healthcheck"
    assert record["env"] == "staging"
    assert record["message"] == "health_service_started"
    assert "timestamp" in record
