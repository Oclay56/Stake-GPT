from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text

from .local_helper_cli import (
    APP_DISPLAY_NAME,
    CLI_VERSION,
    ROOT_DIR,
    StakeGptCli,
    check_local_helper_setup,
    stake_site_profile,
)

try:
    from textual import events
    from textual.app import App, ComposeResult
    from textual.containers import Container, Vertical
    from textual.widgets import Label, ListItem, ListView, Static

    TEXTUAL_AVAILABLE = True
    TEXTUAL_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on local optional package.
    events = object
    App = object
    ComposeResult = Any
    Container = Vertical = Label = ListItem = ListView = Static = object
    TEXTUAL_AVAILABLE = False
    TEXTUAL_IMPORT_ERROR = str(exc)


DEFAULT_TUI_PALETTE = {
    "background": "#111111",
    "panel": "#101010",
    "panelBorder": "#5A5A5A",
    "shellBorder": "#6A6A6A",
    "mutedText": "#7F7F7F",
    "highlightText": "#B8B19C",
    "titleText": "#F1EED0",
    "accentText": "#A46214",
    "readyText": "#00E701",
    "activeText": "#74B9FF",
    "errorText": "#FF6B8A",
    "rowHover": "#3A3A3A",
    "rowText": "#B8B19C",
    "shortcutText": "#7F7F7F",
    "outputPanel": "#101010",
}
MENU_ROW_WIDTH = 94
TITLE_ROW_WIDTH = 106
OUTPUT_PANEL_WIDTH = 104
OUTPUT_VISIBLE_HEIGHT = 7
OUTPUT_TEXT_WIDTH = 98
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
STD_INPUT_HANDLE = -10
PALETTE_COLOR_CHOICES = (
    "#0A0A0A",
    "#111111",
    "#161616",
    "#1A1A1A",
    "#202020",
    "#262626",
    "#2D2D2D",
    "#333333",
    "#3A3A3A",
    "#454545",
    "#4F4F4F",
    "#5A5A5A",
)


@dataclass(frozen=True)
class TuiAction:
    action_id: str
    label: str
    shortcut: str
    description: str
    command: str
    running_label: str
    confirm: bool = False


TUI_ACTIONS: tuple[TuiAction, ...] = (
    TuiAction("review", "Review", "ctrl+r", "Review the visible Stake board.", "review", "Reviewing"),
    TuiAction("build", "Build", "ctrl+b", "Open builder mode for validated slips.", "build", "Building"),
    TuiAction("backtest", "Analysis", "ctrl+t", "Run the automated historic analysis.", "backtest", "Analyzing"),
    TuiAction("history", "Historic", "ctrl+i", "Auto-import new historic files and show status.", "historic", "Loading historic"),
    TuiAction("logs", "Logs", "ctrl+l", "Show the latest helper logs.", "logs", "Loading logs"),
    TuiAction("doctor", "Doctor", "ctrl+d", "Run diagnostics.", "doctor", "Diagnosing"),
    TuiAction("clean", "Clean", "ctrl+c", "Clear rebuildable cache.", "clean", "Cleaning"),
    TuiAction("domain", "Domain", "ctrl+q", "Toggle Stake domain profile.", "domain", "Switching domain"),
    TuiAction("stop", "Stop", "ctrl+s", "Stop the active helper task.", "stop", "Stop"),
    TuiAction("palette", "Palette", "ctrl+p", "View theme config.", "palette", "Loading palette"),
    TuiAction("exit", "Exit", "ctrl+e", "Close the TUI.", "exit", "Exiting"),
)
MENU_ROW_COUNT = len(TUI_ACTIONS)


def tui_theme_path(*, root_dir: Path = ROOT_DIR) -> Path:
    return root_dir / "data" / "workflow" / "helper-tui-theme.json"


def clean_tui_palette(raw: dict[str, Any] | None = None) -> dict[str, str]:
    palette = dict(DEFAULT_TUI_PALETTE)
    if not isinstance(raw, dict):
        return palette
    for key, value in raw.items():
        if key in palette and isinstance(value, str) and _is_hex_color(value):
            palette[key] = value.upper()
    return palette


def load_tui_palette(path: Path | None = None, *, root_dir: Path = ROOT_DIR) -> dict[str, str]:
    settings_path = path or tui_theme_path(root_dir=root_dir)
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_TUI_PALETTE)
    return clean_tui_palette(raw)


def save_tui_palette(
    palette: dict[str, str],
    path: Path | None = None,
    *,
    root_dir: Path = ROOT_DIR,
) -> Path:
    settings_path = path or tui_theme_path(root_dir=root_dir)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    clean = clean_tui_palette(palette)
    settings_path.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    return settings_path


def textual_dependency_status() -> dict[str, Any]:
    return {"available": TEXTUAL_AVAILABLE, "error": TEXTUAL_IMPORT_ERROR}


