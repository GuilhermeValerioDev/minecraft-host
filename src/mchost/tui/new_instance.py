"""The in-app new-instance wizard."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ..compose import write_compose
from ..config import (
    LOADER_LABELS,
    MEMORY_RE,
    Instance,
    default_tz,
    java_for_mc,
    new_rcon_password,
    save_instance,
    slugify,
    utc_now,
)
from ..errors import MchostError
from ..paths import Layout
from ..versions import LoaderBuild, loader_builds, supported_mc_versions

NONE_ID = "__none__"


class NewInstanceWizard(ModalScreen[str | None]):
    """Collect a name, loader, MC version, loader build, and memory."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    NewInstanceWizard {
        align: center middle;
    }
    #new-form {
        width: 90%;
        max-width: 110;
        height: 90%;
        max-height: 38;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }
    #new-title {
        text-style: bold;
    }
    #new-subtitle {
        color: $text-muted;
        padding-bottom: 1;
    }
    .field-row {
        height: 3;
    }
    .field-label {
        width: 12;
        content-align: left middle;
        color: $text-muted;
    }
    #new-name, #new-memory, #new-loader {
        width: 1fr;
    }
    #new-loader {
        height: 3;
    }
    .loader-button {
        min-width: 8;
        margin-right: 1;
    }
    Button.loader-button.selected {
        background: $accent;
        color: $text;
    }
    #new-columns {
        height: 1fr;
    }
    #new-mc-pane, #new-build-pane {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }
    .picker-label {
        text-style: bold;
        color: $text-muted;
        padding: 0 0 1 0;
    }
    #new-mc-list, #new-build-list {
        height: 1fr;
        border: solid $panel;
    }
    #new-status {
        height: 1;
        color: $text-muted;
        padding: 1 0 0 0;
    }
    #new-error {
        height: 2;
        color: $error;
    }
    #new-buttons {
        height: 3;
        align-horizontal: right;
    }
    #new-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, layout: Layout) -> None:
        super().__init__()
        self._layout = layout
        self.mc_versions: list[str] = []
        self.builds: list[LoaderBuild] = []
        self.selected_loader = "vanilla"
        self.selected_mc: str | None = None
        self.selected_build: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="new-form"):
            yield Static("[bold]New instance[/]", id="new-title")
            yield Static(
                "Name it, choose a loader, then pick the exact version.",
                id="new-subtitle",
            )
            with Horizontal(classes="field-row"):
                yield Static("Name", classes="field-label")
                yield Input(placeholder="My Server", id="new-name")
            with Horizontal(classes="field-row"):
                yield Static("Loader", classes="field-label")
                with Horizontal(id="new-loader"):
                    for value, label in LOADER_LABELS.items():
                        classes = "loader-button"
                        if value == "vanilla":
                            classes += " selected"
                        yield Button(
                            label,
                            id=f"loader-{value}",
                            classes=classes,
                        )
            with Horizontal(classes="field-row"):
                yield Static("Memory", classes="field-label")
                yield Input(value="4G", placeholder="4G", id="new-memory")
            with Horizontal(id="new-columns"):
                with Vertical(id="new-mc-pane"):
                    yield Static("Minecraft", classes="picker-label")
                    yield OptionList(id="new-mc-list")
                with Vertical(id="new-build-pane"):
                    yield Static("Loader build", classes="picker-label")
                    yield OptionList(id="new-build-list")
            yield Static("Loading versions…", id="new-status")
            yield Static("", id="new-error")
            with Horizontal(id="new-buttons"):
                yield Button("Cancel", id="new-cancel")
                yield Button(
                    "Create", id="new-create", variant="primary", disabled=True
                )

    def on_mount(self) -> None:
        self._load_mc_versions(self.selected_loader)

    # -- loading -----------------------------------------------------------

    @work(thread=True, group="new-mc")
    def _load_mc_versions(self, loader: str) -> None:
        try:
            versions = supported_mc_versions(loader)
        except MchostError as exc:
            self.app.call_from_thread(self._show_error, str(exc))
            return
        self.app.call_from_thread(self._populate_mc, versions)

    def _populate_mc(self, versions: list[str]) -> None:
        self.mc_versions = versions
        option_list = self.query_one("#new-mc-list", OptionList)
        option_list.clear_options()
        self._clear_error()
        if not versions:
            self._show_error(
                f"No Minecraft versions found for {self.selected_loader}."
            )
            return
        for version in versions:
            option_list.add_option(Option(version, id=version))
        option_list.highlighted = 0
        self.selected_mc = versions[0]
        self._load_builds(self.selected_mc)

    def _load_builds(self, mc_version: str) -> None:
        self._set_status(
            f"Loading {self.selected_loader} builds for {mc_version}…"
        )
        if self.selected_loader == "vanilla":
            self._populate_builds([])
            return
        self._load_builds_worker(mc_version)

    @work(thread=True, exclusive=True, group="new-builds")
    def _load_builds_worker(self, mc_version: str) -> None:
        try:
            builds = loader_builds(self.selected_loader, mc_version)
        except MchostError as exc:
            self.app.call_from_thread(self._show_error, str(exc))
            return
        self.app.call_from_thread(self._populate_builds, builds)

    def _populate_builds(self, builds: list[LoaderBuild]) -> None:
        self.builds = builds
        option_list = self.query_one("#new-build-list", OptionList)
        option_list.clear_options()
        self._clear_error()

        if self.selected_loader == "vanilla":
            option_list.add_option(Option("No loader build required", id=NONE_ID))
            option_list.highlighted = 0
            self.selected_build = None
            self._set_status("Vanilla needs no loader build.")
            self._refresh_create()
            return

        if not builds:
            option_list.add_option(
                Option("No builds available", id=NONE_ID, disabled=True)
            )
            self.selected_build = None
            self._set_status(
                f"No {self.selected_loader} builds found for {self.selected_mc}."
            )
            self._refresh_create()
            return

        for build in builds:
            markers = []
            if build.recommended:
                markers.append("recommended")
            if build.latest:
                markers.append("latest")
            suffix = f"  [dim]({', '.join(markers)})[/]" if markers else ""
            option_list.add_option(
                Option(f"{build.version}{suffix}", id=build.version)
            )
        versions = [build.version for build in builds]
        preferred = next(
            (build.version for build in builds if build.recommended),
            builds[0].version,
        )
        self.selected_build = preferred
        option_list.highlighted = versions.index(preferred)
        self._set_status(f"{len(builds)} builds for {self.selected_mc}.")
        self._refresh_create()

    # -- selection ---------------------------------------------------------

    def _handle_option(self, event: OptionList.OptionMessage) -> None:
        option = event.option
        if option is None or option.id is None:
            return
        if event.option_list.id == "new-mc-list":
            self.selected_mc = option.id
            self._load_builds(option.id)
        elif event.option_list.id == "new-build-list":
            self.selected_build = None if option.id == NONE_ID else option.id
            self._refresh_create()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        self._handle_option(event)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self._handle_option(event)

    def _select_loader(self, loader: str) -> None:
        if loader == self.selected_loader:
            return
        self.selected_loader = loader
        for value in LOADER_LABELS:
            button = self.query_one(f"#loader-{value}", Button)
            button.set_class(value == loader, "selected")
        self.selected_build = None
        self.query_one("#new-build-list", OptionList).clear_options()
        self.mc_versions = []
        self._load_mc_versions(loader)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "new-name":
            self._refresh_create()

    def _refresh_create(self) -> None:
        name = self.query_one("#new-name", Input).value.strip()
        needs_build = self.selected_loader != "vanilla"
        ready = bool(name) and bool(self.selected_mc) and (
            not needs_build or bool(self.selected_build)
        )
        self.query_one("#new-create", Button).disabled = not ready

    def _show_error(self, message: str) -> None:
        self._set_status("")
        self.query_one("#new-error", Static).update(message)

    def _clear_error(self) -> None:
        self.query_one("#new-error", Static).update("")

    def _set_status(self, message: str) -> None:
        self.query_one("#new-status", Static).update(message)

    # -- actions -----------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id == "new-cancel":
            self.dismiss(None)
        elif button_id == "new-create":
            self._create()
        elif button_id.startswith("loader-"):
            self._select_loader(button_id.removeprefix("loader-"))

    def _create(self) -> None:
        name = self.query_one("#new-name", Input).value.strip()
        memory = self.query_one("#new-memory", Input).value.strip().upper() or "4G"
        if not name:
            self._show_error("Give the instance a name.")
            return
        if not self.selected_mc:
            self._show_error("Pick a Minecraft version first.")
            return
        if self.selected_loader != "vanilla" and not self.selected_build:
            self._show_error("Pick a loader build first.")
            return
        if not MEMORY_RE.match(memory):
            self._show_error("Memory must look like 4G or 2048M.")
            return

        slug = slugify(name)
        if self._layout.config_file(slug).is_file():
            self._show_error(f"An instance named {slug!r} already exists.")
            return

        try:
            instance = Instance(
                name=name,
                slug=slug,
                kind="server",
                loader=self.selected_loader,
                mc_version=self.selected_mc,
                loader_version=self.selected_build,
                memory=memory,
                tz=default_tz(),
                java_version=java_for_mc(self.selected_mc),
                eula=True,
                rcon_password=new_rcon_password(),
                created_at=utc_now(),
            )
            instance.validate()
            self._layout.instance_dir(slug).mkdir(parents=True, exist_ok=True)
            self._layout.server_dir(slug).mkdir(parents=True, exist_ok=True)
            self._layout.backups_for(slug).mkdir(parents=True, exist_ok=True)
            save_instance(self._layout, instance)
            write_compose(self._layout, instance)
        except (MchostError, OSError) as exc:
            self._show_error(str(exc))
            return
        self.dismiss(slug)
