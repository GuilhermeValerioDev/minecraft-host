"""Modrinth modpack search, server-pack resolution, and installation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

import httpx

from .config import (
    Instance,
    ModpackConfig,
    default_tz,
    java_for_mc,
    new_rcon_password,
    save_instance,
    slugify,
)
from .compose import write_compose
from .engine import Engine, restore_ownership
from .errors import MchostError
from .paths import Layout
from .versions import USER_AGENT, _get_json

API = "https://api.modrinth.com/v2"
SERVER_ENVIRONMENTS = {
    "server_only",
    "dedicated_server_only",
    "server_only_client_optional",
}
SERVER_RE = re.compile(
    r"(?i)(serverpack|server[-_ ]?pack|(?:^|[^a-z0-9])server(?:[^a-z0-9]|$))"
)
CLIENT_RE = re.compile(r"(?i)(^|[^a-z0-9])client([^a-z0-9]|$)")


@dataclass(frozen=True)
class ModpackHit:
    slug: str
    title: str
    description: str
    downloads: int
    icon_url: str
    versions: tuple[str, ...]
    game_versions: tuple[str, ...]
    loaders: tuple[str, ...]


@dataclass(frozen=True)
class ModpackProject:
    id: str
    slug: str
    title: str
    description: str
    downloads: int
    icon_url: str
    versions: tuple[str, ...]
    game_versions: tuple[str, ...]
    loaders: tuple[str, ...]


@dataclass(frozen=True)
class ModrinthFile:
    filename: str
    url: str
    sha512: str
    size: int
    primary: bool

    @property
    def kind(self) -> str | None:
        suffix = Path(self.filename).suffix.lower()
        if suffix == ".mrpack":
            return "mrpack"
        if suffix == ".zip":
            return "zip"
        return None


@dataclass(frozen=True)
class ModrinthVersion:
    id: str
    project_id: str
    name: str
    version_number: str
    game_versions: tuple[str, ...]
    loaders: tuple[str, ...]
    environment: str
    version_type: str
    date_published: str
    files: tuple[ModrinthFile, ...]


@dataclass(frozen=True)
class Artifact:
    file: ModrinthFile
    kind: str
    mode: str
    reason: str


@dataclass(frozen=True)
class MrpackInfo:
    name: str
    mc_version: str
    loader: str
    loader_version: str | None


KNOWN_LOADERS = {
    "fabric",
    "forge",
    "neoforge",
    "quilt",
    "paper",
    "purpur",
    "spigot",
    "bukkit",
    "velocity",
    "waterfall",
    "bungeecord",
}


def _parse_hit(data: dict[str, Any]) -> ModpackHit:
    game_versions = tuple(str(item) for item in data.get("versions", []))
    categories = data.get("categories", [])
    loaders = tuple(
        str(item) for item in categories if str(item).lower() in KNOWN_LOADERS
    )
    return ModpackHit(
        slug=str(data.get("slug", "")),
        title=str(data.get("title", "")),
        description=str(data.get("description", "")),
        downloads=int(data.get("downloads", 0)),
        icon_url=str(data.get("icon_url") or ""),
        versions=game_versions,
        game_versions=game_versions,
        loaders=loaders,
    )


def search_modpacks(query: str, *, limit: int = 20, offset: int = 0) -> list[ModpackHit]:
    url = (
        f"{API}/search?"
        + urlencode(
            {
                "query": query,
                "limit": limit,
                "offset": offset,
                "facets": json.dumps([["project_type:modpack"]]),
                "index": "relevance",
            }
        )
    )
    data = _get_json(url, f"modrinth_search_{quote(query)}_{offset}_{limit}")
    return [_parse_hit(hit) for hit in data.get("hits", [])]


def get_project(reference: str) -> ModpackProject:
    data = _get_json(
        f"{API}/project/{quote(reference)}", f"modrinth_project_{reference}"
    )
    categories = data.get("categories", [])
    loaders = tuple(
        str(item) for item in categories if str(item).lower() in KNOWN_LOADERS
    )
    return ModpackProject(
        id=str(data.get("id", "")),
        slug=str(data.get("slug", reference)),
        title=str(data.get("title", reference)),
        description=str(data.get("description", "")),
        downloads=int(data.get("downloads", 0)),
        icon_url=str(data.get("icon_url") or ""),
        versions=tuple(str(item) for item in data.get("versions", [])),
        game_versions=tuple(
            str(item) for item in data.get("game_versions", [])
        ),
        loaders=tuple(str(item) for item in data.get("loaders", [])) or loaders,
    )


def get_versions(reference: str) -> list[ModrinthVersion]:
    data = _get_json(
        f"{API}/project/{quote(reference)}/version?include_changelog=false",
        f"modrinth_versions_{reference}",
    )
    versions: list[ModrinthVersion] = []
    for item in data:
        files = tuple(
            ModrinthFile(
                filename=str(file.get("filename", "")),
                url=str(file.get("url", "")),
                sha512=str(file.get("hashes", {}).get("sha512", "")),
                size=int(file.get("size", 0)),
                primary=bool(file.get("primary", False)),
            )
            for file in item.get("files", [])
        )
        versions.append(
            ModrinthVersion(
                id=str(item.get("id", "")),
                project_id=str(item.get("project_id", "")),
                name=str(item.get("name", "")),
                version_number=str(item.get("version_number", "")),
                game_versions=tuple(
                    str(value) for value in item.get("game_versions", [])
                ),
                loaders=tuple(str(value) for value in item.get("loaders", [])),
                environment=str(item.get("environment", "")),
                version_type=str(item.get("version_type", "release")),
                date_published=str(item.get("date_published", "")),
                files=files,
            )
        )
    versions.sort(key=lambda version: version.date_published, reverse=True)
    return versions


def select_version(
    versions: list[ModrinthVersion],
    reference: str | None = None,
    *,
    version_type: str = "release",
) -> ModrinthVersion:
    if not versions:
        raise MchostError("this modpack has no versions")
    if reference:
        for version in versions:
            if version.id == reference or version.version_number == reference:
                return version
        raise MchostError(f"no modpack version matching {reference!r}")
    for version in versions:
        if version.version_type == version_type:
            return version
    for version in versions:
        if version.version_type == "release":
            return version
    return versions[0]


def _is_server_name(name: str) -> bool:
    return bool(SERVER_RE.search(name))


def _is_client_name(name: str) -> bool:
    return bool(CLIENT_RE.search(name))


def _file_rank(file: ModrinthFile) -> tuple[int, int, int, int]:
    kind = file.kind
    return (
        1 if _is_server_name(file.filename) else 0,
        1 if kind == "mrpack" else 0,
        1 if file.primary else 0,
        file.size,
    )


def _server_files(version: ModrinthVersion) -> list[ModrinthFile]:
    return [
        file
        for file in version.files
        if file.kind is not None
        and _is_server_name(file.filename)
        and not _is_client_name(file.filename)
    ]


def _full_file(version: ModrinthVersion) -> ModrinthFile | None:
    candidates = [file for file in version.files if file.kind is not None]
    if not candidates:
        return None
    candidates.sort(key=_file_rank, reverse=True)
    return candidates[0]


def _base_version(value: str) -> str:
    value = re.sub(r"(?i)(server|client|pack|serverpack|server-pack)", "", value)
    return re.sub(r"[^0-9A-Za-z]+", "", value).lower()


def _same_release_family(
    selected: ModrinthVersion, sibling: ModrinthVersion
) -> bool:
    left = selected.version_number
    right = sibling.version_number
    if left and right and (left in right or right in left):
        return True
    base_left = _base_version(left)
    base_right = _base_version(right)
    return bool(base_left) and base_left == base_right


def _compatible(selected: ModrinthVersion, sibling: ModrinthVersion) -> bool:
    if selected.game_versions and sibling.game_versions:
        if not set(selected.game_versions) & set(sibling.game_versions):
            return False
    if selected.loaders and sibling.loaders:
        if not set(selected.loaders) & set(sibling.loaders):
            return False
    return True


def _artifact_from_file(
    file: ModrinthFile, mode: str, reason: str
) -> Artifact:
    kind = file.kind
    if kind is None:
        raise MchostError("unsupported modpack file type")
    return Artifact(file=file, kind=kind, mode=mode, reason=reason)


def resolve_artifact(
    version: ModrinthVersion,
    all_versions: list[ModrinthVersion],
    *,
    mode: str = "auto",
) -> Artifact:
    """Prefer a published server pack; fall back to the full pack."""
    if mode not in ("auto", "server", "full"):
        raise MchostError("mode must be auto, server, or full")

    if mode != "full":
        in_version = _server_files(version)
        if in_version:
            in_version.sort(key=_file_rank, reverse=True)
            return _artifact_from_file(
                in_version[0],
                "server",
                "explicit server pack in this version",
            )

        for sibling in all_versions:
            if sibling.id == version.id:
                continue
            if not _same_release_family(version, sibling):
                continue
            if not _compatible(version, sibling):
                continue
            server_files = _server_files(sibling)
            if sibling.environment in SERVER_ENVIRONMENTS and not server_files:
                full = _full_file(sibling)
                if full:
                    return _artifact_from_file(
                        full,
                        "server",
                        f"server-only version {sibling.version_number}",
                    )
            if server_files:
                server_files.sort(key=_file_rank, reverse=True)
                return _artifact_from_file(
                    server_files[0],
                    "server",
                    f"server pack from version {sibling.version_number}",
                )

    if mode == "server":
        raise MchostError(
            f"no server pack published for {version.version_number}"
        )

    full = _full_file(version)
    if full is None:
        raise MchostError(
            f"no .mrpack or .zip file for version {version.version_number}"
        )
    return _artifact_from_file(
        full,
        "full",
        "full pack (no separate server pack published)",
    )


def _sha512_file(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_artifact(
    artifact: Artifact,
    destination: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download and verify the artifact, returning the local path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and artifact.file.sha512:
        try:
            if _sha512_file(destination) == artifact.file.sha512:
                return destination
        except OSError:
            pass

    temporary = destination.with_name(destination.name + ".part")
    digest = hashlib.sha512()
    downloaded = 0
    try:
        with httpx.stream(
            "GET",
            artifact.file.url,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=120,
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    handle.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
        actual = digest.hexdigest()
        if artifact.file.sha512 and actual != artifact.file.sha512:
            raise MchostError(
                f"SHA-512 mismatch for {artifact.file.filename}: "
                f"expected {artifact.file.sha512}, got {actual}"
            )
        temporary.replace(destination)
    except httpx.HTTPError as exc:
        raise MchostError(
            f"could not download {artifact.file.filename}: {exc}"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return destination


def parse_mrpack_index(path: Path) -> MrpackInfo:
    try:
        with zipfile.ZipFile(path) as archive:
            index = json.loads(archive.read("modrinth.index.json"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise MchostError(f"could not read mrpack index from {path}: {exc}") from exc

    dependencies = index.get("dependencies", {})
    loader = "vanilla"
    loader_version = None
    for key, value in (
        ("neoforge", "neoforge"),
        ("forge", "forge"),
        ("fabric-loader", "fabric"),
        ("quilt-loader", "quilt"),
    ):
        if dependencies.get(key):
            loader = value
            loader_version = str(dependencies[key])
            break
    mc_version = str(dependencies.get("minecraft", ""))
    if not mc_version:
        raise MchostError(f"{path} does not declare a Minecraft version")
    return MrpackInfo(
        name=str(index.get("name") or path.stem),
        mc_version=mc_version,
        loader=loader,
        loader_version=loader_version,
    )


def _zip_loader(version: ModrinthVersion) -> tuple[str, str | None]:
    for loader in ("neoforge", "forge", "fabric", "quilt"):
        if loader in version.loaders:
            return loader, None
    return "vanilla", None


def unique_slug(layout: Layout, name: str) -> str:
    base = slugify(name)
    slug = base
    index = 2
    while layout.config_file(slug).is_file() or layout.instance_dir(slug).exists():
        slug = f"{base}-{index}"
        index += 1
    return slug


@dataclass
class InstallResult:
    instance: Instance
    project: ModpackProject
    version: ModrinthVersion
    artifact: Artifact
    pack_path: Path


@dataclass
class UpdateCheck:
    project: ModpackProject
    current: ModrinthVersion | None
    latest: ModrinthVersion
    artifact: Artifact
    available: bool
    reason: str


@dataclass
class UpdateResult:
    instance: Instance
    project: ModpackProject
    old_version: str
    new_version: str
    artifact: Artifact
    updated: bool


def install_modpack(
    layout: Layout,
    reference: str,
    *,
    version_reference: str | None = None,
    name: str | None = None,
    mode: str = "auto",
    memory: str = "6G",
    tz: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> InstallResult:
    project = get_project(reference)
    versions = get_versions(project.slug)
    version = select_version(versions, version_reference)
    artifact = resolve_artifact(version, versions, mode=mode)

    instance_name = name or project.title
    slug = unique_slug(layout, instance_name)
    instance_dir = layout.instance_dir(slug)
    pack_dir = layout.pack_dir(slug)
    instance_dir.mkdir(parents=True)
    pack_dir.mkdir(parents=True)

    try:
        destination = pack_dir / Path(artifact.file.filename).name
        download_artifact(artifact, destination, progress=progress)

        if artifact.kind == "mrpack":
            info = parse_mrpack_index(destination)
            mc_version = info.mc_version
            loader = info.loader
            loader_version = info.loader_version
        else:
            loader, loader_version = _zip_loader(version)
            mc_version = (
                version.game_versions[0] if version.game_versions else "LATEST"
            )

        modpack = ModpackConfig(
            source="modrinth",
            project=project.slug,
            version_id=version.id,
            version_number=version.version_number,
            mode=artifact.mode,
            file_name=destination.name,
            file_kind=artifact.kind,
            sha512=artifact.file.sha512,
            url=artifact.file.url,
            resolved={
                "minecraft": mc_version,
                "loader": loader,
                "loader_version": loader_version or "",
            },
        )
        instance = Instance(
            name=instance_name,
            slug=slug,
            kind="modpack",
            loader=loader,
            mc_version=mc_version,
            loader_version=loader_version,
            memory=memory.upper(),
            tz=tz or default_tz(),
            java_version=java_for_mc(mc_version),
            eula=True,
            rcon_password=new_rcon_password(),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            modpack=modpack,
        )
        instance.validate()
        layout.server_dir(slug).mkdir(parents=True, exist_ok=True)
        layout.backups_for(slug).mkdir(parents=True, exist_ok=True)
        save_instance(layout, instance)
        write_compose(layout, instance)
    except Exception:
        shutil.rmtree(instance_dir, ignore_errors=True)
        raise

    return InstallResult(
        instance=instance,
        project=project,
        version=version,
        artifact=artifact,
        pack_path=destination,
    )


def check_modpack_update(
    instance: Instance, *, mode: str | None = None
) -> UpdateCheck:
    pack = instance.modpack
    if instance.kind != "modpack" or pack is None:
        raise MchostError(f"{instance.slug} is not a modpack instance")
    if pack.source != "modrinth":
        raise MchostError(
            "this is a local pack; install a newer server pack to update it"
        )
    project = get_project(pack.project)
    versions = get_versions(project.slug)
    latest = select_version(versions)
    current = next((version for version in versions if version.id == pack.version_id), None)
    effective_mode = mode or "auto"
    artifact = resolve_artifact(latest, versions, mode=effective_mode)
    available = latest.id != pack.version_id and (
        current is None or latest.date_published > current.date_published
    )
    reason = (
        f"{latest.version_number} is available"
        if available
        else f"{pack.version_number} is up to date"
    )
    return UpdateCheck(
        project=project,
        current=current,
        latest=latest,
        artifact=artifact,
        available=available,
        reason=reason,
    )


def update_modpack(
    layout: Layout,
    instance: Instance,
    *,
    version_reference: str | None = None,
    mode: str | None = None,
    engine: Engine | None = None,
    restart: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> UpdateResult:
    pack = instance.modpack
    if instance.kind != "modpack" or pack is None:
        raise MchostError(f"{instance.slug} is not a modpack instance")
    if pack.source != "modrinth":
        raise MchostError(
            "this is a local pack; install a newer server pack to update it"
        )
    project = get_project(pack.project)
    versions = get_versions(project.slug)
    target = select_version(versions, version_reference)
    effective_mode = mode or "auto"
    artifact = resolve_artifact(target, versions, mode=effective_mode)
    old_version = pack.version_number

    if target.id == pack.version_id and artifact.file.sha512 == pack.sha512:
        return UpdateResult(
            instance=instance,
            project=project,
            old_version=old_version,
            new_version=target.version_number,
            artifact=artifact,
            updated=False,
        )

    was_running = engine is not None and engine.is_running(instance.slug)
    if was_running:
        engine.down(instance.slug)
        restore_ownership(layout, instance)

    try:
        pack_dir = layout.pack_dir(instance.slug)
        destination = pack_dir / Path(artifact.file.filename).name
        download_artifact(artifact, destination, progress=progress)

        if artifact.kind == "mrpack":
            info = parse_mrpack_index(destination)
            mc_version = info.mc_version
            loader = info.loader
            loader_version = info.loader_version
        else:
            loader, loader_version = _zip_loader(target)
            mc_version = (
                target.game_versions[0] if target.game_versions else "LATEST"
            )

        new_pack = ModpackConfig(
            source="modrinth",
            project=project.slug,
            version_id=target.id,
            version_number=target.version_number,
            mode=artifact.mode,
            file_name=destination.name,
            file_kind=artifact.kind,
            sha512=artifact.file.sha512,
            url=artifact.file.url,
            resolved={
                "minecraft": mc_version,
                "loader": loader,
                "loader_version": loader_version or "",
            },
        )
        instance.loader = loader
        instance.mc_version = mc_version
        instance.loader_version = loader_version
        instance.java_version = java_for_mc(mc_version)
        instance.modpack = new_pack
        instance.validate()
        save_instance(layout, instance)
        write_compose(layout, instance)

        for child in pack_dir.iterdir():
            if child.is_file() and child != destination:
                child.unlink(missing_ok=True)
    finally:
        if was_running and restart and engine is not None:
            engine.up(instance.slug)

    return UpdateResult(
        instance=instance,
        project=project,
        old_version=old_version,
        new_version=target.version_number,
        artifact=artifact,
        updated=True,
    )

