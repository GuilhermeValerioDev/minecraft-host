"""Edit an instance's display name, memory, and version fields."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..compose import write_compose
from ..config import (
    LOADER_LABELS,
    MEMORY_RE,
    Instance,
    java_for_mc,
    save_instance,
)
from ..errors import MchostError
from ..paths import Layout


class EditInstance(ModalScreen[bool]):
    """Edit name, memory, Minecraft version, and loader build in one form."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    CSS = """
    EditInstance {
        align: center middle;
    }
    #edit-panel {
        width: 68;
        height: auto;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }
    #edit-title {
        text-style: bold;
    }
    #edit-subtitle {
        color: $text-muted;
        padding-bottom: 1;
    }
    .edit-row {
        height: 3;
    }
    .edit-label {
        width: 15;
        content-align: left middle;
        color: $text-muted;
    }
    #edit-name, #edit-memory, #edit-mc, #edit-loader-version {
        width: 1fr;
    }
    #edit-hint {
        height: 4;
        color: $text-muted;
        padding-top: 1;
    }
    #edit-error {
        height: 2;
        color: $error;
    }
    #edit-buttons {
        height: 3;
        align-horizontal: right;
    }
    #edit-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, layout: Layout, instance: Instance) -> None:
        super().__init__()
        self._layout = layout
        self.instance = instance

    def compose(self) -> ComposeResult:
        loader = LOADER_LABELS.get(self.instance.loader, self.instance.loader)
        with Vertical(id="edit-panel"):
            yield Static("[bold]Edit instance[/]", id="edit-title")
            yield Static(
                f"{self.instance.slug} · {loader}",
                id="edit-subtitle",
            )
            with Horizontal(classes="edit-row"):
                yield Static("Name", classes="edit-label")
                yield Input(value=self.instance.name, id="edit-name")
            with Horizontal(classes="edit-row"):
                yield Static("Memory", classes="edit-label")
                yield Input(value=self.instance.memory, id="edit-memory")
            with Horizontal(classes="edit-row"):
                yield Static("Minecraft", classes="edit-label")
                yield Input(value=self.instance.mc_version, id="edit-mc")
            with Horizontal(classes="edit-row"):
                yield Static("Loader build", classes="edit-label")
                yield Input(
                    value=self.instance.loader_version or "",
                    placeholder="blank = latest / not pinned",
                    id="edit-loader-version",
                )
            hint = (
                "The slug and container name do not change. "
                "Restart the server to apply version or memory changes."
            )
            if self.instance.kind == "modpack" and self.instance.modpack:
                if self.instance.modpack.source == "modrinth":
                    hint += (
                        "\nModrinth packs normally drive the container from their "
                        "pack file; these fields update instance metadata."
                    )
                else:
                    hint += (
                        "\nThis local pack's compose values are updated from these "
                        "fields."
                    )
            yield Static(hint, id="edit-hint")
            yield Static("", id="edit-error")
            with Horizontal(id="edit-buttons"):
                yield Button("Cancel", id="edit-cancel")
                yield Button("Apply", id="edit-apply", variant="primary")

    def _error(self, message: str) -> None:
        self.query_one("#edit-error", Static).update(message)

    def _apply(self) -> None:
        name = self.query_one("#edit-name", Input).value.strip()
        memory = self.query_one("#edit-memory", Input).value.strip().upper()
        mc_version = self.query_one("#edit-mc", Input).value.strip()
        loader_version = (
            self.query_one("#edit-loader-version", Input).value.strip() or None
        )
        if not name:
            self._error("The instance needs a name.")
            return
        if not MEMORY_RE.match(memory):
            self._error("Memory must look like 4G or 4096M.")
            return
        if not mc_version:
            self._error("The Minecraft version cannot be empty.")
            return

        old = (
            self.instance.name,
            self.instance.memory,
            self.instance.mc_version,
            self.instance.loader_version,
            self.instance.java_version,
            dict(self.instance.modpack.resolved) if self.instance.modpack else None,
        )
        self.instance.name = name
        self.instance.memory = memory
        self.instance.mc_version = mc_version
        self.instance.loader_version = loader_version
        self.instance.java_version = java_for_mc(mc_version)
        if self.instance.modpack is not None:
            self.instance.modpack.resolved["minecraft"] = mc_version
            self.instance.modpack.resolved["loader"] = self.instance.loader
            self.instance.modpack.resolved["loader_version"] = loader_version or ""
        try:
            self.instance.validate()
            save_instance(self._layout, self.instance)
            write_compose(self._layout, self.instance)
        except (MchostError, OSError) as exc:
            (
                self.instance.name,
                self.instance.memory,
                self.instance.mc_version,
                self.instance.loader_version,
                self.instance.java_version,
                resolved,
            ) = old
            if resolved is not None and self.instance.modpack is not None:
                self.instance.modpack.resolved = resolved
            self._error(str(exc))
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._apply()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "edit-apply":
            self._apply()
        elif event.button.id == "edit-cancel":
            self.action_cancel()
