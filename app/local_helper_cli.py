from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .local_helper_setup import check_local_helper_setup


ROOT_DIR = Path(__file__).resolve().parents[1]
APP_DISPLAY_NAME = "Stake-GPT"
DEFAULT_TEXT_COLOR = "#F4F6F8"
DEFAULT_CLI_COLOR_SETTINGS = {"textColor": DEFAULT_TEXT_COLOR}
RESET = "\033[0m"
SHORT_RULE = "─" * 40
WIDE_RULE = "─" * 78
RGB_PALETTE = [
    ("Default White", "#F4F6F8"),
    ("Stake Green", "#00E701"),
    ("Soft Blue", "#74B9FF"),
    ("Gold", "#FFD166"),
    ("Rose", "#FF6B8A"),
    ("Violet", "#B388FF"),
    ("Mint", "#7CE7C7"),
]


def cli_color_settings_path(*, root_dir: Path = ROOT_DIR) -> Path:
    return root_dir / "data" / "workflow" / "helper-cli-color.json"


def cli_color_presets_dir(*, root_dir: Path = ROOT_DIR) -> Path:
    return root_dir / "data" / "workflow" / "color-presets"


def normalize_color_choice(value: Any, *, fallback: str) -> str:
    if isinstance(value, (tuple, list)) and len(value) == 3:
        try:
            red, green, blue = [int(part) for part in value]
        except (TypeError, ValueError):
            return fallback
        if all(0 <= part <= 255 for part in (red, green, blue)):
            return f"#{red:02X}{green:02X}{blue:02X}"
        return fallback

    text = str(value or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
        return text.upper()
    if re.fullmatch(r"[0-9A-Fa-f]{6}", text):
        return f"#{text.upper()}"
    return fallback


def clean_cli_color_settings(settings: dict[str, str]) -> dict[str, str]:
    return {
        "textColor": normalize_color_choice(
            settings.get("textColor"),
            fallback=DEFAULT_TEXT_COLOR,
        )
    }


def load_cli_color_settings(path: Path | None = None) -> dict[str, str]:
    settings_path = path or cli_color_settings_path()
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CLI_COLOR_SETTINGS)
    if not isinstance(raw, dict):
        return dict(DEFAULT_CLI_COLOR_SETTINGS)
    return clean_cli_color_settings(raw)


