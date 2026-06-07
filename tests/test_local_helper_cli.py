from __future__ import annotations

from pathlib import Path

import pytest

from app.local_helper_cli import (
    DEFAULT_CLI_COLOR_SETTINGS,
    RICH_AVAILABLE,
    ROOT_DIR,
    SEMANTIC_COLORS,
    StakeGptCli,
    _colored_prompt_line,
    clean_cli_color_settings,
    clean_stake_site,
    cli_color_presets_dir,
    cli_color_settings_path,
    cli_log_path,
    color_preset_path,
    format_doctor_screen,
    format_main_menu,
    format_setup_screen,
    is_exit_command,
    list_cli_color_presets,
    load_cli_color_preset,
    load_cli_color_settings,
    normalize_color_choice,
    rgb_escape,
    safe_color_preset_name,
    save_cli_color_preset,
    save_cli_color_settings,
    stake_site_profile,
    strip_ansi,
)


def _passing_setup_report() -> dict:
    return {
        "ok": True,
        "checks": [
            {
                "name": "Python venv",
                "ok": True,
                "detail": str(ROOT_DIR / ".venv" / "Scripts" / "python.exe"),
            },
            {"name": ".env file", "ok": True, "detail": str(ROOT_DIR / ".env")},
            {"name": "SUPABASE_URL configured", "ok": True, "detail": "SUPABASE_URL"},
            {
                "name": "SUPABASE_SERVICE_ROLE_KEY configured",
                "ok": True,
                "detail": "SUPABASE_SERVICE_ROLE_KEY",
            },
            {
                "name": "Chrome executable",
                "ok": True,
                "detail": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            },
        ],
        "warnings": [],
    }


def test_cli_color_settings_path_uses_workflow_folder(tmp_path):
    path = cli_color_settings_path(root_dir=tmp_path)

    assert path == tmp_path / "data" / "workflow" / "helper-cli-color.json"


def test_cli_color_presets_dir_uses_workflow_folder(tmp_path):
    path = cli_color_presets_dir(root_dir=tmp_path)

    assert path == tmp_path / "data" / "workflow" / "color-presets"


def test_cli_log_path_uses_logs_folder(tmp_path):
    assert cli_log_path(root_dir=tmp_path) == tmp_path / "logs" / "latest.log"


def test_stake_site_profiles_use_separate_domains_ports_and_profiles(tmp_path):
    assert clean_stake_site("stake.bet") == "bet"
    assert clean_stake_site("https://stake.bet/sports/baseball/usa/mlb") == "bet"
    assert clean_stake_site("https://stake.com") == "com"

    com = stake_site_profile("com", root_dir=tmp_path)
    bet = stake_site_profile("bet", root_dir=tmp_path)

    assert com["baseUrl"] == "https://stake.com"
    assert com["cdpUrl"] == "http://127.0.0.1:9222"
    assert com["profileDir"] == tmp_path / "data" / "chrome-stake-ui"
    assert bet["baseUrl"] == "https://stake.bet"
    assert bet["cdpUrl"] == "http://127.0.0.1:9223"
    assert bet["profileDir"] == tmp_path / "data" / "chrome-stake-ui-bet"


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


def test_main_menu_uses_polished_status_and_commands():
    menu = format_main_menu(
        status="ready",
        mode="review-safe",
        browser="checking",
        supabase="checking",
        setup_report=_passing_setup_report(),
    )

    assert "Stake-GPT [ready] CLI v" in menu
    assert "Environment: local" in menu
    assert "Workspace:" in menu
    assert "━" * 10 in menu
    assert "System:" in menu
    assert "[OK] Browser      Chrome connected" in menu
    assert "[OK] Supabase     Connected" in menu
    assert "[OK] Python       .venv active" in menu
    assert "Status:" in menu
    assert "  State: Ready" in menu
    assert "  Stake site: stake.com" in menu
    assert "Commands:" in menu
    assert "review, r" in menu and "Scan board" in menu
    assert "build, b" in menu and "Build validated slip" in menu
    assert "status, s" in menu and "Show status" in menu
    assert "domain, q" in menu and "Toggle Stake site" in menu
    assert "logs, l" in menu and "View logs" in menu
    assert "doctor, d" in menu and "Run full system check" in menu
    assert "clean, c" in menu and "Clear cache" in menu
    assert "help, h" in menu and "Show help" in menu
    assert "exit, e" in menu and "Close app" in menu
    assert "[1]" not in menu
    assert "[0]" not in menu
    assert menu.rstrip().endswith("stake-gpt [ready] >")


def test_main_menu_shows_fixed_loading_tail_for_active_states():
    menu = format_main_menu(
        status="building",
        mode="build-enabled",
        browser="checking",
        supabase="checking",
        setup_report=_passing_setup_report(),
    )

    assert "Stake-GPT [building] ... CLI v" in menu
    assert "  State: Building" in menu
    assert menu.rstrip().endswith("stake-gpt [building] ... >")


