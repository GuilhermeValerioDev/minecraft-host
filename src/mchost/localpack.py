"""Install a user-provided CurseForge-style server pack archive."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable
from urllib.parse import unquote, urlparse

import httpx
import yaml

from .compose import write_compose
from .config import (
    LOADERS,
    Instance,
    ModpackConfig,
    default_tz,
    java_for_mc,
    new_rcon_password,
    save_instance,
    utc_now,
)
from .errors import MchostError
from .modrinth import unique_slug
from .paths import Layout
from .versions import USER_AGENT

MARKERS = {
    "mods",
    "config",
    "server.properties",
    "manifest.json",
    "libraries",
    "scripts",
    "startserver.sh",
    "start.sh",
    "run.sh",
    "user_jvm_args.txt",
    "defaultconfigs",
    "plugins",
}
START_SCRIPTS = ("startserver.sh", "start.sh", "run.sh", "user_jvm_args.txt")


@dataclass(frozen=True)
class ServerStarterConfig:
    modpack_url: str
    ignore_projects: frozenset[int]
    ignore_files: tuple[str, ...]
    max_ram: str | None


@dataclass(frozen=True)
class LocalPackInfo:
    name: str
    pack_version: str
    mc_version: str | None
    loader: str | None
    loader_version: str | None
    notes: str
    suggested_memory: str | None = None
    serverstarter: ServerStarterConfig | None = None
    curseforge_files: int = 0


def _archive_kind(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        return "tar"
    raise MchostError(
        "CurseForge server packs must be .zip, .tar.gz, .tgz, or .tar"
    )


def _member_names(path: Path) -> list[str]:
    kind = _archive_kind(path)
    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as archive:
                return [name.replace("\\", "/") for name in archive.namelist()]
        with tarfile.open(path, "r:*") as archive:
            return [name.replace("\\", "/") for name in archive.getnames()]
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise MchostError(f"could not read {path.name}: {exc}") from exc


def _read_member(path: Path, member: str, limit: int = 1024 * 1024) -> bytes | None:
    kind = _archive_kind(path)
    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo(member)
                if info.file_size > limit:
                    return None
                return archive.read(member)
        with tarfile.open(path, "r:*") as archive:
            handle: BinaryIO | None = archive.extractfile(member)
            if handle is None:
                return None
            data = handle.read(limit + 1)
            return data if len(data) <= limit else None
    except (OSError, KeyError, zipfile.BadZipFile, tarfile.TarError):
        return None


def mc_from_neoforge(version: str) -> str | None:
    if version.startswith("47."):
        return "1.20.1"
    parts = version.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1])
    except (ValueError, IndexError):
        return None
    if major >= 26:
        return f"{major}.{minor}" if minor else str(major)
    if major == 20 and minor == 1:
        return "1.20.1"
    return f"1.{major}.{minor}" if minor else f"1.{major}"


def _loader_from_id(value: str) -> tuple[str | None, str | None]:
    lowered = value.lower()
    for prefix, loader in (
        ("neoforge-", "neoforge"),
        ("forge-", "forge"),
        ("fabric-", "fabric"),
        ("quilt-", "quilt"),
    ):
        if lowered.startswith(prefix):
            version = value[len(prefix) :]
            if loader == "forge":
                head, separator, tail = version.partition("-")
                if separator and re.match(
                    r"^(?:1|26)\.\d+(?:\.\d+)?$", head
                ):
                    version = tail
            return loader, version
    return None, None


def _loader_from_installer(url: str) -> str | None:
    lowered = url.lower()
    if "neoforged/neoforge" in lowered:
        return "neoforge"
    if "minecraftforge/forge" in lowered:
        return "forge"
    if "fabric" in lowered:
        return "fabric"
    if "quilt" in lowered:
        return "quilt"
    return None


def _parse_serverstarter(
    path: Path, names: list[str]
) -> tuple[
    ServerStarterConfig | None,
    str,
    str,
    str | None,
    str | None,
    str | None,
]:
    """Parse a TeamAOF ServerStarter server-setup-config.yaml."""
    candidates = sorted(
        (name for name in names if Path(name).name == "server-setup-config.yaml"),
        key=len,
    )
    if not candidates:
        return None, "", "", None, None, None
    raw = _read_member(path, candidates[0], limit=256 * 1024)
    if not raw:
        return None, "", "", None, None, None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None, "", "", None, None, None
    if not isinstance(data, dict):
        return None, "", "", None, None, None

    install = data.get("install") or {}
    modpack = data.get("modpack") or {}
    launch = data.get("launch") or {}
    if not isinstance(install, dict):
        install = {}
    if not isinstance(modpack, dict):
        modpack = {}
    if not isinstance(launch, dict):
        launch = {}

    installer_url = str(install.get("installerUrl") or "")
    loader_version = str(install.get("loaderVersion") or "") or None
    loader = _loader_from_installer(installer_url)
    mc_version = str(install.get("mcVersion") or "") or None
    if not loader and loader_version:
        if re.match(r"^(?:20|21|26|47)\.", loader_version):
            loader = "neoforge"
    if not mc_version and loader == "neoforge" and loader_version:
        mc_version = mc_from_neoforge(loader_version)
    pack_name = str(modpack.get("name") or "")
    modpack_url = str(install.get("modpackUrl") or "")

    format_specific = install.get("formatSpecific") or {}
    if not isinstance(format_specific, dict):
        format_specific = {}
    ignore_projects: set[int] = set()
    for value in format_specific.get("ignoreProject") or []:
        try:
            ignore_projects.add(int(value))
        except (TypeError, ValueError):
            pass
    ignore_files = tuple(
        str(value) for value in (install.get("ignoreFiles") or []) if value
    )
    max_ram = str(launch.get("maxRam") or "") or None

    pack_version = ""
    match = re.search(
        r"[-_]v?([0-9]+(?:\.[0-9]+){1,3})\.zip$",
        Path(urlparse(modpack_url).path).name,
        re.IGNORECASE,
    )
    if match:
        pack_version = match.group(1)

    config = None
    if modpack_url:
        config = ServerStarterConfig(
            modpack_url=modpack_url,
            ignore_projects=frozenset(ignore_projects),
            ignore_files=ignore_files,
            max_ram=max_ram,
        )
    return (
        config,
        pack_name,
        pack_version,
        mc_version,
        loader,
        loader_version,
    )


def inspect_local_pack(path: Path) -> LocalPackInfo:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise MchostError(f"archive not found: {path}")
    kind = _archive_kind(path)
    names = [name for name in _member_names(path) if name and not name.endswith("/")]
    if not names:
        raise MchostError(f"{path.name} is empty")

    loader: str | None = None
    loader_version: str | None = None
    mc_version: str | None = None
    pack_name = path.stem
    pack_version = ""
    curseforge_files = 0
    notes: list[str] = []

    manifests = sorted(
        (name for name in names if Path(name).name == "manifest.json"),
        key=len,
    )
    if manifests:
        raw = _read_member(path, manifests[0])
        if raw:
            try:
                manifest = json.loads(raw)
            except json.JSONDecodeError:
                manifest = None
            if isinstance(manifest, dict):
                minecraft = manifest.get("minecraft", {})
                if isinstance(minecraft, dict):
                    mc_version = str(minecraft.get("version") or "") or None
                    mod_loaders = minecraft.get("modLoaders", [])
                    if isinstance(mod_loaders, list):
                        chosen = next(
                            (
                                item
                                for item in mod_loaders
                                if isinstance(item, dict)
                                and item.get("primary")
                            ),
                            mod_loaders[0] if mod_loaders else None,
                        )
                        if isinstance(chosen, dict):
                            loader, loader_version = _loader_from_id(
                                str(chosen.get("id") or "")
                            )
                pack_name = str(manifest.get("name") or pack_name)
                pack_version = str(manifest.get("version") or "")
                if pack_version and pack_name.endswith(pack_version):
                    pack_name = pack_name[: -len(pack_version)].rstrip(" -_")
                files = manifest.get("files") or []
                if isinstance(files, list):
                    curseforge_files = len(files)
                notes.append(
                    f"manifest.json: {loader or 'unknown loader'} "
                    f"{loader_version or ''} / MC {mc_version or 'unknown'}"
                )

    for name in names:
        match = re.search(
            r"(?:^|/)libraries/net/neoforged/neoforge/([^/]+)/", name
        )
        if match and not loader_version:
            loader = loader or "neoforge"
            loader_version = match.group(1)
            mc_version = mc_version or mc_from_neoforge(loader_version)
            notes.append(f"libraries: NeoForge {loader_version}")
        match = re.search(
            r"(?:^|/)libraries/net/minecraftforge/forge/([^/]+)/", name
        )
        if match and not loader_version:
            raw = match.group(1)
            if "-" in raw:
                mc, version = raw.split("-", 1)
                mc_version = mc_version or mc
                loader = loader or "forge"
                loader_version = version
                notes.append(f"libraries: Forge {version} / MC {mc}")
        if name.endswith("fabric-server-launch.jar"):
            loader = loader or "fabric"
            notes.append("fabric-server-launch.jar")
        if name.endswith("quilt-server-launch.jar"):
            loader = loader or "quilt"
            notes.append("quilt-server-launch.jar")

    script_names = [
        name
        for name in names
        if Path(name).name in START_SCRIPTS
    ]
    for name in script_names[:4]:
        raw = _read_member(path, name, limit=128 * 1024)
        if not raw:
            continue
        try:
            text = raw.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:
            continue
        match = re.search(
            r"libraries/net/minecraftforge/forge/([\d.]+)-([\d.]+)", text
        )
        if match and not loader_version:
            mc_version = mc_version or match.group(1)
            loader = loader or "forge"
            loader_version = match.group(2)
            notes.append(f"{Path(name).name}: Forge {match.group(2)}")
        match = re.search(
            r"libraries/net/neoforged/neoforge/([\d.]+)", text
        )
        if match and not loader_version:
            loader = loader or "neoforge"
            loader_version = match.group(1)
            mc_version = mc_version or mc_from_neoforge(loader_version)
            notes.append(f"{Path(name).name}: NeoForge {match.group(1)}")
        match = re.search(r"forge-([\d.]+)-([\d.]+)\.jar", text)
        if match and not loader_version:
            mc_version = mc_version or match.group(1)
            loader = loader or "forge"
            loader_version = match.group(2)
            notes.append(f"{Path(name).name}: Forge {match.group(2)}")
        if "fabric-server-launch.jar" in text:
            loader = loader or "fabric"

    for name in names:
        match = re.search(r"(?:^|/)minecraft_server\.([\d.]+)\.jar$", name)
        if match:
            mc_version = mc_version or match.group(1)
            loader = loader or "vanilla"
            notes.append(f"vanilla server jar {match.group(1)}")

    (
        starter_config,
        starter_name,
        starter_version,
        starter_mc,
        starter_loader,
        starter_loader_version,
    ) = _parse_serverstarter(path, names)
    if starter_name:
        pack_name = starter_name
    if starter_version and not pack_version:
        pack_version = starter_version
    if starter_mc:
        mc_version = mc_version or starter_mc
    if starter_loader:
        loader = loader or starter_loader
    if starter_loader_version:
        loader_version = loader_version or starter_loader_version
    if starter_config:
        notes.append(
            "server-setup-config.yaml: "
            f"{starter_loader or 'unknown loader'} "
            f"{starter_loader_version or ''} / MC {starter_mc or 'unknown'}"
        )

    if not notes:
        notes.append(
            "No loader metadata found; choose a loader and Minecraft version."
        )
    return LocalPackInfo(
        name=pack_name,
        pack_version=pack_version,
        mc_version=mc_version,
        loader=loader,
        loader_version=loader_version,
        notes="; ".join(dict.fromkeys(notes)),
        suggested_memory=starter_config.max_ram if starter_config else None,
        serverstarter=starter_config,
        curseforge_files=curseforge_files,
    )


def _safe_extract(path: Path, destination: Path) -> None:
    kind = _archive_kind(path)
    destination = destination.resolve()
    if kind == "zip":
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    name = member.filename.replace("\\", "/")
                    if not name or name.endswith("/"):
                        continue
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise MchostError(f"unsafe archive member: {name}")
                    target = (destination / name).resolve()
                    if not target.is_relative_to(destination):
                        raise MchostError(f"unsafe archive member: {name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    mode = (member.external_attr >> 16) & 0o777
                    if mode:
                        try:
                            target.chmod(mode)
                        except OSError:
                            pass
        except (OSError, zipfile.BadZipFile) as exc:
            raise MchostError(f"could not extract {path.name}: {exc}") from exc
        return

    try:
        with tarfile.open(path, "r:*") as archive:
            try:
                archive.extractall(destination, filter="data")
            except TypeError:
                archive.extractall(destination)
    except (OSError, tarfile.TarError) as exc:
        raise MchostError(f"could not extract {path.name}: {exc}") from exc


def _flatten_root(tmp: Path) -> Path:
    entries = list(tmp.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        candidate = entries[0]
        if any((candidate / marker).exists() for marker in MARKERS):
            return candidate
    return tmp


def _download_file(url: str, destination: Path, *, retries: int = 2) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise MchostError(f"refusing to download a non-HTTP URL: {url}")
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    last_error: Exception | None = None
    for _attempt in range(retries):
        try:
            with httpx.stream(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=120,
            ) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 256):
                        handle.write(chunk)
            temporary.replace(destination)
            return destination
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
    raise MchostError(f"could not download {url}: {last_error}")


def _extract_overrides(
    pack_path: Path,
    server: Path,
    ignore_patterns: tuple[str, ...] = (),
    prefix: str = "overrides",
) -> None:
    root = server.resolve()
    stem = prefix.strip("/") + "/"
    try:
        with zipfile.ZipFile(pack_path) as archive:
            for member in archive.infolist():
                name = member.filename.replace("\\", "/")
                if not name.startswith(stem) or name.endswith("/"):
                    continue
                relative = name[len(stem) :]
                if not relative:
                    continue
                if any(
                    fnmatch.fnmatch(relative, pattern)
                    for pattern in ignore_patterns
                ):
                    continue
                target = (root / relative).resolve()
                if not target.is_relative_to(root):
                    raise MchostError(f"unsafe override path: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    try:
                        target.chmod(mode)
                    except OSError:
                        pass
    except (OSError, zipfile.BadZipFile) as exc:
        raise MchostError(f"could not extract overrides: {exc}") from exc


def _read_curseforge_manifest(path: Path) -> dict | None:
    if _archive_kind(path) != "zip":
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("manifest.json")
        manifest = json.loads(raw)
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("manifestType") != "minecraftModpack":
        return None
    if not isinstance(manifest.get("files"), list):
        return None
    return manifest


def _download_curseforge_file(
    project_id: int, file_id: int, directory: Path, *, retries: int = 2
) -> Path:
    """Download via CurseForge's public website download endpoint.

    Some CurseForge manifests omit ``downloadUrl``. The website endpoint
    redirects to the CDN and includes the original filename in the redirect.
    """
    url = (
        f"https://www.curseforge.com/api/v1/mods/{project_id}"
        f"/files/{file_id}/download"
    )
    directory.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for _attempt in range(retries):
        temporary: Path | None = None
        try:
            with httpx.stream(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=120,
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if content_type.startswith("text/html"):
                    raise MchostError(
                        f"CurseForge returned HTML for {project_id}/{file_id}"
                    )
                filename = Path(unquote(response.url.path)).name
                if not filename:
                    disposition = response.headers.get(
                        "content-disposition", ""
                    )
                    match = re.search(
                        r'filename="?([^";]+)"?', disposition
                    )
                    if match:
                        filename = match.group(1)
                if not filename:
                    raise MchostError(
                        f"could not determine filename for {project_id}/{file_id}"
                    )
                destination = directory / filename
                if destination.is_file() and destination.stat().st_size > 0:
                    return destination
                temporary = destination.with_name(destination.name + ".part")
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 256):
                        handle.write(chunk)
            temporary.replace(destination)
            return destination
        except (httpx.HTTPError, OSError, MchostError) as exc:
            last_error = exc
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    raise MchostError(
        f"could not download CurseForge file {project_id}/{file_id}: {last_error}"
    )


def _install_curseforge_manifest(
    archive: Path,
    manifest: dict,
    pack_dir: Path,
    server: Path,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[str, str, str | None, str]:
    """Install a CurseForge manifest pack without a ServerStarter config."""
    prefix = str(manifest.get("overrides") or "overrides")
    _extract_overrides(archive, server, (), prefix)

    targets: list[tuple[str, object, object]] = []
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict):
            continue
        try:
            project_id = int(entry.get("projectID", 0))
            file_id = int(entry.get("fileID", 0))
        except (TypeError, ValueError):
            continue
        if not project_id or not file_id:
            continue
        download_url = entry.get("downloadUrl")
        if download_url:
            filename = Path(unquote(urlparse(str(download_url)).path)).name
            if not filename:
                continue
            destination = server / "mods" / filename
            if destination.exists():
                continue
            targets.append(("url", str(download_url), destination))
        else:
            targets.append(("curseforge", project_id, file_id))

    total = len(targets)
    if progress:
        progress(0, total)
    failures: list[str] = []
    completed = 0
    if targets:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {}
            for kind, first, second in targets:
                if kind == "url":
                    futures[
                        pool.submit(_download_file, first, second)
                    ] = (kind, first, second)
                else:
                    futures[
                        pool.submit(
                            _download_curseforge_file, first, second, server / "mods"
                        )
                    ] = (kind, first, second)
            for future in as_completed(futures):
                try:
                    future.result()
                except (MchostError, OSError) as exc:
                    failures.append(str(exc))
                completed += 1
                if progress:
                    progress(completed, total)
    if failures:
        raise MchostError(
            f"{len(failures)} mod downloads failed; first error: {failures[0]}"
        )

    minecraft = manifest.get("minecraft") or {}
    if not isinstance(minecraft, dict):
        minecraft = {}
    mc_version = str(minecraft.get("version") or "") or "LATEST"
    loader = "vanilla"
    loader_version = None
    mod_loaders = minecraft.get("modLoaders") or []
    if isinstance(mod_loaders, list) and mod_loaders:
        chosen = next(
            (
                item
                for item in mod_loaders
                if isinstance(item, dict) and item.get("primary")
            ),
            mod_loaders[0] if isinstance(mod_loaders[0], dict) else None,
        )
        if isinstance(chosen, dict):
            loader, loader_version = _loader_from_id(
                str(chosen.get("id") or "")
            )
            loader = loader or "vanilla"
    return (
        loader,
        mc_version,
        loader_version,
        str(manifest.get("version") or ""),
    )


def _install_serverstarter(
    info: LocalPackInfo,
    pack_dir: Path,
    server: Path,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[str, str, str | None, str]:
    """Install a TeamAOF ServerStarter pack by resolving its CurseForge manifest."""
    config = info.serverstarter
    if config is None or not config.modpack_url:
        raise MchostError("server-setup-config.yaml does not declare a modpack URL")

    pack_name = Path(urlparse(config.modpack_url).path).name or "modpack.zip"
    full_pack = pack_dir / pack_name
    _download_file(config.modpack_url, full_pack)

    try:
        with zipfile.ZipFile(full_pack) as archive:
            manifest = json.loads(archive.read("manifest.json"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise MchostError(f"could not read {pack_name}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise MchostError(f"{pack_name} has an invalid manifest")

    _extract_overrides(full_pack, server, config.ignore_files)

    minecraft = manifest.get("minecraft") or {}
    if not isinstance(minecraft, dict):
        minecraft = {}
    mc_version = str(minecraft.get("version") or "") or info.mc_version or "LATEST"
    loader = info.loader or "vanilla"
    loader_version = info.loader_version
    mod_loaders = minecraft.get("modLoaders") or []
    if isinstance(mod_loaders, list) and mod_loaders:
        chosen = next(
            (
                item
                for item in mod_loaders
                if isinstance(item, dict) and item.get("primary")
            ),
            mod_loaders[0] if isinstance(mod_loaders[0], dict) else None,
        )
        if isinstance(chosen, dict):
            parsed_loader, parsed_version = _loader_from_id(
                str(chosen.get("id") or "")
            )
            loader = parsed_loader or loader
            loader_version = parsed_version or loader_version

    files = manifest.get("files") or []
    targets: list[tuple[str, Path]] = []
    missing: list[int] = []
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            continue
        try:
            project_id = int(entry.get("projectID", 0))
        except (TypeError, ValueError):
            project_id = 0
        if project_id in config.ignore_projects:
            continue
        url = entry.get("downloadUrl")
        if not url:
            if entry.get("required", True):
                missing.append(project_id)
            continue
        filename = Path(urlparse(str(url)).path).name
        if not filename or filename in seen:
            continue
        seen.add(filename)
        relative = f"mods/{filename}"
        if any(
            fnmatch.fnmatch(relative, pattern)
            or fnmatch.fnmatch(filename, pattern)
            for pattern in config.ignore_files
        ):
            continue
        destination = server / "mods" / filename
        if destination.exists():
            continue
        targets.append((str(url), destination))

    if missing:
        raise MchostError(
            f"{len(missing)} required mods have no download URL in the pack "
            f"manifest (project IDs: {', '.join(map(str, missing[:8]))})"
        )

    total = len(targets)
    if progress:
        progress(0, total)
    failures: list[str] = []
    completed = 0
    if targets:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(_download_file, url, destination): (url, destination)
                for url, destination in targets
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except (MchostError, OSError) as exc:
                    failures.append(str(exc))
                completed += 1
                if progress:
                    progress(completed, total)
    if failures:
        raise MchostError(
            f"{len(failures)} mod downloads failed; first error: {failures[0]}"
        )

    return (
        loader,
        mc_version,
        loader_version,
        str(manifest.get("version") or info.pack_version or ""),
    )


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_local_pack(
    layout: Layout,
    archive: Path,
    *,
    name: str,
    loader: str,
    mc_version: str,
    loader_version: str | None = None,
    memory: str = "6G",
    tz: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Instance:
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise MchostError(f"archive not found: {archive}")
    kind = _archive_kind(archive)
    if loader not in LOADERS:
        raise MchostError(f"loader must be one of {', '.join(LOADERS)}")
    if not mc_version.strip():
        raise MchostError("a Minecraft version is required")
    if not name.strip():
        raise MchostError("a name is required")

    info = inspect_local_pack(archive)
    slug = unique_slug(layout, name)
    instance_dir = layout.instance_dir(slug)
    pack_dir = layout.pack_dir(slug)
    instance_dir.mkdir(parents=True)
    pack_dir.mkdir(parents=True)

    try:
        destination = pack_dir / archive.name
        if archive != destination:
            shutil.copy2(archive, destination)
        digest = _sha512(destination)
        server = layout.server_dir(slug)
        server.mkdir(parents=True, exist_ok=True)
        pack_version = info.pack_version
        pack_url = ""
        manifest = _read_curseforge_manifest(archive)

        if info.serverstarter is not None:
            loader, mc_version, loader_version, pack_version = (
                _install_serverstarter(info, pack_dir, server, progress)
            )
            pack_url = info.serverstarter.modpack_url
        elif manifest is not None:
            loader, mc_version, loader_version, pack_version = (
                _install_curseforge_manifest(
                    archive, manifest, pack_dir, server, progress
                )
            )
        else:
            tmp = instance_dir / ".extract"
            tmp.mkdir()
            try:
                _safe_extract(archive, tmp)
                root = _flatten_root(tmp)
                for entry in root.iterdir():
                    target = server / entry.name
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    shutil.move(str(entry), str(target))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        pack = ModpackConfig(
            source="local",
            project=info.name or name,
            version_id="",
            version_number=pack_version,
            mode="server",
            file_name=destination.name,
            file_kind=kind,
            sha512=digest,
            url=pack_url,
            resolved={
                "minecraft": mc_version,
                "loader": loader,
                "loader_version": loader_version or "",
            },
        )
        instance = Instance(
            name=name.strip(),
            slug=slug,
            kind="modpack",
            loader=loader,
            mc_version=mc_version.strip(),
            loader_version=loader_version or None,
            memory=memory.upper(),
            tz=tz or default_tz(),
            java_version=java_for_mc(mc_version),
            eula=True,
            rcon_password=new_rcon_password(),
            created_at=utc_now(),
            modpack=pack,
        )
        instance.validate()
        layout.backups_for(slug).mkdir(parents=True, exist_ok=True)
        save_instance(layout, instance)
        write_compose(layout, instance)
    except Exception:
        shutil.rmtree(instance_dir, ignore_errors=True)
        raise
    return instance
