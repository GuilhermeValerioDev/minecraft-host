from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from textual.widgets import Button, DataTable, Input, Static, TabbedContent

from mchost.compose import write_compose
from mchost.config import Instance, save_instance
from mchost.engine import EngineInfo
from mchost.paths import Layout
from mchost.tui.app import MchostApp
from mchost.tui.new_instance import NewInstanceWizard
from mchost.tui.new_instance_chooser import NewInstanceChooser
from mchost.tui.version_picker import VersionPicker
from mchost.tui.widgets import ConfirmDialog, ConfirmSwitch, ContextMenu
from mchost.tui.modpack_browser import ModpackBrowser
from mchost.tui.config_editor import ConfigEditor
from mchost.tui.local_pack_installer import LocalPackInstaller
from mchost.tui.memory_editor import MemoryEditor
from mchost.modrinth import (
    InstallResult,
    ModrinthFile,
    ModrinthVersion,
    ModpackHit,
    ModpackProject,
)
from mchost.config import ModpackConfig
from mchost.versions import LoaderBuild


class FakeEngine:
    def __init__(self) -> None:
        self.info = EngineInfo("docker", "docker", ("docker", "compose"))
        self.states = {"demo": "exited"}
        self.calls: list[tuple] = []

    def state(self, slug: str) -> str:
        return self.states.get(slug, "missing")

    def is_running(self, slug: str) -> bool:
        return self.state(slug) == "running"

    def started_at(self, slug: str):
        return datetime.now(timezone.utc) - timedelta(seconds=65)

    def running_slugs(self) -> list[str]:
        return [slug for slug, state in self.states.items() if state == "running"]

    def up(self, slug: str) -> None:
        self.states[slug] = "running"
        self.calls.append(("up", slug))

    def down(self, slug: str) -> None:
        self.states[slug] = "exited"
        self.calls.append(("down", slug))

    def exec_capture(self, slug: str, argv) -> str:
        self.calls.append(("exec", slug, list(argv)))
        return "There are 0 of a max of 20 players online."

    def exec(self, slug: str, argv, *, tty: bool = False) -> None:
        self.calls.append(("exec-tty", slug, list(argv)))

    def logs(self, slug: str, *, tail: int = 200, follow: bool = True) -> None:
        pass

    def ping(self) -> bool:
        return True


def make_app(tmp_path, names=("demo",)):
    layout = Layout(tmp_path)
    layout.ensure()
    for name in names:
        instance = Instance(
            name=name.title(),
            slug=name,
            loader="fabric",
            mc_version="1.20.4",
            loader_version="0.15.11",
            memory="2G",
            tz="UTC",
            java_version=17,
            rcon_password="secret",
        )
        save_instance(layout, instance)
        write_compose(layout, instance)
    fake = FakeEngine()
    return MchostApp(layout, engine=fake), fake


def test_dashboard_mouse_flow(tmp_path):
    app, fake = make_app(tmp_path)

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert app.selected_slug == "demo"
            assert app.query_one("#row-demo") is not None

            from textual.widgets import Button

            assert app.query_one("#btn-stop", Button).disabled is True
            assert app.query_one("#btn-start", Button).disabled is False

            await pilot.click("#btn-start")
            await pilot.pause(0.3)
            assert ("up", "demo") in fake.calls
            assert app.query_one("#btn-start", Button).disabled is True
            assert app.query_one("#btn-stop", Button).disabled is False

            from textual.widgets import RichLog

            assert app.query_one(TabbedContent).active == "tab-console"
            app._append_log("hello-from-the-server")
            await pilot.pause(0.1)
            console_lines = [str(line) for line in app.query_one("#console-log", RichLog).lines]
            assert any("hello-from-the-server" in line for line in console_lines)

            app.query_one(TabbedContent).active = "tab-logs"
            await pilot.pause(0.1)
            log_lines = [str(line) for line in app.query_one("#logs-view", RichLog).lines]
            assert any("hello-from-the-server" in line for line in log_lines)

            app.query_one(TabbedContent).active = "tab-console"
            await pilot.pause(0.1)
            app.query_one("#console-input", Input).value = "list"
            await pilot.click("#console-send")
            await pilot.pause(0.3)
            assert any(call[:2] == ("exec", "demo") for call in fake.calls)

            await pilot.click("#btn-stop")
            await pilot.pause(0.3)
            assert ("down", "demo") in fake.calls

            await pilot.click("#row-demo", button=3)
            await pilot.pause(0.1)
            assert isinstance(app.screen, ContextMenu)
            await pilot.press("escape")
            await pilot.pause(0.1)

    asyncio.run(run())


