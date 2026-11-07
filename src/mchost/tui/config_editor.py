"""A small modal text editor for config files."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from ..errors import MchostError
from ..files import write_text_file


class ConfigEditor(ModalScreen[bool]):
    """Edit a UTF-8 text file inside the config folder."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    CSS = """
    ConfigEditor {
        align: center middle;
    }
    #editor-panel {
        width: 94%;
        max-width: 120;
        height: 94%;
        max-height: 46;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }
    #editor-title {
        text-style: bold;
    }
    #editor-path {
        color: $text-muted;
        padding-bottom: 1;
    }
    #editor-area {
        height: 1fr;
        border: solid $panel;
    }
    #editor-error {
        height: 2;
        color: $error;
    }
    #editor-buttons {
        height: 3;
        align-horizontal: right;
    }
    #editor-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, path: Path, relative: str, text: str) -> None:
        super().__init__()
        self.path = path
        self.relative = relative
        self.original_text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="editor-panel"):
            yield Static("[bold]Edit config[/]", id="editor-title")
            yield Static(str(self.relative), id="editor-path")
            yield TextArea(
                self.original_text,
                show_line_numbers=True,
                id="editor-area",
            )
            yield Static("", id="editor-error")
            with Horizontal(id="editor-buttons"):
                yield Button("Cancel", id="editor-cancel")
                yield Button("Save", id="editor-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#editor-area", TextArea).focus()

    def _error(self, message: str) -> None:
        self.query_one("#editor-error", Static).update(message)

    def action_save(self) -> None:
        text = self.query_one("#editor-area", TextArea).text
        try:
            write_text_file(self.path, text)
        except MchostError as exc:
            self._error(str(exc))
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "editor-save":
            self.action_save()
        elif event.button.id == "editor-cancel":
            self.action_cancel()