def console_input_mode_without_text_selection(mode: int) -> int:
    return (
        int(mode)
        | ENABLE_EXTENDED_FLAGS
        | ENABLE_MOUSE_INPUT
        | ENABLE_WINDOW_INPUT
        | ENABLE_VIRTUAL_TERMINAL_INPUT
    ) & ~ENABLE_QUICK_EDIT_MODE


def disable_terminal_text_selection() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        next_mode = console_input_mode_without_text_selection(mode.value)
        return bool(kernel32.SetConsoleMode(handle, next_mode))
    except Exception:
        return False


def _powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def build_backtest_console_command(root_dir: Path = ROOT_DIR) -> list[str]:
    python_exe = root_dir / ".venv" / "Scripts" / "python.exe"
    command = (
        "$Host.UI.RawUI.WindowTitle = 'Stake-GPT Historic Analysis'; "
        "$Host.UI.RawUI.BackgroundColor = 'Black'; "
        "$Host.UI.RawUI.ForegroundColor = 'Gray'; "
        "try { "
        "$raw = $Host.UI.RawUI; "
        "$max = $raw.MaxPhysicalWindowSize; "
        "$w = [Math]::Min(170, $max.Width); "
        "$h = [Math]::Min(42, $max.Height); "
        "$bufferHeight = [Math]::Max($raw.BufferSize.Height, 1000); "
        "$raw.BufferSize = New-Object System.Management.Automation.Host.Size -ArgumentList $w, $bufferHeight; "
        "$raw.WindowSize = New-Object System.Management.Automation.Host.Size -ArgumentList $w, $h; "
        "} catch {}; "
        "$env:STAKE_GPT_BACKTEST_RICH_WIDTH = '170'; "
        "Clear-Host; "
        f"Set-Location -LiteralPath {_powershell_literal(root_dir)}; "
        f"& {_powershell_literal(python_exe)} -m app.bet_history analysis --rich; "
        "Write-Host ''; "
        "Write-Host 'Historic analysis complete. Close this window when finished.' -ForegroundColor DarkGray"
    )
    return [
        "powershell.exe",
        "-NoLogo",
        "-NoExit",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
    ]


def launch_backtest_console(root_dir: Path = ROOT_DIR) -> subprocess.Popen:
    kwargs: dict[str, Any] = {"cwd": root_dir}
    creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    if creation_flags:
        kwargs["creationflags"] = creation_flags
    return subprocess.Popen(build_backtest_console_command(root_dir), **kwargs)


def build_tui_actions() -> tuple[TuiAction, ...]:
    return TUI_ACTIONS


def find_tui_action(action_id: str) -> TuiAction | None:
    clean_id = action_id.strip().lower()
    for action in TUI_ACTIONS:
        if action.action_id == clean_id or action.command == clean_id:
            return action
    return None


def format_tui_action_row(action: TuiAction, *, width: int = MENU_ROW_WIDTH) -> str:
    inner_width = max(width - 2, 24)
    left = f" {action.label}"
    right = f"{action.shortcut} "
    gap = " " * max(inner_width - len(left) - len(right), 1)
    return f"[{left}{gap}{right}]"


def rich_tui_action_row(
    action: TuiAction,
    *,
    width: int = MENU_ROW_WIDTH,
    palette: dict[str, str] | None = None,
) -> Text:
    colors = clean_tui_palette(palette)
    row = format_tui_action_row(action, width=width)
    text = Text(row, style=colors["shortcutText"])
    label_start = 2
    label_end = label_start + len(action.label)
    text.stylize(f"bold {colors['rowText']}", label_start, label_end)
    text.stylize(colors["shortcutText"], max(0, row.rfind(action.shortcut)), len(row) - 1)
    return text


def rich_title_row(
    system_state: str,
    *,
    frame: int = 2,
    width: int = TITLE_ROW_WIDTH,
    palette: dict[str, str] | None = None,
) -> Text:
    colors = clean_tui_palette(palette)
    left = f"{APP_DISPLAY_NAME} "
    version = f"[v{CLI_VERSION}]"
    display_state, role = _display_status_parts(system_state, frame=frame)
    right = f"System: {display_state}"
    gap = " " * max(width - len(left) - len(version) - len(right), 1)
    text = Text()
    text.append(left, style=colors["mutedText"])
    text.append(version, style=colors["titleText"])
    text.append(gap, style=colors["mutedText"])
    text.append("System: ", style=colors["mutedText"])
    role_color = {
        "ready": colors["readyText"],
        "active": colors["activeText"],
        "error": colors["errorText"],
    }.get(role, colors["mutedText"])
    text.append(display_state, style=role_color)
    return text


def rich_stake_site_row(
    site_label: str,
    *,
    width: int = TITLE_ROW_WIDTH,
    palette: dict[str, str] | None = None,
) -> Text:
    colors = clean_tui_palette(palette)
    label = "Stake site: "
    display_site = f"[{site_label}]"
    right = f"{label}{display_site}"
    gap = " " * max(width - len(right), 0)
    text = Text(f"{gap}{label}", style=colors["mutedText"])
    text.append(display_site, style=colors["accentText"])
    return text


