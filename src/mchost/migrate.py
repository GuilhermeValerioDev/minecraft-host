"""Import the original single-instance `.env` configuration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .compose import write_compose
from .config import (
    LOADERS,
    Instance,
    default_tz,
    instance_slugs,
    java_for_mc,
    new_rcon_password,
    save_instance,
    slugify,
    utc_now,
)
from .errors import ConfigError
from .paths import Layout


@dataclass
class MigrationResult:
    instance: Instance
    warnings: list[str] = field(default_factory=list)

def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key.strip()] = value
    return values


def migrate(layout: Layout) -> MigrationResult:
    """Read `.env`, create ``instances/<slug>/instance.toml``, keep the data."""
    if not layout.legacy_env.is_file():
        raise ConfigError(f"no legacy configuration found at {layout.legacy_env}")

    env = parse_env_file(layout.legacy_env)
    legacy_name = env.get("INSTANCE", "").strip()
    if not legacy_name:
        raise ConfigError("legacy .env does not set INSTANCE")

    loader = env.get("TYPE", "VANILLA").strip().lower()
    if loader not in LOADERS:
        raise ConfigError(
            f"legacy TYPE={env.get('TYPE')!r} is not one of {', '.join(LOADERS)}"
        )

    warnings: list[str] = []
    mc_version = env.get("VERSION", "").strip()
    if not mc_version:
        mc_version = "LATEST"
        warnings.append("legacy .env had no VERSION; set an exact version in instance.toml")

    memory = env.get("MEMORY", "4G").strip().upper() or "4G"
    tz = env.get("TZ", "").strip() or default_tz()
    rcon_password = env.get("RCON_PASSWORD", "").strip() or new_rcon_password()

    if Path(legacy_name).name != legacy_name or legacy_name in (".", ".."):
        raise ConfigError(f"legacy INSTANCE={legacy_name!r} is not a safe directory name")

    slug = slugify(legacy_name)
    layout.instance_dir(slug).mkdir(parents=True, exist_ok=True)

    # Keep the existing data path exactly where it was. Renaming it would make
    # Syncthing see a delete/create pair and potentially re-download the world.
    legacy_server = layout.instances_dir / legacy_name / "server"
    legacy_server.mkdir(parents=True, exist_ok=True)
    data_dir = None if legacy_name == slug else f"instances/{legacy_name}/server"

    layout.pack_dir(slug).mkdir(parents=True, exist_ok=True)
    layout.backups_for(slug).mkdir(parents=True, exist_ok=True)

    instance = Instance(
        name=legacy_name,
        slug=slug,
        kind="server",
        loader=loader,
        mc_version=mc_version,
        loader_version=None,
        memory=memory,
        tz=tz,
        java_version=java_for_mc(mc_version),
        eula=True,
        rcon_password=rcon_password,
        port=25565,
        rcon_port=25575,
        bluemap_port=8100,
        created_at=utc_now(),
        data_dir=data_dir,
    )
    save_instance(layout, instance)
    write_compose(layout, instance)

    layout.state_dir.mkdir(parents=True, exist_ok=True)
    layout.migration_marker.write_text(
        json.dumps(
            {
                "slug": slug,
                "source": str(layout.legacy_env),
                "migrated_at": utc_now(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if instance.loader_version is None and loader not in ("vanilla", "paper"):
        warnings.append(
            f"{loader} loader version is not pinned yet; choose one from the "
            "dashboard's Version button or with 'mchost versions' before the "
            "first start"
        )
    if mc_version == "LATEST":
        warnings.append("Minecraft version is not pinned; the server will use the latest")

    return MigrationResult(instance=instance, warnings=warnings)


def already_migrated(layout: Layout) -> bool:
    return layout.migration_marker.is_file()


def maybe_auto_migrate(layout: Layout) -> MigrationResult | None:
    """Import a legacy `.env` on first use, but never overwrite instances."""
    if not layout.legacy_env.is_file():
        return None
    if layout.migration_marker.is_file():
        return None
    if instance_slugs(layout):
        return None
    return migrate(layout)
