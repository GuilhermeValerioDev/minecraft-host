from __future__ import annotations

from mchost import versions
from mchost.versions import (
    _builds_from_versions,
    _neoforge_prefix,
    is_prerelease,
    loader_builds,
    supported_mc_versions,
    version_key,
)


def test_version_key_orders_numerically():
    values = ["0.9.0", "0.10.0", "0.10.1", "1.20.4", "1.21"]
    assert sorted(values, key=version_key) == values


def test_is_prerelease():
    assert is_prerelease("0.31.0-beta.6")
    assert is_prerelease("1.20.4-rc1")
    assert not is_prerelease("0.20.1")


def test_neoforge_prefix():
    assert _neoforge_prefix("1.20.1") == "47."
    assert _neoforge_prefix("1.20.4") == "20.4."
    assert _neoforge_prefix("1.21") == "21.0."
    assert _neoforge_prefix("1.21.1") == "21.1."


def test_paper_versions_filter_prereleases(monkeypatch):
    monkeypatch.setattr(
        versions,
        "_get_json",
        lambda url, key, **kwargs: {
            "versions": {
                "26.3": ["26.3", "26.3-rc-3"],
                "1.20": ["1.20.6", "1.20.4", "1.20-pre1"],
            }
        },
    )
    assert supported_mc_versions("paper") == ["26.3", "1.20.6", "1.20.4"]


def test_paper_builds(monkeypatch):
    monkeypatch.setattr(
        versions,
        "_get_json",
        lambda url, key, **kwargs: [
            {"id": 132, "channel": "STABLE"},
            {"id": 133, "channel": "EXPERIMENTAL"},
            {"id": 131, "channel": "STABLE"},
        ],
    )
    builds = loader_builds("paper", "1.21.11")
    assert [build.version for build in builds] == ["133", "132", "131"]
    assert builds[0].latest is True
    assert builds[0].stable is False
    assert builds[1].recommended is True
    assert builds[1].stable is True


def test_fabric_prerelease_is_not_recommended(monkeypatch):
    monkeypatch.setattr(
        versions,
        "_get_json",
        lambda url, key, **kwargs: [
            {"loader": {"version": "0.20.1", "stable": True}},
            {"loader": {"version": "0.21.0-beta.1", "stable": True}},
        ],
    )
    builds = loader_builds("fabric", "1.20.4")
    assert builds[0].version == "0.21.0-beta.1"
    assert builds[0].stable is False
    assert builds[1].recommended is True


def test_neoforge_builds_are_filtered_by_mc(monkeypatch):
    monkeypatch.setattr(
        versions,
        "_neoforge_versions",
        lambda: ["20.4.253", "20.4.251", "21.1.80"],
    )
    builds = loader_builds("neoforge", "1.20.4")
    assert [build.version for build in builds] == ["20.4.253", "20.4.251"]
    assert builds[0].recommended is True


def test_forge_uses_promotions(monkeypatch):
    monkeypatch.setattr(
        versions,
        "_forge_versions",
        lambda: ["1.20.4-47.2.0", "1.20.4-47.1.0", "1.20.1-47.2.0"],
    )
    monkeypatch.setattr(
        versions,
        "_forge_promotions",
        lambda: {"1.20.4-latest": "47.2.0", "1.20.4-recommended": "47.1.0"},
    )
    builds = loader_builds("forge", "1.20.4")
    by_version = {build.version: build for build in builds}
    assert by_version["47.2.0"].latest is True
    assert by_version["47.1.0"].recommended is True


def test_builds_from_versions_marks_prereleases_unstable():
    builds = _builds_from_versions(
        [("1.0.0", True), ("1.1.0-beta.1", True), ("0.9.0", True)]
    )
    assert builds[0].version == "1.1.0-beta.1"
    assert builds[0].stable is False
    assert builds[1].recommended is True
