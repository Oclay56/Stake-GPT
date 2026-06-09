from __future__ import annotations

import subprocess

from app.local_helper_tui import (
    DEFAULT_TUI_PALETTE,
    ENABLE_EXTENDED_FLAGS,
    ENABLE_MOUSE_INPUT,
    ENABLE_QUICK_EDIT_MODE,
    ENABLE_VIRTUAL_TERMINAL_INPUT,
    ENABLE_WINDOW_INPUT,
    MENU_ROW_COUNT,
    MENU_ROW_WIDTH,
    OUTPUT_VISIBLE_HEIGHT,
    build_backtest_console_command,
    build_tui_actions,
    clean_tui_palette,
    console_input_mode_without_text_selection,
    find_tui_action,
    format_historic_tui_summary,
    format_historic_update_tui_summary,
    format_running_status,
    format_tui_action_row,
    rich_stake_site_row,
    rich_title_row,
    rich_tui_action_row,
    launch_backtest_console,
    textual_dependency_status,
)


def test_tui_actions_cover_existing_helper_shortcuts():
    actions = build_tui_actions()
    shortcuts = {action.shortcut: action.command for action in actions}

    assert shortcuts == {
        "ctrl+r": "review",
        "ctrl+b": "build",
        "ctrl+t": "backtest",
        "ctrl+i": "historic",
        "ctrl+l": "logs",
        "ctrl+d": "doctor",
        "ctrl+c": "clean",
        "ctrl+q": "domain",
        "ctrl+s": "stop",
        "ctrl+p": "palette",
        "ctrl+e": "exit",
    }


def test_tui_clean_action_runs_inline_without_confirmation():
    clean = find_tui_action("clean")

    assert clean is not None
    assert clean.confirm is False


def test_tui_action_lookup_accepts_action_id_and_command():
    assert find_tui_action("backtest").shortcut == "ctrl+t"
    assert find_tui_action("domain").shortcut == "ctrl+q"
    assert find_tui_action("missing") is None


def test_tui_action_rows_use_single_bracket_pair_and_include_shortcuts():
    rows = [format_tui_action_row(action) for action in build_tui_actions()]

    assert len(rows) == MENU_ROW_COUNT
    assert all(row.count("[") == 1 and row.count("]") == 1 for row in rows)
    assert all(row.startswith("[ ") and row.endswith(" ]") for row in rows)
    assert all(len(row) == MENU_ROW_WIDTH for row in rows)
    assert "Build" in rows[1]
    assert "Build slip" not in rows[1]
    assert "ctrl+b" in rows[1]
    assert "Analysis" in rows[2]
    assert "Backtest" not in rows[2]
    assert "ctrl+t" in rows[2]
    assert "Historic" in rows[3]
    assert "History" not in rows[3]
    assert "Palette" in rows[-2]
    assert "ctrl+p" in rows[-2]


def test_tui_does_not_expose_classic_cli_action():
    assert find_tui_action("classic") is None


def test_tui_running_status_keeps_dots_after_bracketed_text():
    action = find_tui_action("build")

    assert action is not None
    assert format_running_status(action, frame=2) == "[ Building ]..."


def test_tui_palette_keeps_grok_style_defaults_and_allows_hex_overrides():
    assert DEFAULT_TUI_PALETTE["background"] == "#111111"
    assert DEFAULT_TUI_PALETTE["panelBorder"] == "#5A5A5A"
    assert DEFAULT_TUI_PALETTE["shellBorder"] == "#6A6A6A"
    assert DEFAULT_TUI_PALETTE["mutedText"] == "#7F7F7F"
    assert DEFAULT_TUI_PALETTE["highlightText"] == "#B8B19C"
    assert DEFAULT_TUI_PALETTE["titleText"] == "#F1EED0"
    assert DEFAULT_TUI_PALETTE["accentText"] == "#A46214"
    assert DEFAULT_TUI_PALETTE["readyText"] == "#00E701"
    assert DEFAULT_TUI_PALETTE["activeText"] == "#74B9FF"
    assert DEFAULT_TUI_PALETTE["rowText"] == "#B8B19C"
    assert DEFAULT_TUI_PALETTE["shortcutText"] == "#7F7F7F"

    cleaned = clean_tui_palette({"background": "#111111", "mutedText": "bad"})

    assert cleaned["background"] == "#111111"
    assert cleaned["mutedText"] == "#7F7F7F"


def test_tui_rich_row_styles_label_and_shortcut_separately():
    action = find_tui_action("review")

    assert action is not None
    row = rich_tui_action_row(action)

    assert row.plain == format_tui_action_row(action)
    assert "Review" in row.plain
    assert "Review slip" not in row.plain
    assert "ctrl+r" in row.plain
    assert any("bold" in str(span.style) for span in row.spans)


