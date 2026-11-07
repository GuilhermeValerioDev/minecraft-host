from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from mchost import modrinth
from mchost.compose import write_compose
from mchost.config import Instance, ModpackConfig, load_instance, save_instance
from mchost.errors import MchostError
from mchost.modrinth import (
    Artifact,
    ModrinthFile,
    ModrinthVersion,
    ModpackProject,
    check_modpack_update,
    download_artifact,
    install_modpack,
    parse_mrpack_index,
    resolve_artifact,
    unique_slug,
    update_modpack,
)
from mchost.paths import Layout


def make_file(name: str, *, primary: bool = False) -> ModrinthFile:
    return ModrinthFile(
        filename=name,
        url=f"https://example.invalid/{name}",
        sha512="abc",
        size=100,
        primary=primary,
    )


def make_version(
    version_id: str,
    number: str,
    files: list[ModrinthFile],
    *,
    environment: str = "",
    loaders: tuple[str, ...] = ("fabric",),
    game_versions: tuple[str, ...] = ("1.20.1",),
    date_published: str = "2026-01-01T00:00:00Z",
    version_type: str = "release",
) -> ModrinthVersion:
    return ModrinthVersion(
        id=version_id,
        project_id="project",
        name=f"Pack {number}",
        version_number=number,
        game_versions=game_versions,
        loaders=loaders,
        environment=environment,
        version_type=version_type,
        date_published=date_published,
        files=tuple(files),
    )


def test_same_version_server_file_wins():
    version = make_version(
        "v1",
        "1.0.0",
        [
            make_file("pack.mrpack", primary=True),
            make_file("pack-server.zip"),
        ],
    )
    artifact = resolve_artifact(version, [version])
    assert artifact.kind == "zip"
    assert artifact.mode == "server"
    assert artifact.file.filename == "pack-server.zip"
    assert "explicit server" in artifact.reason


def test_sibling_server_version_wins():
    selected = make_version("v1", "1.7.42", [make_file("pack.mrpack", primary=True)])
    sibling = make_version(
        "v2", "1.7.42-server", [make_file("pack-server.mrpack", primary=True)]
    )
    artifact = resolve_artifact(selected, [selected, sibling])
    assert artifact.mode == "server"
    assert artifact.file.filename == "pack-server.mrpack"
    assert "1.7.42-server" in artifact.reason


def test_server_only_environment_without_server_filename():
    selected = make_version("v1", "1.7.42", [make_file("pack.mrpack", primary=True)])
    sibling = make_version(
        "v2",
        "1.7.42",
        [make_file("pack.mrpack", primary=True)],
        environment="server_only",
    )
    artifact = resolve_artifact(selected, [selected, sibling])
    assert artifact.mode == "server"
    assert artifact.reason == "server-only version 1.7.42"


def test_no_server_pack_falls_back_to_full():
    version = make_version("v1", "1.0.0", [make_file("pack.mrpack", primary=True)])
    artifact = resolve_artifact(version, [version])
    assert artifact.mode == "full"
    assert artifact.kind == "mrpack"
    assert "no separate server" in artifact.reason


def test_server_only_mode_rejects_missing_server_pack():
    version = make_version("v1", "1.0.0", [make_file("pack.mrpack", primary=True)])
    with pytest.raises(MchostError, match="no server pack"):
        resolve_artifact(version, [version], mode="server")


def test_filename_containing_observer_is_not_a_server_pack():
    version = make_version(
        "v1", "1.0.0", [make_file("observer-tools.mrpack", primary=True)]
    )
    artifact = resolve_artifact(version, [version])
    assert artifact.mode == "full"


def test_concatenated_serverpack_is_detected():
    version = make_version(
        "v1",
        "1.0.0",
        [
            make_file("justsomeoptimizationserverpack.mrpack", primary=True),
            make_file("full.mrpack"),
        ],
    )
    artifact = resolve_artifact(version, [version])
    assert artifact.mode == "server"
    assert artifact.file.filename == "justsomeoptimizationserverpack.mrpack"


def test_parse_mrpack_index(tmp_path):
    path = tmp_path / "pack.mrpack"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "modrinth.index.json",
            json.dumps(
                {
                    "name": "Test Pack",
                    "dependencies": {
                        "minecraft": "1.20.1",
                        "fabric-loader": "0.15.11",
                    },
                }
            ),
        )
    info = parse_mrpack_index(path)
    assert info.name == "Test Pack"
    assert info.mc_version == "1.20.1"
    assert info.loader == "fabric"
    assert info.loader_version == "0.15.11"


