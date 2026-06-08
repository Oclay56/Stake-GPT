from __future__ import annotations

import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .local_helper_setup import check_local_helper_setup

try:
    from rich.console import Console
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by environments without rich.
    Console = None
    Text = None
    RICH_AVAILABLE = False


ROOT_DIR = Path(__file__).resolve().parents[1]
APP_DISPLAY_NAME = "Stake-GPT"
CLI_VERSION = "1.5.0"
DEFAULT_TEXT_COLOR = "#F4F6F8"
DEFAULT_CLI_COLOR_SETTINGS = {"textColor": DEFAULT_TEXT_COLOR}
RESET = "\033[0m"
SHORT_RULE = "-" * 40
WIDE_RULE = "-" * 78
HEAVY_RULE = "━" * 78
RGB_PALETTE = [
    ("Default White", "#F4F6F8"),
    ("Stake Green", "#00E701"),
    ("Soft Blue", "#74B9FF"),
    ("Gold", "#FFD166"),
    ("Rose", "#FF6B8A"),
    ("Violet", "#B388FF"),
    ("Mint", "#7CE7C7"),
]
SEMANTIC_COLORS = {
    "accent": "#B388FF",
    "ok": "#00E701",
    "warn": "#FFD166",
    "fail": "#FF6B8A",
    "info": "#74B9FF",
}
ERROR_LOG_PATTERNS = ("error", "fail", "missing", "traceback", "warn")
COMMAND_ROWS = [
    ("review, r", "Scan board"),
    ("build, b", "Build validated slip"),
    ("status, s", "Show status"),
    ("domain, q", "Toggle Stake site"),
    ("historic, i", "Import bet historic"),
    ("analysis, z", "Analyze historic"),
    ("logs, l", "View logs"),
    ("doctor, d", "Run full system check"),
    ("clean, c", "Clear cache"),
    ("help, h", "Show help"),
    ("exit, e", "Close app"),
]
HELP_EXTRA_ROWS = [
    ("logs --tail", "Tail the latest helper log"),
    ("logs --errors", "Show warnings and errors"),
    ("historic --dry-run <file>", "Preview a historic import"),
    ("historic", "Auto-import new historic files and show status"),
    ("historic report", "Show imported historic summary without syncing"),
    ("historic sync", "Import new files from the historic import folder"),
    ("historic review", "Show rows needing review"),
    ("historic enrich", "Store frozen historical MLB snapshots"),
    ("historic enrich --missing-only", "Enrich only historic legs not yet linked"),
    ("analysis", "Open historic analysis dashboard"),
    ("analysis tickets", "Show ticket-level SGM performance"),
    ("analysis signals", "Show market/player/line signals"),
    ("analysis calibration", "Show historical calibration buckets"),
    ("analysis --ticket <id>", "Analyze only one ticket"),
    ("historic analysis --import-id <id>", "Analyze only one import/session"),
    ("historic imports", "List saved historic imports"),
    ("clean --yes", "Clear cache without confirmation"),
    ("setup", "Run quick setup checks"),
    ("stop", "Stop the running helper"),
    ("domain bet", "Use stake.bet profile"),
    ("domain com", "Use stake.com profile"),
    ("color", "Adjust CLI accent color"),
]
LOADING_PROMPT_STATES = {"building", "reviewing", "cleaning"}
LOADING_DOT_FRAMES = (".  ", ".. ", "...")
LOADING_PROMPT_INTERVAL_SECONDS = 0.35
STAKE_SITE_PROFILES = {
    "com": {
        "label": "stake.com",
        "baseUrl": "https://stake.com",
        "cdpUrl": "http://127.0.0.1:9222",
        "profileDir": Path("data") / "chrome-stake-ui",
    },
    "bet": {
        "label": "stake.bet",
        "baseUrl": "https://stake.bet",
        "cdpUrl": "http://127.0.0.1:9223",
        "profileDir": Path("data") / "chrome-stake-ui-bet",
    },
}


def cli_color_settings_path(*, root_dir: Path = ROOT_DIR) -> Path:
    return root_dir / "data" / "workflow" / "helper-cli-color.json"


def cli_color_presets_dir(*, root_dir: Path = ROOT_DIR) -> Path:
    return root_dir / "data" / "workflow" / "color-presets"


def cli_log_path(*, root_dir: Path = ROOT_DIR) -> Path:
    return root_dir / "logs" / "latest.log"


def clean_stake_site(value: Any, *, fallback: str = "com") -> str:
    raw = str(value or "").strip().lower()
    if "://" in raw:
        parsed = urlparse(raw)
        raw = str(parsed.netloc or parsed.path).strip().lower()
    raw = raw.split("/", 1)[0]
    aliases = {
        "stake.com": "com",
        "https://stake.com": "com",
        "com": "com",
        ".com": "com",
        "stake.bet": "bet",
        "https://stake.bet": "bet",
        "bet": "bet",
        ".bet": "bet",
    }
    return aliases.get(raw, fallback if fallback in STAKE_SITE_PROFILES else "com")


