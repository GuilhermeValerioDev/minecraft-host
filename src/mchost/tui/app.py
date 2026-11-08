"""The mchost interactive dashboard."""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from ..compose import write_compose
from ..config import LOADER_LABELS, Instance, resolve_instance, scan_instances
from ..engine import Engine, detect_engine, restore_ownership
from ..errors import MchostError
from ..files import (
    delete_file,
    is_mod_disabled,
    read_text_file,
    toggle_mod,
)
from ..instances import delete_instance
from ..paths import Layout
from ..versions import version_key
from ..backups import (
    Backup,
    create_backup,
    delete_backup,
    get_backup,
    human_size,
    list_backups,
    restore_backup,
)
from ..modrinth import (
    check_modpack_update,
    update_modpack,
)
from .widgets import ConfirmDialog, ConfirmSwitch, ContextMenu, InstanceRow
from .new_instance import NewInstanceWizard
from .version_picker import VersionPicker
from .modpack_browser import ModpackBrowser
from .config_editor import ConfigEditor
from .local_pack_installer import LocalPackInstaller
from .memory_editor import MemoryEditor

STATE_COLORS = {
    "running": "green",
    "restarting": "red",
    "exited": "yellow",
    "created": "yellow",
    "paused": "yellow",
    "missing": "dim",
    "unknown": "dim",
}


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def short_time(value: str) -> str:
    return value.replace("T", " ")[:16] if value else "—"


