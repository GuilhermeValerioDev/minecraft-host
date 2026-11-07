"""Read-only health checks for the host directory."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import MEMORY_RE, scan_instances
from .engine import Engine, detect_engine, server_ownership_warning
from .errors import EngineError, MchostError
from .paths import Layout

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    status: str
    title: str
    detail: str


def parse_memory(value: str) -> int:
    """Return memory in bytes from a value such as ``8G`` or ``2048M``."""
    match = MEMORY_RE.match(value.upper())
    if not match:
        return 0
    amount = float(value[:-1])
    unit = value[-1].upper()
    return int(amount * (1024**3 if unit == "G" else 1024**2))


def host_memory() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _port_available(port: int, host: str = "0.0.0.0") -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _rcon_exposed(layout: Layout, slug: str) -> bool:
    path = layout.compose_file(slug)
    if not path.is_file():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return False
        ports = data.get("services", {}).get("minecraft", {}).get("ports", [])
        if not isinstance(ports, list):
            return False
    except (OSError, yaml.YAMLError):
        return False
    for entry in ports:
        text = str(entry)
        if text.endswith(":25575") and not text.startswith("127.0.0.1:"):
            return True
    return False


def run_checks(layout: Layout) -> list[Check]:
    checks: list[Check] = []

    try:
        info = detect_engine()
        engine = Engine(layout, info)
        checks.append(
            Check(
                OK,
                "Container engine",
                f"{info.kind} with {' '.join(info.compose)}",
            )
        )
        if engine.ping():
            checks.append(Check(OK, "Engine daemon", f"{info.kind} is reachable"))
        else:
            checks.append(
                Check(FAIL, "Engine daemon", f"cannot reach the {info.kind} daemon")
            )
    except EngineError as exc:
        engine = None
        checks.append(Check(FAIL, "Container engine", str(exc)))

    if os.access(layout.root, os.W_OK):
        checks.append(Check(OK, "Host directory", str(layout.root)))
    else:
        checks.append(Check(FAIL, "Host directory", f"{layout.root} is not writable"))

    if layout.legacy_env.is_file():
        if layout.migration_marker.is_file():
            checks.append(
                Check(OK, "Legacy .env", "present but already imported; not used by mchost")
            )
        else:
            checks.append(
                Check(
                    WARN,
                    "Legacy .env",
                    "found and not imported yet; run 'mchost migrate'",
                )
            )

    instances, errors = scan_instances(layout)
    checks.append(
        Check(
            OK if not errors else FAIL,
            "Instances",
            f"{len(instances)} configured"
            + (f", {len(errors)} unreadable" if errors else ""),
        )
    )
    for slug, message in errors:
        checks.append(Check(FAIL, f"Instance {slug}", message))

    total_memory = host_memory()
    for instance in instances:
        if total_memory and parse_memory(instance.memory) > total_memory * 0.85:
            checks.append(
                Check(
                    WARN,
                    f"Memory · {instance.slug}",
                    f"{instance.memory} is more than 85% of host RAM "
                    f"({total_memory / 1024**3:.1f}G)",
                )
            )

    for instance in instances:
        try:
            running = engine is not None and engine.is_running(instance.slug)
        except MchostError:
            running = False
        if running:
            continue
        for label, port, host in (
            ("game", instance.port, "0.0.0.0"),
            ("rcon", instance.rcon_port, "127.0.0.1"),
            ("bluemap", instance.bluemap_port, "0.0.0.0"),
        ):
            if port is None:
                continue
            if not _port_available(port, host):
                checks.append(
                    Check(
                        WARN,
                        f"Port · {instance.slug}",
                        f"{label} port {port} is already in use on {host}",
                    )
                )

        warning = server_ownership_warning(layout, instance)
        if warning:
            checks.append(Check(WARN, f"Ownership · {instance.slug}", warning))

        if _rcon_exposed(layout, instance.slug):
            checks.append(
                Check(
                    WARN,
                    f"RCON exposure · {instance.slug}",
                    "RCON is published beyond localhost; bind it to 127.0.0.1",
                )
            )

    return checks