def format_historic_tui_summary(sync_report: dict[str, Any]) -> list[str]:
    history = sync_report.get("history") or {}
    files = list(history.get("importFiles") or [])
    return [
        f"Files checked: {int(sync_report.get('filesConsidered') or 0)}",
        (
            f"Imported: {int(sync_report.get('filesImported') or 0)} | "
            f"Skipped: {int(sync_report.get('filesSkippedDuplicate') or 0)} | "
            f"Refreshed: {int(sync_report.get('refreshedLegs') or 0)} | "
            f"Failed: {int(sync_report.get('filesFailed') or 0)}"
        ),
        (
            f"Parsed legs: {int(history.get('parsedLegs') or 0)} | "
            f"Training eligible: {int(history.get('trainingEligible') or 0)}"
        ),
        f"Needs review: {int(history.get('needsReview') or 0)}",
        f"Import files ({len(files)})",
    ]


def format_running_status(action: TuiAction, *, frame: int = 2) -> str:
    dots = "." * ((frame % 3) + 1)
    return f"[ {action.running_label} ]{dots:<3}"


def rich_page_status(
    action: TuiAction,
    *,
    frame: int = 2,
    page_result: str = "",
    palette: dict[str, str] | None = None,
) -> Text:
    colors = clean_tui_palette(palette)
    if page_result == "done":
        return Text("[ Done ]", style=colors["readyText"])
    if page_result == "failed":
        return Text("[ Failed ]", style=colors["errorText"])
    return Text(format_running_status(action, frame=frame), style=colors["activeText"])


def _display_status_parts(status: str, *, frame: int = 2) -> tuple[str, str]:
    clean = str(status or "ready").strip().lower()
    aliases = {
        "cleaning cache": "cleaning",
        "importing history": "historic",
        "importing historic": "historic",
        "history report": "historic",
        "historic report": "historic",
        "history review": "historic",
        "historic review": "historic",
        "historic analysis": "analyzing",
        "setup ready": "ready",
    }
    clean = aliases.get(clean, clean)
    if clean == "ready":
        return "[ready]", "ready"
    if "error" in clean or "failed" in clean or "needs attention" in clean:
        return f"[{clean}]", "error"
    dots = "." * ((frame % 3) + 1)
    padded_dots = f"{dots:<3}"
    return f"[{clean}] {padded_dots}", "active"


def _is_hex_color(value: str) -> bool:
    text = value.strip()
    if len(text) != 7 or not text.startswith("#"):
        return False
    return all(char in "0123456789abcdefABCDEF" for char in text[1:])