def save_cli_color_settings(
    settings: dict[str, str],
    path: Path | None = None,
) -> Path:
    settings_path = path or cli_color_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(clean_cli_color_settings(settings), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return settings_path


def safe_color_preset_name(name: str) -> str:
    cleaned = "".join(
        "-" if char in '<>:"/\\|?*' or ord(char) < 32 else char
        for char in str(name).strip()
    ).strip(" .")
    if not cleaned:
        raise ValueError("Enter a preset name before saving.")
    return cleaned


def color_preset_path(name: str, directory: Path | None = None) -> Path:
    preset_name = safe_color_preset_name(name)
    return (directory or cli_color_presets_dir()) / f"{preset_name}.json"


def save_cli_color_preset(
    name: str,
    settings: dict[str, str],
    directory: Path | None = None,
) -> Path:
    path = color_preset_path(name, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(clean_cli_color_settings(settings), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_cli_color_preset(
    name: str,
    directory: Path | None = None,
) -> dict[str, str]:
    path = color_preset_path(name, directory)
    if not path.exists():
        raise FileNotFoundError(f"Color preset not found: {path.stem}")
    return load_cli_color_settings(path)


def list_cli_color_presets(directory: Path | None = None) -> list[str]:
    preset_dir = directory or cli_color_presets_dir()
    try:
        files = preset_dir.glob("*.json")
    except OSError:
        return []
    return sorted(path.stem for path in files if path.is_file())


def rgb_escape(hex_color: str) -> str:
    color = normalize_color_choice(hex_color, fallback=DEFAULT_TEXT_COLOR)
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    return f"\033[38;2;{red};{green};{blue}m"


def colorize(text: str, hex_color: str) -> str:
    return f"{rgb_escape(hex_color)}{text}{RESET}"


def is_exit_command(command: str) -> bool:
    return command.strip().lower() in {"0", "6", "exit", "q", "quit"}


def _setup_check_label(name: str) -> str:
    if name == "SUPABASE_URL configured":
        return "SUPABASE_URL"
    if name == "SUPABASE_SERVICE_ROLE_KEY configured":
        return "SERVICE_ROLE_KEY"
    return name


def _setup_check_detail(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "")
    if name.endswith(" configured"):
        return "configured" if item.get("ok") else "missing"
    return str(item.get("detail") or "")


def format_main_menu(
    *,
    status: str,
    mode: str,
    browser: str,
    supabase: str,
    setup_report: dict[str, Any] | None = None,
) -> str:
    lines = [
        "Stake-GPT",
        SHORT_RULE,
        f"Status: {status}",
        f"Mode: {mode}",
        f"Browser: {browser}",
        f"Supabase: {supabase}",
        "",
        "Commands:",
        "  review      start board scan",
        "  build       build validated slip",
        "  setup       run setup check",
        "  clean       clean cache",
        "  stop        stop current task",
        "  exit        close app",
    ]
    if setup_report is not None:
        lines.extend(["", WIDE_RULE, "", format_setup_screen(setup_report)])
    lines.extend(["", "stake-gpt >"])
    return "\n".join(lines)


def format_setup_screen(report: dict[str, Any]) -> str:
    lines = ["System Check", WIDE_RULE]
    for item in report.get("checks") or []:
        mark = "OK" if item.get("ok") else "MISSING"
        label = _setup_check_label(str(item.get("name") or ""))
        detail = _setup_check_detail(item)
        lines.append(f"[{mark}] {label + ':':<20}{detail}")

    warnings = list(report.get("warnings") or [])
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"[!] {warning}")

    lines.append("")
    lines.append("Ready." if report.get("ok") else "Fix missing items before starting the helper.")
    return "\n".join(lines)


def enable_virtual_terminal() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        return


class StakeGptCli:
    def __init__(
        self,
        *,
        root_dir: Path = ROOT_DIR,
        input_func=input,
        output_func=print,
    ) -> None:
        self.root_dir = root_dir
        self.input = input_func
        self.output = output_func
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.status = "idle"
        self.mode = "review-safe"
        self.browser = "ready"
        self.supabase = "connected"
        self.text_color = load_cli_color_settings(
            cli_color_settings_path(root_dir=root_dir)
        )["textColor"]

    def run(self) -> int:
        enable_virtual_terminal()
        while True:
            self.drain_output()
            setup_report = check_local_helper_setup(self.root_dir)
            command = self.input(colorize(format_main_menu(
                status=self.status,
                mode=self.mode,
                browser=self.browser,
                supabase=self.supabase,
                setup_report=setup_report,
            ), self.text_color) + " ").strip().lower()
            if is_exit_command(command):
                self.stop_helper()
                return 0
            self.handle_command(command)

    def handle_command(self, command: str) -> None:
        if command in {"1", "review", "r"}:
            self.start_helper("review")
        elif command in {"2", "build", "b"}:
            self.start_helper("build")
        elif command in {"3", "setup", "s"}:
            self.run_setup_check()
        elif command in {"4", "clean", "cache", "c"}:
            self.run_cache_cleanup()
        elif command in {"5", "stop"}:
            self.stop_helper()
        elif command in {"color", "theme", "t"}:
            self.run_color_menu()
        elif command:
            self.output(colorize(f"Unknown command: {command}\n", self.text_color))

    def start_helper(self, mode: str) -> None:
        if self.process and self.process.poll() is None:
            self.output(colorize("Helper is already running.\n", self.text_color))
            return

        report = check_local_helper_setup(self.root_dir)
        if not report["ok"]:
            self.output(colorize(format_setup_screen(report) + "\n", self.text_color))
            self.status = "setup needs attention"
            return

        python_exe = self.root_dir / ".venv" / "Scripts" / "python.exe"
        self.status = f"running {mode}"
        self.mode = "build-enabled" if mode == "build" else "review-safe"
        self.output(colorize(f"Starting helper in {mode} mode...\n", self.text_color))
        self.process = subprocess.Popen(
            [str(python_exe), "-m", "app.local_stake_helper", "--mode", mode],
            cwd=self.root_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self.capture_output, daemon=True).start()

    def run_setup_check(self) -> None:
        report = check_local_helper_setup(self.root_dir)
        self.output(colorize(format_setup_screen(report), self.text_color))
        self.output(colorize("\n\n[0] Back\n", self.text_color))
        self.input(colorize("stake-gpt/setup > ", self.text_color))
        self.status = "setup ready" if report.get("ok") else "setup needs attention"

    def run_cache_cleanup(self) -> None:
        python_exe = self.root_dir / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            self.output(colorize(f"Could not find {python_exe}. Run setup first.\n", self.text_color))
            self.status = "setup needs attention"
            return
        if not (self.root_dir / ".env").exists():
            self.output(colorize(f"Could not find {self.root_dir / '.env'}.\n", self.text_color))
            self.status = "setup needs attention"
            return

        self.status = "cleaning cache"
        self.output(colorize("Running Supabase cache cleanup...\n", self.text_color))
        completed = subprocess.run(
            [str(python_exe), "-m", "app.supabase_cache"],
            cwd=self.root_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if completed.stdout:
            self.output(colorize(completed.stdout.rstrip() + "\n", self.text_color))
        self.output(colorize(f"Cleanup exited with code {completed.returncode}.\n", self.text_color))
        self.status = "idle" if completed.returncode == 0 else "cleanup failed"
        self.input(colorize("\n[0] Back\nstake-gpt/cache > ", self.text_color))

    def stop_helper(self) -> None:
        if not self.process or self.process.poll() is not None:
            self.status = "stopped"
            return

        self.output(colorize("Stopping helper...\n", self.text_color))
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.status = "stopped"
        self.output(colorize("Helper stopped.\n", self.text_color))

    def format_color_menu(self) -> str:
        lines = [
            "Text Color",
            "------------------------------------------------------------",
            f"Current: {self.text_color}",
            "",
        ]
        for index, (name, color) in enumerate(RGB_PALETTE, start=1):
            lines.append(f"  [{index}] {name:<16} {color}")
        lines.extend(
            [
                "  [8] Custom HEX",
                "  [9] Custom RGB",
                "  [10] Save Preset",
                "  [11] Load Preset",
                "  [0] Back",
                "",
                "stake-gpt/color >",
            ]
        )
        return "\n".join(lines)

    def run_color_menu(self) -> None:
        while True:
            choice = self.input(colorize(self.format_color_menu() + " ", self.text_color)).strip()
            if choice in {"0", "back", "b"}:
                return
            if choice.isdigit() and 1 <= int(choice) <= len(RGB_PALETTE):
                self.set_text_color(RGB_PALETTE[int(choice) - 1][1])
                continue
            if choice == "8":
                raw = self.input(colorize("HEX color (#RRGGBB): ", self.text_color)).strip()
                self.set_text_color(raw)
                continue
            if choice == "9":
                raw = self.input(colorize("RGB color (R,G,B): ", self.text_color)).strip()
                parts = [item.strip() for item in raw.split(",")]
                if len(parts) != 3:
                    self.output(colorize("Enter RGB as R,G,B.\n", self.text_color))
                    continue
                self.set_text_color(tuple(parts))
                continue
            if choice == "10":
                name = self.input(colorize("Preset name: ", self.text_color)).strip()
                try:
                    path = save_cli_color_preset(
                        name,
                        {"textColor": self.text_color},
                        cli_color_presets_dir(root_dir=self.root_dir),
                    )
                except ValueError as exc:
                    self.output(colorize(f"{exc}\n", self.text_color))
                    continue
                self.output(colorize(f"Saved preset: {path.stem}\n", self.text_color))
                continue
            if choice == "11":
                self.load_preset_from_menu()
                continue
            self.output(colorize("Unknown color command.\n", self.text_color))

    def set_text_color(self, value: Any) -> None:
        color = normalize_color_choice(value, fallback=self.text_color)
        self.text_color = color
        save_cli_color_settings(
            {"textColor": self.text_color},
            cli_color_settings_path(root_dir=self.root_dir),
        )
        self.output(colorize(f"Text color set to {self.text_color}.\n", self.text_color))

    def load_preset_from_menu(self) -> None:
        preset_dir = cli_color_presets_dir(root_dir=self.root_dir)
        preset_dir.mkdir(parents=True, exist_ok=True)
        presets = list_cli_color_presets(preset_dir)
        if not presets:
            self.output(colorize(f"No presets found in {preset_dir}.\n", self.text_color))
            return
        for index, name in enumerate(presets, start=1):
            self.output(colorize(f"  [{index}] {name}\n", self.text_color))
        choice = self.input(colorize("[0] Back\nPreset > ", self.text_color)).strip()
        if choice in {"0", "back", "b"}:
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(presets)):
            self.output(colorize("Invalid preset choice.\n", self.text_color))
            return
        settings = load_cli_color_preset(presets[int(choice) - 1], preset_dir)
        self.set_text_color(settings["textColor"])

    def capture_output(self) -> None:
        if not self.process or not self.process.stdout:
            return
        for line in self.process.stdout:
            self.output_queue.put(line)
        code = self.process.poll()
        self.output_queue.put(f"Helper exited with code {code}.\n")

    def drain_output(self) -> None:
        while True:
            try:
                text = self.output_queue.get_nowait()
            except queue.Empty:
                return
            self.output(colorize(text.rstrip("\n") + "\n", self.text_color))


def main() -> int:
    return StakeGptCli().run()


if __name__ == "__main__":
    raise SystemExit(main())
