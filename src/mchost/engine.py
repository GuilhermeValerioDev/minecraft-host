"""Docker/Podman and Compose operations.

The CLI uses these methods synchronously. The TUI can call them from a worker
thread (``asyncio.to_thread``) without needing a second implementation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .config import Instance, instance_slugs
from .errors import EngineError
from .paths import Layout


@dataclass(frozen=True)
class EngineInfo:
    kind: str
    binary: str
    compose: tuple[str, ...]


def _runs(argv: Sequence[str], timeout: int = 20) -> bool:
    try:
        result = subprocess.run(
            list(argv),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_engine() -> EngineInfo:
    """Detect a working engine + compose provider, preferring Podman."""
    podman = shutil.which("podman")
    if podman and _runs([podman, "compose", "version"]):
        return EngineInfo("podman", podman, (podman, "compose"))

    podman_compose = shutil.which("podman-compose")
    if podman and podman_compose and _runs([podman_compose, "--version"]):
        return EngineInfo("podman", podman, (podman_compose,))

    docker = shutil.which("docker")
    if docker and _runs([docker, "compose", "version"]):
        return EngineInfo("docker", docker, (docker, "compose"))

    docker_compose = shutil.which("docker-compose")
    if docker_compose and _runs([docker_compose, "--version"]):
        return EngineInfo("docker", docker or docker_compose, (docker_compose,))

    raise EngineError(
        "no working container engine found. Install Docker with the Compose "
        "plugin, or Podman with podman-compose."
    )


class Engine:
    def __init__(self, layout: Layout, info: EngineInfo | None = None) -> None:
        self.layout = layout
        self.info = info or detect_engine()

    @property
    def name(self) -> str:
        return self.info.kind

    def ping(self) -> bool:
        """Return True when the engine daemon is reachable."""
        return _runs([self.info.binary, "info"], timeout=15)

    def container_name(self, slug: str) -> str:
        return f"mc-{slug}"

    def project_name(self, slug: str) -> str:
        return f"mc-{slug}"

    def compose_command(self, slug: str, *args: str) -> list[str]:
        return [
            *self.info.compose,
            "-p",
            self.project_name(slug),
            "-f",
            str(self.layout.compose_file(slug)),
            *args,
        ]

    def _run(
        self,
        argv: Sequence[str],
        *,
        slug: str | None = None,
        check: bool = True,
        capture: bool = True,
        inherit: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, object] = {"cwd": str(self.layout.root)}
        if slug is not None:
            kwargs["cwd"] = str(self.layout.instance_dir(slug))
        if inherit:
            result = subprocess.run(list(argv), check=False, **kwargs)
        else:
            result = subprocess.run(
                list(argv),
                check=False,
                text=True,
                capture_output=capture,
                **kwargs,
            )
        if check and result.returncode != 0:
            detail = ""
            if not inherit:
                detail = (result.stderr or result.stdout or "").strip()
            if len(detail) > 800:
                detail = detail[-800:]
            command = " ".join(argv)
            raise EngineError(
                f"command failed ({result.returncode}): {command}"
                + (f"\n{detail}" if detail else "")
            )
        return result

    # -- compose lifecycle -------------------------------------------------

    def up(self, slug: str) -> None:
        self._run(self.compose_command(slug, "up", "-d", "--remove-orphans"), slug=slug)

    def down(self, slug: str) -> None:
        self._run(self.compose_command(slug, "down", "--remove-orphans"), slug=slug)

    def compose(self, slug: str, *args: str, inherit: bool = False) -> subprocess.CompletedProcess[str]:
        return self._run(
            self.compose_command(slug, *args), slug=slug, inherit=inherit
        )

    # -- inspection --------------------------------------------------------

    def _ps(self, slug: str) -> list[dict[str, object]]:
        result = self._run(
            [
                self.info.binary,
                "ps",
                "-a",
                "--filter",
                f"name={self.container_name(slug)}",
                "--format",
                "json",
            ]
        )
        output = (result.stdout or "").strip()
        if not output:
            return []
        try:
            if output.startswith("["):
                data = json.loads(output)
                return data if isinstance(data, list) else []
            return [json.loads(line) for line in output.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise EngineError(f"could not parse container list: {exc}") from exc

    @staticmethod
    def _container_names(item: dict[str, object]) -> list[str]:
        names = item.get("Names") or item.get("names") or []
        if isinstance(names, str):
            return [names.lstrip("/")]
        if isinstance(names, list):
            return [str(name).lstrip("/") for name in names]
        return []

    @staticmethod
    def _container_state(item: dict[str, object]) -> str:
        state = item.get("State") or item.get("state")
        if state:
            return str(state)
        status = item.get("Status") or item.get("status") or ""
        return str(status).split()[0].lower() if status else "unknown"

    def state(self, slug: str) -> str:
        wanted = self.container_name(slug)
        for item in self._ps(slug):
            if wanted in self._container_names(item):
                return self._container_state(item)
        return "missing"

    def is_running(self, slug: str) -> bool:
        # A restarting container is still "in use": its ports are held, and
        # the UI should keep tailing its logs.
        return self.state(slug).lower() in ("running", "restarting")

    def started_at(self, slug: str) -> datetime | None:
        """Return the container's last start time, if it is available."""
        result = self._run(
            [
                self.info.binary,
                "inspect",
                "--format",
                "{{.State.StartedAt}}",
                self.container_name(slug),
            ],
            check=False,
        )
        text = (result.stdout or "").strip()
        if result.returncode != 0 or not text or text.startswith("0001-01-01"):
            return None
        text = text.replace("Z", "+00:00")
        # Docker timestamps can have nanoseconds; Python accepts microseconds.
        text = re.sub(r"(\.\d{6})\d+", r"\1", text)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def running_slugs(self) -> list[str]:
        return [slug for slug in instance_slugs(self.layout) if self.is_running(slug)]

    # -- interaction -------------------------------------------------------

    def logs(self, slug: str, *, tail: int = 200, follow: bool = True) -> None:
        argv = [self.info.binary, "logs", "--tail", str(tail)]
        if follow:
            argv.append("-f")
        argv.append(self.container_name(slug))
        self._run(argv, inherit=True)

    def exec(self, slug: str, argv: Sequence[str], *, tty: bool = False) -> None:
        flags = ["-it"] if tty else ["-i"]
        self._run(
            [self.info.binary, "exec", *flags, self.container_name(slug), *argv],
            inherit=True,
        )

    def exec_capture(self, slug: str, argv: Sequence[str]) -> str:
        """Run a command in the container and return its combined output."""
        result = self._run(
            [self.info.binary, "exec", "-i", self.container_name(slug), *argv],
            slug=slug,
        )
        parts = [part for part in (result.stdout, result.stderr) if part]
        return "\n".join(part.rstrip() for part in parts).strip()


