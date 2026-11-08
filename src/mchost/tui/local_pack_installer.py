"""Install a CurseForge server pack that the user downloaded."""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..config import LOADER_LABELS, MEMORY_RE
from ..errors import MchostError
from ..localpack import LocalPackInfo, inspect_local_pack, install_local_pack
from ..paths import Layout

MODES = ("vanilla", "paper", "fabric", "quilt", "forge", "neoforge")


class LocalPackInstaller(ModalScreen[str | None]):
    """Pick a local server-pack archive, inspect it, and install it."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    LocalPackInstaller {
        align: center middle;
    }
    #local-form {
        width: 92%;
        max-width: 112;
        height: 92%;
        max-height: 38;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }
    #local-title {
        text-style: bold;
    }
    #local-subtitle {
        color: $text-muted;
        padding-bottom: 1;
    }
    .field-row {
        height: 3;
    }
    .field-label {
        width: 14;
        content-align: left middle;
        color: $text-muted;
    }
    #local-path, #local-name, #local-mc, #local-loader-version, #local-memory {
        width: 1fr;
    }
    #local-inspect {
        min-width: 10;
        margin-left: 1;
    }
    .loader-button {
        min-width: 8;
        margin-right: 1;
    }
    Button.loader-button.selected {
        background: $accent;
        color: $text;
    }
    #local-info {
        height: 4;
        color: $text-muted;
        padding: 1 0 0 0;
    }
    #local-status {
        height: 1;
        color: $text-muted;
    }
    #local-error {
        height: 2;
        color: $error;
    }
    #local-buttons {
        height: 3;
        align-horizontal: right;
    }
    #local-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, layout: Layout) -> None:
        super().__init__()
        self._layout = layout
        self.selected_loader = "vanilla"
        self._info: LocalPackInfo | None = None
        self._installing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="local-form"):
            yield Static("[bold]Install CurseForge pack[/]", id="local-title")
            yield Static(
                "Download the server pack yourself, then point mchost at the "
                "local .zip/.tar.gz. mchost extracts it and wires up the loader.",
                id="local-subtitle",
            )
            with Horizontal(classes="field-row"):
                yield Static("Archive", classes="field-label")
                yield Input(placeholder="/path/to/server-pack.zip", id="local-path")
                yield Button("Inspect", id="local-inspect", variant="primary")
            with Horizontal(classes="field-row"):
                yield Static("Name", classes="field-label")
                yield Input(placeholder="My CurseForge Pack", id="local-name")
            with Horizontal(classes="field-row"):
                yield Static("Loader", classes="field-label")
                with Horizontal(id="local-loaders"):
                    for value in MODES:
                        classes = "loader-button"
                        if value == "vanilla":
                            classes += " selected"
                        yield Button(
                            LOADER_LABELS[value],
                            id=f"local-loader-{value}",
                            classes=classes,
                        )
            with Horizontal(classes="field-row"):
                yield Static("Minecraft", classes="field-label")
                yield Input(placeholder="1.20.1", id="local-mc")
            with Horizontal(classes="field-row"):
                yield Static("Loader build", classes="field-label")
                yield Input(
                    placeholder="e.g. 47.2.0 (blank = latest)",
                    id="local-loader-version",
                )
            with Horizontal(classes="field-row"):
                yield Static("Memory", classes="field-label")
                yield Input(value="6G", id="local-memory")
            yield Static("", id="local-info")
            yield Static("", id="local-status")
            yield Static("", id="local-error")
            with Horizontal(id="local-buttons"):
                yield Button("Cancel", id="local-cancel")
                yield Button(
                    "Install", id="local-install", variant="primary", disabled=True
                )

    def on_mount(self) -> None:
        self.query_one("#local-path", Input).focus()

    # -- inspection --------------------------------------------------------

    def _inspect(self) -> None:
        raw = self.query_one("#local-path", Input).value.strip()
        if not raw:
            self._show_error("Enter the path to a server-pack archive.")
            return
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            self._show_error(f"Archive not found: {path}")
            return
        self._set_status("Inspecting archive…")
        self._clear_error()
        self._inspect_worker(path)

    @work(thread=True, group="local-inspect")
    def _inspect_worker(self, path: Path) -> None:
        try:
            info = inspect_local_pack(path)
        except (MchostError, OSError) as exc:
            self.app.call_from_thread(self._show_error, str(exc))
            return
        self.app.call_from_thread(self._apply_info, info)

    def _apply_info(self, info: LocalPackInfo) -> None:
        self._info = info
        name = self.query_one("#local-name", Input)
        if not name.value.strip():
            name.value = info.name
        if info.loader:
            self._select_loader(info.loader)
        if info.mc_version:
            self.query_one("#local-mc", Input).value = info.mc_version
        if info.loader_version:
            self.query_one("#local-loader-version", Input).value = (
                info.loader_version
            )
        if info.suggested_memory:
            self.query_one("#local-memory", Input).value = info.suggested_memory
        note = info.notes
        if info.serverstarter is not None:
            note += (
                "\nServerStarter pack: mods and overrides are downloaded "
                "during install."
            )
        elif info.curseforge_files:
            note += (
                f"\nCurseForge manifest: {info.curseforge_files} mods will be "
                "downloaded during install."
            )
        self.query_one("#local-info", Static).update(
            f"{note}\n"
            f"Pack version: {info.pack_version or 'unknown'}"
        )
        self._set_status("Archive inspected.")
        self._clear_error()
        self._refresh_install()

    # -- loader ------------------------------------------------------------

    def _select_loader(self, loader: str) -> None:
        if loader not in MODES:
            return
        self.selected_loader = loader
        for value in MODES:
            button = self.query_one(f"#local-loader-{value}", Button)
            button.set_class(value == loader, "selected")
        self._refresh_install()

    # -- install -----------------------------------------------------------

    def _refresh_install(self) -> None:
        path = self.query_one("#local-path", Input).value.strip()
        name = self.query_one("#local-name", Input).value.strip()
        mc = self.query_one("#local-mc", Input).value.strip()
        memory = self.query_one("#local-memory", Input).value.strip().upper()
        ready = (
            not self._installing
            and bool(path)
            and bool(name)
            and bool(mc)
            and bool(MEMORY_RE.match(memory or "6G"))
        )
        self.query_one("#local-install", Button).disabled = not ready

    def _install(self) -> None:
        raw = self.query_one("#local-path", Input).value.strip()
        name = self.query_one("#local-name", Input).value.strip()
        mc = self.query_one("#local-mc", Input).value.strip()
        loader_version = (
            self.query_one("#local-loader-version", Input).value.strip() or None
        )
        memory = (
            self.query_one("#local-memory", Input).value.strip().upper() or "6G"
        )
        if not raw:
            self._show_error("Enter the archive path.")
            return
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            self._show_error(f"Archive not found: {path}")
            return
        if not name:
            self._show_error("Give the instance a name.")
            return
        if not mc:
            self._show_error("Enter the Minecraft version.")
            return
        if not MEMORY_RE.match(memory):
            self._show_error("Memory must look like 6G or 4096M.")
            return
        self._installing = True
        self.query_one("#local-install", Button).disabled = True
        self._set_status("Installing local server pack…")
        self._clear_error()
        self._install_worker(
            path, name, self.selected_loader, mc, loader_version, memory
        )

    @work(thread=True, group="local-install")
    def _install_worker(
        self,
        path: Path,
        name: str,
        loader: str,
        mc_version: str,
        loader_version: str | None,
        memory: str,
    ) -> None:
        def progress(done: int, total: int) -> None:
            if total:
                self.app.call_from_thread(
                    self._set_status,
                    f"Installing mods… {done}/{total}",
                )

        try:
            instance = install_local_pack(
                self._layout,
                path,
                name=name,
                loader=loader,
                mc_version=mc_version,
                loader_version=loader_version,
                memory=memory,
                progress=progress,
            )
        except (MchostError, OSError) as exc:
            self.app.call_from_thread(self._install_failed, str(exc))
            return
        self.app.call_from_thread(self.dismiss, instance.slug)

    def _install_failed(self, message: str) -> None:
        self._installing = False
        self._refresh_install()
        self._show_error(message)

    # -- helpers -----------------------------------------------------------

    def _set_status(self, message: str) -> None:
        self.query_one("#local-status", Static).update(message)

    def _show_error(self, message: str) -> None:
        self._set_status("")
        self.query_one("#local-error", Static).update(message)

    def _clear_error(self) -> None:
        self.query_one("#local-error", Static).update("")

    def action_cancel(self) -> None:
        if self._installing:
            self._set_status("Install in progress…")
            return
        self.dismiss(None)

    # -- events ------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in (
            "local-path",
            "local-name",
            "local-mc",
            "local-memory",
        ):
            self._refresh_install()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "local-path":
            self._inspect()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id == "local-inspect":
            self._inspect()
        elif button_id == "local-cancel":
            self.action_cancel()
        elif button_id == "local-install":
            self._install()
        elif button_id.startswith("local-loader-"):
            self._select_loader(button_id.removeprefix("local-loader-"))
