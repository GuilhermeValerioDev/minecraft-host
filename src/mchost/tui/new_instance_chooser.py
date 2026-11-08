"""Choose how to create a new instance."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class NewInstanceChooser(ModalScreen[str | None]):
    """Ask whether to create a custom server or install a pack."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    CSS = """
    NewInstanceChooser {
        align: center middle;
    }
    #chooser {
        width: 58;
        height: auto;
        border: thick $accent;
        background: $panel;
        padding: 2 3;
    }
    #chooser-title {
        text-style: bold;
    }
    #chooser-subtitle {
        color: $text-muted;
        padding: 0 0 2 0;
    }
    #chooser Button {
        width: 100%;
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="chooser"):
            yield Static("[bold]Add an instance[/]", id="chooser-title")
            yield Static(
                "Create a custom server or install a prebuilt pack.",
                id="chooser-subtitle",
            )
            yield Button(
                "Custom server",
                id="choice-custom",
                variant="primary",
            )
            yield Button("Modrinth pack", id="choice-modrinth")
            yield Button("CurseForge pack", id="choice-curseforge")
            yield Button("Cancel", id="choice-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        choices = {
            "choice-custom": "custom",
            "choice-modrinth": "modrinth",
            "choice-curseforge": "curseforge",
        }
        if event.button.id == "choice-cancel":
            self.dismiss(None)
            return
        self.dismiss(choices.get(event.button.id or ""))

    def action_cancel(self) -> None:
        self.dismiss(None)
