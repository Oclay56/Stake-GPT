from __future__ import annotations

from pathlib import Path

import pytest

from app.local_helper_cli import (
    DEFAULT_CLI_COLOR_SETTINGS,
    ROOT_DIR,
    StakeGptCli,
    clean_cli_color_settings,
    cli_color_presets_dir,
    cli_color_settings_path,
    color_preset_path,
    format_main_menu,
    format_setup_screen,
    list_cli_color_presets,
    load_cli_color_preset,
    load_cli_color_settings,
    normalize_color_choice,
    safe_color_preset_name,
    save_cli_color_preset,
    save_cli_color_settings,
)


def test_cli_color_settings_path_uses_workflow_folder(tmp_path):
    path = cli_color_settings_path(root_dir=tmp_path)

    assert path == tmp_path / "data" / "workflow" / "helper-cli-color.json"


def test_cli_color_presets_dir_uses_workflow_folder(tmp_path):
    path = cli_color_presets_dir(root_dir=tmp_path)

    assert path == tmp_path / "data" / "workflow" / "color-presets"


def test_normalize_color_choice_accepts_rgb_tuple_and_hex():
    assert normalize_color_choice((18, 52, 86), fallback="#000000") == "#123456"
    assert normalize_color_choice("#ABCDEF", fallback="#000000") == "#ABCDEF"


def test_normalize_color_choice_rejects_invalid_values():
    assert normalize_color_choice(None, fallback="#111111") == "#111111"
    assert normalize_color_choice((18, 52), fallback="#111111") == "#111111"
    assert normalize_color_choice("not-a-color", fallback="#111111") == "#111111"


def test_clean_cli_color_settings_only_keeps_text_color():
    cleaned = clean_cli_color_settings(
        {
            "textColor": "#00E701",
            "backgroundColor": "#FFFFFF",
            "outlineColor": "#FF0000",
        }
    )

    assert cleaned == {"textColor": "#00E701"}


def test_save_and_load_cli_color_settings(tmp_path):
    settings_path = tmp_path / "settings.json"

    save_cli_color_settings({"textColor": "#00E701"}, settings_path)

    assert load_cli_color_settings(settings_path) == {"textColor": "#00E701"}


def test_load_cli_color_settings_returns_default_when_missing(tmp_path):
    assert load_cli_color_settings(tmp_path / "missing.json") == DEFAULT_CLI_COLOR_SETTINGS


def test_save_and_load_cli_color_preset(tmp_path):
    preset_dir = tmp_path / "color-presets"

    path = save_cli_color_preset("Stake Green", {"textColor": "#00E701"}, preset_dir)

    assert path == preset_dir / "Stake Green.json"
    assert load_cli_color_preset("Stake Green", preset_dir) == {"textColor": "#00E701"}


def test_cli_color_preset_name_replaces_invalid_filename_characters(tmp_path):
    path = color_preset_path('Red/Blue: "Night"', directory=tmp_path)

    assert path.name == "Red-Blue- -Night-.json"


def test_cli_color_preset_name_rejects_empty_name():
    with pytest.raises(ValueError):
        safe_color_preset_name("   ")


def test_cli_color_presets_are_listed_without_json_extension(tmp_path):
    save_cli_color_preset("Green", {"textColor": "#00E701"}, tmp_path)
    save_cli_color_preset("White", {"textColor": "#F4F6F8"}, tmp_path)

    assert list_cli_color_presets(tmp_path) == ["Green", "White"]


def test_main_menu_uses_numbered_commands_and_status_lines():
    menu = format_main_menu(
        status="idle",
        mode="review-safe",
        browser="ready",
        supabase="connected",
    )

    assert "Stake-GPT" in menu
    assert "Status: idle" in menu
    assert "[1] Review Mode" in menu
    assert "[2] Build Slip" in menu
    assert "[3] Setup Check" in menu
    assert "[4] Clean Cache" in menu
    assert "[5] Color" in menu
    assert "[6] Stop" in menu
    assert "[0] Exit" in menu
    assert menu.rstrip().endswith("stake-gpt >")


def test_setup_screen_formats_command_style_check():
    screen = format_setup_screen(
        {
            "ok": True,
            "checks": [
                {
                    "name": "Python venv",
                    "ok": True,
                    "detail": str(ROOT_DIR / ".venv" / "Scripts" / "python.exe"),
                },
                {"name": ".env file", "ok": True, "detail": str(ROOT_DIR / ".env")},
            ],
            "warnings": ["AZP_SUPABASE_AUTO_CLEANUP_MINUTES is not set; defaulting to 60."],
        }
    )

    assert screen.startswith("System Check")
    assert "[OK] Python venv:" in screen
    assert "Warnings:" in screen
    assert "[!] AZP_SUPABASE_AUTO_CLEANUP_MINUTES is not set; defaulting to 60." in screen
    assert screen.endswith("Ready.")


def test_cli_color_menu_has_back_option():
    cli = StakeGptCli(root_dir=Path("C:/fake/AZP"))

    screen = cli.format_color_menu()

    assert "Text Color" in screen
    assert "[0] Back" in screen
    assert "Custom RGB" in screen