def stake_site_profile(site: str, *, root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    clean_site = clean_stake_site(site)
    profile = dict(STAKE_SITE_PROFILES[clean_site])
    profile["site"] = clean_site
    profile["profileDir"] = root_dir / profile["profileDir"]
    return profile


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
    return command.strip().lower() in {"0", "6", "e", "exit", "quit"}


def split_cli_command(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    return [part.strip().strip('"').strip("'") for part in parts if part.strip()]


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _maybe_color(text: str, role: str, use_color: bool) -> str:
    if not use_color:
        return text
    return colorize(text, SEMANTIC_COLORS.get(role, DEFAULT_TEXT_COLOR))


def _mark(label: str, role: str, *, use_color: bool = False) -> str:
    return _maybe_color(f"[{label}]", role, use_color)


def _rich_style(role: str) -> str:
    return SEMANTIC_COLORS.get(role, DEFAULT_TEXT_COLOR)


def _prompt_state_role(state: str) -> str:
    if state == "ready":
        return "ok"
    if state in {"setup", "stopped"}:
        return "warn"
    return "info"


def _rich_status(text: str, role: str) -> str:
    label = "OK" if role == "ok" else "WARN" if role == "warn" else "FAIL" if role == "fail" else "INFO"
    return f"[{_rich_style(role)}][{label}][/] {text}"


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


def _setup_check_role(item: dict[str, Any]) -> str:
    return "ok" if item.get("ok") else "fail"


def _setup_check_mark(item: dict[str, Any], *, use_color: bool = False) -> str:
    return _mark("OK", "ok", use_color=use_color) if item.get("ok") else _mark("FAIL", "fail", use_color=use_color)


def _health_label(report: dict[str, Any] | None) -> str:
    if not report:
        return "UNKNOWN"
    if not report.get("ok"):
        return "NEEDS ATTENTION"
    if report.get("warnings"):
        return "GOOD WITH WARNINGS"
    return "READY"


def _prompt_state(status: str) -> str:
    cleaned = str(status or "").strip().lower()
    if "build" in cleaned:
        return "building"
    if "review" in cleaned or "scan" in cleaned:
        return "reviewing"
    if "setup" in cleaned or "attention" in cleaned:
        return "setup"
    if "error" in cleaned or "failed" in cleaned:
        return "error"
    if "clean" in cleaned:
        return "cleaning"
    if "stop" in cleaned:
        return "stopped"
    return "ready"


def _loading_dots(frame: int | None = None) -> str:
    if frame is None:
        return "..."
    return LOADING_DOT_FRAMES[frame % len(LOADING_DOT_FRAMES)]


def _state_tag(status: str, *, frame: int | None = None) -> str:
    state = _prompt_state(status)
    suffix = f" {_loading_dots(frame)}" if state in LOADING_PROMPT_STATES else ""
    return f"[{state}]{suffix}"


def _prompt_line(status: str, *, trailing_space: bool = False, frame: int | None = None) -> str:
    line = f"stake-gpt {_state_tag(status, frame=frame)} >"
    return f"{line} " if trailing_space else line


def _colored_prompt_line(status: str, *, trailing_space: bool = False, frame: int | None = None) -> str:
    state = _prompt_state(status)
    tag = colorize(_state_tag(status, frame=frame), SEMANTIC_COLORS[_prompt_state_role(state)])
    line = f"stake-gpt {tag} >"
    return f"{line} " if trailing_space else line


def _display_state(status: str) -> str:
    state = _prompt_state(status)
    return {
        "building": "Building",
        "reviewing": "Reviewing",
        "cleaning": "Cleaning cache",
        "setup": "Setup needs attention",
        "error": "Error",
        "stopped": "Stopped",
        "ready": "Ready",
    }.get(state, "Ready")


def _browser_status(setup_report: dict[str, Any] | None, fallback: str) -> tuple[str, str]:
    for item in (setup_report or {}).get("checks") or []:
        if item.get("name") == "Chrome executable":
            return ("Chrome connected", "ok") if item.get("ok") else ("Chrome missing", "fail")
    return (fallback, "info")


def _supabase_status(setup_report: dict[str, Any] | None, fallback: str) -> tuple[str, str]:
    checks = {
        str(item.get("name") or ""): bool(item.get("ok"))
        for item in (setup_report or {}).get("checks") or []
    }
    if not checks:
        return (fallback, "info")
    required = (
        checks.get("SUPABASE_URL configured"),
        checks.get("SUPABASE_SERVICE_ROLE_KEY configured"),
    )
    if all(required):
        return ("Connected", "ok")
    return ("missing credentials", "fail")


def _python_status(setup_report: dict[str, Any] | None) -> tuple[str, str]:
    for item in (setup_report or {}).get("checks") or []:
        if item.get("name") == "Python venv":
            return (".venv active", "ok") if item.get("ok") else (".venv missing", "fail")
    return ("unknown", "info")


def _cleanup_status(setup_report: dict[str, Any] | None) -> tuple[str, str]:
    warnings = [str(warning) for warning in (setup_report or {}).get("warnings") or []]
    if any("AZP_SUPABASE_AUTO_CLEANUP_MINUTES" in warning for warning in warnings):
        return ("Using default: 60 minutes", "warn")
    return ("Configured", "ok")


def _warning_detail_lines(warning: str, *, use_color: bool = False) -> list[str]:
    text = str(warning or "").strip()
    mark = _mark("WARN", "warn", use_color=use_color)
    if "AZP_SUPABASE_AUTO_CLEANUP_MINUTES" in text:
        return [
            f"{mark} AZP_SUPABASE_AUTO_CLEANUP_MINUTES is not set.",
            "       Impact: not dangerous; using default cleanup timing.",
            "       Using default: 60 minutes.",
            "       To customize, add AZP_SUPABASE_AUTO_CLEANUP_MINUTES=60 to .env.",
        ]
    if "AZP_LOCAL_UI_JOB_TABLE" in text:
        return [
            f"{mark} AZP_LOCAL_UI_JOB_TABLE is not set.",
            "       Impact: not dangerous; using the default Supabase job table.",
            "       Using default: local_ui_jobs.",
            "       To customize, add AZP_LOCAL_UI_JOB_TABLE=local_ui_jobs to .env.",
        ]
    if text:
        return [
            f"{mark} {text}",
            "       Review this warning before relying on build-mode helper output.",
        ]
    return []


def _command_lines(*, include_extra: bool = False) -> list[str]:
    rows = [*COMMAND_ROWS, *(HELP_EXTRA_ROWS if include_extra else [])]
    return [f"  {command:<14} {description}" for command, description in rows]


def load_environment_label(root_dir: Path = ROOT_DIR) -> str:
    for key in ("AZP_ENV", "ENVIRONMENT", "APP_ENV"):
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    env_path = root_dir / ".env"
    if not env_path.exists():
        return "local"
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in {"AZP_ENV", "ENVIRONMENT", "APP_ENV"}:
                cleaned = value.strip().strip('"').strip("'")
                if cleaned:
                    return cleaned
    except OSError:
        return "local"
    return "local"


def format_main_menu(
    *,
    status: str,
    mode: str,
    browser: str,
    supabase: str,
    setup_report: dict[str, Any] | None = None,
    root_dir: Path = ROOT_DIR,
    log_path: Path | None = None,
    environment: str | None = None,
    last_scan: str = "none",
    last_build: str = "none",
    last_exit: str = "none",
    stake_site: str = "com",
    use_color: bool = False,
) -> str:
    browser_label, browser_role = _browser_status(setup_report, browser)
    supabase_label, supabase_role = _supabase_status(setup_report, supabase)
    python_label, python_role = _python_status(setup_report)
    cleanup_label, cleanup_role = _cleanup_status(setup_report)
    lines = [
        f"{APP_DISPLAY_NAME} {_state_tag(status)} CLI v{CLI_VERSION}",
        f"Environment: {environment or load_environment_label(root_dir)}",
        f"Workspace: {root_dir}",
        "",
        HEAVY_RULE,
        "",
        "System:",
        f"  {_mark('OK' if browser_role == 'ok' else 'FAIL', browser_role, use_color=use_color)} Browser      {browser_label}",
        f"  {_mark('OK' if supabase_role == 'ok' else 'FAIL', supabase_role, use_color=use_color)} Supabase     {supabase_label}",
        f"  {_mark('OK' if python_role == 'ok' else 'FAIL', python_role, use_color=use_color)} Python       {python_label}",
        f"  {_mark('WARN' if cleanup_role == 'warn' else 'OK', cleanup_role, use_color=use_color)} Cleanup      {cleanup_label}",
        "",
        HEAVY_RULE,
        "",
        "Status:",
        f"  State: {_display_state(status)}",
        f"  Mode: {mode}",
        f"  Stake site: {stake_site_profile(stake_site, root_dir=root_dir)['label']}",
        f"  Last scan: {last_scan}",
        f"  Last build: {last_build}",
        "",
        HEAVY_RULE,
        "",
        "Commands:",
        *_command_lines(),
    ]
    lines.extend(["", HEAVY_RULE, "", _prompt_line(status)])
    return "\n".join(lines)


def format_status_screen(
    *,
    status: str,
    mode: str,
    browser: str,
    supabase: str,
    setup_report: dict[str, Any] | None = None,
    root_dir: Path = ROOT_DIR,
    log_path: Path | None = None,
    environment: str | None = None,
    last_scan: str = "none",
    last_build: str = "none",
    last_exit: str = "none",
    stake_site: str = "com",
    use_color: bool = False,
) -> str:
    menu = format_main_menu(
        status=status,
        mode=mode,
        browser=browser,
        supabase=supabase,
        setup_report=setup_report,
        root_dir=root_dir,
        log_path=log_path,
        environment=environment,
        last_scan=last_scan,
        last_build=last_build,
        last_exit=last_exit,
        stake_site=stake_site,
        use_color=use_color,
    )
    return menu.rsplit("\n\nstake-gpt [", 1)[0]


def format_setup_screen(report: dict[str, Any], *, use_color: bool = False) -> str:
    lines = ["System Check", WIDE_RULE]
    for item in report.get("checks") or []:
        label = _setup_check_label(str(item.get("name") or ""))
        detail = _setup_check_detail(item)
        lines.append(f"{_setup_check_mark(item, use_color=use_color)} {label + ':':<20}{detail}")

    warnings = list(report.get("warnings") or [])
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in warnings:
            lines.extend(_warning_detail_lines(str(warning), use_color=use_color))

    lines.append("")
    lines.append("Ready." if report.get("ok") else "Fix missing items before starting the helper.")
    return "\n".join(lines)


def format_help_screen(*, use_color: bool = False) -> str:
    lines = [
        f"{APP_DISPLAY_NAME} CLI Help",
        WIDE_RULE,
        "",
        "Commands:",
        *_command_lines(include_extra=True),
        "",
        "Examples:",
        "  review",
        "  build",
        "  status",
        "  historic C:\\path\\bets.csv --dry-run",
        "  historic sync",
        "  historic report",
        "  historic review",
        "  historic analysis",
        "  historic imports",
        "  logs --errors",
        "  clean --yes",
    ]
    return "\n".join(lines)


def build_doctor_report(
    *,
    root_dir: Path,
    setup_report: dict[str, Any],
    log_path: Path,
) -> dict[str, Any]:
    diagnostics = [
        _diagnostic_check("Workspace writable", _path_is_writable(root_dir), str(root_dir)),
        _diagnostic_check("Logs writable", _path_is_writable(log_path.parent), str(log_path)),
        _diagnostic_check("Launcher BAT", (root_dir / "Stake-GPT.bat").exists(), str(root_dir / "Stake-GPT.bat")),
        _diagnostic_check("Requirements file", (root_dir / "requirements.txt").exists(), str(root_dir / "requirements.txt")),
    ]
    checks = list(setup_report.get("checks") or []) + diagnostics
    return {
        "ok": bool(setup_report.get("ok")) and all(item["ok"] for item in diagnostics),
        "checks": checks,
        "warnings": list(setup_report.get("warnings") or []),
    }


def format_doctor_screen(report: dict[str, Any], *, use_color: bool = False) -> str:
    health = _health_label(report)
    role = "ok" if health == "READY" else "warn" if "WARN" in health else "fail"
    lines = [
        "Doctor",
        WIDE_RULE,
        f"System health: {_maybe_color(health, role, use_color)}",
        "",
    ]
    lines.append(format_setup_screen(report, use_color=use_color))
    lines.extend(
        [
            "",
            "Notes:",
            "- Doctor checks local prerequisites and write access.",
            "- Supabase table/network checks happen when helper jobs or cleanup run.",
        ]
    )
    return "\n".join(lines)


def format_log_screen(log_path: Path, lines: list[str], *, heading: str = "Logs") -> str:
    output = [
        heading,
        WIDE_RULE,
        f"Path: {log_path}",
        "",
    ]
    if lines:
        output.extend(line.rstrip("\n") for line in lines)
    else:
        output.append("No log entries yet.")
    return "\n".join(output)


def _diagnostic_check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _path_is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".stake-gpt-write-test"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


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
        self.rich_console = Console() if RICH_AVAILABLE and input_func is input and output_func is print else None
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.status = "ready"
        self.mode = "review-safe"
        self.browser = "checking"
        self.supabase = "checking"
        self.last_scan = "none"
        self.last_build = "none"
        self.last_helper_exit = "none"
        self.environment = load_environment_label(root_dir)
        self.stake_site = clean_stake_site(
            os.getenv("AZP_STAKE_SITE") or os.getenv("AZP_STAKE_DOMAIN")
        )
        self.log_path = cli_log_path(root_dir=root_dir)
        self.text_color = load_cli_color_settings(
            cli_color_settings_path(root_dir=root_dir)
        )["textColor"]
        self._initialize_log()

    def run(self) -> int:
        enable_virtual_terminal()
        while True:
            self.drain_output()
            setup_report = check_local_helper_setup(self.root_dir)
            command = self.prompt_command(setup_report)
            self.write_log(f"> {command or '[empty]'}\n")
            if is_exit_command(command):
                self.stop_helper()
                return 0
            self.handle_command(command)

    def prompt_command(self, setup_report: dict[str, Any]) -> str:
        if self.rich_console is not None:
            self.rich_console.print(self.rich_main_menu(setup_report))
            if self._can_animate_prompt():
                return self.animated_prompt_command().strip().lower()
            return self.rich_console.input(self.rich_prompt(), markup=False).strip().lower()
        return self.input(
            format_main_menu(
                status=self.status,
                mode=self.mode,
                browser=self.browser,
                supabase=self.supabase,
                setup_report=setup_report,
                root_dir=self.root_dir,
                log_path=self.log_path,
                environment=self.environment,
                last_scan=self.last_scan,
                last_build=self.last_build,
                last_exit=self.last_helper_exit,
                stake_site=self.stake_site,
                use_color=True,
            )
            + " "
        ).strip().lower()

    def rich_prompt(self, *, frame: int | None = None) -> Any:
        state = _prompt_state(self.status)
        style = _rich_style(_prompt_state_role(state))
        if RICH_AVAILABLE:
            prompt = Text()
            prompt.append("stake-gpt", style=f"bold {_rich_style('accent')}")
            prompt.append(" ")
            prompt.append(f"[{state}]", style=style)
            if state in LOADING_PROMPT_STATES:
                prompt.append(f" {_loading_dots(frame)}", style=style)
            prompt.append(" > ")
            return prompt
        return _prompt_line(self.status, trailing_space=True, frame=frame)

    def _can_animate_prompt(self) -> bool:
        return (
            os.name == "nt"
            and self.input is input
            and self.output is print
            and sys.stdin.isatty()
            and sys.stdout.isatty()
            and _prompt_state(self.status) in LOADING_PROMPT_STATES
        )

    def animated_prompt_command(self) -> str:
        try:
            import msvcrt
        except ImportError:
            if self.rich_console is not None:
                return self.rich_console.input(self.rich_prompt(), markup=False)
            return self.input(_prompt_line(self.status, trailing_space=True))

        buffer: list[str] = []
        frame = 0
        last_rendered_length = 0
        last_tick = time.monotonic()
        last_prompt_state = _prompt_state(self.status)

        def render() -> None:
            nonlocal last_rendered_length
            prompt = _colored_prompt_line(self.status, trailing_space=True, frame=frame)
            line = prompt + "".join(buffer)
            visible_length = len(strip_ansi(line))
            padding = " " * max(0, last_rendered_length - visible_length)
            sys.stdout.write("\r" + line + padding)
            sys.stdout.flush()
            last_rendered_length = visible_length

        render()
        while True:
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in {"\r", "\n"}:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return "".join(buffer)
                if char == "\x03":
                    raise KeyboardInterrupt
                if char == "\x08":
                    if buffer:
                        buffer.pop()
                    render()
                    continue
                if char in {"\x00", "\xe0"}:
                    msvcrt.getwch()
                    continue
                if char.isprintable():
                    buffer.append(char)
                    render()
                    continue

            now = time.monotonic()
            current_state = _prompt_state(self.status)
            if current_state != last_prompt_state:
                last_prompt_state = current_state
                render()
            if current_state in LOADING_PROMPT_STATES and now - last_tick >= LOADING_PROMPT_INTERVAL_SECONDS:
                frame = (frame + 1) % len(LOADING_DOT_FRAMES)
                last_tick = now
                render()
            time.sleep(0.03)

    def rich_main_menu(self, setup_report: dict[str, Any]) -> Any:
        if not RICH_AVAILABLE:
            return format_main_menu(
                status=self.status,
                mode=self.mode,
                browser=self.browser,
                supabase=self.supabase,
                setup_report=setup_report,
                root_dir=self.root_dir,
                log_path=self.log_path,
                environment=self.environment,
                last_scan=self.last_scan,
                last_build=self.last_build,
                last_exit=self.last_helper_exit,
                stake_site=self.stake_site,
            )

        browser_label, browser_role = _browser_status(setup_report, self.browser)
        supabase_label, supabase_role = _supabase_status(setup_report, self.supabase)
        python_label, python_role = _python_status(setup_report)
        cleanup_label, cleanup_role = _cleanup_status(setup_report)

        text = Text()

        def line(value: str = "", style: str | None = None) -> None:
            text.append(value, style=style)
            text.append("\n")

        def status_line(label: str, detail: str, role: str) -> None:
            mark = "WARN" if role == "warn" else "OK" if role == "ok" else "FAIL"
            text.append("  ")
            text.append(f"[{mark}]", style=_rich_style(role))
            text.append(f" {label:<12}{detail}\n")

        state = _prompt_state(self.status)
        state_role = _prompt_state_role(state)
        text.append(APP_DISPLAY_NAME, style="bold")
        text.append(f" [{state}]", style=f"bold {_rich_style(state_role)}")
        if state in LOADING_PROMPT_STATES:
            text.append(" ...", style=f"bold {_rich_style(state_role)}")
        text.append(f" CLI v{CLI_VERSION}\n", style="bold")
        line(f"Environment: {self.environment}")
        line(f"Workspace: {self.root_dir}")
        line()
        line(HEAVY_RULE, f"bold {_rich_style('accent')}")
        line()
        line("System:", "bold")
        status_line("Browser", browser_label, browser_role)
        status_line("Supabase", supabase_label, supabase_role)
        status_line("Python", python_label, python_role)
        status_line("Cleanup", cleanup_label, cleanup_role)
        line()
        line(HEAVY_RULE, f"bold {_rich_style('accent')}")
        line()
        line("Status:", "bold")
        line(f"  State: {_display_state(self.status)}")
        line(f"  Mode: {self.mode}")
        line(
            f"  Stake site: "
            f"{stake_site_profile(self.stake_site, root_dir=self.root_dir)['label']}"
        )
        line(f"  Last scan: {self.last_scan}")
        line(f"  Last build: {self.last_build}")
        line()
        line(HEAVY_RULE, f"bold {_rich_style('accent')}")
        line()
        line("Commands:", "bold")
        for command, description in COMMAND_ROWS:
            text.append(f"  {command:<14}", style=_rich_style("accent"))
            text.append(f"{description}\n")
        line()
        line(HEAVY_RULE, f"bold {_rich_style('accent')}")
        return text

    def handle_command(self, command: str) -> None:
        parts = split_cli_command(command)
        base = parts[0].lower() if parts else ""
        args = {part.lower() for part in parts[1:]}
        if base in {"1", "review", "r"}:
            self._set_site_from_command_args(args)
            self.start_helper("review")
        elif base in {"2", "build", "b"}:
            self._set_site_from_command_args(args)
            self.start_helper("build")
        elif base in {"status", "s"}:
            self.run_status()
        elif base == "q":
            self.toggle_stake_site()
        elif base in {"domain", "site"}:
            self.run_stake_site_command(args)
        elif base in {"history", "historic", "hist", "import", "i"}:
            self.run_bet_history(parts[1:])
        elif base in {"analysis", "analyze", "backtest", "bt", "z"}:
            self.run_backtest(parts[1:])
        elif base in {"logs", "l"}:
            self.run_logs(args)
        elif base in {"doctor", "d"}:
            self.run_doctor()
        elif base in {"3", "setup"}:
            self.run_setup_check()
        elif base in {"4", "clean", "cache", "c"}:
            if "--yes" in args or "-y" in args:
                self.run_cache_cleanup(assume_yes=True)
            else:
                self.run_cache_cleanup()
        elif base in {"5", "stop"}:
            self.stop_helper()
        elif base in {"color", "theme", "t"}:
            self.run_color_menu()
        elif base in {"help", "h", "?"}:
            self.emit(format_help_screen(use_color=True) + "\n", role="info")
        elif command:
            self.emit(f"Unknown command: {command}\nType help to see available commands.\n", role="warn")

    def _set_site_from_command_args(self, args: set[str]) -> None:
        for arg in args:
            if arg in {"com", "stake.com", "bet", "stake.bet"}:
                self.set_stake_site(arg, announce=False)
                return

    def set_stake_site(self, value: str, *, announce: bool = True) -> None:
        clean_site = clean_stake_site(value, fallback=self.stake_site)
        if clean_site == self.stake_site:
            if announce:
                profile = stake_site_profile(self.stake_site, root_dir=self.root_dir)
                self.emit(f"Stake site already set to {profile['label']}.\n", role="info")
            return
        if self.process and self.process.poll() is None:
            self.emit("Stopping helper before switching Stake site...\n", role="warn")
            self.stop_helper()
        self.stake_site = clean_site
        profile = stake_site_profile(self.stake_site, root_dir=self.root_dir)
        if announce:
            self.emit(
                f"Stake site set to {profile['label']} "
                f"({profile['cdpUrl']}, {profile['profileDir']}).\n",
                role="ok",
            )

    def toggle_stake_site(self) -> None:
        self.set_stake_site("bet" if self.stake_site == "com" else "com")

    def run_stake_site_command(self, args: set[str]) -> None:
        if not args:
            self.toggle_stake_site()
            return
        target = next(iter(args))
        if target not in {"com", "stake.com", "bet", "stake.bet"}:
            self.emit("Use domain com or domain bet.\n", role="warn")
            return
        self.set_stake_site(target)

    def emit(self, text: str, *, role: str = "accent", log: bool = True) -> None:
        if self.rich_console is not None:
            self.rich_console.print(self.rich_output(text, role))
        else:
            self.output(colorize(text, SEMANTIC_COLORS.get(role, self.text_color)))
        if log:
            self.write_log(strip_ansi(text))

    def rich_output(self, text: str, role: str) -> Any:
        clean = strip_ansi(text).rstrip("\n")
        style = _rich_style(role)
        return Text(clean, style=style) if RICH_AVAILABLE else colorize(clean, style)

    def _initialize_log(self) -> None:
        self.write_log(
            f"\n[{self._timestamp()}] {APP_DISPLAY_NAME} CLI v{CLI_VERSION} session started.\n"
        )

    def write_log(self, text: str) -> None:
        try:
            if not self.root_dir.exists():
                return
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            return

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def start_helper(self, mode: str) -> None:
        if self.process and self.process.poll() is None:
            self.emit("Helper is already running.\n", role="warn")
            return

        self.emit("[1/3] Running setup checks...\n", role="info")
        report = check_local_helper_setup(self.root_dir)
        if not report["ok"]:
            self.emit(format_setup_screen(report, use_color=True) + "\n", role="fail")
            self.status = "setup needs attention"
            return

        python_exe = self.root_dir / ".venv" / "Scripts" / "python.exe"
        self.status = "building" if mode == "build" else "reviewing"
        self.mode = "build-enabled" if mode == "build" else "review-safe"
        if mode == "build":
            self.last_build = f"started {datetime.now().strftime('%H:%M:%S')}"
        else:
            self.last_scan = f"started {datetime.now().strftime('%H:%M:%S')}"
        self.emit("[2/3] Launching local helper...\n", role="info")
        stake_profile = stake_site_profile(self.stake_site, root_dir=self.root_dir)
        helper_env = os.environ.copy()
        helper_env.update(
            {
                "AZP_STAKE_SITE": stake_profile["site"],
                "AZP_STAKE_BASE_URL": stake_profile["baseUrl"],
                "AZP_STAKE_START_URL": stake_profile["baseUrl"],
                "AZP_STAKE_CHROME_PROFILE": str(stake_profile["profileDir"]),
            }
        )
        self.process = subprocess.Popen(
            [
                str(python_exe),
                "-m",
                "app.local_stake_helper",
                "--mode",
                mode,
                "--cdp-url",
                stake_profile["cdpUrl"],
                "--stake-base-url",
                stake_profile["baseUrl"],
                "--stake-start-url",
                stake_profile["baseUrl"],
                "--chrome-profile",
                str(stake_profile["profileDir"]),
            ],
            cwd=self.root_dir,
            env=helper_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self.capture_output, daemon=True).start()
        self.emit("[3/3] Helper running. Watching for Stake UI jobs.\n", role="ok")

    def run_setup_check(self) -> None:
        report = check_local_helper_setup(self.root_dir)
        self.emit(format_setup_screen(report, use_color=True) + "\n", role="info")
        self.status = "setup ready" if report.get("ok") else "setup needs attention"

    def run_status(self) -> None:
        report = check_local_helper_setup(self.root_dir)
        self.emit(
            format_status_screen(
                status=self.status,
                mode=self.mode,
                browser=self.browser,
                supabase=self.supabase,
                setup_report=report,
                root_dir=self.root_dir,
                log_path=self.log_path,
                environment=self.environment,
                last_scan=self.last_scan,
                last_build=self.last_build,
                last_exit=self.last_helper_exit,
                stake_site=self.stake_site,
                use_color=True,
            )
            + "\n",
            role="info",
        )

    def run_doctor(self) -> None:
        report = check_local_helper_setup(self.root_dir)
        doctor = build_doctor_report(
            root_dir=self.root_dir,
            setup_report=report,
            log_path=self.log_path,
        )
        self.emit(format_doctor_screen(doctor, use_color=True) + "\n", role="info")
        self.status = "ready" if doctor.get("ok") else "setup needs attention"

    def run_logs(self, args: set[str]) -> None:
        if "--errors" in args:
            lines = self._read_error_log_lines(limit=80)
            self.emit(format_log_screen(self.log_path, lines, heading="Logs: warnings/errors") + "\n", role="warn", log=False)
            return
        limit = 120 if "--tail" in args else 40
        lines = self._read_log_lines(limit=limit)
        self.emit(format_log_screen(self.log_path, lines, heading="Logs: latest") + "\n", role="info", log=False)

    def run_bet_history(self, args: list[str]) -> None:
        python_exe = self.root_dir / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            self.emit(f"Could not find {python_exe}. Run setup first.\n", role="fail")
            self.status = "setup needs attention"
            return

        clean_args = [arg for arg in args if arg]
        if not clean_args:
            history_args = ["sync"]
            self.status = "historic sync"
        else:
            subcommand = clean_args[0].lower()
            if subcommand == "report":
                history_args = ["report", *clean_args[1:]]
                self.status = "historic report"
            elif subcommand == "sync":
                history_args = ["sync", *clean_args[1:]]
                self.status = "historic sync"
            elif subcommand == "review":
                history_args = ["review", *clean_args[1:]]
                self.status = "historic review"
            elif subcommand == "enrich":
                history_args = ["enrich", *clean_args[1:]]
                self.status = "historic enrich"
            elif subcommand in {"analysis", "backtest"}:
                history_args = ["analysis", *clean_args[1:]]
                self.status = "historic analysis"
            elif subcommand == "imports":
                history_args = ["imports", *clean_args[1:]]
                self.status = "historic imports"
            elif subcommand == "delete-import":
                history_args = ["delete-import", *clean_args[1:]]
                self.status = "historic delete"
            elif subcommand == "import":
                history_args = ["import", *clean_args[1:]]
                self.status = "importing historic"
            else:
                history_args = ["import", *clean_args]
                self.status = "importing historic"

        self.emit("Running bet historic command...\n", role="info")
        completed = subprocess.run(
            [str(python_exe), "-m", "app.bet_history", *history_args],
            cwd=self.root_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output = (completed.stdout or "").rstrip()
        if output:
            self.emit(output + "\n", role="info" if completed.returncode == 0 else "fail")
        self.emit(
            f"Bet historic exited with code {completed.returncode}.\n",
            role="ok" if completed.returncode == 0 else "fail",
        )
        self.status = "ready" if completed.returncode == 0 else "historic import failed"

    def run_backtest(self, args: list[str]) -> None:
        python_exe = self.root_dir / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            self.emit(f"Could not find {python_exe}. Run setup first.\n", role="fail")
            self.status = "setup needs attention"
            return

        clean_args = [arg for arg in args if arg]
        self.status = "analyzing"
        self.emit("Running historic analysis command...\n", role="info")
        completed = subprocess.run(
            [str(python_exe), "-m", "app.bet_history", "analysis", *clean_args],
            cwd=self.root_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output = (completed.stdout or "").rstrip()
        if output:
            self.emit(output + "\n", role="info" if completed.returncode == 0 else "fail")
        self.emit(
            f"Historic analysis exited with code {completed.returncode}.\n",
            role="ok" if completed.returncode == 0 else "fail",
        )
        self.status = "ready" if completed.returncode == 0 else "historic analysis failed"

    def run_cache_cleanup(self, *, assume_yes: bool = False) -> None:
        python_exe = self.root_dir / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            self.emit(f"Could not find {python_exe}. Run setup first.\n", role="fail")
            self.status = "setup needs attention"
            return
        if not (self.root_dir / ".env").exists():
            self.emit(f"Could not find {self.root_dir / '.env'}.\n", role="fail")
            self.status = "setup needs attention"
            return

        if not assume_yes:
            self.emit(
                "This will delete old Supabase local-helper job rows and rebuildable local cache files.\n"
                "It keeps GPT decision ledger rows, Chrome login/session data, logs, and never places/cancels bets.\n",
                role="warn",
            )
            confirm = self.input(colorize("Continue? [y/N]: ", SEMANTIC_COLORS["warn"])).strip().lower()
            self.write_log(f"clean confirmation: {confirm or '[empty]'}\n")
            if confirm not in {"y", "yes"}:
                self.status = "ready"
                self.emit("Cleanup cancelled.\n", role="info")
                return

        self.status = "cleaning cache"
        self.emit("Running Supabase and local cache cleanup...\n", role="info")
        completed = subprocess.run(
            [str(python_exe), "-m", "app.supabase_cache", "--root-dir", str(self.root_dir)],
            cwd=self.root_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if completed.stdout:
            role = "warn" if "warn" in completed.stdout.lower() else "info"
            self.emit(completed.stdout.rstrip() + "\n", role=role)
        self.emit(
            f"Cleanup exited with code {completed.returncode}.\n",
            role="ok" if completed.returncode == 0 else "fail",
        )
        self.status = "ready" if completed.returncode == 0 else "cleanup failed"

    def stop_helper(self) -> None:
        if not self.process or self.process.poll() is not None:
            self.status = "ready"
            return

        self.emit("Stopping helper...\n", role="info")
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.status = "ready"
        self.emit("Helper stopped.\n", role="ok")

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

    def _read_log_lines(self, *, limit: int = 40) -> list[str]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return lines[-max(limit, 1):]

    def _read_error_log_lines(self, *, limit: int = 80) -> list[str]:
        matched = [
            line
            for line in self._read_log_lines(limit=1000)
            if any(pattern in line.lower() for pattern in ERROR_LOG_PATTERNS)
        ]
        return matched[-max(limit, 1):]

    def capture_output(self) -> None:
        if not self.process or not self.process.stdout:
            return
        for line in self.process.stdout:
            self.output_queue.put(line)
        code = self.process.poll()
        self.last_helper_exit = f"code {code} at {datetime.now().strftime('%H:%M:%S')}"
        self.status = "ready" if code == 0 else "helper error"
        self.output_queue.put(f"Helper exited with code {code}.\n")

    def drain_output(self) -> None:
        while True:
            try:
                text = self.output_queue.get_nowait()
            except queue.Empty:
                return
            role = "fail" if any(pattern in text.lower() for pattern in ("error", "failed", "traceback")) else "warn" if "warn" in text.lower() else "info"
            self.emit(text.rstrip("\n") + "\n", role=role)


def main() -> int:
    return StakeGptCli().run()


if __name__ == "__main__":
    raise SystemExit(main())