if TEXTUAL_AVAILABLE:

    class CommandRow(ListItem):
        def __init__(self, action: TuiAction, *, palette: dict[str, str] | None = None) -> None:
            self.tui_action = action
            super().__init__(Label(rich_tui_action_row(action, palette=palette), classes="command-label"))


    class PaletteTargetRow(ListItem):
        def __init__(self, target_id: str, label: str) -> None:
            self.target_id = target_id
            super().__init__(Label(format_tui_action_row(_palette_row_action(label)), classes="command-label"))


    class PaletteColorRow(ListItem):
        def __init__(self, color: str) -> None:
            self.color = color
            super().__init__(Label(format_tui_action_row(_palette_row_action(color)), classes="command-label"))


    def _palette_row_action(label: str) -> TuiAction:
        return TuiAction("palette-row", label, "enter", "", "palette-row", "Palette")


    class StakeGptTui(App[None]):
        CSS = f"""
        Screen {{
            background: {DEFAULT_TUI_PALETTE["background"]};
            color: {DEFAULT_TUI_PALETTE["mutedText"]};
            overflow: hidden hidden;
        }}

        #workspace-top {{
            dock: top;
            height: 3;
            width: 100%;
            padding: 1 1 0 1;
            background: {DEFAULT_TUI_PALETTE["background"]};
            color: {DEFAULT_TUI_PALETTE["mutedText"]};
        }}

        #screen-root {{
            align: center middle;
            height: 1fr;
            width: 100%;
            background: {DEFAULT_TUI_PALETTE["background"]};
        }}

        #shell-stack {{
            width: 116;
            height: 24;
            background: {DEFAULT_TUI_PALETTE["background"]};
        }}

        #shell {{
            width: 116;
            height: 22;
            max-height: 22;
            min-height: 22;
            background: {DEFAULT_TUI_PALETTE["panel"]};
            border: round {DEFAULT_TUI_PALETTE["shellBorder"]};
            padding: 1 4;
            overflow: hidden hidden;
        }}

        #footer-stable {{
            height: 1;
            width: 116;
            padding: 0 0;
            content-align: right top;
            background: {DEFAULT_TUI_PALETTE["background"]};
            color: {DEFAULT_TUI_PALETTE["mutedText"]};
        }}

        #title,
        #stake-site,
        #page-title,
        #page-status,
        #hint {{
            height: 1;
            color: {DEFAULT_TUI_PALETTE["mutedText"]};
        }}

        #stake-site {{
            color: {DEFAULT_TUI_PALETTE["accentText"]};
            content-align: right top;
        }}

        #spacer {{
            height: 1;
        }}

        #shell-bottom-fill {{
            height: 1fr;
            background: {DEFAULT_TUI_PALETTE["panel"]};
        }}

        #menu-wrap {{
            width: 100%;
            height: {MENU_ROW_COUNT};
            align: center top;
            background: {DEFAULT_TUI_PALETTE["panel"]};
            overflow: hidden hidden;
        }}

        #actions {{
            width: {MENU_ROW_WIDTH};
            height: {MENU_ROW_COUNT};
            background: {DEFAULT_TUI_PALETTE["panel"]};
            scrollbar-size: 0 0;
            overflow: hidden hidden;
        }}

        CommandRow {{
            width: {MENU_ROW_WIDTH};
            height: 1;
            color: {DEFAULT_TUI_PALETTE["rowText"]};
            background: {DEFAULT_TUI_PALETTE["panel"]};
            padding: 0 0;
        }}

        CommandRow:hover,
        CommandRow.--highlight {{
            color: {DEFAULT_TUI_PALETTE["highlightText"]};
            background: {DEFAULT_TUI_PALETTE["rowHover"]};
            text-style: bold;
        }}

        .command-label {{
            width: {MENU_ROW_WIDTH};
            height: 1;
        }}

        #page-title {{
            text-style: bold;
            color: {DEFAULT_TUI_PALETTE["highlightText"]};
        }}

        #page-status {{
            color: {DEFAULT_TUI_PALETTE["activeText"]};
            margin: 1 0;
        }}

        #output-panel {{
            width: {OUTPUT_PANEL_WIDTH};
            height: 9;
            background: {DEFAULT_TUI_PALETTE["outputPanel"]};
            border: round {DEFAULT_TUI_PALETTE["panelBorder"]};
            padding: 1 2;
            overflow: hidden hidden;
        }}

        #output-text {{
            width: {OUTPUT_TEXT_WIDTH};
            height: {OUTPUT_VISIBLE_HEIGHT};
            color: {DEFAULT_TUI_PALETTE["mutedText"]};
            background: {DEFAULT_TUI_PALETTE["outputPanel"]};
            overflow: hidden hidden;
        }}

        #palette-panel {{
            height: 1fr;
            background: {DEFAULT_TUI_PALETTE["panel"]};
            overflow: hidden hidden;
        }}

        #palette-target-label,
        #palette-color-label,
        #palette-config {{
            height: 1;
            color: {DEFAULT_TUI_PALETTE["mutedText"]};
        }}

        #palette-targets {{
            width: {MENU_ROW_WIDTH};
            height: 2;
            background: {DEFAULT_TUI_PALETTE["panel"]};
            scrollbar-size: 0 0;
        }}

        #palette-colors {{
            width: {MENU_ROW_WIDTH};
            height: 5;
            background: {DEFAULT_TUI_PALETTE["panel"]};
            scrollbar-size: 0 0;
        }}

        #hint {{
            margin-top: 1;
        }}

        .hidden {{
            display: none;
        }}
        """

        BINDINGS = [
            ("ctrl+r", "run_action('review')", "Review"),
            ("ctrl+b", "run_action('build')", "Build"),
            ("ctrl+t", "run_action('backtest')", "Analysis"),
            ("ctrl+i", "run_action('history')", "Historic"),
            ("ctrl+l", "run_action('logs')", "Logs"),
            ("ctrl+d", "run_action('doctor')", "Doctor"),
            ("ctrl+c", "run_action('clean')", "Clean"),
            ("ctrl+q", "run_action('domain')", "Domain"),
            ("ctrl+s", "run_action('stop')", "Stop"),
            ("ctrl+p", "run_action('palette')", "Palette"),
            ("ctrl+e", "run_action('exit')", "Exit"),
            ("r", "run_action('review')", "Review"),
            ("b", "run_action('build')", "Build"),
            ("t", "run_action('backtest')", "Analysis"),
            ("i", "run_action('history')", "Historic"),
            ("l", "run_action('logs')", "Logs"),
            ("d", "run_action('doctor')", "Doctor"),
            ("c", "run_action('clean')", "Clean"),
            ("q", "run_action('domain')", "Domain"),
            ("s", "run_action('stop')", "Stop"),
            ("p", "run_action('palette')", "Palette"),
            ("e", "run_action('exit')", "Exit"),
            ("escape", "back", "Back"),
        ]

        def __init__(self, *, root_dir: Path = ROOT_DIR) -> None:
            super().__init__()
            self.root_dir = root_dir
            self.display_workspace = Path.home()
            self.palette = load_tui_palette(root_dir=root_dir)
            self.ui_thread: threading.Thread | None = None
            self.cli = StakeGptCli(
                root_dir=root_dir,
                output_func=self._append_output,
                input_func=lambda prompt="": "",
            )
            self._active_action: TuiAction | None = None
            self._busy = False
            self._confirm_clean_until = 0.0
            self._status_frame = 0
            self._last_render_state: tuple[Any, ...] | None = None
            self._setup_state = "checking"
            self._active_subprocess: Any = None
            self._selected_palette_target = "background"
            self._stop_requested = False
            self._inline_message = ""
            self._output_lines: list[str] = []
            self._output_scroll = 0
            self._page_result = ""

        def compose(self) -> ComposeResult:
            yield Static("", id="workspace-top")
            with Container(id="screen-root"):
                with Vertical(id="shell-stack"):
                    with Vertical(id="shell"):
                        yield Static("", id="title")
                        yield Static("", id="spacer")
                        with Container(id="menu-wrap"):
                            yield ListView(
                                *[CommandRow(action, palette=self.palette) for action in TUI_ACTIONS],
                                id="actions",
                            )
                        yield Static("", id="page-title", classes="hidden")
                        yield Static("", id="page-status", classes="hidden")
                        with Container(id="output-panel", classes="hidden"):
                            yield Static("", id="output-text")
                        with Vertical(id="palette-panel", classes="hidden"):
                            yield Static("", id="palette-target-label")
                            yield ListView(
                                PaletteTargetRow("background", "Background"),
                                PaletteTargetRow("panel", "Center console"),
                                id="palette-targets",
                            )
                            yield Static("", id="palette-color-label")
                            yield ListView(
                                *[PaletteColorRow(color) for color in PALETTE_COLOR_CHOICES],
                                id="palette-colors",
                            )
                            yield Static("", id="palette-config")
                        yield Static("", id="shell-bottom-fill")
                        yield Static("", id="hint")
                        yield Static("", id="stake-site")
                    yield Static("[stable]", id="footer-stable")

        def on_mount(self) -> None:
            disable_terminal_text_selection()
            self.ui_thread = threading.current_thread()
            self._setup_state = self._read_setup_state()
            self._apply_palette()
            self._refresh_layout(force=True)
            self.set_interval(0.45, self._tick)

        def _apply_palette(self) -> None:
            root = self.query_one("#screen-root", Container)
            workspace_top = self.query_one("#workspace-top", Static)
            footer_stable = self.query_one("#footer-stable", Static)
            shell_stack = self.query_one("#shell-stack", Vertical)
            shell = self.query_one("#shell", Vertical)
            bottom_fill = self.query_one("#shell-bottom-fill", Static)
            menu_wrap = self.query_one("#menu-wrap", Container)
            actions = self.query_one("#actions", ListView)
            output_panel = self.query_one("#output-panel", Container)
            output_text = self.query_one("#output-text", Static)
            palette_panel = self.query_one("#palette-panel", Vertical)
            palette_targets = self.query_one("#palette-targets", ListView)
            palette_colors = self.query_one("#palette-colors", ListView)

            root.styles.background = self.palette["background"]
            workspace_top.styles.background = self.palette["background"]
            footer_stable.styles.background = self.palette["background"]
            shell_stack.styles.background = self.palette["background"]
            shell.styles.background = self.palette["panel"]
            shell.styles.border = ("round", self.palette["shellBorder"])
            bottom_fill.styles.background = self.palette["panel"]
            menu_wrap.styles.background = self.palette["panel"]
            actions.styles.background = self.palette["panel"]
            output_panel.styles.background = self.palette["outputPanel"]
            output_panel.styles.border = ("round", self.palette["panelBorder"])
            output_text.styles.background = self.palette["outputPanel"]
            palette_panel.styles.background = self.palette["panel"]
            palette_targets.styles.background = self.palette["panel"]
            palette_colors.styles.background = self.palette["panel"]

            for selector, color in (
                ("#workspace-top", "mutedText"),
                ("#footer-stable", "mutedText"),
                ("#title", "mutedText"),
                ("#stake-site", "mutedText"),
                ("#page-title", "highlightText"),
                ("#page-status", "activeText"),
                ("#output-text", "mutedText"),
                ("#palette-target-label", "mutedText"),
                ("#palette-color-label", "mutedText"),
                ("#palette-config", "mutedText"),
                ("#hint", "mutedText"),
            ):
                self.query_one(selector, Static).styles.color = self.palette[color]
            for row in self.query(CommandRow):
                row.query_one(Label).update(rich_tui_action_row(row.tui_action, palette=self.palette))

        def _tick(self) -> None:
            disable_terminal_text_selection()
            self.cli.drain_output()
            if self._status_is_active():
                self._status_frame = (self._status_frame + 1) % 3
            self._refresh_layout()

        def _read_setup_state(self) -> str:
            setup = check_local_helper_setup(self.root_dir)
            return "ready" if setup.get("ok") else "needs attention"

        def _display_system_status(self) -> str:
            if self._setup_state == "stopping":
                return "stopping"
            cli_status = str(self.cli.status or "ready").strip().lower()
            if cli_status in {"building", "reviewing", "cleaning cache", "analyzing"}:
                return cli_status
            if self._busy and self._active_action is not None:
                return self._active_action.running_label.lower()
            if self._setup_state != "ready":
                return self._setup_state
            return "ready"

        def _status_is_active(self) -> bool:
            _, role = _display_status_parts(self._display_system_status(), frame=self._status_frame)
            return role == "active"

        def _refresh_layout(self, *, force: bool = False) -> None:
            profile = stake_site_profile(self.cli.stake_site, root_dir=self.root_dir)
            on_page = self._active_action is not None
            on_palette = self._active_action is not None and self._active_action.action_id == "palette"
            page_status: str | Text = ""
            if on_page and self._active_action is not None:
                page_status = rich_page_status(
                    self._active_action,
                    frame=self._status_frame,
                    page_result=self._page_result,
                    palette=self.palette,
                )
            hint_text = self._hint_text(on_page=on_page)
            render_state = (
                str(self.display_workspace),
                profile["label"],
                self._display_system_status(),
                self._status_frame if self._status_is_active() else 0,
                self._active_action.action_id if self._active_action else "",
                self.cli.status,
                self._busy,
                page_status,
                hint_text,
                self._selected_palette_target,
                self._inline_message,
                self._page_result,
                self._output_scroll,
                len(self._output_lines),
                tuple(sorted(self.palette.items())),
            )
            if not force and render_state == self._last_render_state:
                return
            self._last_render_state = render_state

            self.query_one("#workspace-top", Static).update(str(self.display_workspace))
            self.query_one("#title", Static).update(
                rich_title_row(self._display_system_status(), frame=self._status_frame, palette=self.palette)
            )
            self.query_one("#stake-site", Static).update(rich_stake_site_row(profile["label"], palette=self.palette))

            self.query_one("#menu-wrap", Container).set_class(on_page, "hidden")
            self.query_one("#page-title", Static).set_class(not on_page, "hidden")
            self.query_one("#page-status", Static).set_class((not on_page) or on_palette, "hidden")
            self.query_one("#output-panel", Container).set_class((not on_page) or on_palette, "hidden")
            self.query_one("#palette-panel", Vertical).set_class(not on_palette, "hidden")

            if on_page and self._active_action is not None:
                self.query_one("#page-title", Static).update(self._active_action.label)
                self.query_one("#page-status", Static).update(page_status)
            if on_palette:
                self._refresh_palette_page()
            elif on_page:
                self._refresh_output_panel()

            self.query_one("#hint", Static).update(hint_text)

        def _hint_text(self, *, on_page: bool) -> str:
            if self._confirm_clean_until > time.monotonic():
                return "Press ctrl+c again to confirm cleanup. Escape returns to menu."
            if on_page:
                return "Escape returns to menu. ctrl+e exits."
            if self._inline_message:
                return self._inline_message
            return "Click a row, press Enter, or use ctrl shortcuts."

        def _refresh_palette_page(self) -> None:
            target_label = "Background" if self._selected_palette_target == "background" else "Center console"
            self.query_one("#palette-target-label", Static).update(f"Target: {target_label}")
            self.query_one("#palette-color-label", Static).update("Colors:")
            self.query_one("#palette-config", Static).update(
                "Current config: "
                f"Background {self.palette['background']} | "
                f"Center console {self.palette['panel']}"
            )

        def _append_output(self, text: str) -> None:
            if self.ui_thread is threading.current_thread():
                self._write_output(text)
                return
            try:
                self.call_from_thread(self._write_output, text)
            except RuntimeError:
                pass

        def _write_output(self, text: str) -> None:
            self._output_lines.extend(str(text).rstrip("\n").splitlines() or [""])
            self._output_scroll = max(0, len(self._output_lines) - OUTPUT_VISIBLE_HEIGHT)
            self._refresh_output_panel()

        def _refresh_output_panel(self) -> None:
            visible_height = OUTPUT_VISIBLE_HEIGHT
            max_scroll = max(0, len(self._output_lines) - visible_height)
            self._output_scroll = max(0, min(self._output_scroll, max_scroll))
            visible = self._output_lines[self._output_scroll : self._output_scroll + visible_height]
            clipped = [self._clip_output_line(line) for line in visible]
            self.query_one("#output-text", Static).update("\n".join(clipped))

        def _clip_output_line(self, line: str) -> str:
            clean = str(line).replace("\t", "    ")
            if len(clean) <= OUTPUT_TEXT_WIDTH:
                return clean
            return clean[: max(0, OUTPUT_TEXT_WIDTH - 1)] + "…"

        def _open_page(self, action: TuiAction) -> None:
            self._active_action = action
            self._page_result = ""
            self._output_lines = []
            self._output_scroll = 0
            if action.action_id != "palette":
                self.query_one("#output-panel", Container).focus()
            else:
                self.query_one("#palette-colors", ListView).focus()
            self._refresh_layout(force=True)

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            if isinstance(event.item, PaletteTargetRow):
                self._selected_palette_target = event.item.target_id
                self._refresh_layout(force=True)
                return
            if isinstance(event.item, PaletteColorRow):
                self._apply_palette_color(event.item.color)
                return
            action = getattr(event.item, "tui_action", None)
            if isinstance(action, TuiAction):
                self.action_run_action(action.action_id)

        def _apply_palette_color(self, color: str) -> None:
            self.palette[self._selected_palette_target] = color
            if self._selected_palette_target == "panel":
                self.palette["outputPanel"] = color
            save_tui_palette(self.palette, root_dir=self.root_dir)
            self._apply_palette()
            self._refresh_layout(force=True)

        def action_back(self) -> None:
            if self._active_action is None:
                return
            self._active_action = None
            self._page_result = ""
            self._confirm_clean_until = 0.0
            self._refresh_layout(force=True)

        def action_run_action(self, action_id: str) -> None:
            action = find_tui_action(action_id)
            if action is None:
                self._append_output(f"Unknown action: {action_id}")
                return
            if action.action_id == "exit":
                self.cli.stop_helper()
                self._stop_active_subprocess()
                self.exit(None)
                return
            if action.action_id == "stop":
                self._dispatch_stop_inline()
                return
            if action.action_id == "domain":
                self._toggle_domain_inline()
                return
            if action.action_id in {"review", "build"}:
                self._start_helper_inline(action)
                return
            if action.action_id == "clean":
                self._start_clean_inline(action)
                return
            if self._busy:
                self._write_output("Another command is already running.")
                return
            self._busy = True
            self.cli.status = action.running_label.lower()
            self._open_page(action)
            self._confirm_clean_until = 0.0
            threading.Thread(target=self._run_action_thread, args=(action,), daemon=True).start()

        def _clean_confirmed(self) -> bool:
            return self._confirm_clean_until > time.monotonic()

        def _start_helper_inline(self, action: TuiAction) -> None:
            if self._busy:
                self._inline_message = "Another command is already running."
                self._refresh_layout(force=True)
                return
            self._active_action = None
            self._inline_message = ""
            self._busy = True
            self.cli.status = "building" if action.action_id == "build" else "reviewing"
            self._refresh_layout(force=True)
            threading.Thread(target=self._start_helper_inline_thread, args=(action,), daemon=True).start()

        def _start_helper_inline_thread(self, action: TuiAction) -> None:
            try:
                self.cli.start_helper("build" if action.action_id == "build" else "review")
            finally:
                self._busy = False
                try:
                    self.call_from_thread(self._refresh_layout, force=True)
                except RuntimeError:
                    pass

        def _start_clean_inline(self, action: TuiAction) -> None:
            if self._busy:
                self._inline_message = "Another command is already running."
                self._refresh_layout(force=True)
                return
            self._active_action = None
            self._inline_message = ""
            self._busy = True
            self.cli.status = "cleaning cache"
            self._refresh_layout(force=True)
            threading.Thread(target=self._clean_inline_thread, args=(action,), daemon=True).start()

        def _clean_inline_thread(self, action: TuiAction) -> None:
            try:
                code = self._run_module_command(["-m", "app.supabase_cache", "--root-dir", str(self.root_dir)])
                self.cli.status = "ready" if code == 0 else "cleanup failed"
                self._inline_message = "Cleanup complete." if code == 0 else "Cleanup failed."
            finally:
                self._busy = False
                self._active_subprocess = None
                try:
                    self.call_from_thread(self._refresh_layout, force=True)
                except RuntimeError:
                    pass

        def _run_action_thread(self, action: TuiAction) -> None:
            failed = False
            try:
                self._dispatch_cli_action(action)
            except Exception as exc:  # pragma: no cover - defensive UI boundary.
                self._append_output(f"{action.label} failed: {exc}")
                self.cli.status = "error"
                failed = True
            finally:
                if self._stop_requested:
                    self.cli.status = "ready"
                    failed = False
                if action.action_id != "palette":
                    status_text = str(self.cli.status or "").lower()
                    self._page_result = "failed" if failed or "failed" in status_text or "error" in status_text else "done"
                self._busy = False
                self._active_subprocess = None
                self._stop_requested = False
                try:
                    self.call_from_thread(self._refresh_layout, force=True)
                except RuntimeError:
                    pass

        def _run_module_command(self, args: list[str]) -> int:
            python_exe = self.root_dir / ".venv" / "Scripts" / "python.exe"
            self._active_subprocess = subprocess.Popen(
                [str(python_exe), *args],
                cwd=self.root_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert self._active_subprocess.stdout is not None
            for line in self._active_subprocess.stdout:
                self._append_output(line.rstrip("\n"))
            return int(self._active_subprocess.wait())

        def _run_module_command_capture(self, args: list[str]) -> tuple[int, str]:
            python_exe = self.root_dir / ".venv" / "Scripts" / "python.exe"
            self._active_subprocess = subprocess.Popen(
                [str(python_exe), *args],
                cwd=self.root_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert self._active_subprocess.stdout is not None
            output_lines = [line.rstrip("\n") for line in self._active_subprocess.stdout]
            return int(self._active_subprocess.wait()), "\n".join(output_lines)

        def _dispatch_cli_action(self, action: TuiAction) -> None:
            if action.action_id == "review":
                self.cli.start_helper("review")
            elif action.action_id == "build":
                self.cli.start_helper("build")
            elif action.action_id == "backtest":
                self.cli.status = "analyzing"
                launch_backtest_console(self.root_dir)
                self._append_output("Historic analysis opened in a separate PowerShell window.")
                self._append_output("Close that window when finished.")
                self.cli.status = "ready"
            elif action.action_id == "history":
                self.cli.status = "historic sync"
                code, output = self._run_module_command_capture(["-m", "app.bet_history", "sync", "--json"])
                if code == 0:
                    try:
                        report = json.loads(output)
                    except json.JSONDecodeError:
                        for line in output.splitlines():
                            self._append_output(line)
                    else:
                        for line in format_historic_tui_summary(report):
                            self._append_output(line)
                else:
                    for line in output.splitlines() or ["Historic sync failed."]:
                        self._append_output(line)
                self.cli.status = "ready" if code == 0 else "historic failed"
            elif action.action_id == "logs":
                self.cli.run_logs({"--tail"})
                self.cli.status = "ready"
            elif action.action_id == "doctor":
                self.cli.run_doctor()
            elif action.action_id == "clean":
                self.cli.status = "cleaning cache"
                code = self._run_module_command(["-m", "app.supabase_cache", "--root-dir", str(self.root_dir)])
                self.cli.status = "ready" if code == 0 else "cleanup failed"
            elif action.action_id == "domain":
                self.cli.toggle_stake_site()
            elif action.action_id == "palette":
                self._append_output(f"Theme config: {tui_theme_path(root_dir=self.root_dir)}")
            else:
                self._append_output(f"No handler for {action.label}.")

        def _dispatch_stop(self) -> None:
            stopped = False
            if self.cli.process and self.cli.process.poll() is None:
                self.cli.stop_helper()
                stopped = True
            if self._stop_active_subprocess():
                stopped = True
            if stopped:
                self._stop_requested = True
                self._busy = False
                self.cli.status = "ready"
                self._append_output("Stopped active task.")
            else:
                self._append_output("No active task to stop.")
            self._refresh_layout(force=True)

        def _dispatch_stop_inline(self) -> None:
            self._active_action = None
            self._setup_state = "stopping"
            self._inline_message = "Stopping active task..."
            self._refresh_layout(force=True)
            threading.Thread(target=self._stop_inline_thread, daemon=True).start()

        def _stop_inline_thread(self) -> None:
            stopped = False
            if self.cli.process and self.cli.process.poll() is None:
                self.cli.stop_helper()
                stopped = True
            if self._stop_active_subprocess():
                stopped = True
            self._busy = False
            self.cli.status = "ready"
            self._setup_state = "ready"
            self._inline_message = "Stopped active task." if stopped else "No active task to stop."
            try:
                self.call_from_thread(self._refresh_layout, force=True)
            except RuntimeError:
                pass

        def _toggle_domain_inline(self) -> None:
            target = "bet" if self.cli.stake_site == "com" else "com"
            self.cli.set_stake_site(target, announce=False)
            profile = stake_site_profile(self.cli.stake_site, root_dir=self.root_dir)
            self._inline_message = f"Stake site set to {profile['label']}."
            self._refresh_layout(force=True)

        def _stop_active_subprocess(self) -> bool:
            process = self._active_subprocess
            if process is None or process.poll() is not None:
                return False
            process.terminate()
            try:
                process.wait(timeout=3)
            except Exception:
                process.kill()
            return True

        def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
            self._scroll_active_panel(event, -3)

        def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
            self._scroll_active_panel(event, 3)

        def _scroll_active_panel(self, event: Any, y: int) -> None:
            if self._active_action is None:
                return
            if hasattr(event, "stop"):
                event.stop()
            if self._active_action.action_id == "palette":
                widget = self.query_one("#palette-colors", ListView)
                if hasattr(widget, "scroll_relative"):
                    widget.scroll_relative(y=y, animate=False)
                return
            max_scroll = max(0, len(self._output_lines) - OUTPUT_VISIBLE_HEIGHT)
            self._output_scroll = max(0, min(self._output_scroll + y, max_scroll))
            self._refresh_output_panel()
            self._refresh_layout(force=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stake-GPT Grok-style helper TUI.")
    parser.add_argument("--check", action="store_true", help="Report TUI dependency status.")
    parser.add_argument("--dump-actions", action="store_true", help="Print available TUI actions.")
    args = parser.parse_args(argv)

    if args.check:
        status = textual_dependency_status()
        print(f"textual: {'available' if status['available'] else 'missing'}")
        if status["error"]:
            print(status["error"])
        return 0 if status["available"] else 1

    if args.dump_actions:
        for action in TUI_ACTIONS:
            print(format_tui_action_row(action))
        return 0

    if not TEXTUAL_AVAILABLE:
        print("Textual is not installed. Run:")
        print("  .\\.tools\\uv\\uv.exe pip install -r requirements-local.txt")
        if TEXTUAL_IMPORT_ERROR:
            print(f"Reason: {TEXTUAL_IMPORT_ERROR}")
        return 1

    StakeGptTui().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
