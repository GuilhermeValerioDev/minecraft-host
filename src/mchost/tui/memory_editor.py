"""Small modal for changing an instance's Java heap."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..compose import write_compose
from ..config import MEMORY_RE, Instance, save_instance
from ..errors import MchostError
from ..paths import Layout


class MemoryEditor(ModalScreen[bool]):
    """Edit the MEMORY value for any instance, including modpacks."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    CSS = """
    MemoryEditor {
        align: center middle;
    }
    #memory-panel {
        width: 64;
        height: auto;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }
    #memory-title {
        text-style: bold;
    }
    #memory-subtitle {
        color: $text-muted;
        padding-bottom: 1;
    }
    #memory-value {
        width: 1fr;
    }
    #memory-error {
        height: 2;
        color: $error;
    }
    #memory-buttons {
        height: 3;
        align-horizontal: right;
    }
    #memory-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, layout: Layout, instance: Instance) -> None:
        super().__init__()
        self._layout = layout
        self.instance = instance

    def compose(self) -> ComposeResult:
        with Vertical(id="memory-panel"):
            yield Static("[bold]Java memory[/]", id="memory-title")
            yield Static(
                f"{self.instance.name} · currently {self.instance.memory}",
                id="memory-subtitle",
            )
            yield Input(value=self.instance.memory, id="memory-value")
            yield Static("", id="memory-error")
            with Horizontal(id="memory-buttons"):
                yield Button("Cancel", id="memory-cancel")
                yield Button("Apply", id="memory-apply", variant="primary")

    def on_mount(self) -> None:
        field = self.query_one("#memory-value", Input)
        field.focus()
        field.cursor_position = len(field.value)

    def _error(self, message: str) -> None:
        self.query_one("#memory-error", Static).update(message)

    def _apply(self) -> None:
        value = self.query_one("#memory-value", Input).value.strip().upper()
        if not MEMORY_RE.match(value):
            self._error("Use a value like 4G, 6G, 8G, or 4096M.")
            return
        old = self.instance.memory
        self.instance.memory = value
        try:
            self.instance.validate()
            save_instance(self._layout, self.instance)
            write_compose(self._layout, self.instance)
        except (MchostError, OSError) as exc:
            self.instance.memory = old
            self._error(str(exc))
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "memory-value":
            self._apply()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "memory-apply":
            self._apply()
        elif event.button.id == "memory-cancel":
            self.action_cancel()