def test_parse_mrpack_rejects_bad_archive(tmp_path):
    path = tmp_path / "broken.mrpack"
    path.write_bytes(b"not a zip")
    with pytest.raises(MchostError, match="could not read"):
        parse_mrpack_index(path)


def test_download_artifact_reuses_verified_local_file(tmp_path):
    data = b"hello pack"
    destination = tmp_path / "pack.mrpack"
    destination.write_bytes(data)
    artifact = Artifact(
        ModrinthFile(
            filename="pack.mrpack",
            url="https://example.invalid/pack.mrpack",
            sha512=hashlib.sha512(data).hexdigest(),
            size=len(data),
            primary=True,
        ),
        "mrpack",
        "full",
        "",
    )
    assert download_artifact(artifact, destination) == destination


def test_download_artifact_verifies_stream(tmp_path, monkeypatch):
    data = b"streamed pack"

    class FakeResponse:
        headers = {"content-length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, size):
            yield data

    monkeypatch.setattr(modrinth.httpx, "stream", lambda *a, **k: FakeResponse())
    artifact = Artifact(
        ModrinthFile(
            filename="pack.mrpack",
            url="https://example.invalid/pack.mrpack",
            sha512=hashlib.sha512(data).hexdigest(),
            size=len(data),
            primary=True,
        ),
        "mrpack",
        "full",
        "",
    )
    destination = tmp_path / "pack.mrpack"
    download_artifact(artifact, destination)
    assert destination.read_bytes() == data


def test_download_artifact_rejects_bad_hash(tmp_path, monkeypatch):
    data = b"streamed pack"

    class FakeResponse:
        headers = {"content-length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, size):
            yield data

    monkeypatch.setattr(modrinth.httpx, "stream", lambda *a, **k: FakeResponse())
    artifact = Artifact(
        ModrinthFile(
            filename="pack.mrpack",
            url="https://example.invalid/pack.mrpack",
            sha512="deadbeef",
            size=len(data),
            primary=True,
        ),
        "mrpack",
        "full",
        "",
    )
    with pytest.raises(MchostError, match="SHA-512 mismatch"):
        download_artifact(artifact, tmp_path / "pack.mrpack")


def test_install_modpack_creates_instance(tmp_path, monkeypatch):
    layout = Layout(tmp_path)
    layout.ensure()
    version = make_version(
        "v1", "1.0.0", [make_file("pack-server.mrpack", primary=True)]
    )
    project = ModpackProject(
        id="project",
        slug="test-pack",
        title="Test Pack",
        description="",
        downloads=1,
        icon_url="",
        versions=("v1",),
        game_versions=("1.20.1",),
        loaders=("fabric",),
    )
    monkeypatch.setattr(modrinth, "get_project", lambda reference: project)
    monkeypatch.setattr(modrinth, "get_versions", lambda reference: [version])

    def fake_download(artifact, destination, progress=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(
                "modrinth.index.json",
                json.dumps(
                    {
                        "name": "Test Pack",
                        "dependencies": {
                            "minecraft": "1.20.1",
                            "fabric-loader": "0.15.11",
                        },
                    }
                ),
            )
        return destination

    monkeypatch.setattr(modrinth, "download_artifact", fake_download)

    result = install_modpack(layout, "test-pack", name="My Pack")

    assert result.instance.slug == "my-pack"
    loaded = load_instance(layout, "my-pack")
    assert loaded.kind == "modpack"
    assert loaded.loader == "fabric"
    assert loaded.mc_version == "1.20.1"
    assert loaded.loader_version == "0.15.11"
    assert loaded.modpack is not None
    assert loaded.modpack.file_kind == "mrpack"
    assert loaded.modpack.mode == "server"
    assert result.pack_path.is_file()
    compose = layout.compose_file("my-pack").read_text(encoding="utf-8")
    assert "TYPE: MODRINTH" in compose
    assert "MODRINTH_MODPACK: /modpacks/pack-server.mrpack" in compose


def test_unique_slug(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    assert unique_slug(layout, "My Pack") == "my-pack"
    layout.instance_dir("my-pack").mkdir()
    assert unique_slug(layout, "My Pack") == "my-pack-2"


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


def make_modpack_instance(layout: Layout) -> Instance:
    instance = Instance(
        name="My Pack",
        slug="my-pack",
        kind="modpack",
        loader="fabric",
        mc_version="1.20.1",
        loader_version="0.15.0",
        memory="6G",
        tz="UTC",
        java_version=17,
        rcon_password="secret",
        modpack=ModpackConfig(
            project="test-pack",
            version_id="v1",
            version_number="1.0.0",
            mode="server",
            file_name="old-pack.mrpack",
            file_kind="mrpack",
            sha512="old",
            resolved={
                "minecraft": "1.20.1",
                "loader": "fabric",
                "loader_version": "0.15.0",
            },
        ),
    )
    instance.validate()
    save_instance(layout, instance)
    write_compose(layout, instance)
    layout.server_dir(instance.slug).mkdir(parents=True, exist_ok=True)
    layout.pack_dir(instance.slug).mkdir(parents=True, exist_ok=True)
    (layout.pack_dir(instance.slug) / "old-pack.mrpack").write_bytes(b"old")
    return instance


def test_check_modpack_update_available(tmp_path, monkeypatch):
    layout = Layout(tmp_path)
    layout.ensure()
    instance = make_modpack_instance(layout)
    current = make_version(
        "v1",
        "1.0.0",
        [make_file("pack-server.mrpack", primary=True)],
        date_published="2026-01-01T00:00:00Z",
    )
    latest = make_version(
        "v2",
        "1.1.0",
        [make_file("pack-server.mrpack", primary=True)],
        date_published="2026-02-01T00:00:00Z",
    )
    project = ModpackProject(
        id="project",
        slug="test-pack",
        title="Test Pack",
        description="",
        downloads=1,
        icon_url="",
        versions=("v1", "v2"),
        game_versions=("1.20.1",),
        loaders=("fabric",),
    )
    monkeypatch.setattr(modrinth, "get_project", lambda reference: project)
    monkeypatch.setattr(modrinth, "get_versions", lambda reference: [latest, current])

    update = check_modpack_update(instance)
    assert update.available is True
    assert update.latest.id == "v2"
    assert update.artifact.mode == "server"

    monkeypatch.setattr(modrinth, "get_versions", lambda reference: [current])
    update = check_modpack_update(instance)
    assert update.available is False


def test_update_modpack_replaces_pack_and_restarts(tmp_path, monkeypatch):
    layout = Layout(tmp_path)
    layout.ensure()
    instance = make_modpack_instance(layout)
    latest = make_version(
        "v2",
        "1.1.0",
        [make_file("pack-server.mrpack", primary=True)],
        date_published="2026-02-01T00:00:00Z",
    )
    project = ModpackProject(
        id="project",
        slug="test-pack",
        title="Test Pack",
        description="",
        downloads=1,
        icon_url="",
        versions=("v2",),
        game_versions=("1.21.1",),
        loaders=("fabric",),
    )
    monkeypatch.setattr(modrinth, "get_project", lambda reference: project)
    monkeypatch.setattr(modrinth, "get_versions", lambda reference: [latest])

    def fake_download(artifact, destination, progress=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(
                "modrinth.index.json",
                json.dumps(
                    {
                        "name": "Test Pack",
                        "dependencies": {
                            "minecraft": "1.21.1",
                            "fabric-loader": "0.16.0",
                        },
                    }
                ),
            )
        return destination

    monkeypatch.setattr(modrinth, "download_artifact", fake_download)
    engine = FakeEngine(running=True)

    result = update_modpack(layout, instance, engine=engine, restart=True)

    assert result.updated is True
    assert result.old_version == "1.0.0"
    assert result.new_version == "1.1.0"
    loaded = load_instance(layout, "my-pack")
    assert loaded.modpack.version_id == "v2"
    assert loaded.modpack.file_name == "pack-server.mrpack"
    assert loaded.mc_version == "1.21.1"
    assert loaded.loader_version == "0.16.0"
    assert loaded.java_version == 21
    assert ("down", "my-pack") in engine.calls
    assert ("up", "my-pack") in engine.calls
    pack_dir = layout.pack_dir("my-pack")
    assert (pack_dir / "pack-server.mrpack").is_file()
    assert not (pack_dir / "old-pack.mrpack").exists()
    compose = layout.compose_file("my-pack").read_text(encoding="utf-8")
    assert "MODRINTH_MODPACK: /modpacks/pack-server.mrpack" in compose
