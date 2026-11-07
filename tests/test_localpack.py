from __future__ import annotations

import json
import zipfile

import pytest

from mchost.compose import write_compose
from mchost.config import load_instance
from mchost.errors import MchostError
from mchost.localpack import (
    inspect_local_pack,
    install_local_pack,
    mc_from_neoforge,
)
from mchost.paths import Layout


def make_pack_zip(path, *, wrapped: bool = False, manifest: dict | None = None):
    prefix = "MyPack/" if wrapped else ""
    with zipfile.ZipFile(path, "w") as archive:
        if manifest is not None:
            archive.writestr(prefix + "manifest.json", json.dumps(manifest))
        archive.writestr(prefix + "server.properties", "motd=hello\n")
        archive.writestr(prefix + "mods/example.jar", b"jar")
        archive.writestr(prefix + "config/example.toml", "a = 1\n")
    return path


FORGE_MANIFEST = {
    "name": "My Forge Pack",
    "version": "1.2.3",
    "minecraft": {
        "version": "1.20.1",
        "modLoaders": [{"id": "forge-47.2.0", "primary": True}],
    },
}


def test_inspect_forge_manifest(tmp_path):
    archive = make_pack_zip(tmp_path / "pack.zip", manifest=FORGE_MANIFEST)
    info = inspect_local_pack(archive)
    assert info.name == "My Forge Pack"
    assert info.pack_version == "1.2.3"
    assert info.mc_version == "1.20.1"
    assert info.loader == "forge"
    assert info.loader_version == "47.2.0"
    assert "manifest.json" in info.notes


def test_inspect_neoforge_libraries(tmp_path):
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "libraries/net/neoforged/neoforge/21.1.80/neoforge-21.1.80.jar",
            b"jar",
        )
        handle.writestr("mods/example.jar", b"jar")
    info = inspect_local_pack(archive)
    assert info.loader == "neoforge"
    assert info.loader_version == "21.1.80"
    assert info.mc_version == "1.21.1"


def test_mc_from_neoforge():
    assert mc_from_neoforge("47.1.106") == "1.20.1"
    assert mc_from_neoforge("20.4.253") == "1.20.4"
    assert mc_from_neoforge("21.1.80") == "1.21.1"
    assert mc_from_neoforge("21.0.10") == "1.21"
    assert mc_from_neoforge("26.3.5") == "26.3"


def test_install_local_pack_extracts_and_creates_instance(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    archive = make_pack_zip(
        tmp_path / "pack.zip", wrapped=True, manifest=FORGE_MANIFEST
    )

    instance = install_local_pack(
        layout,
        archive,
        name="My Forge Pack",
        loader="forge",
        mc_version="1.20.1",
        loader_version="47.2.0",
        memory="6G",
    )

    loaded = load_instance(layout, instance.slug)
    assert loaded.kind == "modpack"
    assert loaded.modpack.source == "local"
    assert loaded.modpack.file_kind == "zip"
    assert loaded.modpack.project == "My Forge Pack"
    server = layout.server_dir_for(loaded)
    assert (server / "mods/example.jar").is_file()
    assert (server / "config/example.toml").is_file()
    assert (server / "server.properties").is_file()
    assert not (server / "MyPack").exists()
    compose = layout.compose_file(loaded.slug).read_text(encoding="utf-8")
    assert "TYPE: FORGE" in compose
    assert "VERSION: 1.20.1" in compose
    assert "FORGE_VERSION: 47.2.0" in compose
    assert "GENERIC_PACK" not in compose
    assert "MODRINTH_MODPACK" not in compose
    assert (layout.pack_dir(loaded.slug) / "pack.zip").is_file()


def test_install_local_pack_rejects_traversal(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../evil.txt", "bad")
    with pytest.raises(MchostError, match="unsafe archive"):
        install_local_pack(
            layout,
            archive,
            name="Evil",
            loader="vanilla",
            mc_version="1.20.1",
        )
    assert not layout.instance_dir("evil").exists()