def test_command_palette_opens(tmp_path):
    app, _fake = make_app(tmp_path)

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.action_command_palette()
            await pilot.pause(0.2)
            assert app.screen.__class__.__name__ == "CommandPalette"
            await pilot.press("escape")
            await pilot.pause(0.1)

    asyncio.run(run())


def test_command_palette_custom_commands_have_help_text(tmp_path):
    from textual.app import SystemCommand

    app, _fake = make_app(tmp_path)

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            commands = list(app.get_system_commands(app.screen))
            custom = [c for c in commands if isinstance(c, SystemCommand) and c.title.startswith("Start ")]
            assert custom, "no custom Start command"
            assert isinstance(custom[0].help, str)
            assert callable(custom[0].callback)

    asyncio.run(run())


def test_command_palette_can_start_instance(tmp_path):
    app, fake = make_app(tmp_path)

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.action_command_palette()
            await pilot.pause(0.2)
            for key in "start":
                await pilot.press(key)
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert ("up", "demo") in fake.calls

    asyncio.run(run())


def test_starting_another_instance_asks_to_switch(tmp_path):
    app, fake = make_app(tmp_path, names=("alpha", "beta"))
    fake.states = {"alpha": "running", "beta": "exited"}

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert app.selected_slug == "alpha"

            app.handle_row_action("beta", "start")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmSwitch)

            await pilot.click("#confirm-switch")
            await pilot.pause(0.5)
            assert ("down", "alpha") in fake.calls
            assert ("up", "beta") in fake.calls

    asyncio.run(run())


def test_context_menu_change_version_opens_picker(tmp_path, monkeypatch):
    import mchost.tui.version_picker as picker_module

    monkeypatch.setattr(
        picker_module, "supported_mc_versions", lambda loader: ["1.21.1", "1.20.4"]
    )
    monkeypatch.setattr(
        picker_module,
        "loader_builds",
        lambda loader, mc: [LoaderBuild("21.1.80", recommended=True, latest=True)],
    )

    app, _fake = make_app(tmp_path)

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.show_context_menu("demo")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ContextMenu)
            await pilot.click("#ctx-version")
            await pilot.pause(0.4)
            assert isinstance(app.screen, VersionPicker)
            await pilot.press("escape")
            await pilot.pause(0.1)

    asyncio.run(run())


def test_mods_and_configs_tabs_list_files(tmp_path):
    app, _fake = make_app(tmp_path)
    server = app.layout.server_dir("demo")
    (server / "mods").mkdir(parents=True)
    (server / "mods" / "example.jar").write_bytes(b"x" * 1500)
    (server / "config" / "nested").mkdir(parents=True)
    (server / "config" / "example.toml").write_text("x", encoding="utf-8")
    (server / "config" / "nested" / "more.toml").write_text("y", encoding="utf-8")

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.2)
            app.query_one(TabbedContent).active = "tab-mods"
            await pilot.pause(0.5)
            mods = app.query_one("#mods-table", DataTable)
            assert mods.row_count == 1
            assert "example.jar" in str(mods.get_row_at(0)[0])
            summary = app.query_one("#mods-summary", Static)
            assert "1 file" in str(summary.content)

            app.query_one(TabbedContent).active = "tab-configs"
            await pilot.pause(0.5)
            configs = app.query_one("#configs-table", DataTable)
            assert configs.row_count == 2

    asyncio.run(run())


def test_backups_tab_creates_and_restores(tmp_path):
    app, _fake = make_app(tmp_path)
    server = app.layout.server_dir("demo")
    (server / "world").mkdir(parents=True)
    (server / "world" / "level.dat").write_text("original", encoding="utf-8")
    (server / "config").mkdir()
    (server / "config" / "example.toml").write_text("x", encoding="utf-8")

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.2)
            app.query_one(TabbedContent).active = "tab-backups"
            await pilot.pause(0.2)
            await pilot.click("#backup-world")
            await pilot.pause(0.8)
            table = app.query_one("#backups-table", DataTable)
            assert table.row_count == 1
            summary = app.query_one("#backups-summary", Static)
            assert "1 backup" in str(summary.content)

            (server / "world" / "level.dat").write_text("changed", encoding="utf-8")
            await pilot.click("#backups-restore")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.click("#dialog-confirm")
            await pilot.pause(0.8)
            assert (server / "world" / "level.dat").read_text(encoding="utf-8") == "original"

    asyncio.run(run())


