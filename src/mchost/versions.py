"""Resolve Minecraft and mod-loader versions from upstream metadata.

Everything is cached on disk for an hour, so opening the picker repeatedly is
cheap. Network calls are synchronous; the TUI runs them in a worker thread.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .errors import MchostError
from .paths import cache_dir

CACHE_TTL = 60 * 60
USER_AGENT = f"mchost/{__version__} (Minecraft server manager)"

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_PROJECT = "https://fill.papermc.io/v3/projects/paper"
PAPER_BUILDS = "https://fill.papermc.io/v3/projects/paper/versions/{mc}/builds"
FABRIC_GAME = "https://meta.fabricmc.net/v2/versions/game"
FABRIC_LOADERS = "https://meta.fabricmc.net/v2/versions/loader/{mc}"
QUILT_GAME = "https://meta.quiltmc.org/v3/versions/game"
QUILT_LOADERS = "https://meta.quiltmc.org/v3/versions/loader/{mc}"
QUILT_LOADERS_GLOBAL = "https://meta.quiltmc.org/v3/versions/loader"
NEOFORGE_VERSIONS = (
    "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
)
FORGE_METADATA = (
    "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
)
FORGE_PROMOTIONS = (
    "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
)


@dataclass(frozen=True)
class LoaderBuild:
    version: str
    stable: bool = True
    latest: bool = False
    recommended: bool = False

    @property
    def label(self) -> str:
        markers = []
        if self.recommended:
            markers.append("recommended")
        if self.latest:
            markers.append("latest")
        return f"{self.version} ({', '.join(markers)})" if markers else self.version


def version_key(value: str) -> tuple:
    tokens = re.findall(r"\d+|[A-Za-z]+", value.lower())
    return tuple((int(token), token) if token.isdigit() else (-1, token) for token in tokens)


def is_prerelease(value: str) -> bool:
    return bool(re.search(r"(?i)(alpha|beta|rc|pre|snapshot|dev)", value))


def _cache_file(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:120]
    directory = cache_dir() / "versions"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe}.json"


def _read_cache(key: str, *, ttl: int | None = CACHE_TTL) -> Any | None:
    path = _cache_file(key)
    try:
        if ttl is not None and time.time() - path.stat().st_mtime > ttl:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(key: str, data: Any) -> None:
    path = _cache_file(key)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _get_json(url: str, key: str, *, ttl: int = CACHE_TTL) -> Any:
    cached = _read_cache(key, ttl=ttl)
    if cached is not None:
        return cached
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        stale = _read_cache(key, ttl=None)
        if stale is not None:
            return stale
        raise MchostError(f"could not fetch {url}: {exc}") from exc
    _write_cache(key, data)
    return data


def _get_text(url: str, key: str, *, ttl: int = CACHE_TTL) -> str:
    path = _cache_file(key)
    try:
        if time.time() - path.stat().st_mtime <= ttl:
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
            follow_redirects=True,
        )
        response.raise_for_status()
        text = response.text
    except httpx.HTTPError as exc:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            raise MchostError(f"could not fetch {url}: {exc}") from exc
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return text


def _mojang_releases(include_snapshots: bool = False) -> list[str]:
    data = _get_json(MOJANG_MANIFEST, "mojang_manifest")
    versions = data.get("versions", [])
    return [
        item["id"]
        for item in versions
        if include_snapshots or item.get("type") == "release"
    ]


def _neoforge_prefix(mc_version: str) -> str:
    """Map a Minecraft version to the NeoForge maven prefix."""
    match = re.match(r"^1\.(\d+)(?:\.(\d+))?$", mc_version)
    if not match:
        return ""
    minor = int(match.group(1))
    patch = int(match.group(2) or 0)
    if (minor, patch) == (20, 1):
        return "47."
    return f"{minor}.{patch}."


def _neoforge_versions() -> list[str]:
    data = _get_json(NEOFORGE_VERSIONS, "neoforge_versions")
    return [str(item) for item in data.get("versions", [])]


def _forge_versions() -> list[str]:
    text = _get_text(FORGE_METADATA, "forge_metadata")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise MchostError(f"could not parse Forge metadata: {exc}") from exc
    return [node.text for node in root.findall(".//version") if node.text]


def _forge_promotions() -> dict[str, str]:
    data = _get_json(FORGE_PROMOTIONS, "forge_promotions")
    promos = data.get("promos", {})
    return {str(key): str(value) for key, value in promos.items()}


def supported_mc_versions(loader: str, *, include_snapshots: bool = False) -> list[str]:
    """Return Minecraft versions the loader supports, newest first."""
    if loader == "paper":
        data = _get_json(PAPER_PROJECT, "paper_project")
        versions: list[str] = []
        for group in data.get("versions", {}).values():
            versions.extend(str(item) for item in group)
        return sorted(
            [version for version in versions if not is_prerelease(version)],
            key=version_key,
            reverse=True,
        )

    if loader == "fabric":
        data = _get_json(FABRIC_GAME, "fabric_game")
        versions = [
            item["version"]
            for item in data
            if include_snapshots or item.get("stable", True)
        ]
        return sorted(versions, key=version_key, reverse=True)

    if loader == "quilt":
        data = _get_json(QUILT_GAME, "quilt_game")
        versions = [
            item["version"]
            for item in data
            if include_snapshots or item.get("stable", True)
        ]
        return sorted(versions, key=version_key, reverse=True)

    releases = _mojang_releases(include_snapshots)
    if loader == "vanilla":
        return releases

    if loader == "neoforge":
        available = _neoforge_versions()
        supported = []
        for mc_version in releases:
            prefix = _neoforge_prefix(mc_version)
            if prefix and any(version.startswith(prefix) for version in available):
                supported.append(mc_version)
        return supported

    if loader == "forge":
        available = _forge_versions()
        prefixes = {version.split("-", 1)[0] for version in available if "-" in version}
        return [mc_version for mc_version in releases if mc_version in prefixes]

    raise MchostError(f"unknown loader {loader!r}")


def loader_builds(loader: str, mc_version: str) -> list[LoaderBuild]:
    """Return loader builds for a Minecraft version, newest first."""
    if loader == "vanilla":
        return []

    if loader == "paper":
        data = _get_json(
            PAPER_BUILDS.format(mc=mc_version), f"paper_builds_{mc_version}"
        )
        if isinstance(data, dict) and data.get("ok") is False:
            raise MchostError(
                f"Paper has no builds for {mc_version}: "
                f"{data.get('message', 'unknown error')}"
            )
        builds = data if isinstance(data, list) else []
        ordered = sorted(
            builds, key=lambda item: int(item.get("id", 0)), reverse=True
        )
        result: list[LoaderBuild] = []
        recommended_marked = False
        for index, item in enumerate(ordered):
            channel = str(item.get("channel", "STABLE")).upper()
            stable = channel == "STABLE"
            recommended = stable and not recommended_marked
            if recommended:
                recommended_marked = True
            result.append(
                LoaderBuild(
                    version=str(item.get("id")),
                    stable=stable,
                    latest=index == 0,
                    recommended=recommended,
                )
            )
        return result

    if loader == "fabric":
        data = _get_json(
            FABRIC_LOADERS.format(mc=mc_version), f"fabric_loaders_{mc_version}"
        )
        versions = [
            (item.get("loader", {}).get("version"), item.get("loader", {}).get("stable", True))
            for item in data
        ]
        return _builds_from_versions(versions)

    if loader == "quilt":
        try:
            data = _get_json(
                QUILT_LOADERS.format(mc=mc_version), f"quilt_loaders_{mc_version}"
            )
            versions = [
                (item.get("loader", {}).get("version"), item.get("loader", {}).get("stable", True))
                for item in data
            ]
        except MchostError:
            data = _get_json(QUILT_LOADERS_GLOBAL, "quilt_loaders_global")
            versions = [
                (item.get("version"), item.get("stable", True)) for item in data
            ]
        return _builds_from_versions(versions)

    if loader == "neoforge":
        prefix = _neoforge_prefix(mc_version)
        versions = [
            version for version in _neoforge_versions() if version.startswith(prefix)
        ]
        return _builds_from_versions([(version, True) for version in versions])

    if loader == "forge":
        versions = [
            version.split("-", 1)[1]
            for version in _forge_versions()
            if version.startswith(f"{mc_version}-")
        ]
        promos = _forge_promotions()
        recommended = promos.get(f"{mc_version}-recommended")
        latest = promos.get(f"{mc_version}-latest")
        result = _builds_from_versions([(version, True) for version in versions])
        return [
            LoaderBuild(
                version=build.version,
                stable=build.stable,
                latest=build.version == latest,
                recommended=build.version == recommended,
            )
            for build in result
        ]

    raise MchostError(f"unknown loader {loader!r}")


def _builds_from_versions(
    versions: list[tuple[str, bool]],
) -> list[LoaderBuild]:
    cleaned = [
        (str(version), bool(stable) and not is_prerelease(str(version)))
        for version, stable in versions
        if version
    ]
    cleaned.sort(key=lambda item: version_key(item[0]), reverse=True)
    result: list[LoaderBuild] = []
    recommended_marked = False
    for index, (version, stable) in enumerate(cleaned):
        recommended = stable and not recommended_marked
        if recommended:
            recommended_marked = True
        result.append(
            LoaderBuild(
                version=version,
                stable=stable,
                latest=index == 0,
                recommended=recommended,
            )
        )
    return result


def latest_build(loader: str, mc_version: str) -> LoaderBuild | None:
    builds = loader_builds(loader, mc_version)
    return builds[0] if builds else None


def recommended_build(loader: str, mc_version: str) -> LoaderBuild | None:
    builds = loader_builds(loader, mc_version)
    for build in builds:
        if build.recommended:
            return build
    for build in builds:
        if build.stable:
            return build
    return builds[0] if builds else None
