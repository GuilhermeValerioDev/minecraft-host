from __future__ import annotations

import pytest

from mchost.config import (
    Instance,
    default_tz,
    java_for_mc,
    load_instance,
    save_instance,
    slugify,
)
from mchost.errors import ConfigError
from mchost.paths import Layout


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("MyServer", "my-server"),
        ("FTBNeoTech", "ftb-neo-tech"),
        ("  Vanilla 1.21.1  ", "vanilla-1-21-1"),
        ("hello_world", "hello-world"),
        ("---", "server"),
    ],
)
def test_slugify(value, expected):
    assert slugify(value) == expected


@pytest.mark.parametrize(
    ("mc", "java"),
    [
        ("1.16.5", 8),
        ("1.17.1", 17),
        ("1.20.4", 17),
        ("1.20.5", 21),
        ("1.21.1", 21),
        ("1.21.11", 21),
        ("26.1.2", 25),
        ("26.3", 25),
        ("LATEST", 21),
    ],
)
def test_java_for_mc(mc, java):
    assert java_for_mc(mc) == java


def make_instance(**overrides):
    values = dict(
        name="Test Server",
        slug="test-server",
        loader="neoforge",
        mc_version="1.20.4",
        loader_version="47.1.106",
        memory="6G",
        tz=default_tz(),
        java_version=17,
        rcon_password="secret",
    )
    values.update(overrides)
    return Instance(**values)


def test_instance_roundtrip(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    instance = make_instance()
    save_instance(layout, instance)

    loaded = load_instance(layout, instance.slug)
    assert loaded.name == "Test Server"
    assert loaded.loader == "neoforge"
    assert loaded.loader_version == "47.1.106"
    assert loaded.memory == "6G"
    assert loaded.java == 17

    mode = (tmp_path / "instances" / "test-server" / "instance.toml").stat().st_mode
    assert mode & 0o077 == 0, "instance.toml contains RCON credentials"


def test_validation_rejects_bad_memory():
    with pytest.raises(ConfigError, match="memory"):
        make_instance(memory="lots").validate()


def test_validation_rejects_duplicate_ports():
    with pytest.raises(ConfigError, match="distinct"):
        make_instance(port=25565, rcon_port=25565).validate()


def test_validation_requires_eula():
    with pytest.raises(ConfigError, match="eula"):
        make_instance(eula=False).validate()


def test_validation_rejects_bad_loader():
    with pytest.raises(ConfigError, match="loader"):
        make_instance(loader="paperclip").validate()
