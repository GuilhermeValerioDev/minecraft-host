"""Instance lifecycle helpers that are not part of config parsing."""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import Instance, scan_instances
from .engine import Engine
from .errors import MchostError
from .paths import Layout


def delete_instance(
    layout: Layout, instance: Instance, *, engine: Engine | None = None
) -> list[Path]:
    """Stop the container and remove the instance, its data, and backups."""
    root = layout.root.resolve()
    instances_root = layout.instances_dir.resolve()
    backups_root = layout.backups_dir.resolve()
    instance_dir = layout.instance_dir(instance.slug).resolve()
    data_dir = layout.server_dir_for(instance).resolve()
    backups_dir = layout.backups_for(instance.slug).resolve()

    for path in (instance_dir, data_dir, backups_dir):
        if not path.is_relative_to(root):
            raise MchostError(f"refusing to delete a path outside the host: {path}")
        if path in (root, instances_root, backups_root):
            raise MchostError(f"refusing to delete a host directory: {path}")

    others, _errors = scan_instances(layout)
    for other in others:
        if other.slug == instance.slug:
            continue
        if layout.server_dir_for(other).resolve() == data_dir:
            raise MchostError(
                f"refusing to delete {data_dir}: it is shared with {other.slug}"
            )

    if engine is not None:
        try:
            state = engine.state(instance.slug)
            if state != "missing":
                engine.down(instance.slug)
        except MchostError as exc:
            raise MchostError(
                f"could not stop {instance.slug} before deleting: {exc}"
            ) from exc

    removed: list[Path] = []
    for path in (instance_dir, data_dir, backups_dir):
        if path.exists():
            shutil.rmtree(path)
            removed.append(path)

    # Legacy instances keep their data under instances/<LegacyName>/server.
    # Remove the now-empty wrapper but never the instances/ root itself.
    parent = data_dir.parent
    if (
        parent != instances_root
        and parent.is_dir()
        and parent.is_relative_to(instances_root)
    ):
        try:
            parent.rmdir()
            removed.append(parent)
        except OSError:
            pass

    return removed
