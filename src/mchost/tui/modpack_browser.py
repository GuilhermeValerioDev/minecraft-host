"""Browse Modrinth modpacks and install a server-ready artifact."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static
from rich.style import Style
from rich.text import Text

from ..config import MEMORY_RE
from ..errors import MchostError
from ..modrinth import (
    Artifact,
    InstallResult,
    ModpackHit,
    ModpackProject,
    ModrinthVersion,
    get_project,
    get_versions,
    install_modpack,
    resolve_artifact,
    search_modpacks,
    select_version,
)
from ..paths import Layout

MODES = (("auto", "Auto"), ("server", "Server pack"), ("full", "Full pack"))


class ModpackBrowser(ModalScreen[str | None]):
    """Search Modrinth, pick a version, and install it as a new instance."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    ModpackBrowser {
        align: center middle;
    }
    #pack-browser {
        width: 92%;
        max-width: 116;
        height: 96%;
        max-height: 48;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }
    #pack-title {
        text-style: bold;
    }
    #pack-subtitle {
        color: $text-muted;
        padding-bottom: 1;
    }
    #pack-search-row {
        height: 3;
    }
    #pack-query {
        width: 1fr;
    }
    #pack-results {
        height: 6;
        border: solid $panel;
    }
    #pack-versions {
        height: 6;
        border: solid $panel;
    }
    #pack-link-row {
        height: 3;
    }
    #pack-link {
        width: 1fr;
        content-align: left middle;
        color: $text-muted;
    }
    #pack-copy-link {
        min-width: 12;
    }
    #pack-info {
        height: 4;
        color: $text-muted;
        padding: 1 0 0 0;
    }
    #pack-form {
        height: 3;
    }
    #pack-name {
        width: 1fr;
    }
    #pack-memory {
        width: 12;
    }
    .mode-button {
        min-width: 12;
        margin-right: 1;
    }
    Button.mode-button.selected {
        background: $accent;
        color: $text;
    }
    #pack-modes {
        height: 3;
    }
    #pack-status {
        height: 1;
        color: $text-muted;
    }
    #pack-error {
        height: 2;
        color: $error;
    }
    #pack-buttons {
        height: 3;
        align-horizontal: right;
    }
    #pack-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, layout: Layout) -> None:
        super().__init__()
        self._layout = layout
        self.hits: list[ModpackHit] = []
        self.project: ModpackProject | None = None
        self.versions: list[ModrinthVersion] = []
        self.selected_version: ModrinthVersion | None = None
        self.artifact: Artifact | None = None
        self.mode = "auto"
        self._installing = False
        self._loaded_slug: str | None = None
        self.current_link: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="pack-browser"):
            yield Static("[bold]Modrinth modpacks[/]", id="pack-title")
            yield Static(
                "mchost prefers a published server pack; otherwise it uses the full pack.",
                id="pack-subtitle",
            )
            with Horizontal(id="pack-search-row"):
                yield Input(
                    placeholder="Search modpacks…", id="pack-query"
                )
                yield Button("Search", id="pack-search", variant="primary")
            yield DataTable(
                id="pack-results", zebra_stripes=True, cursor_type="row"
            )
            with Horizontal(id="pack-link-row"):
                yield Static(
                    "Select a modpack to see its Modrinth page.",
                    id="pack-link",
                )
                yield Button(
                    "Copy link", id="pack-copy-link", disabled=True
                )
            yield DataTable(
                id="pack-versions", zebra_stripes=True, cursor_type="row"
            )
            yield Static("", id="pack-info")
            with Horizontal(id="pack-form"):
                yield Input(placeholder="Instance name", id="pack-name")
                yield Input(value="6G", placeholder="6G", id="pack-memory")
            with Horizontal(id="pack-modes"):
                for value, label in MODES:
                    classes = "mode-button"
                    if value == "auto":
                        classes += " selected"
                    yield Button(
                        label, id=f"pack-mode-{value}", classes=classes
                    )
            yield Static("", id="pack-status")
            yield Static("", id="pack-error")
            with Horizontal(id="pack-buttons"):
                yield Button("Cancel", id="pack-cancel")
                yield Button(
                    "Install", id="pack-install", variant="primary", disabled=True
                )

    def on_mount(self) -> None:
        results = self.query_one("#pack-results", DataTable)
        results.add_columns("Title", "Loaders", "MC", "Downloads")
        versions = self.query_one("#pack-versions", DataTable)
        versions.add_columns("Version", "Type", "MC", "Loaders")
        self._search("")

    # -- search ------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "pack-query":
            self._search(event.input.value.strip())

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "pack-name":
            self._refresh_install()

    @work(thread=True, exclusive=True, group="pack-search")
    def _search(self, query: str) -> None:
        self.app.call_from_thread(self._set_status, "Searching Modrinth…")
        try:
            hits = search_modpacks(query)
        except MchostError as exc:
            self.app.call_from_thread(self._show_error, str(exc))
            return
        self.app.call_from_thread(self._populate_results, hits)

    def _populate_results(self, hits: list[ModpackHit]) -> None:
        self.hits = hits
        self._loaded_slug = None
        table = self.query_one("#pack-results", DataTable)
        table.clear()
        for hit in hits:
            table.add_row(
                hit.title,
                ", ".join(hit.loaders) or "—",
                ", ".join(hit.game_versions[:3]) or "—",
                f"{hit.downloads:,}",
                key=hit.slug,
            )
        if hits:
            self._set_link(hits[0].slug)
        self._set_status(f"{len(hits)} results")
        self._clear_error()

    def _set_link(self, slug: str) -> None:
        url = f"https://modrinth.com/modpack/{slug}"
        self.current_link = url
        link = Text(url, style=Style(link=url, color="cyan", underline=True))
        self.query_one("#pack-link", Static).update(link)
        self.query_one("#pack-copy-link", Button).disabled = False

    # -- project / versions ------------------------------------------------

    @work(thread=True, exclusive=True, group="pack-project")
    def _load_project(self, slug: str) -> None:
        self._loaded_slug = slug
        self._set_link(slug)
        self.app.call_from_thread(self._set_status, "Loading versions…")
        try:
            project = get_project(slug)
            versions = get_versions(slug)
        except MchostError as exc:
            self.app.call_from_thread(self._show_error, str(exc))
            return
        self.app.call_from_thread(self._populate_project, project, versions)

    def _populate_project(
        self, project: ModpackProject, versions: list[ModrinthVersion]
    ) -> None:
        self.project = project
        self.versions = versions
        self.selected_version = None
        self.artifact = None
        table = self.query_one("#pack-versions", DataTable)
        table.clear()
        for version in versions:
            table.add_row(
                version.version_number,
                version.version_type,
                ", ".join(version.game_versions[:3]),
                ", ".join(version.loaders),
                key=version.id,
            )
        name_input = self.query_one("#pack-name", Input)
        name_input.value = project.title
        if not versions:
            self._show_error("This modpack has no versions.")
            return
        try:
            version = select_version(versions)
        except MchostError as exc:
            self._show_error(str(exc))
            return
        self._select_version(version.id)
        self.query_one("#pack-versions", DataTable).move_cursor(
            row=versions.index(version)
        )
        self._set_status(
            f"{project.title} · {len(versions)} versions · defaults to {version.version_number}"
        )
        self._clear_error()

    # -- version / artifact ------------------------------------------------

    def _select_version(self, version_id: str) -> None:
        self.selected_version = next(
            (version for version in self.versions if version.id == version_id),
            None,
        )
        if not self.selected_version:
            return
        self._resolve()

    def _resolve(self) -> None:
        if not self.selected_version or not self.versions:
            return
        try:
            self.artifact = resolve_artifact(
                self.selected_version, self.versions, mode=self.mode
            )
        except MchostError as exc:
            self.artifact = None
            self.query_one("#pack-info", Static).update("")
            self._show_error(str(exc))
            self._refresh_install()
            return
        artifact = self.artifact
        self.query_one("#pack-info", Static).update(
            f"[bold]{artifact.mode} pack[/] · {artifact.reason}\n"
            f"{artifact.file.filename} · "
            f"{artifact.file.size / 1024 / 1024:.1f} MB\n"
            f"[dim]SHA-512 {artifact.file.sha512[:24]}…[/]"
        )
        self._clear_error()
        self._refresh_install()

    # -- modes -------------------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        for value, _label in MODES:
            button = self.query_one(f"#pack-mode-{value}", Button)
            button.set_class(value == mode, "selected")
        self._resolve()

    # -- install -----------------------------------------------------------

    def _refresh_install(self) -> None:
        name = self.query_one("#pack-name", Input).value.strip()
        ready = (
            not self._installing
            and self.artifact is not None
            and bool(self.selected_version)
            and bool(name)
        )
        self.query_one("#pack-install", Button).disabled = not ready

    def _install(self) -> None:
        if not self.project or not self.selected_version or not self.artifact:
            self._show_error("Pick a modpack and version first.")
            return
        name = self.query_one("#pack-name", Input).value.strip()
        memory = self.query_one("#pack-memory", Input).value.strip().upper() or "6G"
        if not name:
            self._show_error("Give the instance a name.")
            return
        if not MEMORY_RE.match(memory):
            self._show_error("Memory must look like 6G or 4096M.")
            return
        self._installing = True
        self.query_one("#pack-install", Button).disabled = True
        self._set_status("Downloading modpack…")
        self._clear_error()
        self._install_worker(
            self.project.slug,
            self.selected_version.id,
            name,
            memory,
            self.mode,
        )

    @work(thread=True, group="pack-install")
    def _install_worker(
        self,
        slug: str,
        version_id: str,
        name: str,
        memory: str,
        mode: str,
    ) -> None:
        def progress(downloaded: int, total: int) -> None:
            if total:
                percent = downloaded / total * 100
                message = f"Downloading… {percent:.0f}%"
            else:
                message = f"Downloading… {downloaded / 1024 / 1024:.1f} MB"
            self.app.call_from_thread(self._set_status, message)

        try:
            result = install_modpack(
                self._layout,
                slug,
                version_reference=version_id,
                name=name,
                mode=mode,
                memory=memory,
                progress=progress,
            )
        except (MchostError, OSError) as exc:
            self.app.call_from_thread(self._install_failed, str(exc))
            return
        self.app.call_from_thread(self._install_done, result)

    def _install_done(self, result: InstallResult) -> None:
        self._installing = False
        self.dismiss(result.instance.slug)

    def _install_failed(self, message: str) -> None:
        self._installing = False
        self._refresh_install()
        self._show_error(message)

    # -- helpers -----------------------------------------------------------

    def _set_status(self, message: str) -> None:
        self.query_one("#pack-status", Static).update(message)

    def _show_error(self, message: str) -> None:
        self._set_status("")
        self.query_one("#pack-error", Static).update(message)

    def _clear_error(self) -> None:
        self.query_one("#pack-error", Static).update("")

    def action_cancel(self) -> None:
        if self._installing:
            self._set_status("Install in progress…")
            return
        self.dismiss(None)

    # -- events ------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        if event.data_table.id == "pack-results":
            self._set_link(key)
            if key != self._loaded_slug:
                self._load_project(key)
        elif event.data_table.id == "pack-versions":
            self._select_version(key)

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        key = str(event.row_key.value)
        if event.data_table.id == "pack-results":
            self._set_link(key)
            if key != self._loaded_slug:
                self._load_project(key)
        elif event.data_table.id == "pack-versions":
            self._select_version(key)

    def on_mouse_move(self, event) -> None:
        """Update the Modrinth link as the pointer moves over a result row."""
        try:
            results = self.query_one("#pack-results", DataTable)
            if event.widget is not results:
                return
            meta = event.style.meta or {}
            row = meta.get("row")
            if row is None or row < 0:
                return
            ordered = results.ordered_rows
            if row >= len(ordered):
                return
            key = str(ordered[row].key.value)
            if key and key != self._loaded_slug:
                self._set_link(key)
        except Exception:
            return

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id == "pack-search":
            self._search(self.query_one("#pack-query", Input).value.strip())
        elif button_id == "pack-cancel":
            self.action_cancel()
        elif button_id == "pack-install":
            self._install()
        elif button_id == "pack-copy-link":
            if self.current_link:
                try:
                    self.app.copy_to_clipboard(self.current_link)
                    self._set_status("Copied Modrinth link.")
                except Exception:
                    self._set_status(self.current_link)
        elif button_id.startswith("pack-mode-"):
            self._set_mode(button_id.removeprefix("pack-mode-"))