def test_format_uptime():
    from mchost.tui.app import format_uptime

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert format_uptime(start, start + timedelta(seconds=5)) == "00:00:05"
    assert (
        format_uptime(start, start + timedelta(hours=1, minutes=2, seconds=3))
        == "01:02:03"
    )
    assert (
        format_uptime(start, start + timedelta(days=1, seconds=1))
        == "1d 00:00:01"
    )


def test_overview_shows_uptime_only_while_running(tmp_path):
    import io

    from rich.console import Console

    app, fake = make_app(tmp_path)

    def overview_text():
        instance = app._selected()
        assert instance is not None
        buffer = io.StringIO()
        console = Console(
            file=buffer, width=120, record=True, force_terminal=False
        )
        console.print(app._overview(instance))
        return buffer.getvalue()

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.2)
            assert app._uptime_label(app._selected()) is None
            assert "up " not in overview_text()

            fake.states["demo"] = "running"
            app._poll_states()
            await pilot.pause(0.4)
            assert app._uptime_label(app._selected()) is not None
            assert "up " in overview_text()
            assert "demo" in app._started_at

            fake.states["demo"] = "exited"
            app._poll_states()
            await pilot.pause(0.4)
            assert app._uptime_label(app._selected()) is None
            assert "up " not in overview_text()

    asyncio.run(run())


def test_app_focus_forces_a_repaint(tmp_path):
    from textual import events

    app, _fake = make_app(tmp_path)
    called = []

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.2)
            app._repaint_after_focus = lambda: called.append(True)
            app.post_message(events.AppFocus())
            await pilot.pause(0.3)

    asyncio.run(run())
    assert called, "AppFocus did not trigger a repaint"


def test_delete_instance_from_context_menu(tmp_path):
    app, _fake = make_app(tmp_path)
    layout = app.layout

    async def run():
        async with app.run_test(size=(120, 46)) as pilot:
            await pilot.pause(0.2)
            app.show_context_menu("demo")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ContextMenu)
            await pilot.click("#ctx-delete")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.click("#dialog-confirm")
            await pilot.pause(0.8)
            assert not layout.config_file("demo").exists()
            assert not layout.instance_dir("demo").exists()

    asyncio.run(run())


def test_mods_tab_can_disable_and_delete(tmp_path):
    app, _fake = make_app(tmp_path)
    mods = app.layout.server_dir("demo") / "mods"
    mods.mkdir(parents=True)
    mod_file = mods / "example.jar"
    mod_file.write_bytes(b"jar")

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.2)
            app.query_one(TabbedContent).active = "tab-mods"
            await pilot.pause(0.5)
            table = app.query_one("#mods-table", DataTable)
            assert table.row_count == 1
            table.move_cursor(row=0)
            await pilot.pause(0.2)

            await pilot.click("#mods-toggle")
            await pilot.pause(0.5)
            assert not mod_file.exists()
            assert (mods / "example.jar.disabled").exists()

            table = app.query_one("#mods-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause(0.2)
            await pilot.click("#mods-toggle")
            await pilot.pause(0.5)
            assert mod_file.exists()
            assert not (mods / "example.jar.disabled").exists()

            table = app.query_one("#mods-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause(0.2)
            await pilot.click("#mods-delete")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.click("#dialog-confirm")
            await pilot.pause(0.4)
            assert not mod_file.exists()

    asyncio.run(run())


def test_configs_tab_can_edit(tmp_path):
    app, _fake = make_app(tmp_path)
    config = app.layout.server_dir("demo") / "config"
    config.mkdir(parents=True)
    config_file = config / "example.toml"
    config_file.write_text("a = 1\n", encoding="utf-8")

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.2)
            app.query_one(TabbedContent).active = "tab-configs"
            await pilot.pause(0.5)
            table = app.query_one("#configs-table", DataTable)
            assert table.row_count == 1
            table.move_cursor(row=0)
            await pilot.pause(0.2)

            await pilot.click("#configs-edit")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfigEditor)
            from textual.widgets import TextArea

            app.screen.query_one("#editor-area", TextArea).text = "a = 2\n"
            await pilot.click("#editor-save")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, ConfigEditor)

    asyncio.run(run())
    assert config_file.read_text(encoding="utf-8") == "a = 2\n"