def test_setup_screen_formats_actionable_warnings():
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
    assert "-" * 78 in screen
    assert "[OK] Python venv:        " in screen
    assert "Warnings:" in screen
    assert "[WARN] AZP_SUPABASE_AUTO_CLEANUP_MINUTES is not set." in screen
    assert "Using default: 60 minutes." in screen
    assert "AZP_SUPABASE_AUTO_CLEANUP_MINUTES=60" in screen
    assert screen.endswith("Ready.")


def test_doctor_screen_summarizes_health():
    screen = format_doctor_screen(_passing_setup_report())

    assert screen.startswith("Doctor")
    assert "System health: READY" in screen
    assert "System Check" in screen
    assert "Doctor checks local prerequisites and write access." in screen


def test_command_aliases_route_to_expected_actions(monkeypatch):
    cli = StakeGptCli(root_dir=Path("C:/fake/AZP"))
    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(cli, "start_helper", lambda mode: calls.append(("start", mode)))
    monkeypatch.setattr(cli, "run_status", lambda: calls.append(("status", None)))
    monkeypatch.setattr(cli, "run_logs", lambda args: calls.append(("logs", ",".join(sorted(args)))))
    monkeypatch.setattr(cli, "run_doctor", lambda: calls.append(("doctor", None)))
    monkeypatch.setattr(cli, "run_setup_check", lambda: calls.append(("setup", None)))
    monkeypatch.setattr(cli, "run_cache_cleanup", lambda assume_yes=False: calls.append(("clean", "yes" if assume_yes else "ask")))
    monkeypatch.setattr(cli, "run_color_menu", lambda: calls.append(("color", None)))
    monkeypatch.setattr(cli, "stop_helper", lambda: calls.append(("stop", None)))
    monkeypatch.setattr(cli, "emit", lambda *args, **kwargs: calls.append(("help", None)))
    monkeypatch.setattr(cli, "run_stake_site_command", lambda args: calls.append(("domain", ",".join(sorted(args)))))
    monkeypatch.setattr(cli, "toggle_stake_site", lambda: calls.append(("toggle", None)))

    for command in (
        "review",
        "r",
        "build",
        "b",
        "status",
        "s",
        "domain bet",
        "q",
        "logs",
        "L --errors",
        "doctor",
        "D",
        "setup",
        "clean",
        "C --yes",
        "H",
        "stop",
        "color",
    ):
        cli.handle_command(command)

    assert calls == [
        ("start", "review"),
        ("start", "review"),
        ("start", "build"),
        ("start", "build"),
        ("status", None),
        ("status", None),
        ("domain", "bet"),
        ("toggle", None),
        ("logs", ""),
        ("logs", "--errors"),
        ("doctor", None),
        ("doctor", None),
        ("setup", None),
        ("clean", "ask"),
        ("clean", "yes"),
        ("help", None),
        ("stop", None),
        ("color", None),
    ]


def test_clean_confirmation_can_cancel(tmp_path):
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text("SUPABASE_URL=x\n", encoding="utf-8")
    outputs: list[str] = []
    cli = StakeGptCli(
        root_dir=tmp_path,
        input_func=lambda prompt: "n",
        output_func=lambda text: outputs.append(text),
    )

    cli.run_cache_cleanup()

    assert "Cleanup cancelled." in "".join(outputs)
    assert cli.status == "ready"


def test_logs_command_reads_latest_and_error_lines(tmp_path):
    outputs: list[str] = []
    cli = StakeGptCli(root_dir=tmp_path, output_func=lambda text: outputs.append(text))
    cli.write_log("[OK] one\n")
    cli.write_log("[WARN] two\n")
    cli.write_log("Helper error: three\n")

    cli.run_logs(set())
    cli.run_logs({"--errors"})

    joined = "".join(outputs)
    assert "Logs: latest" in joined
    assert "Logs: warnings/errors" in joined
    assert "[WARN] two" in joined
    assert "Helper error: three" in joined


def test_rich_prompt_keeps_status_spacing_and_simple_dashboard(tmp_path):
    cli = StakeGptCli(root_dir=tmp_path)

    prompt = cli.rich_prompt()
    prompt_text = prompt.plain if hasattr(prompt, "plain") else str(prompt)
    assert prompt_text == "stake-gpt [ready] > "
    if RICH_AVAILABLE:
        rendered = cli.rich_main_menu(_passing_setup_report())
        assert type(rendered).__name__ == "Text"
        assert "Stake-GPT [ready] CLI v" in rendered.plain


