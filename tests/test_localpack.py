from __future__ import annotations

import json
import shutil
import zipfile

import pytest
import yaml

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


def make_serverstarter_pack(tmp_path):
    full = tmp_path / "Craftoria-1.37.0.zip"
    manifest = {
        "name": "Craftoria",
        "version": "1.37.0",
        "minecraft": {
            "version": "1.21.1",
            "modLoaders": [{"id": "neoforge-21.1.249", "primary": True}],
        },
        "files": [
            {
                "projectID": 1,
                "downloadUrl": "https://example.invalid/mods/keep.jar",
                "required": True,
            },
            {
                "projectID": 2,
                "downloadUrl": "https://example.invalid/mods/ignored-client.jar",
                "required": True,
            },
            {
                "projectID": 3,
                "downloadUrl": "https://example.invalid/mods/optifine-x.jar",
                "required": True,
            },
            {
                "projectID": 4,
                "downloadUrl": "https://example.invalid/mods/keep2.jar",
                "required": True,
            },
        ],
    }
    with zipfile.ZipFile(full, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("overrides/config/example.toml", "a = 1\n")
        archive.writestr("overrides/mods/override.jar", b"override")

    setup = {
        "_specver": 2,
        "modpack": {"name": "Craftoria", "description": "test"},
        "install": {
            "mcVersion": None,
            "loaderVersion": "21.1.249",
            "modpackUrl": "https://example.invalid/Craftoria-1.37.0.zip",
            "installerUrl": "https://maven.neoforged.net/releases/net/neoforged/neoforge/21.1.249/neoforge-21.1.249-installer.jar",
            "ignoreFiles": ["mods/optifine*.jar", "resources/**"],
            "formatSpecific": {"ignoreProject": [2]},
        },
        "launch": {"maxRam": "5G"},
    }
    server_pack = tmp_path / "Craftoria-Server-1.37.0.zip"
    with zipfile.ZipFile(server_pack, "w") as archive:
        archive.writestr("server-setup-config.yaml", yaml.safe_dump(setup))
        archive.writestr("startserver.sh", "#!/usr/bin/env bash\n")
    return server_pack, full


def test_inspect_serverstarter_pack(tmp_path):
    server_pack, _full = make_serverstarter_pack(tmp_path)
    info = inspect_local_pack(server_pack)
    assert info.name == "Craftoria"
    assert info.pack_version == "1.37.0"
    assert info.loader == "neoforge"
    assert info.loader_version == "21.1.249"
    assert info.mc_version == "1.21.1"
    assert info.suggested_memory == "5G"
    assert info.serverstarter is not None
    assert info.serverstarter.ignore_projects == frozenset({2})
    assert "server-setup-config.yaml" in info.notes


def test_install_serverstarter_downloads_mods_and_overrides(tmp_path, monkeypatch):
    layout = Layout(tmp_path)
    layout.ensure()
    server_pack, full_pack = make_serverstarter_pack(tmp_path)

    def fake_download(url, destination, *, retries=2):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if url == "https://example.invalid/Craftoria-1.37.0.zip":
            shutil.copy2(full_pack, destination)
        else:
            destination.write_bytes(b"jar")
        return destination

    monkeypatch.setattr("mchost.localpack._download_file", fake_download)

    instance = install_local_pack(
        layout,
        server_pack,
        name="Craftoria",
        loader="neoforge",
        mc_version="1.21.1",
        loader_version="21.1.249",
        memory="5G",
    )

    loaded = load_instance(layout, instance.slug)
    assert loaded.kind == "modpack"
    assert loaded.modpack.source == "local"
    assert loaded.modpack.version_number == "1.37.0"
    assert loaded.loader == "neoforge"
    assert loaded.mc_version == "1.21.1"
    assert loaded.loader_version == "21.1.249"
    server = layout.server_dir_for(loaded)
    assert (server / "mods/keep.jar").is_file()
    assert (server / "mods/keep2.jar").is_file()
    assert (server / "mods/override.jar").is_file()
    assert not (server / "mods/ignored-client.jar").exists()
    assert not (server / "mods/optifine-x.jar").exists()
    assert (server / "config/example.toml").is_file()
    compose = layout.compose_file(loaded.slug).read_text(encoding="utf-8")
    assert "TYPE: NEOFORGE" in compose
    assert "VERSION: 1.21.1" in compose
    assert "NEOFORGE_VERSION: 21.1.249" in compose
    assert "GENERIC_PACK" not in compose
    assert (layout.pack_dir(loaded.slug) / "Craftoria-1.37.0.zip").is_file()


def make_curseforge_client_pack(tmp_path):
    manifest = {
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": "New Age",
        "version": "1.4.2",
        "minecraft": {
            "version": "1.21.1",
            "modLoaders": [{"id": "neoforge-21.1.250", "primary": True}],
        },
        "overrides": "overrides",
        "files": [
            {"projectID": 1, "fileID": 11, "required": True},
            {"projectID": 2, "fileID": 22, "required": True},
        ],
    }
    archive = tmp_path / "new-age.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("manifest.json", json.dumps(manifest))
        handle.writestr("overrides/config/example.toml", "a = 1\n")
        handle.writestr("overrides/mods/override.jar", b"override")
    return archive


def test_inspect_curseforge_client_pack(tmp_path):
    archive = make_curseforge_client_pack(tmp_path)
    info = inspect_local_pack(archive)
    assert info.name == "New Age"
    assert info.pack_version == "1.4.2"
    assert info.loader == "neoforge"
    assert info.loader_version == "21.1.250"
    assert info.mc_version == "1.21.1"
    assert info.curseforge_files == 2
    assert info.serverstarter is None


def test_install_curseforge_client_pack_downloads_mods(tmp_path, monkeypatch):
    layout = Layout(tmp_path)
    layout.ensure()
    archive = make_curseforge_client_pack(tmp_path)

    def fake_curseforge(project_id, file_id, directory, *, retries=2):
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"mod-{project_id}.jar"
        destination.write_bytes(b"jar")
        return destination

    monkeypatch.setattr(
        "mchost.localpack._download_curseforge_file", fake_curseforge
    )

    instance = install_local_pack(
        layout,
        archive,
        name="New Age",
        loader="neoforge",
        mc_version="1.21.1",
        loader_version="21.1.250",
    )

    loaded = load_instance(layout, instance.slug)
    assert loaded.kind == "modpack"
    assert loaded.modpack.source == "local"
    assert loaded.modpack.version_number == "1.4.2"
    assert loaded.loader == "neoforge"
    assert loaded.mc_version == "1.21.1"
    assert loaded.loader_version == "21.1.250"
    server = layout.server_dir_for(loaded)
    assert (server / "mods/mod-1.jar").is_file()
    assert (server / "mods/mod-2.jar").is_file()
    assert (server / "mods/override.jar").is_file()
    assert (server / "config/example.toml").is_file()
    compose = layout.compose_file(loaded.slug).read_text(encoding="utf-8")
    assert "TYPE: NEOFORGE" in compose
    assert "VERSION: 1.21.1" in compose
    assert "NEOFORGE_VERSION: 21.1.250" in compose
    assert "GENERIC_PACK" not in compose
