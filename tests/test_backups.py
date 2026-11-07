from __future__ import annotations

import json
import tarfile

import pytest

from mchost import backups
from mchost.backups import (
    create_backup,
    delete_backup,
    get_backup,
    human_size,
    list_backups,
    prune_backups,
    restore_backup,
)
from mchost.compose import write_compose
from mchost.config import Instance, save_instance
from mchost.errors import MchostError
from mchost.paths import Layout


class FakeEngine:
    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.calls: list[tuple] = []

    def is_running(self, slug: str) -> bool:
        return self.running

    def down(self, slug: str) -> None:
        self.calls.append(("down", slug))
        self.running = False

    def up(self, slug: str) -> None:
        self.calls.append(("up", slug))
        self.running = True

    def exec_capture(self, slug: str, argv) -> str:
        self.calls.append(("exec", slug, list(argv)))
        return ""


def make_server(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    instance = Instance(
        name="Backup Test",
        slug="backup-test",
        loader="neoforge",
        mc_version="1.20.4",
        loader_version="47.1.106",
        memory="2G",
        tz="UTC",
        java_version=17,
        rcon_password="secret",
    )
    save_instance(layout, instance)
    write_compose(layout, instance)
    server = layout.server_dir(instance.slug)
    (server / "world").mkdir(parents=True)
    (server / "world" / "level.dat").write_text("original", encoding="utf-8")
    (server / "world_nether").mkdir()
    (server / "world_nether" / "level.dat").write_text("nether", encoding="utf-8")
    (server / "config").mkdir()
    (server / "config" / "example.toml").write_text("setting = 1\n", encoding="utf-8")
    (server / "defaultconfigs").mkdir()
    (server / "defaultconfigs" / "default.toml").write_text("x\n", encoding="utf-8")
    (server / "mods").mkdir()
    (server / "mods" / "example.jar").write_bytes(b"jar")
    (server / "logs").mkdir()
    (server / "logs" / "latest.log").write_text("noise\n", encoding="utf-8")
    (server / "libraries").mkdir()
    (server / "libraries" / "lib.jar").write_bytes(b"lib")
    (server / "server.properties").write_text("motd=hi\n", encoding="utf-8")
    return layout, instance, server


def test_world_backup_contents(tmp_path):
    layout, instance, server = make_server(tmp_path)

    backup = create_backup(layout, instance, kind="world")

    assert backup.archive_path.is_file()
    assert backup.size > 0
    assert backup.sha256
    assert (backup.path / "manifest.json").is_file()
    assert (backup.path / "instance.toml").is_file()
    with tarfile.open(backup.archive_path, "r:gz") as archive:
        names = set(archive.getnames())
    assert "world/level.dat" in names
    assert "world_nether/level.dat" in names
    assert "config/example.toml" in names
    assert "server.properties" in names
    assert "mods/example.jar" not in names
    assert "logs/latest.log" not in names

    manifest = json.loads((backup.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["instance"].get("rcon_password") is None
    assert manifest["kind"] == "world"


def test_full_backup_includes_mods_and_excludes_cache(tmp_path):
    layout, instance, server = make_server(tmp_path)

    backup = create_backup(layout, instance, kind="full")

    with tarfile.open(backup.archive_path, "r:gz") as archive:
        names = set(archive.getnames())
    assert "mods/example.jar" in names
    assert "world/level.dat" in names
    assert "logs/latest.log" not in names
    assert "libraries/lib.jar" not in names


def test_running_server_is_quiesced(tmp_path):
    layout, instance, _server = make_server(tmp_path)
    engine = FakeEngine(running=True)

    create_backup(layout, instance, kind="world", engine=engine)

    commands = [call[2][1] for call in engine.calls if call[0] == "exec"]
    assert commands == ["save-off", "save-all flush", "save-on"]


def test_prune_keeps_newest(tmp_path, monkeypatch):
    layout, instance, _server = make_server(tmp_path)
    ids = iter(["20260101-000001", "20260101-000002", "20260101-000003"])
    monkeypatch.setattr(backups, "_backup_id", lambda: next(ids))
    for _ in range(3):
        create_backup(layout, instance, kind="world", keep=0)

    removed = prune_backups(layout, instance.slug, 2)

    assert removed == ["20260101-000001"]
    remaining = [backup.backup_id for backup in list_backups(layout, instance.slug)]
    assert remaining == ["20260101-000003", "20260101-000002"]


def test_restore_replaces_modified_files(tmp_path):
    layout, instance, server = make_server(tmp_path)
    backup = create_backup(layout, instance, kind="world")
    (server / "world" / "level.dat").write_text("changed", encoding="utf-8")
    (server / "config" / "example.toml").write_text("changed\n", encoding="utf-8")

    restore_backup(layout, instance, backup)

    assert (server / "world" / "level.dat").read_text(encoding="utf-8") == "original"
    assert (
        server / "config" / "example.toml"
    ).read_text(encoding="utf-8") == "setting = 1\n"


def test_restore_running_server_stops_and_starts(tmp_path):
    layout, instance, _server = make_server(tmp_path)
    backup = create_backup(layout, instance, kind="world")
    engine = FakeEngine(running=True)

    restore_backup(layout, instance, backup, engine=engine, start_after=True)

    assert ("down", instance.slug) in engine.calls
    assert ("up", instance.slug) in engine.calls


def test_get_and_delete(tmp_path):
    layout, instance, _server = make_server(tmp_path)
    backup = create_backup(layout, instance, kind="world")

    assert get_backup(layout, instance.slug, "latest").backup_id == backup.backup_id
    delete_backup(layout, instance.slug, backup.backup_id)
    assert list_backups(layout, instance.slug) == []


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(1024**2) == "1.0 MB"


def test_missing_server_dir(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    instance = Instance(
        name="Missing",
        slug="missing",
        loader="vanilla",
        mc_version="1.21.1",
        rcon_password="x",
    )
    with pytest.raises(MchostError, match="does not exist"):
        create_backup(layout, instance)