def test_tui_title_row_places_system_status_on_same_line():
    row = rich_title_row("ready")

    assert "Stake-GPT [v1.5.0]" in row.plain
    assert "Stake-GPT v1.5.0" not in row.plain
    assert "Stake-GPT CLI v1.5.0" not in row.plain
    assert row.plain.rstrip().endswith("System: [ready]")
    assert row.spans[0].style == DEFAULT_TUI_PALETTE["mutedText"]
    assert row.spans[1].style == DEFAULT_TUI_PALETTE["titleText"]


def test_tui_title_row_animates_active_status_outside_brackets():
    row = rich_title_row("building", frame=2)

    assert row.plain.rstrip().endswith("System: [building] ...")


def test_tui_stake_site_row_keeps_label_muted_and_domain_accented():
    row = rich_stake_site_row("stake.com")

    assert row.plain.rstrip().endswith("Stake site: [stake.com]")
    assert row.style == DEFAULT_TUI_PALETTE["mutedText"]
    assert any(span.style == DEFAULT_TUI_PALETTE["accentText"] for span in row.spans)


def test_tui_historic_summary_shows_more_than_five_import_files():
    report = {
        "filesConsidered": 7,
        "filesImported": 0,
        "filesSkippedDuplicate": 7,
        "refreshedLegs": 0,
        "filesFailed": 0,
        "history": {
            "parsedLegs": 116,
            "trainingEligible": 116,
            "needsReview": 0,
            "importFiles": [f"p{index}.txt" for index in range(1, 8)],
        },
    }

    lines = format_historic_tui_summary(report)
    text = "\n".join(lines)

    assert len(lines) <= OUTPUT_VISIBLE_HEIGHT
    assert "Import files (7)" in text
    assert "p1.txt" not in text
    assert "p6.txt" not in text
    assert "p7.txt" not in text


def test_tui_historic_update_summary_shows_pipeline_without_file_names():
    report = {
        "sync": {
            "filesConsidered": 8,
            "filesImported": 1,
            "filesSkippedDuplicate": 7,
            "refreshedLegs": 0,
            "filesFailed": 0,
            "history": {
                "parsedLegs": 241,
                "trainingEligible": 241,
                "importFiles": [f"p{index}.txt" for index in range(1, 9)],
            },
        },
        "enrich": {"targets": 100, "legsEnriched": 90},
        "analysis": {
            "enrichment": {"coverageRate": 0.75},
            "finalOutcome": {
                "ticketSample": {
                    "tickets": 25,
                    "gradedTickets": 24,
                    "roi": -0.12,
                }
            },
        },
    }

    lines = format_historic_update_tui_summary(report)
    text = "\n".join(lines)

    assert "Enriched: 90 | Targets: 100 | Coverage: 75.0%" in text
    assert "Tickets: 24/25 | ROI: -12.0%" in text
    assert "Import files (8)" in text
    assert "p1.txt" not in text
    assert "p8.txt" not in text


def test_tui_dependency_status_is_explicit():
    status = textual_dependency_status()

    assert set(status) == {"available", "error"}
    assert isinstance(status["available"], bool)
    assert isinstance(status["error"], str)


def test_console_input_mode_disables_text_selection_but_keeps_mouse_input():
    mode = ENABLE_QUICK_EDIT_MODE

    updated = console_input_mode_without_text_selection(mode)

    assert not updated & ENABLE_QUICK_EDIT_MODE
    assert updated & ENABLE_EXTENDED_FLAGS
    assert updated & ENABLE_MOUSE_INPUT
    assert updated & ENABLE_WINDOW_INPUT
    assert updated & ENABLE_VIRTUAL_TERMINAL_INPUT


def test_backtest_console_command_runs_backtest_in_powershell(tmp_path):
    command = build_backtest_console_command(tmp_path)
    script = command[-1]

    assert command[:6] == [
        "powershell.exe",
        "-NoLogo",
        "-NoExit",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
    ]
    assert "$Host.UI.RawUI.BackgroundColor = 'Black'" in script
    assert "$Host.UI.RawUI.ForegroundColor = 'Gray'" in script
    assert "$env:STAKE_GPT_BACKTEST_RICH_WIDTH = '170'" in script
    assert "$raw.WindowSize" in script
    assert "Clear-Host" in script
    assert "Stake-GPT Historic Analysis" in script
    assert f"Set-Location -LiteralPath '{tmp_path}'" in script
    assert "-m app.bet_history analysis" in script
    assert "--rich" in script
    assert "Historic analysis complete. Close this window when finished." in script


def test_launch_backtest_console_uses_new_console(monkeypatch, tmp_path):
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr("app.local_helper_tui.subprocess.Popen", FakePopen)

    launch_backtest_console(tmp_path)

    args, kwargs = calls[0]
    assert args == build_backtest_console_command(tmp_path)
    assert kwargs["cwd"] == tmp_path
    if hasattr(subprocess, "CREATE_NEW_CONSOLE"):
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_CONSOLE
