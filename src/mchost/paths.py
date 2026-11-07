"""Locating and naming the files that make up a host directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Instance

INSTANCE_CONFIG = "instance.toml"
COMPOSE_FILE = "compose.yaml"
LEGACY_ENV = ".env"
STATE_DIR = ".mchost"


def find_root(start: Path | None = None) -> Path:
    """Find the host directory that owns ``instances/``.

    Walk up from the current directory looking for an ``instances`` folder so
    that running ``mchost`` from a subdirectory still works. If nothing is
    found, the starting directory is used and becomes the host directory.
    """
    override = os.environ.get("MCHOST_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "instances").is_dir():
            return candidate
    return current


@dataclass(frozen=True)
class Layout:
    """Paths for one host directory."""

    root: Path

    @property
    def instances_dir(self) -> Path:
        return self.root / "instances"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def legacy_env(self) -> Path:
        return self.root / LEGACY_ENV

    @property
    def migration_marker(self) -> Path:
        return self.state_dir / "migrated.json"

    def ensure(self) -> None:
        self.instances_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Keep the empty directories in git even after they are synced.
        for directory in (self.instances_dir, self.backups_dir):
            (directory / ".gitkeep").touch(exist_ok=True)

    def instance_dir(self, slug: str) -> Path:
        return self.instances_dir / slug

    def config_file(self, slug: str) -> Path:
        return self.instance_dir(slug) / INSTANCE_CONFIG

    def compose_file(self, slug: str) -> Path:
        return self.instance_dir(slug) / COMPOSE_FILE

    def server_dir(self, slug: str) -> Path:
        return self.instance_dir(slug) / "server"

    def server_dir_for(self, instance: "Instance") -> Path:
        """The data directory, which may live outside the instance folder.

        Legacy imports keep their original ``instances/<name>/server`` path so
        that Syncthing does not see a rename and re-download the world.
        """
        if instance.data_dir:
            return (self.root / instance.data_dir).resolve()
        return self.server_dir(instance.slug)

    def compose_data_source(self, instance: "Instance") -> str:
        server = self.server_dir_for(instance)
        relative = os.path.relpath(server, self.instance_dir(instance.slug).resolve())
        return relative if relative.startswith(".") else f"./{relative}"

    def pack_dir(self, slug: str) -> Path:
        return self.instance_dir(slug) / "pack"

    def backups_for(self, slug: str) -> Path:
        return self.backups_dir / slug


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "mchost"