def test_memory_editor_works_for_modpacks(tmp_path):
    from mchost.compose import write_compose
    from mchost.config import Instance, ModpackConfig, load_instance, save_instance

    app, _fake = make_app(tmp_path)
    layout = app.layout
    pack = Instance(
        name="Pack",
        slug="pack",
        kind="modpack",
        loader="fabric",
        mc_version="1.20.1",
        loader_version="0.15.0",
        memory="4G",
        tz="UTC",
        java_version=17,
        rcon_password="secret",
        modpack=ModpackConfig(
            project="test-pack",
            version_id="v1",
            version_number="1.0.0",
            mode="server",
            file_name="pack.mrpack",
            file_kind="mrpack",
            sha512="abc",
        ),
    )
    save_instance(layout, pack)
    write_compose(layout, pack)

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.2)
            app.select_instance("pack")
            app.action_memory()
            await pilot.pause(0.3)
            assert isinstance(app.screen, MemoryEditor)
            app.screen.query_one("#memory-value", Input).value = "10G"
            await pilot.click("#memory-apply")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, MemoryEditor)

    asyncio.run(run())
    loaded = load_instance(layout, "pack")
    assert loaded.memory == "10G"
    compose = layout.compose_file("pack").read_text(encoding="utf-8")
    assert "MEMORY: 10G" in compose


def test_local_curseforge_pack_installer(tmp_path):
    import json
    import zipfile

    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "manifest.json",
            json.dumps(
                {
                    "name": "My Forge Pack",
                    "version": "1.2.3",
                    "minecraft": {
                        "version": "1.20.1",
                        "modLoaders": [{"id": "forge-47.2.0", "primary": True}],
                    },
                }
            ),
        )
        handle.writestr("mods/example.jar", b"jar")
        handle.writestr("config/example.toml", "a = 1\n")

    app, _fake = make_app(tmp_path, names=())

    async def run():
        async with app.run_test(size=(120, 46)) as pilot:
            await pilot.pause(0.2)
            app.action_local_pack_installer()
            await pilot.pause(0.3)
            installer = app.screen
            assert isinstance(installer, LocalPackInstaller)
            installer.query_one("#local-path", Input).value = str(archive)
            await pilot.click("#local-inspect")
            await pilot.pause(0.5)
            assert installer.query_one("#local-name", Input).value == "My Forge Pack"
            assert installer.query_one("#local-mc", Input).value == "1.20.1"
            assert installer.selected_loader == "forge"

            await pilot.click("#local-install")
            await pilot.pause(0.8)
            assert not isinstance(app.screen, LocalPackInstaller)

            app.query_one(TabbedContent).active = "tab-modpacks"
            await pilot.pause(0.3)
            assert "Local pack" in str(
                app.query_one("#modpacks-summary", Static).content
            )
            assert app.query_one("#modpacks-check", Button).disabled is True
            assert app.query_one("#modpacks-link", Button).disabled is True

    asyncio.run(run())

    from mchost.config import load_instance

    loaded = load_instance(app.layout, "my-forge-pack")
    assert loaded.kind == "modpack"
    assert loaded.modpack.source == "local"
    assert loaded.modpack.file_kind == "zip"
    assert (app.layout.server_dir("my-forge-pack") / "mods/example.jar").is_file()
    assert app.selected_slug == "my-forge-pack"


