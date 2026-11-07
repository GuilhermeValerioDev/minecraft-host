"""A mouse-friendly Minecraft × loader version picker."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from ..compose import write_compose
from ..config import LOADER_LABELS, Instance, java_for_mc, save_instance
from ..errors import MchostError
from ..paths import Layout
from ..versions import LoaderBuild, loader_builds, supported_mc_versions

NONE_ID = "__none__"


class VersionPicker(ModalScreen[bool]):
    """Pick a Minecraft version and an exact loader build."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    VersionPicker {
        align: center middle;
    }
    #picker {
        width: 90%;
        max-width: 110;
        height: 90%;
        max-height: 34;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }
    #picker-title {
        text-style: bold;
    }
    #picker-subtitle {
        color: $text-muted;
        padding-bottom: 1;
    }
    #picker-columns {
        height: 1fr;
    }
    #mc-pane, #build-pane {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }
    .picker-label {
        text-style: bold;
        color: $text-muted;
        padding: 0 0 1 0;
    }
    #mc-list, #build-list {
        height: 1fr;
        border: solid $panel;
    }
    #picker-status {
        height: 1;
        color: $text-muted;
        padding: 1 0 0 0;
    }
    #picker-error {
        height: 2;
        color: $error;
    }
    #picker-buttons {
        height: 3;
        align-horizontal: right;
    }
    #picker-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, layout: Layout, instance: Instance) -> None:
        super().__init__()
        self._layout = layout
        self.instance = instance
        self.mc_versions: list[str] = []
        self.builds: list[LoaderBuild] = []
        self.selected_mc: str | None = None
        self.selected_build: str | None = None

    def compose(self) -> ComposeResult:
        loader = LOADER_LABELS.get(self.instance.loader, self.instance.loader)
        with Vertical(id="picker"):
            yield Static("[bold]Change version[/]", id="picker-title")
            yield Static(
                f"{self.instance.name} · {loader} · currently MC "
                f"{self.instance.mc_version}",
                id="picker-subtitle",
            )
            with Horizontal(id="picker-columns"):
                with Vertical(id="mc-pane"):
                    yield Static("Minecraft", classes="picker-label")
                    yield OptionList(id="mc-list")
                with Vertical(id="build-pane"):
                    yield Static("Loader build", classes="picker-label")
                    yield OptionList(id="build-list")
            yield Static("", id="picker-status")
            yield Static("", id="picker-error")
            with Horizontal(id="picker-buttons"):
                yield Button("Cancel", id="picker-cancel")
                yield Button("Apply", id="picker-apply", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self._load_mc_versions()

    # -- loading -----------------------------------------------------------

    @work(thread=True, group="picker-mc")
    def _load_mc_versions(self) -> None:
        try:
            versions = supported_mc_versions(self.instance.loader)
        except MchostError as exc:
            self.app.call_from_thread(self._show_error, str(exc))
            return
        self.app.call_from_thread(self._populate_mc, versions)

    def _populate_mc(self, versions: list[str]) -> None:
        self.mc_versions = versions
        option_list = self.query_one("#mc-list", OptionList)
        option_list.clear_options()
        if not versions:
            self._show_error(
                f"No Minecraft versions found for {self.instance.loader}."
            )
            return

        for version in versions:
            option_list.add_option(Option(version, id=version))

        index = 0
        if self.instance.mc_version in versions:
            index = versions.index(self.instance.mc_version)
        option_list.highlighted = index
        self.selected_mc = versions[index]
        self._load_builds(self.selected_mc)

    def _load_builds(self, mc_version: str) -> None:
        self._set_status(f"Loading {self.instance.loader} builds for {mc_version}…")
        if self.instance.loader == "vanilla":
            self._populate_builds([])
            return
        self._load_builds_worker(mc_version)

    @work(thread=True, exclusive=True, group="picker-builds")
    def _load_builds_worker(self, mc_version: str) -> None:
        try:
            builds = loader_builds(self.instance.loader, mc_version)
        except MchostError as exc:
            self.app.call_from_thread(self._show_error, str(exc))
            return
        self.app.call_from_thread(self._populate_builds, builds)

    def _populate_builds(self, builds: list[LoaderBuild]) -> None:
        self.builds = builds
        option_list = self.query_one("#build-list", OptionList)
        option_list.clear_options()
        self._clear_error()

        if self.instance.loader == "vanilla":
            option_list.add_option(Option("No loader build required", id=NONE_ID))
            option_list.highlighted = 0
            self.selected_build = None
            self._set_status("Vanilla needs no loader build.")
            self._refresh_apply()
            return

        if not builds:
            option_list.add_option(
                Option("No builds available", id=NONE_ID, disabled=True)
            )
            self._set_status(
                f"No {self.instance.loader} builds found for {self.selected_mc}."
            )
            self.selected_build = None
            self._refresh_apply()
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
        preferred = (
            self.selected_build
            if self.selected_build in versions
            else next(
                (build.version for build in builds if build.recommended),
                builds[0].version,
            )
        )
        self.selected_build = preferred
        option_list.highlighted = versions.index(preferred)
        self._set_status(f"{len(builds)} builds for {self.selected_mc}.")
        self._refresh_apply()

    def _show_error(self, message: str) -> None:
        self._set_status("")
        self.query_one("#picker-error", Static).update(message)

    def _clear_error(self) -> None:
        self.query_one("#picker-error", Static).update("")

    def _set_status(self, message: str) -> None:
        self.query_one("#picker-status", Static).update(message)

    def _refresh_apply(self) -> None:
        needs_build = self.instance.loader != "vanilla"
        ready = bool(self.selected_mc) and (not needs_build or bool(self.selected_build))
        self.query_one("#picker-apply", Button).disabled = not ready

    # -- selection ---------------------------------------------------------

    def _handle_option(self, event: OptionList.OptionMessage) -> None:
        option = event.option
        if option is None or option.id is None:
            return
        if event.option_list.id == "mc-list":
            self.selected_mc = option.id
            self._load_builds(option.id)
        elif event.option_list.id == "build-list":
            self.selected_build = None if option.id == NONE_ID else option.id
            self._refresh_apply()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        self._handle_option(event)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self._handle_option(event)

    # -- actions -----------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "picker-cancel":
            self.dismiss(False)
        elif event.button.id == "picker-apply":
            self._apply()

    def _apply(self) -> None:
        if not self.selected_mc:
            self._show_error("Pick a Minecraft version first.")
            return
        if self.instance.loader != "vanilla" and not self.selected_build:
            self._show_error("Pick a loader build first.")
            return
        try:
            self.instance.mc_version = self.selected_mc
            self.instance.loader_version = self.selected_build
            self.instance.java_version = java_for_mc(self.selected_mc)
            save_instance(self._layout, self.instance)
            write_compose(self._layout, self.instance)
        except (MchostError, OSError) as exc:
            self._show_error(str(exc))
            return
        self.dismiss(True)