def test_rich_prompt_adds_non_jitter_loading_tail_for_active_state(tmp_path):
    cli = StakeGptCli(root_dir=tmp_path)
    cli.status = "building"

    prompt = cli.rich_prompt()
    prompt_text = prompt.plain if hasattr(prompt, "plain") else str(prompt)
    assert prompt_text == "stake-gpt [building] ... > "
    frames = []
    for frame in range(3):
        framed_prompt = cli.rich_prompt(frame=frame)
        frames.append(framed_prompt.plain if hasattr(framed_prompt, "plain") else str(framed_prompt))
    assert frames == [
        "stake-gpt [building] .   > ",
        "stake-gpt [building] ..  > ",
        "stake-gpt [building] ... > ",
    ]
    assert len({len(frame) for frame in frames}) == 1
    if RICH_AVAILABLE:
        rendered = cli.rich_main_menu(_passing_setup_report())
        assert "Stake-GPT [building] ... CLI v" in rendered.plain


def test_animated_prompt_colors_active_state_blue_without_moving_marker():
    frames = [
        _colored_prompt_line("building", trailing_space=True, frame=frame)
        for frame in range(3)
    ]

    assert rgb_escape(SEMANTIC_COLORS["info"]) in frames[0]
    assert strip_ansi(frames[0]) == "stake-gpt [building] .   > "
    assert strip_ansi(frames[1]) == "stake-gpt [building] ..  > "
    assert strip_ansi(frames[2]) == "stake-gpt [building] ... > "
    assert len({len(strip_ansi(frame)) for frame in frames}) == 1


def test_stop_helper_returns_prompt_to_ready(tmp_path):
    cli = StakeGptCli(root_dir=tmp_path)
    cli.status = "building"

    cli.stop_helper()

    assert cli.status == "ready"


def test_start_helper_uses_selected_stake_bet_profile(monkeypatch, tmp_path):
    seen: dict[str, object] = {}

    class FakeProcess:
        stdout = None

        def poll(self):
            return None

    monkeypatch.setattr(
        "app.local_helper_cli.check_local_helper_setup",
        lambda root_dir: {"ok": True, "checks": [], "warnings": []},
    )

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen["env"] = kwargs.get("env")
        seen["cwd"] = kwargs.get("cwd")
        return FakeProcess()

    monkeypatch.setattr("app.local_helper_cli.subprocess.Popen", fake_popen)
    cli = StakeGptCli(root_dir=tmp_path, output_func=lambda text: None)
    cli.set_stake_site("bet", announce=False)

    cli.start_helper("build")

    args = list(seen["args"])
    env = seen["env"]
    assert "--cdp-url" in args
    assert args[args.index("--cdp-url") + 1] == "http://127.0.0.1:9223"
    assert args[args.index("--stake-base-url") + 1] == "https://stake.bet"
    assert args[args.index("--chrome-profile") + 1] == str(
        tmp_path / "data" / "chrome-stake-ui-bet"
    )
    assert env["AZP_STAKE_BASE_URL"] == "https://stake.bet"
    assert env["AZP_STAKE_CHROME_PROFILE"] == str(
        tmp_path / "data" / "chrome-stake-ui-bet"
    )
    assert seen["cwd"] == tmp_path


def test_q_toggles_stake_site_back_and_forth(tmp_path):
    outputs: list[str] = []
    cli = StakeGptCli(root_dir=tmp_path, output_func=outputs.append)

    assert cli.stake_site == "com"

    cli.handle_command("q")
    assert cli.stake_site == "bet"
    assert any("Stake site set to stake.bet" in output for output in outputs)

    cli.handle_command("Q")
    assert cli.stake_site == "com"
    assert any("Stake site set to stake.com" in output for output in outputs)


def test_exit_command_accepts_e_alias():
    assert is_exit_command("6")
    assert is_exit_command("e")
    assert is_exit_command("exit")
    assert not is_exit_command("q")
    assert not is_exit_command("5")


def test_cli_run_starts_directly_on_main_screen(monkeypatch):
    outputs: list[str] = []

    monkeypatch.setattr(
        "app.local_helper_cli.check_local_helper_setup",
        lambda root_dir: {"ok": True, "checks": [], "warnings": []},
    )
    cli = StakeGptCli(
        root_dir=Path("C:/fake/AZP"),
        input_func=lambda prompt: outputs.append(prompt) or "e",
        output_func=lambda text: outputs.append(text),
    )

    assert cli.run() == 0
    assert "Stake-GPT CLI ready." not in "".join(outputs)
    assert "Stake-GPT [ready] CLI v" in outputs[0]
    assert "stake-gpt [ready] >" in outputs[0]


def test_cli_color_menu_has_back_option():
    cli = StakeGptCli(root_dir=Path("C:/fake/AZP"))

    screen = cli.format_color_menu()

    assert "Text Color" in screen
    assert "[0] Back" in screen
    assert "Custom RGB" in screen