def test_modpacks_tab_checks_and_updates(tmp_path, monkeypatch):
    import mchost.tui.app as app_module
    from mchost.compose import write_compose
    from mchost.config import Instance, ModpackConfig, save_instance
    from mchost.modrinth import Artifact, UpdateCheck, UpdateResult

    app, _fake = make_app(tmp_path)
    layout = app.layout
    pack = Instance(
        name="Pack",
        slug="pack",
        kind="modpack",
        loader="fabric",
        mc_version="1.20.1",
        loader_version="0.15.0",
        memory="6G",
        tz="UTC",
        java_version=17,
        rcon_password="secret",
        modpack=ModpackConfig(
            project="test-pack",
            version_id="v1",
            version_number="1.0.0",
            mode="server",
            file_name="pack-server.mrpack",
            file_kind="mrpack",
            sha512="abc",
            resolved={
                "minecraft": "1.20.1",
                "loader": "fabric",
                "loader_version": "0.15.0",
            },
        ),
    )
    save_instance(layout, pack)
    write_compose(layout, pack)

    file = ModrinthFile(
        filename="pack-server.mrpack",
        url="https://example.invalid/pack-server.mrpack",
        sha512="def",
        size=100,
        primary=True,
    )
    latest = ModrinthVersion(
        id="v2",
        project_id="project",
        name="1.1.0",
        version_number="1.1.0",
        game_versions=("1.20.1",),
        loaders=("fabric",),
        environment="",
        version_type="release",
        date_published="2026-02-01T00:00:00Z",
        files=(file,),
    )
    project = ModpackProject(
        id="project",
        slug="test-pack",
        title="Test Pack",
        description="",
        downloads=1,
        icon_url="",
        versions=("v1", "v2"),
        game_versions=("1.20.1",),
        loaders=("fabric",),
    )
    artifact = Artifact(file, "mrpack", "server", "explicit server pack")
    check = UpdateCheck(
        project=project,
        current=None,
        latest=latest,
        artifact=artifact,
        available=True,
        reason="1.1.0 is available",
    )
    monkeypatch.setattr(app_module, "check_modpack_update", lambda instance: check)
    updated_calls = []

    def fake_update(layout_arg, instance, **kwargs):
        updated_calls.append(kwargs)
        return UpdateResult(
            instance=instance,
            project=project,
            old_version="1.0.0",
            new_version="1.1.0",
            artifact=artifact,
            updated=True,
        )

    monkeypatch.setattr(app_module, "update_modpack", fake_update)

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.3)
            app.select_instance("pack")
            app.query_one(TabbedContent).active = "tab-modpacks"
            await pilot.pause(0.3)
            await pilot.click("#modpacks-check")
            await pilot.pause(0.4)
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.click("#dialog-confirm")
            await pilot.pause(0.6)
            assert updated_calls

    asyncio.run(run())


def test_modpack_browser_installs(tmp_path, monkeypatch):
    import mchost.tui.modpack_browser as browser_module

    file = ModrinthFile(
        filename="pack-server.mrpack",
        url="https://example.invalid/pack-server.mrpack",
        sha512="abc",
        size=100,
        primary=True,
    )
    version = ModrinthVersion(
        id="v1",
        project_id="project",
        name="Pack 1.0.0",
        version_number="1.0.0",
        game_versions=("1.20.1",),
        loaders=("fabric",),
        environment="",
        version_type="release",
        date_published="2026-01-01T00:00:00Z",
        files=(file,),
    )
    hit = ModpackHit(
        slug="test-pack",
        title="Test Pack",
        description="",
        downloads=1,
        icon_url="",
        versions=("1.20.1",),
        game_versions=("1.20.1",),
        loaders=("fabric",),
    )
    project = ModpackProject(
        id="project",
        slug="test-pack",
        title="Test Pack",
        description="",
        downloads=1,
        icon_url="",
        versions=("v1",),
        game_versions=("1.20.1",),
        loaders=("fabric",),
    )
    monkeypatch.setattr(browser_module, "search_modpacks", lambda query: [hit])
    monkeypatch.setattr(browser_module, "get_project", lambda slug: project)
    monkeypatch.setattr(browser_module, "get_versions", lambda slug: [version])

    from mchost.compose import write_compose
    from mchost.config import Instance, save_instance

    def fake_install(layout, slug, **kwargs):
        instance = Instance(
            name="Installed Pack",
            slug="installed-pack",
            kind="modpack",
            loader="fabric",
            mc_version="1.20.1",
            loader_version="0.15.11",
            memory="6G",
            tz="UTC",
            java_version=17,
            rcon_password="secret",
            modpack=ModpackConfig(
                project="test-pack",
                version_id="v1",
                version_number="1.0.0",
                mode="server",
                file_name="pack-server.mrpack",
                file_kind="mrpack",
                sha512="abc",
                resolved={
                    "minecraft": "1.20.1",
                    "loader": "fabric",
                    "loader_version": "0.15.11",
                },
            ),
        )
        instance.validate()
        save_instance(layout, instance)
        write_compose(layout, instance)
        return InstallResult(
            instance=instance,
            project=project,
            version=version,
            artifact=browser_module.resolve_artifact(version, [version]),
            pack_path=layout.pack_dir("installed-pack") / "pack-server.mrpack",
        )

    monkeypatch.setattr(browser_module, "install_modpack", fake_install)

    app, _fake = make_app(tmp_path, names=())

    async def run():
        async with app.run_test(size=(120, 46)) as pilot:
            await pilot.pause(0.2)
            app.action_modpack_browser()
            await pilot.pause(0.5)
            browser = app.screen
            assert isinstance(browser, ModpackBrowser)
            results = browser.query_one("#pack-results", DataTable)
            assert results.row_count == 1

            await pilot.click("#pack-results", offset=(2, 1))
            await pilot.pause(0.5)
            versions = browser.query_one("#pack-versions", DataTable)
            assert versions.row_count == 1
            assert browser.artifact is not None
            assert browser.artifact.mode == "server"

            await pilot.click("#pack-install")
            await pilot.pause(0.8)
            assert not isinstance(app.screen, ModpackBrowser)

    asyncio.run(run())
    from mchost.config import load_instance

    loaded = load_instance(app.layout, "installed-pack")
    assert loaded.kind == "modpack"
    assert app.selected_slug == "installed-pack"