def format_uptime(started: datetime, now: datetime | None = None) -> str:
    """Format a start time as a compact, ticking uptime clock."""
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    seconds = max(0, int((current - started).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def scan_files(
    root: Path, limit: int = 500
) -> tuple[list[tuple[str, int, float]], bool]:
    """Return ``(relative_path, size, mtime)`` entries under ``root``."""
    if not root.is_dir():
        return [], False
    entries: list[tuple[str, int, float]] = []
    for current, dirs, names in os.walk(root):
        dirs.sort(key=version_key)
        names.sort(key=version_key)
        for name in names:
            full = Path(current) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            entries.append(
                (str(full.relative_to(root)), stat.st_size, stat.st_mtime)
            )
            if len(entries) >= limit:
                return entries, True
    return entries, False


class LogStreamer:
    """Tail a container's logs without blocking the UI."""

    def __init__(
        self,
        engine: Engine,
        layout: Layout,
        instance: Instance,
        on_line: Callable[[str], None],
    ) -> None:
        self.on_line = on_line
        self._stopped = False
        self.process = subprocess.Popen(
            [
                engine.info.binary,
                "logs",
                "--tail",
                "200",
                "-f",
                engine.container_name(instance.slug),
            ],
            cwd=str(layout.instance_dir(instance.slug)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                if self._stopped:
                    break
                self.on_line(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        self._stopped = True
        if self.process.poll() is None:
            self.process.terminate()
        try:
            if self.process.stdout:
                self.process.stdout.close()
        except OSError:
            pass

    def alive(self) -> bool:
        return self.thread.is_alive() and not self._stopped


class MchostApp(App[None]):
    """Sidebar, overview, live logs, and an RCON console."""

    CSS_PATH = "app.tcss"
    TITLE = "mchost"
    SUB_TITLE = "one server at a time"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("r", "restart", "Restart"),
        Binding("v", "version", "Version"),
        Binding("m", "memory", "Memory"),
        Binding("b", "backup", "Backup"),
        Binding("n", "new", "New"),
        Binding("f5", "refresh", "Refresh"),
        Binding("ctrl+p", "command_palette", "Commands"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, layout: Layout, engine: Engine | None = None) -> None:
        super().__init__()
        self.layout = layout
        self.engine = engine
        self._engine_override = engine
        self.engine_error: str | None = None
        self.instances: list[Instance] = []
        self.scan_errors: list[tuple[str, str]] = []
        self.rows: dict[str, InstanceRow] = {}
        self.selected_slug: str | None = None
        self.states: dict[str, str] = {}
        self._log_streamer: LogStreamer | None = None
        self._stream_slug: str | None = None
        self._log_history: list[str] = []
        self._modpack_link: str | None = None
        self._started_at: dict[str, datetime] = {}

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with VerticalScroll(id="sidebar"):
                yield Static("INSTANCES", classes="sidebar-title")
                yield Button("+ New instance", id="new-instance", variant="primary")
                yield Vertical(id="instance-list")
            with Vertical(id="detail"):
                with Horizontal(id="action-bar"):
                    yield Static("mchost", id="detail-title")
                    yield Static("", id="detail-state")
                    yield Button("Start", id="btn-start", variant="success")
                    yield Button("Stop", id="btn-stop", variant="error")
                    yield Button("Restart", id="btn-restart")
                    yield Button("Version", id="btn-version")
                    yield Button("⋯", id="btn-more", tooltip="More actions")
                    yield Button("⌘", id="btn-commands", tooltip="Command palette")
                with TabbedContent(id="tabs"):
                    with TabPane("Overview", id="tab-overview"):
                        yield Static(id="overview")
                    with TabPane("Console", id="tab-console"):
                        with Vertical(id="console-pane"):
                            yield RichLog(id="console-log", wrap=True, markup=False)
                            with Horizontal(id="console-input-row"):
                                yield Input(
                                    placeholder="RCON command…",
                                    id="console-input",
                                )
                                yield Button("Send", id="console-send", variant="primary")
                    with TabPane("Logs", id="tab-logs"):
                        yield RichLog(
                            id="logs-view",
                            wrap=False,
                            markup=False,
                            max_lines=5000,
                        )
                    with TabPane("Mods", id="tab-mods"):
                        with Vertical(classes="file-pane"):
                            with Horizontal(classes="file-toolbar"):
                                yield Static("", id="mods-summary", classes="file-summary")
                                yield Button("Refresh", id="mods-refresh")
                            yield DataTable(
                                id="mods-table",
                                zebra_stripes=True,
                                cursor_type="row",
                            )
                            with Horizontal(classes="file-toolbar"):
                                yield Button(
                                    "Disable", id="mods-toggle", disabled=True
                                )
                                yield Button(
                                    "Delete",
                                    id="mods-delete",
                                    variant="error",
                                    disabled=True,
                                )
                                yield Button(
                                    "Reveal", id="mods-reveal", disabled=True
                                )
                            yield Static(
                                "Disabling renames the file with .disabled. "
                                "Restart the server to apply changes.",
                                classes="file-note",
                            )
                    with TabPane("Configs", id="tab-configs"):
                        with Vertical(classes="file-pane"):
                            with Horizontal(classes="file-toolbar"):
                                yield Static(
                                    "", id="configs-summary", classes="file-summary"
                                )
                                yield Button("Refresh", id="configs-refresh")
                            yield DataTable(
                                id="configs-table",
                                zebra_stripes=True,
                                cursor_type="row",
                            )
                            with Horizontal(classes="file-toolbar"):
                                yield Button(
                                    "Edit",
                                    id="configs-edit",
                                    variant="primary",
                                    disabled=True,
                                )
                                yield Button(
                                    "Reveal", id="configs-reveal", disabled=True
                                )
                            yield Static(
                                "Changes apply when the server restarts.",
                                classes="file-note",
                            )
                    with TabPane("Backups", id="tab-backups"):
                        with Vertical(classes="file-pane"):
                            with Horizontal(classes="file-toolbar"):
                                yield Static(
                                    "", id="backups-summary", classes="file-summary"
                                )
                                yield Button("Backup world", id="backup-world")
                                yield Button("Full", id="backup-full")
                                yield Button("Refresh", id="backups-refresh")
                            yield DataTable(
                                id="backups-table",
                                zebra_stripes=True,
                                cursor_type="row",
                            )
                            with Horizontal(classes="file-toolbar"):
                                yield Button(
                                    "Restore", id="backups-restore", variant="warning"
                                )
                                yield Button(
                                    "Delete", id="backups-delete", variant="error"
                                )
                                yield Button("Reveal", id="backups-reveal")
                            yield Static(
                                "Restoring stops the server, replaces the selected "
                                "files, and restarts it if it was running.",
                                classes="file-note",
                            )
                    with TabPane("Modpacks", id="tab-modpacks"):
                        with Vertical(classes="file-pane"):
                            yield Static("", id="modpacks-summary", classes="file-summary")
                            yield Static("", id="modpacks-details")
                            with Horizontal(classes="file-toolbar"):
                                yield Button(
                                    "Browse Modrinth",
                                    id="modpacks-browse",
                                    variant="primary",
                                )
                                yield Button(
                                    "Install CurseForge pack",
                                    id="modpacks-local",
                                )
                                yield Button(
                                    "Check for updates",
                                    id="modpacks-check",
                                    disabled=True,
                                )
                                yield Button(
                                    "Copy link", id="modpacks-link", disabled=True
                                )
                                yield Button("Refresh", id="modpacks-refresh")
        yield Footer()

    async def on_mount(self) -> None:
        self.theme = "textual-dark"
        for selector in ("#mods-table", "#configs-table"):
            table = self.query_one(selector, DataTable)
            table.add_columns("File", "Size", "Modified", "State")
        backups_table = self.query_one("#backups-table", DataTable)
        backups_table.add_columns("Created", "Kind", "Size", "ID")
        self._load_instances()
        await self._rebuild_sidebar()
        if self.instances:
            self.select_instance(self.instances[0].slug)
        else:
            self._show_empty()
        self._set_buttons()
        if self._engine_override is not None:
            self._set_engine(self._engine_override, None)
        else:
            self._detect_engine()
        self.set_interval(4.0, self._poll_states)
        self.set_interval(1.0, self._tick_uptime)

    def on_unmount(self) -> None:
        self._stop_log_stream()

    def on_app_focus(self, event: events.AppFocus) -> None:
        # Some terminals leave stale scrollbar cells behind after alt-tab.
        # Force a full repaint once focus returns.
        self.call_after_refresh(self._repaint_after_focus)

    def on_screen_resume(self, event: events.ScreenResume) -> None:
        self._repaint_after_focus()

    def _repaint_after_focus(self) -> None:
        self.refresh(repaint=True, layout=True)
        try:
            self.screen.refresh(repaint=True, layout=True)
        except Exception:
            pass
        for widget in self.query(RichLog):
            try:
                widget.refresh(repaint=True, layout=True)
            except Exception:
                pass

    # -- data --------------------------------------------------------------

    def _load_instances(self) -> None:
        self.instances, self.scan_errors = scan_instances(self.layout)
        for slug, message in self.scan_errors:
            self.notify(f"{slug}: {message}", severity="warning", timeout=10)

    async def _rebuild_sidebar(self) -> None:
        container = self.query_one("#instance-list", Vertical)
        await container.remove_children()
        self.rows.clear()

        if not self.instances:
            await container.mount(
                Static(
                    "No instances yet.\n\n[dim]Click + New instance, or run:[/]\n"
                    "[bold]mchost new MyServer[/]",
                    classes="empty",
                )
            )
            return

        for instance in self.instances:
            row = InstanceRow(instance, self.states.get(instance.slug, "unknown"))
            self.rows[instance.slug] = row
            await container.mount(row)

        if self.selected_slug in self.rows:
            self.rows[self.selected_slug].set_selected(True)

    def _selected(self) -> Instance | None:
        if not self.selected_slug:
            return None
        for instance in self.instances:
            if instance.slug == self.selected_slug:
                return instance
        return None

    def select_instance(self, slug: str) -> None:
        if slug not in self.rows:
            return
        self.selected_slug = slug
        for row_slug, row in self.rows.items():
            row.set_selected(row_slug == slug)
        instance = self._selected()
        if instance:
            self._update_detail(instance)
        active = self.query_one(TabbedContent).active
        if active == "tab-mods":
            self._refresh_folder("mods")
        elif active == "tab-configs":
            self._refresh_folder("configs")
        elif active == "tab-backups":
            self._refresh_backups()
        elif active == "tab-modpacks":
            self._refresh_modpacks_tab()
        self._restart_logs()
        self._clear_console()
        self._write_console(f"# Console for {instance.name}" if instance else "")

    # -- engine ------------------------------------------------------------

    @work(thread=True, group="engine")
    def _detect_engine(self) -> None:
        try:
            engine = Engine(self.layout, detect_engine())
        except MchostError as exc:
            self.call_from_thread(self._set_engine, None, str(exc))
            return
        self.call_from_thread(self._set_engine, engine, None)

    def _set_engine(self, engine: Engine | None, error: str | None) -> None:
        self.engine = engine
        self.engine_error = error
        if error:
            self.notify(error, severity="error", timeout=10)
        if self.selected_slug:
            instance = self._selected()
            if instance:
                self._update_detail(instance)
        self._set_buttons()
        self._poll_states()

    # -- state polling -----------------------------------------------------

    @work(thread=True, exclusive=True, group="status")
    def _poll_states(self) -> None:
        if not self.engine:
            return
        states: dict[str, str] = {}
        started: dict[str, datetime] = {}
        for instance in self.instances:
            try:
                states[instance.slug] = self.engine.state(instance.slug)
            except MchostError:
                states[instance.slug] = "unknown"
                continue
            if states[instance.slug] in ("running", "restarting"):
                try:
                    value = self.engine.started_at(instance.slug)
                except (MchostError, OSError):
                    value = None
                if value is not None:
                    started[instance.slug] = value
        self.call_from_thread(self._apply_states, states, started)

    def _apply_states(
        self, states: dict[str, str], started: dict[str, datetime] | None = None
    ) -> None:
        changed = False
        for slug, state in states.items():
            previous = self.states.get(slug)
            self.states[slug] = state
            if state in ("running", "restarting"):
                if started and slug in started:
                    self._started_at[slug] = started[slug]
            else:
                self._started_at.pop(slug, None)
            row = self.rows.get(slug)
            if row:
                row.set_state(state)
            if previous != state:
                changed = True

        for stale in set(self.states) - set(states):
            self.states.pop(stale, None)
        for stale in set(self._started_at) - set(states):
            self._started_at.pop(stale, None)

        instance = self._selected()
        if instance:
            self._update_detail(instance)
        self._set_buttons()
        if changed:
            self._sync_log_stream()

    def _tick_uptime(self) -> None:
        instance = self._selected()
        if not instance:
            return
        if self.states.get(instance.slug) not in ("running", "restarting"):
            return
        if instance.slug not in self._started_at:
            return
        self._update_detail(instance)

    def _sync_log_stream(self) -> None:
        if not self.selected_slug:
            return
        state = self.states.get(self.selected_slug)
        active = state in ("running", "restarting")
        if active:
            died = self._log_streamer is not None and not self._log_streamer.alive()
            if self._stream_slug != self.selected_slug or died:
                self._restart_logs()
        elif self._stream_slug == self.selected_slug:
            self._stop_log_stream()
            self._append_log("— container stopped —")

    # -- rendering ---------------------------------------------------------

    def _uptime_label(self, instance: Instance) -> str | None:
        if self.states.get(instance.slug) not in ("running", "restarting"):
            return None
        started = self._started_at.get(instance.slug)
        return format_uptime(started) if started else None

    def _update_detail(self, instance: Instance) -> None:
        state = self.states.get(instance.slug, "unknown")
        color = STATE_COLORS.get(state, "dim")
        self.query_one("#detail-title", Static).update(
            f"[bold]{instance.name}[/]"
        )
        self.query_one("#detail-state", Static).update(f"[{color}]● {state}[/]")
        self.query_one("#overview", Static).update(self._overview(instance))

    def _overview(self, instance: Instance) -> Panel:
        state = self.states.get(instance.slug, "unknown")
        color = STATE_COLORS.get(state, "dim")
        uptime = self._uptime_label(instance)
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)
        table.add_column()
        state_text = f"[{color}]{state}[/]"
        if uptime:
            state_text += f" · up {uptime}"
        table.add_row("State", state_text)
        table.add_row("Loader", LOADER_LABELS.get(instance.loader, instance.loader))
        table.add_row("Minecraft", instance.mc_version)
        table.add_row(
            "Loader build", instance.loader_version or "[yellow]not pinned[/]"
        )
        table.add_row("Java", str(instance.java))
        table.add_row("Memory", instance.memory)
        table.add_row("Game port", str(instance.port))
        table.add_row("RCON", f"127.0.0.1:{instance.rcon_port}")
        table.add_row(
            "BlueMap",
            str(instance.bluemap_port) if instance.bluemap_port else "disabled",
        )
        table.add_row("Timezone", instance.tz)
        if instance.motd:
            table.add_row("MOTD", instance.motd)
        table.add_row("Data", str(self.layout.server_dir_for(instance)))
        table.add_row("Config", str(self.layout.config_file(instance.slug)))
        if instance.modpack:
            table.add_row(
                "Modpack",
                f"{instance.modpack.project} {instance.modpack.version_number}",
            )
        if self.engine_error:
            table.add_row("Engine", f"[red]{self.engine_error}[/]")
        return Panel(
            table,
            title=f"{instance.name} · {instance.version_label}",
            border_style="cyan",
        )

    def _show_empty(self) -> None:
        self.query_one("#detail-title", Static).update("[bold]mchost[/]")
        self.query_one("#detail-state", Static).update("[dim]no instance[/]")
        self.query_one("#overview", Static).update(
            Panel(
                "No instances yet.\n\n"
                "Click [bold]+ New instance[/] in the sidebar, or run:\n"
                "[bold]mchost new MyServer --loader fabric --mc-version 1.20.4[/]",
                title="Welcome",
                border_style="cyan",
            )
        )
        self.query_one("#logs-view", RichLog).write(
            "No instance selected. Create one with 'mchost new'."
        )

    def _set_buttons(self) -> None:
        has_instance = self.selected_slug is not None
        available = self.engine is not None and has_instance
        state = self.states.get(self.selected_slug or "", "unknown")
        active = state in ("running", "restarting")
        self.query_one("#btn-start", Button).disabled = not available or active
        self.query_one("#btn-stop", Button).disabled = not available or not active
        self.query_one("#btn-restart", Button).disabled = not available
        instance = self._selected()
        self.query_one("#btn-version", Button).disabled = (
            not available or instance is None or instance.kind == "modpack"
        )
        self.query_one("#btn-more", Button).disabled = not has_instance
    # -- logs --------------------------------------------------------------

    def _restart_logs(self) -> None:
        self._stop_log_stream()
        self._log_history.clear()
        log = self.query_one("#logs-view", RichLog)
        log.clear()
        if not self.selected_slug:
            return
        if self.engine is None:
            log.write("Container engine unavailable.")
            return
        if not isinstance(self.engine, Engine):
            return
        try:
            running = self.engine.is_running(self.selected_slug)
        except MchostError as exc:
            log.write(f"error: {exc}")
            return
        if not running:
            log.write("This instance is not running. Press Start or 's'.")
            return
        instance = self._selected()
        if not instance:
            return
        try:
            self._log_streamer = LogStreamer(
                self.engine,
                self.layout,
                instance,
                lambda line: self.call_from_thread(self._append_log, line),
            )
            self._stream_slug = instance.slug
        except OSError as exc:
            log.write(f"could not stream logs: {exc}")

    def _stop_log_stream(self) -> None:
        if self._log_streamer:
            self._log_streamer.stop()
            self._log_streamer = None
        self._stream_slug = None

    def _append_log(self, line: str) -> None:
        self._log_history.append(line)
        if len(self._log_history) > 2000:
            del self._log_history[: len(self._log_history) - 2000]
        for selector in ("#logs-view", "#console-log"):
            try:
                self.query_one(selector, RichLog).write(line)
            except Exception:
                pass

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.data_table.id == "mods-table":
            self._refresh_file_actions("mods")
        elif event.data_table.id == "configs-table":
            self._refresh_file_actions("configs")

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        if event.data_table.id == "mods-table":
            self._refresh_file_actions("mods")
        elif event.data_table.id == "configs-table":
            self._refresh_file_actions("configs")

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        pane = event.pane.id
        if pane == "tab-logs":
            try:
                log = self.query_one("#logs-view", RichLog)
            except Exception:
                return
            log.clear()
            for line in self._log_history:
                log.write(line)
        elif pane == "tab-mods":
            self._refresh_folder("mods")
        elif pane == "tab-configs":
            self._refresh_folder("configs")
        elif pane == "tab-backups":
            self._refresh_backups()
        elif pane == "tab-modpacks":
            self._refresh_modpacks_tab()

    # -- modpack status / updates ------------------------------------------

    def _refresh_modpacks_tab(self) -> None:
        summary = self.query_one("#modpacks-summary", Static)
        details = self.query_one("#modpacks-details", Static)
        check = self.query_one("#modpacks-check", Button)
        link_button = self.query_one("#modpacks-link", Button)
        instance = self._selected()
        if not instance or instance.kind != "modpack" or not instance.modpack:
            self._modpack_link = None
            summary.update("Modrinth modpacks")
            details.update(
                "Select a modpack instance to check for updates, or browse "
                "Modrinth to install one."
            )
            check.disabled = True
            link_button.disabled = True
            return
        pack = instance.modpack
        if pack.source != "modrinth":
            self._modpack_link = None
            summary.update(
                f"Local pack · {pack.project} {pack.version_number}".strip()
            )
            details.update(
                "Local CurseForge server pack\n"
                f"Loader: {LOADER_LABELS.get(instance.loader, instance.loader)} "
                f"{instance.loader_version or ''} · MC {instance.mc_version}\n"
                f"File: {pack.file_name}\n"
                "Install a newer server pack to update this instance."
            )
            check.disabled = True
            link_button.disabled = True
            return
        url = f"https://modrinth.com/modpack/{pack.project}"
        self._modpack_link = url
        summary.update(f"{pack.project} · {pack.version_number}")
        link_text = Text(url, style=Style(link=url, color="cyan", underline=True))
        details_text = Text()
        details_text.append_text(link_text)
        details_text.append(
            f"\nMode: {pack.mode} · File: {pack.file_name}"
        )
        details_text.append(
            f"\nSHA-512 {pack.sha512[:24]}…", style="dim"
        )
        details.update(details_text)
        check.disabled = False
        link_button.disabled = False

    def _check_modpack_updates(self) -> None:
        instance = self._selected()
        if not instance or instance.kind != "modpack" or not instance.modpack:
            self.notify("Select a modpack instance first.", timeout=4)
            return
        if not self.engine:
            self.notify("Container engine unavailable.", severity="error")
            return
        self.notify("Checking Modrinth for updates…", timeout=3)
        self._check_modpack_worker(instance)

    @work(thread=True, group="modpack-check")
    def _check_modpack_worker(self, instance: Instance) -> None:
        try:
            update = check_modpack_update(instance)
        except (MchostError, OSError) as exc:
            self.call_from_thread(self._warn, f"Update check failed: {exc}")
            return
        self.call_from_thread(self._offer_modpack_update, instance, update)

    def _offer_modpack_update(self, instance: Instance, update) -> None:
        if not update.available:
            self.notify(
                f"{update.project.title} is up to date "
                f"({instance.modpack.version_number}).",
                timeout=5,
            )
            return
        self.push_screen(
            ConfirmDialog(
                "Update modpack",
                f"Update {update.project.title} from "
                f"{instance.modpack.version_number} to "
                f"{update.latest.version_number}?\n\n"
                f"{update.artifact.mode} pack · {update.artifact.file.filename}\n"
                "The server will be stopped and restarted if it was running.",
                "Update",
            ),
            lambda confirmed: (
                self._do_modpack_update(instance, update) if confirmed else None
            ),
        )

    def _do_modpack_update(self, instance: Instance, update) -> None:
        if not self.engine:
            self.notify("Container engine unavailable.", severity="error")
            return
        self.notify(
            f"Updating {update.project.title} to {update.latest.version_number}…",
            timeout=4,
        )
        self._update_modpack_worker(
            instance, update.latest.id, update.artifact.mode
        )

    @work(thread=True, group="modpack-update")
    def _update_modpack_worker(
        self, instance: Instance, version_id: str, mode: str
    ) -> None:
        assert self.engine is not None
        try:
            result = update_modpack(
                self.layout,
                instance,
                version_reference=version_id,
                mode=mode,
                engine=self.engine,
                restart=True,
            )
        except (MchostError, OSError) as exc:
            self.call_from_thread(self._warn, f"Update failed: {exc}")
            return
        self.call_from_thread(self._modpack_updated, result)

    def _modpack_updated(self, result) -> None:
        if result.updated:
            self.notify(
                f"Updated {result.project.title} "
                f"{result.old_version} → {result.new_version}.",
                timeout=6,
            )
        else:
            self.notify("Already up to date.", timeout=4)
        self.run_worker(self._reload_after_new_instance(result.instance.slug))
        self._refresh_modpacks_tab()

    # -- backups -----------------------------------------------------------

    def _refresh_backups(self) -> None:
        table = self.query_one("#backups-table", DataTable)
        summary = self.query_one("#backups-summary", Static)
        table.clear()
        instance = self._selected()
        if not instance:
            summary.update("Select an instance.")
            return
        backups = list_backups(self.layout, instance.slug)
        if not backups:
            summary.update("No backups yet.")
            return
        total = sum(backup.size for backup in backups)
        label = "backup" if len(backups) == 1 else "backups"
        summary.update(f"{len(backups)} {label} · {human_size(total)}")
        for backup in backups:
            table.add_row(
                short_time(backup.created_at),
                backup.kind,
                human_size(backup.size),
                backup.backup_id,
                key=backup.backup_id,
            )

    def _selected_backup_id(self) -> str | None:
        table = self.query_one("#backups-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        if not 0 <= table.cursor_row < table.row_count:
            return None
        try:
            row = table.get_row_at(table.cursor_row)
        except Exception:
            return None
        return str(row[3])

    def _start_backup(self, kind: str) -> None:
        instance = self._selected()
        if not instance:
            self.notify("Select an instance first.", timeout=4)
            return
        if not self.engine:
            self.notify("Container engine unavailable.", severity="error")
            return
        self.notify(f"Creating {kind} backup…", timeout=3)
        self._backup_worker(instance, kind)

    @work(thread=True, exclusive=True, group="backup")
    def _backup_worker(self, instance: Instance, kind: str) -> None:
        assert self.engine is not None
        try:
            backup = create_backup(
                self.layout, instance, kind=kind, engine=self.engine
            )
        except (MchostError, OSError) as exc:
            self.call_from_thread(self._warn, f"Backup failed: {exc}")
            return
        self.call_from_thread(self._backup_done, backup)

    def _backup_done(self, backup: Backup) -> None:
        self.notify(
            f"Backup {backup.backup_id} created ({human_size(backup.size)}).",
            timeout=6,
        )
        self._refresh_backups()

    def _restore_selected(self) -> None:
        instance = self._selected()
        backup_id = self._selected_backup_id()
        if not instance or not backup_id:
            self.notify("Select a backup row first.", timeout=4)
            return
        try:
            backup = get_backup(self.layout, instance.slug, backup_id)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(
            ConfirmDialog(
                "Restore backup",
                f"Restore {backup.backup_id} over {instance.name}?\n"
                "Existing world and config files will be replaced. The server "
                "will be stopped first and restarted if it was running.",
                "Restore",
                danger=True,
            ),
            lambda confirmed: (
                self._do_restore(instance, backup) if confirmed else None
            ),
        )

    def _do_restore(self, instance: Instance, backup: Backup) -> None:
        if not self.engine:
            self.notify("Container engine unavailable.", severity="error")
            return
        try:
            was_running = self.engine.is_running(instance.slug)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify("Restoring backup…", timeout=3)
        self._restore_worker(instance, backup, was_running)

    @work(thread=True, exclusive=True, group="backup")
    def _restore_worker(
        self, instance: Instance, backup: Backup, was_running: bool
    ) -> None:
        assert self.engine is not None
        try:
            restore_backup(
                self.layout,
                instance,
                backup,
                engine=self.engine,
                start_after=was_running,
            )
        except (MchostError, OSError) as exc:
            self.call_from_thread(self._warn, f"Restore failed: {exc}")
            return
        self.call_from_thread(self._restore_done, instance.slug)

    def _restore_done(self, slug: str) -> None:
        self.notify(f"Restored {slug}.", timeout=6)
        self._refresh_backups()
        self._poll_states()
        self._restart_logs()

    def _delete_selected(self) -> None:
        instance = self._selected()
        backup_id = self._selected_backup_id()
        if not instance or not backup_id:
            self.notify("Select a backup row first.", timeout=4)
            return
        self.push_screen(
            ConfirmDialog(
                "Delete backup",
                f"Delete backup {backup_id} for {instance.name}? This cannot be undone.",
                "Delete",
                danger=True,
            ),
            lambda confirmed: (
                self._delete_backup(instance.slug, backup_id)
                if confirmed
                else None
            ),
        )

    def _delete_backup(self, slug: str, backup_id: str) -> None:
        try:
            delete_backup(self.layout, slug, backup_id)
        except (MchostError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Deleted backup {backup_id}.", timeout=4)
        self._refresh_backups()

    def _reveal_backups(self) -> None:
        instance = self._selected()
        if instance:
            self._copy_path(self.layout.backups_for(instance.slug))

    def _copy_path(self, path: Path) -> None:
        self._copy_text(str(path))

    def _copy_text(self, text: str) -> None:
        try:
            self.copy_to_clipboard(text)
            self.notify(f"Copied {text}", timeout=5)
        except Exception:
            self.notify(text, timeout=8)

    # -- mods / configs ----------------------------------------------------

    def _refresh_folder(self, kind: str) -> None:
        instance = self._selected()
        if not instance:
            return
        folder = "mods" if kind == "mods" else "config"
        path = self.layout.server_dir_for(instance) / folder
        summary = self.query_one(f"#{kind}-summary", Static)
        summary.update("Loading…")
        self._scan_folder(kind, instance.slug, path)

    @work(thread=True, exclusive=True, group="files")
    def _scan_folder(self, kind: str, slug: str, path: Path) -> None:
        error: str | None = None
        try:
            entries, truncated = scan_files(path)
        except OSError as exc:
            entries, truncated, error = [], False, str(exc)
        self.call_from_thread(
            self._populate_files, kind, slug, path, entries, truncated, error
        )

    def _populate_files(
        self,
        kind: str,
        slug: str,
        path: Path,
        entries: list[tuple[str, int, float]],
        truncated: bool,
        error: str | None,
    ) -> None:
        if slug != self.selected_slug:
            return
        folder = "mods" if kind == "mods" else "config"
        table = self.query_one(f"#{kind}-table", DataTable)
        summary = self.query_one(f"#{kind}-summary", Static)
        table.clear()

        if error:
            summary.update(f"Could not read {folder}/: {error}")
            self._refresh_file_actions(kind)
            return
        if not path.is_dir():
            summary.update(
                f"No {folder}/ folder yet. Start the server once to generate it."
            )
            self._refresh_file_actions(kind)
            return
        if not entries:
            summary.update(f"No files in {folder}/.")
            self._refresh_file_actions(kind)
            return

        for relative, size, modified in entries:
            state = "—"
            if kind == "mods":
                state = (
                    "disabled"
                    if is_mod_disabled(Path(relative))
                    else "enabled"
                )
            table.add_row(
                relative,
                format_size(size),
                datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M"),
                state,
            )
        total = sum(size for _, size, _ in entries)
        label = f"{len(entries)} file" + ("" if len(entries) == 1 else "s")
        extra = " · showing the first 500" if truncated else ""
        summary.update(f"{label} · {format_size(total)}{extra}")
        self._refresh_file_actions(kind)

    def _file_root(self, kind: str) -> Path | None:
        instance = self._selected()
        if not instance:
            return None
        folder = "mods" if kind == "mods" else "config"
        return (self.layout.server_dir_for(instance) / folder).resolve()

    def _selected_file(self, kind: str) -> Path | None:
        table = self.query_one(f"#{kind}-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        if not 0 <= table.cursor_row < table.row_count:
            return None
        try:
            row = table.get_row_at(table.cursor_row)
        except Exception:
            return None
        relative = str(row[0])
        root = self._file_root(kind)
        if root is None:
            return None
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _refresh_file_actions(self, kind: str) -> None:
        path = self._selected_file(kind)
        if kind == "mods":
            toggle = self.query_one("#mods-toggle", Button)
            delete = self.query_one("#mods-delete", Button)
            reveal = self.query_one("#mods-reveal", Button)
            has_file = path is not None
            toggle.disabled = not has_file
            delete.disabled = not has_file
            reveal.disabled = not has_file
            if path is not None:
                toggle.label = "Enable" if is_mod_disabled(path) else "Disable"
        else:
            edit = self.query_one("#configs-edit", Button)
            reveal = self.query_one("#configs-reveal", Button)
            has_file = path is not None
            edit.disabled = not has_file
            reveal.disabled = not has_file

    def _server_is_running(self) -> bool:
        if not self.engine or not self.selected_slug:
            return False
        try:
            return self.engine.is_running(self.selected_slug)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return True

    def _require_stopped(self) -> bool:
        if self._server_is_running():
            self.notify(
                "Stop the server before changing mods.", severity="warning"
            )
            return False
        return True

    def _toggle_selected_mod(self) -> None:
        path = self._selected_file("mods")
        if not path:
            self.notify("Select a mod first.", timeout=4)
            return
        if not self._require_stopped():
            return
        was_disabled = is_mod_disabled(path)
        try:
            result = toggle_mod(path)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        verb = "Enabled" if was_disabled else "Disabled"
        self.notify(f"{verb} {path.name}", timeout=5)
        self._refresh_folder("mods")

    def _delete_selected_mod(self) -> None:
        path = self._selected_file("mods")
        if not path:
            self.notify("Select a mod first.", timeout=4)
            return
        if not self._require_stopped():
            return
        self.push_screen(
            ConfirmDialog(
                "Delete mod",
                f"Delete {path.name}? This cannot be undone.",
                "Delete",
                danger=True,
            ),
            lambda confirmed: self._do_delete_mod(path) if confirmed else None,
        )

    def _do_delete_mod(self, path: Path) -> None:
        try:
            delete_file(path)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Deleted {path.name}", timeout=5)
        self._refresh_folder("mods")

    def _reveal_selected(self, kind: str) -> None:
        path = self._selected_file(kind)
        root = self._file_root(kind)
        if path:
            self._copy_path(path)
        elif root:
            self._copy_path(root)

    def _edit_selected_config(self) -> None:
        path = self._selected_file("configs")
        if not path:
            self.notify("Select a config file first.", timeout=4)
            return
        root = self._file_root("configs")
        relative = str(path.relative_to(root)) if root else path.name
        try:
            text = read_text_file(path)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(
            ConfigEditor(path, relative, text), self._config_saved
        )

    def _config_saved(self, saved: bool | None) -> None:
        if not saved:
            return
        self.notify(
            "Config saved. Restart the server to apply it.", timeout=6
        )
        self._refresh_folder("configs")

    def _clear_console(self) -> None:
        try:
            self.query_one("#console-log", RichLog).clear()
        except Exception:
            pass

    def _write_console(self, line: str) -> None:
        if not line:
            return
        try:
            self.query_one("#console-log", RichLog).write(line)
        except Exception:
            pass

    # -- actions -----------------------------------------------------------

    def _typing(self) -> bool:
        return isinstance(self.focused, Input)

    def action_start(self) -> None:
        if self._typing() or not self.selected_slug:
            return
        self._request_start(self.selected_slug)

    def action_stop(self) -> None:
        if self._typing() or not self.selected_slug:
            return
        self._request_stop(self.selected_slug)

    def action_restart(self) -> None:
        if self._typing() or not self.selected_slug:
            return
        self._request_restart(self.selected_slug)

    def action_backup(self) -> None:
        if self._typing() or not self.selected_slug:
            return
        self.query_one(TabbedContent).active = "tab-backups"
        self._refresh_backups()
        self._start_backup("world")

    def action_version(self) -> None:
        if self._typing() or not self.selected_slug:
            return
        self.show_version_picker()

    def action_memory(self) -> None:
        if self._typing() or not self.selected_slug:
            return
        self.show_memory_editor()

    def show_memory_editor(self) -> None:
        instance = self._selected()
        if not instance:
            self.notify("Select an instance first.", timeout=4)
            return
        self.push_screen(
            MemoryEditor(self.layout, instance), self._memory_updated
        )

    def _show_memory_for(self, slug: str) -> None:
        self.select_instance(slug)
        self.show_memory_editor()

    def _memory_updated(self, updated: bool | None) -> None:
        if not updated:
            return
        instance = self._selected()
        if instance:
            self._update_detail(instance)
        self.notify(
            "Memory updated. Restart the instance to apply it.", timeout=6
        )

    def action_modpack_browser(self) -> None:
        if self._typing():
            return
        self.push_screen(ModpackBrowser(self.layout), self._modpack_created)

    def action_local_pack_installer(self) -> None:
        if self._typing():
            return
        self.push_screen(
            LocalPackInstaller(self.layout), self._local_pack_installed
        )

    def _local_pack_installed(self, slug: str | None) -> None:
        if not slug:
            return
        self.notify(f"Installed local pack as {slug}.", timeout=5)
        self.run_worker(self._reload_after_new_instance(slug))

    def _modpack_created(self, slug: str | None) -> None:
        if not slug:
            return
        self.notify(f"Installed modpack instance {slug}.", timeout=5)
        self.run_worker(self._reload_after_new_instance(slug))

    def action_new(self) -> None:
        if not self._typing():
            self.push_screen(NewInstanceWizard(self.layout), self._new_instance_created)

    def _new_instance_created(self, slug: str | None) -> None:
        if not slug:
            return
        self.notify(f"Created {slug}.", timeout=5)
        self.run_worker(self._reload_after_new_instance(slug))

    async def _reload_after_new_instance(self, slug: str) -> None:
        self._load_instances()
        await self._rebuild_sidebar()
        if slug in self.rows:
            self.select_instance(slug)
        self._set_buttons()
        self._poll_states()

    def action_help(self) -> None:
        if not self._typing():
            self.notify(
                "s start · x stop · r restart · b backup · n new · "
                "F5 refresh · Ctrl+P commands",
                timeout=8,
            )

    async def action_refresh(self) -> None:
        if self._typing():
            return
        self._load_instances()
        await self._rebuild_sidebar()
        if not self.instances:
            self.selected_slug = None
            self._show_empty()
        elif self.selected_slug not in self.rows:
            self.select_instance(self.instances[0].slug)
        else:
            instance = self._selected()
            if instance:
                self._update_detail(instance)
        self._set_buttons()
        self._poll_states()
        self.notify("Refreshed.", timeout=2)

    def handle_row_action(self, slug: str, action: str) -> None:
        if action in ("start", "stop", "restart", "version"):
            self.select_instance(slug)
        if action == "start":
            self._request_start(slug)
        elif action == "stop":
            self._request_stop(slug)
        elif action == "restart":
            self._request_restart(slug)
        elif action == "version":
            self.show_version_picker()
        elif action == "memory":
            self.show_memory_editor()
        elif action == "backup":
            self.select_instance(slug)
            self.query_one(TabbedContent).active = "tab-backups"
            self._refresh_backups()
            self._start_backup("world")
        elif action == "reveal":
            self._reveal(slug)
        elif action == "delete":
            self._confirm_delete_instance(slug)

    def _confirm_delete_instance(self, slug: str) -> None:
        try:
            instance = resolve_instance(self.layout, slug)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        message = (
            f"Delete {instance.name}?\n\n"
            f"Instance: {self.layout.instance_dir(instance.slug)}\n"
            f"Data: {self.layout.server_dir_for(instance)}\n"
            f"Backups: {self.layout.backups_for(instance.slug)}\n\n"
            "The container is stopped first. This cannot be undone."
        )
        self.push_screen(
            ConfirmDialog(
                "Delete instance",
                message,
                "Delete instance",
                danger=True,
            ),
            lambda confirmed: (
                self._delete_instance_worker(instance) if confirmed else None
            ),
        )

    @work(thread=True, group="delete")
    def _delete_instance_worker(self, instance: Instance) -> None:
        try:
            delete_instance(self.layout, instance, engine=self.engine)
        except (MchostError, OSError) as exc:
            self.call_from_thread(self._warn, f"Delete failed: {exc}")
            return
        self.call_from_thread(self._instance_deleted, instance.slug)

    def _instance_deleted(self, slug: str) -> None:
        self.notify(f"Deleted {slug}.", timeout=5)
        if self.selected_slug == slug:
            self.selected_slug = None
        self.run_worker(self._reload_after_delete())

    async def _reload_after_delete(self) -> None:
        self._load_instances()
        await self._rebuild_sidebar()
        if self.instances:
            self.select_instance(self.instances[0].slug)
        else:
            self.selected_slug = None
            self._show_empty()
        self._set_buttons()
        self._poll_states()

    def show_version_picker(self) -> None:
        instance = self._selected()
        if not instance:
            self.notify("Select an instance first.", timeout=4)
            return
        if instance.kind == "modpack":
            self.notify(
                "Modpack versions are managed by their pack; "
                "open the Modpacks tab to check for updates.",
                timeout=7,
            )
            return
        self.push_screen(
            VersionPicker(self.layout, instance), self._version_updated
        )

    def _version_updated(self, updated: bool | None) -> None:
        if not updated:
            return
        self.notify(
            "Version updated. Restart the instance to apply it.", timeout=6
        )
        self.run_worker(self._reload_after_version())

    async def _reload_after_version(self) -> None:
        self._load_instances()
        await self._rebuild_sidebar()
        if self.selected_slug in self.rows:
            self.select_instance(self.selected_slug)
        self._set_buttons()
        self._poll_states()

    def _reveal(self, slug: str) -> None:
        try:
            instance = resolve_instance(self.layout, slug)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        self._copy_path(self.layout.server_dir_for(instance))

    def _request_start(self, slug: str) -> None:
        if not self.engine:
            self.notify("Container engine unavailable.", severity="error")
            return
        try:
            instance = resolve_instance(self.layout, slug)
            if self.engine.is_running(slug):
                self.notify(f"{instance.name} is already running.", timeout=4)
                return
            running_slugs = [item for item in self.engine.running_slugs() if item != slug]
            running = [resolve_instance(self.layout, item) for item in running_slugs]
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return

        if running:
            def confirmed(result: bool | None) -> None:
                if result:
                    self._start_worker(slug, [item.slug for item in running], restart=False)

            self.push_screen(ConfirmSwitch(running, instance), confirmed)
        else:
            self._start_worker(slug, [], restart=False)

    def _request_stop(self, slug: str) -> None:
        if not self.engine:
            self.notify("Container engine unavailable.", severity="error")
            return
        self._start_worker(slug, [], restart=False, stop_only=True)

    def _request_restart(self, slug: str) -> None:
        if not self.engine:
            self.notify("Container engine unavailable.", severity="error")
            return
        try:
            running_slugs = [item for item in self.engine.running_slugs() if item != slug]
            running = [resolve_instance(self.layout, item) for item in running_slugs]
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        if running:
            instance = resolve_instance(self.layout, slug)

            def confirmed(result: bool | None) -> None:
                if result:
                    self._start_worker(
                        slug, [item.slug for item in running], restart=True
                    )

            self.push_screen(ConfirmSwitch(running, instance), confirmed)
        else:
            self._start_worker(slug, [], restart=True)

    @work(thread=True, exclusive=True, group="action")
    def _start_worker(
        self,
        slug: str,
        stop_slugs: list[str],
        *,
        restart: bool,
        stop_only: bool = False,
    ) -> None:
        assert self.engine is not None
        try:
            for other_slug in stop_slugs:
                self._stop_slug(other_slug)
            if stop_only:
                self._stop_slug(slug)
                instance = resolve_instance(self.layout, slug)
                self.call_from_thread(
                    self._after_action, slug, f"Stopped {instance.name}", None
                )
                return
            if restart:
                self._stop_slug(slug)
            instance = resolve_instance(self.layout, slug)
            write_compose(self.layout, instance)
            self.engine.up(slug)
            verb = "Restarted" if restart else "Started"
            self.call_from_thread(
                self._after_action,
                slug,
                f"{verb} {instance.name}",
                None,
                True,
            )
        except MchostError as exc:
            self.call_from_thread(self._after_action, slug, None, str(exc))
        except OSError as exc:
            self.call_from_thread(self._after_action, slug, None, str(exc))

    def _stop_slug(self, slug: str) -> None:
        assert self.engine is not None
        state = self.engine.state(slug)
        if state != "missing":
            self.engine.down(slug)
        instance = resolve_instance(self.layout, slug)
        warning = restore_ownership(self.layout, instance)
        if warning:
            self.call_from_thread(self._warn, warning)

    def _after_action(
        self,
        slug: str,
        message: str | None,
        error: str | None,
        focus_console: bool = False,
    ) -> None:
        if error:
            self.notify(error, severity="error", timeout=10)
        elif message:
            self.notify(message, timeout=4)
        if focus_console and not error:
            self.query_one(TabbedContent).active = "tab-console"
        if slug == self.selected_slug:
            instance = self._selected()
            if instance:
                self._update_detail(instance)
        self._poll_states()

    def _warn(self, message: str) -> None:
        self.notify(message, severity="warning", timeout=8)

    def show_context_menu(self, slug: str) -> None:
        try:
            instance = resolve_instance(self.layout, slug)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(ContextMenu(instance))

    # -- console -----------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "console-input":
            self._send_console()

    def _send_console(self) -> None:
        if not self.selected_slug:
            self.notify("Select an instance first.", timeout=4)
            return
        if not self.engine:
            self.notify("Container engine unavailable.", severity="error")
            return
        input_widget = self.query_one("#console-input", Input)
        command = input_widget.value.strip()
        if not command:
            return
        try:
            running = self.engine.is_running(self.selected_slug)
        except MchostError as exc:
            self.notify(str(exc), severity="error")
            return
        if not running:
            self.notify("The server is not running.", severity="warning")
            return
        input_widget.value = ""
        self._write_console(f"> {command}")
        self._run_console(self.selected_slug, command)

    @work(thread=True, group="rcon")
    def _run_console(self, slug: str, command: str) -> None:
        assert self.engine is not None
        try:
            output = self.engine.exec_capture(slug, ["rcon-cli", command])
        except MchostError as exc:
            output = f"error: {exc}"
        self.call_from_thread(self._write_console, output or "(no output)")

    # -- mouse / buttons ---------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id == "btn-start":
            self.action_start()
        elif button_id == "btn-stop":
            self.action_stop()
        elif button_id == "btn-restart":
            self.action_restart()
        elif button_id == "btn-version":
            self.action_version()
        elif button_id == "btn-more":
            if self.selected_slug:
                self.show_context_menu(self.selected_slug)
        elif button_id == "btn-commands":
            self.action_command_palette()
        elif button_id == "new-instance":
            self.action_new()
        elif button_id == "console-send":
            self._send_console()
        elif button_id == "mods-refresh":
            self._refresh_folder("mods")
        elif button_id == "configs-refresh":
            self._refresh_folder("configs")
        elif button_id == "mods-toggle":
            self._toggle_selected_mod()
        elif button_id == "mods-delete":
            self._delete_selected_mod()
        elif button_id == "mods-reveal":
            self._reveal_selected("mods")
        elif button_id == "configs-edit":
            self._edit_selected_config()
        elif button_id == "configs-reveal":
            self._reveal_selected("configs")
        elif button_id == "backup-world":
            self._start_backup("world")
        elif button_id == "backup-full":
            self._start_backup("full")
        elif button_id == "backups-refresh":
            self._refresh_backups()
        elif button_id == "backups-restore":
            self._restore_selected()
        elif button_id == "backups-delete":
            self._delete_selected()
        elif button_id == "backups-reveal":
            self._reveal_backups()
        elif button_id == "modpacks-browse":
            self.action_modpack_browser()
        elif button_id == "modpacks-local":
            self.action_local_pack_installer()
        elif button_id == "modpacks-check":
            self._check_modpack_updates()
        elif button_id == "modpacks-refresh":
            self._refresh_modpacks_tab()
        elif button_id == "modpacks-link":
            if self._modpack_link:
                self._copy_text(self._modpack_link)

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "New instance",
            "Create a new Minecraft server",
            self.action_new,
        )
        yield SystemCommand(
            "Install Modrinth modpack",
            "Browse Modrinth and create a server instance",
            self.action_modpack_browser,
        )
        yield SystemCommand(
            "Install local CurseForge pack",
            "Install a server-pack archive you downloaded",
            self.action_local_pack_installer,
        )
        for instance in self.instances:
            yield SystemCommand(
                f"Start {instance.name}",
                "Start this instance",
                lambda item=instance: self._request_start(item.slug),
            )
            yield SystemCommand(
                f"Stop {instance.name}",
                "Stop this instance",
                lambda item=instance: self._request_stop(item.slug),
            )
            yield SystemCommand(
                f"Select {instance.name}",
                "Show this instance in the dashboard",
                lambda item=instance: self.select_instance(item.slug),
            )
            yield SystemCommand(
                f"Change memory for {instance.name}",
                "Edit the Java heap for this instance",
                lambda item=instance: self._show_memory_for(item.slug),
            )


def run(layout: Layout) -> None:
    MchostApp(layout).run()
