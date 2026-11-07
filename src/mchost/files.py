"""Small, testable filesystem operations for mods and configs."""

from __future__ import annotations

import os
from pathlib import Path

from .errors import MchostError

MAX_TEXT_BYTES = 512 * 1024
DISABLED_SUFFIX = ".disabled"


def is_mod_disabled(path: Path) -> bool:
    return path.name.endswith(DISABLED_SUFFIX)


def toggle_mod(path: Path) -> Path:
    """Disable a mod by appending .disabled, or enable it by removing it."""
    if not path.is_file():
        raise MchostError(f"not a file: {path}")
    if is_mod_disabled(path):
        target = path.with_name(path.name[: -len(DISABLED_SUFFIX)])
        if target.exists():
            raise MchostError(f"cannot enable {path.name}: {target.name} already exists")
    else:
        target = path.with_name(path.name + DISABLED_SUFFIX)
        if target.exists():
            raise MchostError(f"cannot disable {path.name}: {target.name} already exists")
    path.rename(target)
    return target


def delete_file(path: Path) -> None:
    if not path.is_file():
        raise MchostError(f"not a file: {path}")
    path.unlink()


def read_text_file(path: Path, *, max_bytes: int = MAX_TEXT_BYTES) -> str:
    if not path.is_file():
        raise MchostError(f"not a file: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MchostError(f"could not stat {path}: {exc}") from exc
    if size > max_bytes:
        raise MchostError(
            f"{path.name} is too large to edit ({size / 1024:.0f} KB; "
            f"limit {max_bytes / 1024:.0f} KB)"
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MchostError(f"{path.name} is not a UTF-8 text file") from exc
    except OSError as exc:
        raise MchostError(f"could not read {path}: {exc}") from exc


def write_text_file(path: Path, text: str) -> None:
    if not path.is_file():
        raise MchostError(f"not a file: {path}")
    temporary = path.with_name(path.name + ".tmp")
    try:
        mode = path.stat().st_mode & 0o777
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        raise MchostError(f"could not write {path}: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
