from __future__ import annotations

import pytest

from mchost.compose import write_compose
from mchost.config import Instance, save_instance
from mchost.errors import MchostError
from mchost.instances import delete_instance
from mchost.paths import Layout


class FakeEngine:
    def __init__(self, state: str = "running") -> None:
        self.container_state = state
        self.calls: list[tuple] = []

    def state(self, slug: str) -> str:
        return self.container_state

    def down(self, slug: str) -> None:
        self.calls.append(("down", slug))
        self.container_state = "missing"


def make_instance(layout: Layout, slug: str = "test", **overrides) -> Instance:
    values = dict(
        name="Test",
        slug=slug,
        loader="fabric",
        mc_version="1.20.1",
        loader_version="0.15.0",
        memory="2G",
        tz="UTC",
        java_version=17,
        rcon_password="secret",
    )
    values.update(overrides)
    instance = Instance(**values)
    save_instance(layout, instance)
    write_compose(layout, instance)
    layout.server_dir_for(instance).mkdir(parents=True, exist_ok=True)
    (layout.server_dir_for(instance) / "server.properties").write_text(
        "motd=hi\n", encoding="utf-8"
    )
    layout.backups_for(slug).mkdir(parents=True, exist_ok=True)
    (layout.backups_for(slug) / "manifest.json").write_text("{}", encoding="utf-8")
    return instance


def test_delete_instance_removes_all_files(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    instance = make_instance(layout)
    engine = FakeEngine()

    removed = delete_instance(layout, instance, engine=engine)

    assert ("down", "test") in engine.calls
    assert not layout.instance_dir("test").exists()
    assert not layout.server_dir("test").exists()
    assert not layout.backups_for("test").exists()
    assert removed


def test_delete_instance_keeps_other_instances(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    first = make_instance(layout, "first")
    second = make_instance(layout, "second")

    delete_instance(layout, first, engine=FakeEngine("missing"))

    assert layout.config_file("second").is_file()
    assert layout.server_dir("second").is_dir()


def test_delete_legacy_external_data_dir(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    legacy = layout.instances_dir / "Legacy" / "server"
    legacy.mkdir(parents=True)
    (legacy / "level.dat").write_text("x", encoding="utf-8")
    instance = Instance(
        name="Legacy",
        slug="legacy",
        loader="fabric",
        mc_version="1.20.1",
        rcon_password="secret",
        data_dir="instances/Legacy/server",
    )
    save_instance(layout, instance)
    write_compose(layout, instance)

    delete_instance(layout, instance, engine=FakeEngine("missing"))

    assert not legacy.exists()
    assert not (layout.instances_dir / "Legacy").exists()
    assert layout.instances_dir.is_dir()


def test_delete_refuses_shared_data_dir(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    first = make_instance(layout, "first", data_dir="instances/shared/server")
    second = make_instance(layout, "second", data_dir="instances/shared/server")

    with pytest.raises(MchostError, match="shared"):
        delete_instance(layout, first, engine=FakeEngine("missing"))
    assert layout.config_file("second").is_file()


def test_delete_refuses_host_root(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    instance = make_instance(layout)
    instance.data_dir = "."
    with pytest.raises(MchostError):
        delete_instance(layout, instance)
