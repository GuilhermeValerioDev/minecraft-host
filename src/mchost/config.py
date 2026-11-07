"""Instance configuration: the source of truth for every server."""

from __future__ import annotations

import os
import re
import secrets
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomli_w

from .errors import ConfigError
from .paths import Layout

LOADERS = ("vanilla", "paper", "fabric", "quilt", "forge", "neoforge")
LOADER_LABELS = {
    "vanilla": "Vanilla",
    "paper": "Paper",
    "fabric": "Fabric",
    "quilt": "Quilt",
    "forge": "Forge",
    "neoforge": "NeoForge",
}
KINDS = ("server", "modpack")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MEMORY_RE = re.compile(r"^\d+(?:\.\d+)?[MG]$")


def slugify(value: str) -> str:
    """Turn a display name into a filesystem/container safe slug."""
    value = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "-", value
    )
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value[:63] or "server"


def java_for_mc(mc_version: str) -> int:
    """Choose the Java major version itzg images need for a Minecraft version."""
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", mc_version.strip())
    if not match:
        return 21
    major, minor, patch = (int(part) if part else 0 for part in match.groups())
    if major >= 26:
        # Minecraft's 26.x line requires Java 25 (class file 69).
        return 25
    if major == 1:
        if minor <= 16:
            return 8
        if minor < 20 or (minor == 20 and patch <= 4):
            return 17
    return 21


def default_tz() -> str:
    from_env = os.environ.get("TZ", "").strip()
    if from_env:
        return from_env
    try:
        from_file = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if from_file:
            return from_file
    except OSError:
        pass
    return "UTC"


def new_rcon_password() -> str:
    return secrets.token_urlsafe(18)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ModpackConfig:
    source: str = "modrinth"
    project: str = ""
    version_id: str = ""
    version_number: str = ""
    mode: str = "auto"
    file_name: str = ""
    file_kind: str = ""
    sha512: str = ""
    url: str = ""
    resolved: dict[str, str] = field(default_factory=dict)

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.source not in ("modrinth", "local"):
            issues.append("modpack.source must be 'modrinth' or 'local'")
        if self.mode not in ("auto", "server", "full"):
            issues.append("modpack.mode must be auto, server, or full")
        if self.file_kind and self.file_kind not in ("mrpack", "zip", "tar"):
            issues.append("modpack.file_kind must be mrpack, zip, or tar")
        return issues

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": self.source,
            "project": self.project,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "mode": self.mode,
        }
        for key in ("file_name", "file_kind", "sha512", "url"):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.resolved:
            data["resolved"] = dict(self.resolved)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModpackConfig:
        resolved = data.get("resolved") or {}
        if not isinstance(resolved, dict):
            raise ConfigError("[modpack].resolved must be a table")
        return cls(
            source=str(data.get("source", "modrinth")),
            project=str(data.get("project", "")),
            version_id=str(data.get("version_id", "")),
            version_number=str(data.get("version_number", "")),
            mode=str(data.get("mode", "auto")),
            file_name=str(data.get("file_name", "")),
            file_kind=str(data.get("file_kind", "")),
            sha512=str(data.get("sha512", "")),
            url=str(data.get("url", "")),
            resolved={str(k): str(v) for k, v in resolved.items()},
        )