def _foreign_owner(path: Path, uid: int, limit: int = 500) -> bool:
    seen = 0
    stack = [path]
    while stack and seen < limit:
        current = stack.pop()
        try:
            stat = current.stat()
        except OSError:
            continue
        if stat.st_uid != uid:
            return True
        seen += 1
        if current.is_dir():
            try:
                stack.extend(current.iterdir())
            except OSError:
                pass
    return False


def server_ownership_warning(layout: Layout, instance: Instance) -> str | None:
    server = layout.server_dir_for(instance)
    if not server.is_dir():
        return None
    if not _foreign_owner(server, os.getuid()):
        return None
    return (
        f"{server} contains files not owned by the current user; run "
        f"'sudo chown -R {os.getuid()}:{os.getgid()} {server}'"
    )


def restore_ownership(layout: Layout, instance: Instance) -> str | None:
    """Best-effort chown of the data directory after a shutdown.

    Returns a warning string when ownership could not be restored, otherwise
    ``None``.
    """
    if not instance.restore_ownership:
        return None

    server = layout.server_dir_for(instance)
    if not server.is_dir():
        return None

    uid, gid = os.getuid(), os.getgid()
    if not _foreign_owner(server, uid):
        return None

    if os.geteuid() == 0:
        for root, dirs, files in os.walk(server):
            for name in (root, *dirs, *files):
                try:
                    os.chown(os.path.join(root, name) if name != root else root, uid, gid)
                except OSError:
                    pass
        return None

    sudo = shutil.which("sudo")
    if not sudo:
        return (
            f"could not restore ownership of {server}: sudo not found; run "
            f"'chown -R {uid}:{gid} {server}' as root"
        )

    result = subprocess.run(
        [sudo, "-n", "chown", "-R", f"{uid}:{gid}", str(server)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return (
            f"could not restore ownership of {server}: "
            f"{result.stderr.strip() or 'sudo failed'}; run "
            f"'sudo chown -R {uid}:{gid} {server}'"
        )
    return None
