from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mchost.engine import Engine, EngineInfo
from mchost.paths import Layout


def make_engine(tmp_path: Path) -> Engine:
    layout = Layout(tmp_path)
    layout.ensure()
    return Engine(layout, EngineInfo("docker", "docker", ("docker", "compose")))


def test_compose_command_uses_project_and_file(tmp_path):
    engine = make_engine(tmp_path)
    command = engine.compose_command("my-server", "up", "-d")
    assert command == [
        "docker",
        "compose",
        "-p",
        "mc-my-server",
        "-f",
        str(tmp_path / "instances" / "my-server" / "compose.yaml"),
        "up",
        "-d",
    ]


def test_state_parses_docker_json_lines(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    payload = json.dumps({"Names": "mc-my-server", "State": "running"}) + "\n"
    monkeypatch.setattr(
        engine,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, payload, ""),
    )
    assert engine.state("my-server") == "running"
    assert engine.is_running("my-server") is True


def test_state_parses_podman_json_array(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    payload = json.dumps([{"Names": ["mc-my-server"], "State": "exited"}])
    monkeypatch.setattr(
        engine,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, payload, ""),
    )
    assert engine.state("my-server") == "exited"
    assert engine.is_running("my-server") is False


def test_restarting_container_counts_as_active(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    payload = json.dumps({"Names": "mc-my-server", "State": "restarting"})
    monkeypatch.setattr(
        engine,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, payload, ""),
    )
    assert engine.state("my-server") == "restarting"
    assert engine.is_running("my-server") is True


def test_state_missing(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    monkeypatch.setattr(
        engine,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    assert engine.state("my-server") == "missing"


def test_started_at_parses_docker_timestamp(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    engine = make_engine(tmp_path)
    payload = "2026-01-01T12:00:00.123456789Z\n"
    monkeypatch.setattr(
        engine,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, payload, ""),
    )
    value = engine.started_at("my-server")
    assert value == datetime(
        2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc
    )


def test_started_at_missing_or_invalid(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    for payload, returncode in (
        ("", 0),
        ("0001-01-01T00:00:00Z\n", 0),
        ("not-a-date\n", 0),
        ("2026-01-01T12:00:00Z\n", 1),
    ):
        monkeypatch.setattr(
            engine,
            "_run",
            lambda *args, _p=payload, _r=returncode, **kwargs: subprocess.CompletedProcess(
                [], _r, _p, ""
            ),
        )
        assert engine.started_at("my-server") is None