@dataclass
class Instance:
    name: str
    slug: str
    kind: str = "server"
    loader: str = "vanilla"
    mc_version: str = "LATEST"
    loader_version: str | None = None
    memory: str = "4G"
    tz: str = "UTC"
    java_version: int | None = None
    eula: bool = True
    rcon_password: str = ""
    port: int = 25565
    rcon_port: int = 25575
    bluemap_port: int | None = 8100
    motd: str = ""
    created_at: str = ""
    restore_ownership: bool = True
    backup_keep: int = 5
    data_dir: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    modpack: ModpackConfig | None = None

    @property
    def java(self) -> int:
        return self.java_version or java_for_mc(self.mc_version)

    @property
    def version_label(self) -> str:
        if self.kind == "modpack" and self.modpack:
            return f"{self.modpack.project} {self.modpack.version_number}".strip()
        loader = LOADER_LABELS.get(self.loader, self.loader)
        if self.loader_version:
            return f"{loader} {self.loader_version} · MC {self.mc_version}"
        return f"{loader} · MC {self.mc_version}"

    @property
    def sidebar_label(self) -> str:
        """A compact label that fits the sidebar without wrapping."""
        if self.kind == "modpack" and self.modpack:
            return f"{self.modpack.project} {self.modpack.version_number}".strip()
        loader = LOADER_LABELS.get(self.loader, self.loader)
        return f"{loader} · MC {self.mc_version}"

    def validate(self) -> None:
        issues: list[str] = []

        if not self.name.strip():
            issues.append("name cannot be empty")
        if not SLUG_RE.match(self.slug):
            issues.append(
                "slug must be 1-63 lowercase letters, digits, or dashes "
                f"(got {self.slug!r})"
            )
        if self.kind not in KINDS:
            issues.append(f"kind must be one of {', '.join(KINDS)}")
        if self.loader not in LOADERS:
            issues.append(f"loader must be one of {', '.join(LOADERS)}")
        if not self.mc_version.strip():
            issues.append("mc_version cannot be empty")
        if not MEMORY_RE.match(self.memory.upper()):
            issues.append(f"memory must look like 4G or 2048M (got {self.memory!r})")
        if not self.tz.strip():
            issues.append("tz cannot be empty")
        if not 1 <= self.backup_keep <= 100:
            issues.append("backup_keep must be between 1 and 100")
        if self.java_version is not None and self.java_version not in (8, 11, 17, 21, 25):
            issues.append("java_version must be one of 8, 11, 17, 21, 25")

        for label, value in (("port", self.port), ("rcon_port", self.rcon_port)):
            if not 1 <= value <= 65535:
                issues.append(f"{label} must be between 1 and 65535")
        if self.bluemap_port is not None and not 1 <= self.bluemap_port <= 65535:
            issues.append("bluemap_port must be between 1 and 65535 or omitted")

        used = [("port", self.port), ("rcon_port", self.rcon_port)]
        if self.bluemap_port is not None:
            used.append(("bluemap_port", self.bluemap_port))
        values = [value for _, value in used]
        if len(set(values)) != len(values):
            issues.append("port, rcon_port, and bluemap_port must be distinct")

        if self.kind == "modpack":
            if self.modpack is None:
                issues.append("modpack instances need a [modpack] section")
            else:
                issues.extend(self.modpack.validate())
        elif self.modpack is not None:
            issues.append("[modpack] can only be used when kind = 'modpack'")

        if self.data_dir is not None:
            data_path = Path(self.data_dir)
            if data_path.is_absolute():
                issues.append("data_dir must be relative to the host directory")
            if ".." in data_path.parts:
                issues.append("data_dir cannot contain '..'")
            if not data_path.parts:
                issues.append("data_dir cannot be the host root")

        if not self.eula:
            issues.append("eula must be true to start a Minecraft server")
        if not self.rcon_password:
            issues.append("rcon_password cannot be empty")

        if issues:
            raise ConfigError("; ".join(issues))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "slug": self.slug,
            "kind": self.kind,
            "loader": self.loader,
            "mc_version": self.mc_version,
            "memory": self.memory.upper(),
            "tz": self.tz,
            "java_version": self.java_version if self.java_version is not None else self.java,
            "eula": self.eula,
            "rcon_password": self.rcon_password,
            "port": self.port,
            "rcon_port": self.rcon_port,
            "created_at": self.created_at or utc_now(),
            "restore_ownership": self.restore_ownership,
            "backup_keep": self.backup_keep,
        }
        if self.loader_version:
            data["loader_version"] = self.loader_version
        if self.bluemap_port is not None:
            data["bluemap_port"] = self.bluemap_port
        if self.motd:
            data["motd"] = self.motd
        if self.data_dir:
            data["data_dir"] = self.data_dir
        if self.extra_env:
            data["extra_env"] = dict(self.extra_env)
        if self.modpack is not None:
            data["modpack"] = self.modpack.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], slug: str) -> Instance:
        if not isinstance(data, dict):
            raise ConfigError("instance configuration must be a table")

        extra = data.get("extra_env") or {}
        if not isinstance(extra, dict):
            raise ConfigError("extra_env must be a table")
        extra_env = {str(key): str(value) for key, value in extra.items()}

        modpack_data = data.get("modpack")
        modpack = None
        if modpack_data is not None:
            if not isinstance(modpack_data, dict):
                raise ConfigError("modpack must be a table")
            modpack = ModpackConfig.from_dict(modpack_data)

        java_version = data.get("java_version")
        if java_version is not None:
            try:
                java_version = int(java_version)
            except (TypeError, ValueError) as exc:
                raise ConfigError("java_version must be a number") from exc

        bluemap_port = data.get("bluemap_port", 8100)
        if bluemap_port is not None:
            try:
                bluemap_port = int(bluemap_port)
            except (TypeError, ValueError) as exc:
                raise ConfigError("bluemap_port must be a number") from exc

        try:
            port = int(data.get("port", 25565))
            rcon_port = int(data.get("rcon_port", 25575))
            backup_keep = int(data.get("backup_keep", 5))
        except (TypeError, ValueError) as exc:
            raise ConfigError("port, rcon_port, and backup_keep must be numbers") from exc

        instance = cls(
            name=str(data.get("name", slug)),
            slug=str(data.get("slug", slug)),
            kind=str(data.get("kind", "modpack" if modpack else "server")),
            loader=str(data.get("loader", "vanilla")).lower(),
            mc_version=str(data.get("mc_version", "LATEST")),
            loader_version=(
                str(data["loader_version"]) if data.get("loader_version") else None
            ),
            memory=str(data.get("memory", "4G")).upper(),
            tz=str(data.get("tz", "UTC")),
            java_version=java_version,
            eula=bool(data.get("eula", True)),
            rcon_password=str(data.get("rcon_password", "")),
            port=port,
            rcon_port=rcon_port,
            bluemap_port=bluemap_port,
            motd=str(data.get("motd", "")),
            created_at=str(data.get("created_at", "")),
            restore_ownership=bool(data.get("restore_ownership", True)),
            backup_keep=backup_keep,
            data_dir=str(data["data_dir"]) if data.get("data_dir") else None,
            extra_env=extra_env,
            modpack=modpack,
        )
        instance.validate()
        return instance


