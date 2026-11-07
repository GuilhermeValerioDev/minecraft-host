from __future__ import annotations

import yaml

from mchost.compose import render_compose, write_compose
from mchost.config import Instance, ModpackConfig
from mchost.paths import Layout


def make_instance(**overrides):
    values = dict(
        name="Test",
        slug="test",
        loader="neoforge",
        mc_version="1.20.4",
        loader_version="47.1.106",
        memory="6G",
        tz="America/Sao_Paulo",
        java_version=17,
        rcon_password="secret",
    )
    values.update(overrides)
    return Instance(**values)


def service(instance):
    return yaml.safe_load(render_compose(instance))["services"]["minecraft"]


def test_neoforge_environment_and_ports():
    instance = make_instance()
    svc = service(instance)

    assert svc["image"] == "itzg/minecraft-server:java17"
    assert svc["container_name"] == "mc-test"
    assert svc["environment"]["TYPE"] == "NEOFORGE"
    assert svc["environment"]["VERSION"] == "1.20.4"
    assert svc["environment"]["NEOFORGE_VERSION"] == "47.1.106"
    assert svc["environment"]["RCON_PASSWORD"] == "secret"
    assert "25565:25565" in svc["ports"]
    assert "127.0.0.1:25575:25575" in svc["ports"]
    assert "8100:8100" in svc["ports"]
    assert svc["volumes"] == ["./server:/data:Z"]
    assert svc["healthcheck"]["test"] == ["CMD", "mc-health"]


def test_fabric_loader_version_variable():
    svc = service(
        make_instance(loader="fabric", mc_version="1.20.4", loader_version="0.15.11", java_version=17)
    )
    assert svc["environment"]["FABRIC_LOADER_VERSION"] == "0.15.11"


def test_vanilla_has_no_loader_version_variable():
    svc = service(
        make_instance(
            loader="vanilla",
            loader_version=None,
            mc_version="1.21.1",
            java_version=None,
        )
    )
    assert svc["environment"]["TYPE"] == "VANILLA"
    assert svc["image"] == "itzg/minecraft-server:java21"
    assert not any(
        key.endswith("_VERSION") for key in svc["environment"] if key != "VERSION"
    )


def test_minecraft_26_uses_java_25():
    svc = service(
        make_instance(
            loader="vanilla",
            loader_version=None,
            mc_version="26.3",
            java_version=None,
        )
    )
    assert svc["image"] == "itzg/minecraft-server:java25"


def test_bluemap_can_be_disabled():
    svc = service(make_instance(bluemap_port=None))
    assert all(":8100" not in port for port in svc["ports"])


def test_modrinth_pack_uses_local_mrpack():
    pack = ModpackConfig(
        project="cobbleverse",
        version_id="abc",
        version_number="1.7.42",
        mode="server",
        file_name="cobbleverse-1.7.42-server.mrpack",
        file_kind="mrpack",
        sha512="deadbeef",
    )
    instance = make_instance(kind="modpack", modpack=pack)
    svc = service(instance)

    assert svc["environment"]["TYPE"] == "MODRINTH"
    assert svc["environment"]["MODRINTH_MODPACK"] == "/modpacks/cobbleverse-1.7.42-server.mrpack"
    assert "VERSION" not in svc["environment"]
    assert "./pack:/modpacks:ro" in svc["volumes"]


def test_modrinth_zip_pack_uses_generic_pack():
    pack = ModpackConfig(
        project="cobbleverse",
        version_id="abc",
        version_number="1.7.42",
        mode="server",
        file_name="cobbleverse-1.7.42-server.zip",
        file_kind="zip",
        sha512="deadbeef",
        resolved={"minecraft": "1.20.1", "loader": "fabric", "loader_version": "0.16.5"},
    )
    instance = make_instance(kind="modpack", modpack=pack, loader="vanilla", mc_version="LATEST")
    svc = service(instance)

    assert svc["environment"]["GENERIC_PACK"] == "/modpacks/cobbleverse-1.7.42-server.zip"
    assert svc["environment"]["SKIP_GENERIC_PACK_UPDATE_CHECK"] == "true"
    assert svc["environment"]["TYPE"] == "FABRIC"
    assert svc["environment"]["VERSION"] == "1.20.1"
    assert svc["environment"]["FABRIC_LOADER_VERSION"] == "0.16.5"


def test_write_compose_is_idempotent(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    instance = make_instance()
    path, changed = write_compose(layout, instance)
    assert changed is True
    assert path.is_file()
    _, changed_again = write_compose(layout, instance)
    assert changed_again is False


def test_legacy_data_path_is_mounted_relatively(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    instance = make_instance(slug="ftb-neo-tech", data_dir="instances/FTBNeoTech/server")
    path, _ = write_compose(layout, instance)
    svc = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["minecraft"]
    assert svc["volumes"] == ["../FTBNeoTech/server:/data:Z"]
