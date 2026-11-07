from __future__ import annotations

from mchost.config import load_instance
from mchost.migrate import maybe_auto_migrate, migrate, parse_env_file
from mchost.paths import Layout


def write_legacy_env(root, instance="FTBNeoTech"):
    (root / ".env").write_text(
        "\n".join(
            [
                "# legacy config",
                f"INSTANCE={instance}",
                "TYPE=NEOFORGE",
                "VERSION=1.20.4",
                "MEMORY=8G",
                "TZ=America/Sao_Paulo",
                'RCON_PASSWORD="80085"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_parse_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "export INSTANCE='My Server'\n# comment\nEMPTY=\nMEMORY=4G # inline\n",
        encoding="utf-8",
    )
    values = parse_env_file(path)
    assert values["INSTANCE"] == "My Server"
    assert values["EMPTY"] == ""
    assert values["MEMORY"] == "4G"


def test_migrate_keeps_legacy_data_path_for_syncthing(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    write_legacy_env(tmp_path)
    legacy_server = tmp_path / "instances" / "FTBNeoTech" / "server"
    legacy_server.mkdir(parents=True)
    (legacy_server / "server.properties").write_text("motd=hi\n", encoding="utf-8")

    result = migrate(layout)

    assert result.instance.slug == "ftb-neo-tech"
    assert result.instance.loader == "neoforge"
    assert result.instance.mc_version == "1.20.4"
    assert result.instance.memory == "8G"
    assert result.instance.rcon_password == "80085"
    assert result.instance.data_dir == "instances/FTBNeoTech/server"

    # The world stayed exactly where Syncthing expects it.
    kept = tmp_path / "instances" / "FTBNeoTech" / "server" / "server.properties"
    assert kept.read_text(encoding="utf-8") == "motd=hi\n"
    assert layout.server_dir_for(result.instance) == legacy_server.resolve()
    assert layout.config_file("ftb-neo-tech").is_file()
    assert layout.compose_file("ftb-neo-tech").is_file()
    assert layout.migration_marker.is_file()
    assert layout.legacy_env.is_file(), "the legacy .env stays for the mc shim"

    compose = layout.compose_file("ftb-neo-tech").read_text(encoding="utf-8")
    assert "../FTBNeoTech/server:/data:Z" in compose

    loaded = load_instance(layout, "ftb-neo-tech")
    assert loaded.name == "FTBNeoTech"
    assert loaded.java == 17
    assert loaded.data_dir == "instances/FTBNeoTech/server"


def test_auto_migration_skips_when_instances_exist(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    write_legacy_env(tmp_path)
    existing = layout.instance_dir("existing")
    existing.mkdir()
    layout.config_file("existing").write_text("name = 'Existing'\n", encoding="utf-8")

    assert maybe_auto_migrate(layout) is None
    assert not layout.migration_marker.exists()


def test_auto_migration_runs_once(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    write_legacy_env(tmp_path)

    first = maybe_auto_migrate(layout)
    assert first is not None
    assert maybe_auto_migrate(layout) is None