def load_instance(layout: Layout, slug: str) -> Instance:
    path = layout.config_file(slug)
    if not path.is_file():
        raise ConfigError(
            f"instance {slug!r} does not exist (expected {path})"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return Instance.from_dict(data, slug)


def save_instance(layout: Layout, instance: Instance) -> Path:
    instance.validate()
    path = layout.config_file(instance.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = tomli_w.dumps(instance.to_dict())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def instance_slugs(layout: Layout) -> list[str]:
    if not layout.instances_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in layout.instances_dir.iterdir()
        if entry.is_dir() and layout.config_file(entry.name).is_file()
    )


def scan_instances(layout: Layout) -> tuple[list[Instance], list[tuple[str, str]]]:
    instances: list[Instance] = []
    errors: list[tuple[str, str]] = []
    for slug in instance_slugs(layout):
        try:
            instances.append(load_instance(layout, slug))
        except ConfigError as exc:
            errors.append((slug, str(exc)))
    instances.sort(key=lambda item: item.name.casefold())
    return instances, errors


def list_instances(layout: Layout) -> list[Instance]:
    instances, errors = scan_instances(layout)
    if errors:
        slug, message = errors[0]
        raise ConfigError(f"could not read instance {slug!r}: {message}")
    return instances


def resolve_instance(layout: Layout, reference: str) -> Instance:
    reference = reference.strip()
    if layout.config_file(reference).is_file():
        return load_instance(layout, reference)

    wanted = slugify(reference)
    if layout.config_file(wanted).is_file():
        return load_instance(layout, wanted)

    instances, _ = scan_instances(layout)
    for instance in instances:
        if instance.name.casefold() == reference.casefold():
            return instance

    available = ", ".join(instance.slug for instance in instances) or "none"
    raise ConfigError(
        f"no instance matches {reference!r} (available: {available})"
    )