def test_new_instance_chooser_offers_three_paths(tmp_path):
    app, _fake = make_app(tmp_path, names=())

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.2)
            app.action_new()
            await pilot.pause(0.3)
            chooser = app.screen
            assert isinstance(chooser, NewInstanceChooser)
            assert chooser.query_one("#choice-custom")
            assert chooser.query_one("#choice-modrinth")
            assert chooser.query_one("#choice-curseforge")
            await pilot.click("#choice-cancel")
            await pilot.pause(0.2)

    asyncio.run(run())


def test_new_instance_wizard_creates_instance(tmp_path, monkeypatch):
    import mchost.tui.new_instance as wizard_module

    monkeypatch.setattr(
        wizard_module, "supported_mc_versions", lambda loader: ["1.21.1", "1.20.4"]
    )
    monkeypatch.setattr(
        wizard_module,
        "loader_builds",
        lambda loader, mc: [LoaderBuild("0.16.0", recommended=True, latest=True)],
    )

    app, _fake = make_app(tmp_path, names=())

    async def run():
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause(0.2)
            app.action_new()
            await pilot.pause(0.3)
            assert isinstance(app.screen, NewInstanceChooser)
            await pilot.click("#choice-custom")
            await pilot.pause(0.3)
            wizard = app.screen
            assert isinstance(wizard, NewInstanceWizard)

            wizard.query_one("#new-name", Input).value = "Wizard Server"
            await pilot.click("#loader-fabric")
            wizard._refresh_create()
            await pilot.pause(0.5)
            await pilot.click("#new-create")
            await pilot.pause(0.5)
            assert not isinstance(app.screen, NewInstanceWizard)

    asyncio.run(run())

    from mchost.config import load_instance

    loaded = load_instance(app.layout, "wizard-server")
    assert loaded.loader == "fabric"
    assert loaded.mc_version == "1.21.1"
    assert loaded.loader_version == "0.16.0"
    assert loaded.java_version == 21
    assert app.selected_slug == "wizard-server"


def test_version_picker_updates_instance(tmp_path, monkeypatch):
    import mchost.tui.version_picker as picker_module

    def fake_versions(loader):
        return ["1.21.1", "1.20.4"]

    def fake_builds(loader, mc_version):
        if mc_version == "1.21.1":
            return [LoaderBuild("21.1.80", recommended=True, latest=True)]
        return [LoaderBuild("47.1.106", recommended=True, latest=True)]

    monkeypatch.setattr(picker_module, "supported_mc_versions", fake_versions)
    monkeypatch.setattr(picker_module, "loader_builds", fake_builds)

    app, _fake = make_app(tmp_path)

    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.show_version_picker()
            await pilot.pause(0.5)
            picker = app.screen
            assert isinstance(picker, VersionPicker)
            assert picker.selected_mc == "1.20.4"
            assert picker.selected_build == "47.1.106"
            assert picker.query_one("#mc-list").option_count == 2

            picker.selected_mc = "1.21.1"
            picker._load_builds("1.21.1")
            await pilot.pause(0.4)
            assert picker.selected_build == "21.1.80"

            await pilot.click("#picker-apply")
            await pilot.pause(0.5)
            assert not isinstance(app.screen, VersionPicker)

    asyncio.run(run())

    from mchost.config import load_instance

    loaded = load_instance(app.layout, "demo")
    assert loaded.mc_version == "1.21.1"
    assert loaded.loader_version == "21.1.80"
    assert loaded.java_version == 21
