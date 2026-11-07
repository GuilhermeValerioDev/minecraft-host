"""Install a user-provided CurseForge-style server pack archive."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

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
class LocalPackInfo:
    name: str
    pack_version: str
    mc_version: str | None
    loader: str | None
    loader_version: str | None
    notes: str


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

        tmp = instance_dir / ".extract"
        tmp.mkdir()
        try:
            _safe_extract(archive, tmp)
            root = _flatten_root(tmp)
            server = layout.server_dir(slug)
            server.mkdir(parents=True, exist_ok=True)
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
            version_number=info.pack_version,
            mode="server",
            file_name=destination.name,
            file_kind=kind,
            sha512=digest,
            url="",
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
