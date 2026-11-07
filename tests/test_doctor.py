from __future__ import annotations

from mchost import doctor
from mchost.engine import EngineInfo
from mchost.paths import Layout


def test_parse_memory():
    assert doctor.parse_memory("8G") == 8 * 1024**3
    assert doctor.parse_memory("2048M") == 2048 * 1024**2
    assert doctor.parse_memory("nope") == 0


def test_checks_on_empty_host(tmp_path, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "detect_engine",
        lambda: EngineInfo("docker", "docker", ("docker", "compose")),
    )
    monkeypatch.setattr("mchost.doctor.Engine.ping", lambda self: True)
    layout = Layout(tmp_path)
    layout.ensure()
    checks = doctor.run_checks(layout)
    assert any(check.status == doctor.OK and check.title == "Host directory" for check in checks)
    assert any(check.status == doctor.OK and check.title == "Instances" for check in checks)
    assert not any(check.status == doctor.FAIL for check in checks)
