from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .local_ai_operator import DEFAULT_OLLAMA_MODEL, local_ai_model

ROOT_DIR = Path(__file__).resolve().parents[1]


def _powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def ollama_executable() -> str:
    configured = os.getenv("STAKE_GPT_OLLAMA_EXE") or os.getenv("AZP_OLLAMA_EXE")
    if configured:
        return configured
    bundled = Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"
    if bundled.exists():
        return str(bundled)
    return "ollama.exe"


def build_ai_chat_console_command(
    root_dir: Path = ROOT_DIR,
    *,
    model: str | None = None,
) -> list[str]:
    clean_model = (model or local_ai_model()).strip() or DEFAULT_OLLAMA_MODEL
    command = (
        "$Host.UI.RawUI.WindowTitle = 'Stake-GPT AI'; "
        "$Host.UI.RawUI.BackgroundColor = 'Black'; "
        "$Host.UI.RawUI.ForegroundColor = 'Gray'; "
        "try { "
        "$raw = $Host.UI.RawUI; "
        "$max = $raw.MaxPhysicalWindowSize; "
        "$w = [Math]::Min(130, $max.Width); "
        "$h = [Math]::Min(36, $max.Height); "
        "$bufferHeight = [Math]::Max($raw.BufferSize.Height, 1000); "
        "$raw.BufferSize = New-Object System.Management.Automation.Host.Size -ArgumentList $w, $bufferHeight; "
        "$raw.WindowSize = New-Object System.Management.Automation.Host.Size -ArgumentList $w, $h; "
        "} catch {}; "
        "Clear-Host; "
        f"Set-Location -LiteralPath {_powershell_literal(root_dir)}; "
        "Write-Host 'Stake-GPT AI Chat' -ForegroundColor White; "
        f"Write-Host ('Model: ' + {_powershell_literal(clean_model)}) -ForegroundColor DarkGray; "
        "Write-Host 'Type /bye to exit Ollama chat.' -ForegroundColor DarkGray; "
        "Write-Host ''; "
        f"& {_powershell_literal(ollama_executable())} run {_powershell_literal(clean_model)}; "
        "Write-Host ''; "
        "Write-Host 'AI chat closed. You can close this window.' -ForegroundColor DarkGray"
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


def launch_ai_chat_console(
    root_dir: Path = ROOT_DIR,
    *,
    model: str | None = None,
) -> subprocess.Popen:
    kwargs: dict[str, Any] = {"cwd": root_dir}
    creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    if creation_flags:
        kwargs["creationflags"] = creation_flags
    return subprocess.Popen(build_ai_chat_console_command(root_dir, model=model), **kwargs)
