"""Create, list, restore, and prune server backups."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Instance
from .engine import Engine, restore_ownership
from .errors import MchostError
from .paths import Layout

EXCLUDED_FULL = {
    "logs",
    "crash-reports",
    "cache",
    ".cache",
    "libraries",
    "versions",
    ".mixin.out",
}
WORLD_DIRS = ("config", "defaultconfigs")
WORLD_FILES = (
    "server.properties",
    "eula.txt",
    "ops.json",
    "whitelist.json",
    "banned-ips.json",
    "banned-players.json",
    "usercache.json",
)


@dataclass
class Backup:
    slug: str
    backup_id: str
    path: Path
    manifest: dict[str, Any]

    @property
    def created_at(self) -> str:
        return str(self.manifest.get("created_at", ""))

    @property
    def kind(self) -> str:
        return str(self.manifest.get("kind", "world"))

    @property
    def size(self) -> int:
        value = self.manifest.get("size")
        if isinstance(value, int):
            return value
        archive = self.archive_path
        try:
            return archive.stat().st_size
        except OSError:
            return 0

    @property
    def archive_path(self) -> Path:
        return self.path / str(self.manifest.get("archive", "backup.tar.gz"))

    @property
    def sha256(self) -> str:
        return str(self.manifest.get("sha256", ""))


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _backup_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _select_entries(server: Path, kind: str) -> list[str]:
    if kind == "world":
        entries: list[str] = []
        for entry in server.iterdir():
            if entry.is_dir() and entry.name.startswith("world"):
                entries.append(entry.name)
        for name in (*WORLD_DIRS, *WORLD_FILES):
            if (server / name).exists():
                entries.append(name)
        return sorted(entries)
    if kind == "full":
        return sorted(
            entry.name
            for entry in server.iterdir()
            if entry.name not in EXCLUDED_FULL
        )
    raise MchostError("backup kind must be 'world' or 'full'")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_archive(
    server: Path, destination: Path, entries: list[str]
) -> None:
    tmp = destination.with_name(destination.name + ".tmp")
    try:
        with tarfile.open(tmp, "w:gz") as archive:
            for name in entries:
                source = server / name
                if source.exists() or source.is_symlink():
                    archive.add(source, arcname=name, recursive=True)
        tmp.replace(destination)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def create_backup(
    layout: Layout,
    instance: Instance,
    *,
    kind: str = "world",
    keep: int | None = None,
    engine: Engine | None = None,
) -> Backup:
    """Create a consistent backup and optionally prune old ones."""
    if kind not in ("world", "full"):
        raise MchostError("backup kind must be 'world' or 'full'")
    server = layout.server_dir_for(instance)
    if not server.is_dir():
        raise MchostError(f"server directory does not exist: {server}")
    entries = _select_entries(server, kind)
    if not entries:
        raise MchostError(f"nothing to back up in {server}")

    running = engine is not None and engine.is_running(instance.slug)
    saves_disabled = False
    if running:
        assert engine is not None
        try:
            engine.exec_capture(instance.slug, ["rcon-cli", "save-off"])
            saves_disabled = True
            engine.exec_capture(instance.slug, ["rcon-cli", "save-all flush"])
            time.sleep(1.5)
        except MchostError as exc:
            if saves_disabled:
                try:
                    engine.exec_capture(instance.slug, ["rcon-cli", "save-on"])
                except MchostError:
                    pass
            raise MchostError(
                f"could not quiesce the server for a backup: {exc}. "
                "Stop the instance and try again."
            ) from exc

    backup_id = _backup_id()
    root = layout.backups_for(instance.slug)
    root.mkdir(parents=True, exist_ok=True)
    path = root / backup_id
    suffix = 1
    while path.exists():
        path = root / f"{backup_id}-{suffix}"
        suffix += 1
    path.mkdir(parents=True)

    try:
        archive = path / "backup.tar.gz"
        _write_archive(server, archive, entries)
        size = archive.stat().st_size
        digest = _sha256(archive)
        manifest: dict[str, Any] = {
            "id": path.name,
            "slug": instance.slug,
            "name": instance.name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "archive": archive.name,
            "size": size,
            "sha256": digest,
            "included": entries,
            "loader": instance.loader,
            "mc_version": instance.mc_version,
            "loader_version": instance.loader_version,
            "instance": {
                key: value
                for key, value in instance.to_dict().items()
                if key != "rcon_password"
            },
        }
        (path / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        config_path = layout.config_file(instance.slug)
        if config_path.is_file():
            shutil.copy2(config_path, path / "instance.toml")
    except Exception:
        shutil.rmtree(path, ignore_errors=True)
        raise
    finally:
        if saves_disabled and engine is not None:
            try:
                engine.exec_capture(instance.slug, ["rcon-cli", "save-on"])
            except MchostError:
                pass

    backup = Backup(instance.slug, path.name, path, manifest)
    if keep is None:
        keep = instance.backup_keep
    if keep and keep > 0:
        prune_backups(layout, instance.slug, keep)
    return backup


def list_backups(layout: Layout, slug: str) -> list[Backup]:
    root = layout.backups_for(slug)
    if not root.is_dir():
        return []
    backups: list[Backup] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        backups.append(Backup(slug, child.name, child, manifest))
    backups.sort(
        key=lambda backup: (backup.created_at, backup.backup_id), reverse=True
    )
    return backups


def get_backup(layout: Layout, slug: str, reference: str) -> Backup:
    backups = list_backups(layout, slug)
    if not backups:
        raise MchostError(f"{slug} has no backups")
    if reference == "latest":
        return backups[0]
    for backup in backups:
        if backup.backup_id == reference or backup.path.name == reference:
            return backup
    raise MchostError(f"no backup {reference!r} for {slug}")


def delete_backup(layout: Layout, slug: str, reference: str) -> Backup:
    backup = get_backup(layout, slug, reference)
    shutil.rmtree(backup.path)
    return backup


def prune_backups(layout: Layout, slug: str, keep: int) -> list[str]:
    if keep < 1:
        raise MchostError("keep must be at least 1")
    backups = list_backups(layout, slug)
    removed: list[str] = []
    for backup in backups[keep:]:
        shutil.rmtree(backup.path, ignore_errors=True)
        removed.append(backup.backup_id)
    return removed


def restore_backup(
    layout: Layout,
    instance: Instance,
    backup: Backup,
    *,
    engine: Engine | None = None,
    start_after: bool = False,
) -> None:
    """Replace the instance data with the backup contents."""
    archive_path = backup.archive_path
    if not archive_path.is_file():
        raise MchostError(f"backup archive is missing: {archive_path}")
    if backup.sha256:
        actual = _sha256(archive_path)
        if actual != backup.sha256:
            raise MchostError(
                f"backup checksum mismatch (expected {backup.sha256}, got {actual})"
            )

    running = engine is not None and engine.is_running(instance.slug)
    if running:
        assert engine is not None
        engine.down(instance.slug)
        restore_ownership(layout, instance)

    server = layout.server_dir_for(instance)
    server.mkdir(parents=True, exist_ok=True)
    tmp = server.parent / f".restore-{backup.backup_id}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            try:
                archive.extractall(tmp, filter="data")
            except TypeError:
                archive.extractall(tmp)
        for entry in tmp.iterdir():
            target = server / entry.name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            shutil.move(str(entry), str(target))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if start_after and engine is not None:
        engine.up(instance.slug)
