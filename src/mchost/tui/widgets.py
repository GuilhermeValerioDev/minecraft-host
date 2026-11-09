"""Textual widgets used by the dashboard."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..config import Instance

STATE_CLASSES = {
    "running": "state-running",
    "restarting": "state-restarting",
    "exited": "state-exited",
    "created": "state-created",
    "paused": "state-paused",
    "missing": "state-missing",
    "unknown": "state-unknown",
}


def state_class(state: str) -> str:
    return STATE_CLASSES.get(state, "state-unknown")


class InstanceRow(Horizontal):
    """A clickable sidebar row with inline start/stop/restart buttons."""

    def __init__(self, instance: Instance, state: str = "unknown") -> None:
        super().__init__(id=f"row-{instance.slug}", classes="instance-row")
        self.instance = instance
        self._state = state

    def compose(self) -> ComposeResult:
        yield Static("●", classes=f"dot {state_class(self._state)}")
        yield Vertical(
            Static(self.instance.name, classes="row-name"),
            Static(self.instance.sidebar_label, classes="row-version"),
            classes="row-text",
        )

    def on_click(self, event: Click) -> None:
        if event.button == 1:
            self.app.select_instance(self.instance.slug)
        elif event.button == 3:
            self.app.show_context_menu(self.instance.slug)

    def set_state(self, state: str) -> None:
        self._state = state
        dot = self.query_one(".dot", Static)
        dot.set_classes(f"dot {state_class(state)}")

    def set_selected(self, selected: bool) -> None:
        if selected:
            self.add_class("selected")
        else:
            self.remove_class("selected")


class ContextMenu(ModalScreen[None]):
    """Right-click menu for an instance."""

    BINDINGS = [("escape", "cancel", "Close")]

    CSS = """
    ContextMenu {
        align: center middle;
    }
    #context-menu {
        width: 36;
        height: auto;
        border: thick $accent;
        background: $panel;
        padding: 1;
    }
    #context-title {
        text-style: bold;
        padding: 0 1 1 1;
    }
    #context-menu Button {
        width: 100%;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, instance: Instance) -> None:
        super().__init__()
        self.instance = instance

    def compose(self) -> ComposeResult:
        with Vertical(id="context-menu"):
            yield Static(f"[bold]{self.instance.name}[/]", id="context-title")
            yield Button("Start", id="ctx-start", variant="success")
            yield Button("Stop", id="ctx-stop", variant="error")
            yield Button("Restart", id="ctx-restart")
            yield Button("Edit instance", id="ctx-edit")
            yield Button("Backup", id="ctx-backup")
            yield Button("Reveal data", id="ctx-reveal")
            yield Button(
                "Delete instance", id="ctx-delete", variant="error"
            )
            yield Button("Cancel", id="ctx-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = (event.button.id or "").removeprefix("ctx-")
        if action == "cancel":
            self.dismiss()
            return
        # Dismiss this modal first: actions such as "Change version" push
        # another screen, and dismissing a screen that is no longer on top
        # leaves the stack in a bad state.
        self.dismiss()
        self.app.call_later(
            self.app.handle_row_action, self.instance.slug, action
        )

    def action_cancel(self) -> None:
        self.dismiss()


class ConfirmDialog(ModalScreen[bool]):
    """A general confirmation dialog for restore/delete actions."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    ConfirmDialog {
        align: center middle;
    }
    #dialog {
        width: 68;
        height: auto;
        border: thick $warning;
        background: $panel;
        padding: 1 2;
    }
    #dialog-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #dialog-buttons {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }
    #dialog-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(
        self,
        title: str,
        message: str,
        confirm_label: str = "Confirm",
        *,
        danger: bool = False,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.confirm_label = confirm_label
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"[bold]{self.title_text}[/]", id="dialog-title")
            yield Static(self.message)
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="dialog-cancel")
                yield Button(
                    self.confirm_label,
                    id="dialog-confirm",
                    variant="error" if self.danger else "primary",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "dialog-confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmSwitch(ModalScreen[bool]):
    """Ask before replacing the running server."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    ConfirmSwitch {
        align: center middle;
    }
    #confirm {
        width: 64;
        height: auto;
        border: thick $warning;
        background: $panel;
        padding: 1 2;
    }
    #confirm-buttons {
        height: auto;
        align-horizontal: right;
    }
    #confirm-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, running: list[Instance], target: Instance) -> None:
        super().__init__()
        self.running = running
        self.target = target

    def compose(self) -> ComposeResult:
        names = ", ".join(instance.name for instance in self.running)
        with Vertical(id="confirm"):
            yield Static(
                f"[bold]One server at a time[/]\n\n"
                f"Stop [bold]{names}[/] and start [bold]{self.target.name}[/]?",
                markup=True,
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button("Switch", id="confirm-switch", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm-switch")

    def action_cancel(self) -> None:
        self.dismiss(False)
