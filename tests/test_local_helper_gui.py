from __future__ import annotations

from app.local_helper_gui import (
    HELPER_BG,
    HELPER_FG,
    HELPER_BUTTON_BG,
    active_color_for,
    apply_background_color,
    apply_outline_color,
    helper_color_settings_path,
    load_helper_color_settings,
    normalize_color_choice,
    save_helper_color_settings,
    should_minimize_to_tray,
)


def test_should_minimize_to_tray_only_for_iconic_windows_with_tray_support():
    assert should_minimize_to_tray("iconic", tray_supported=True)
    assert not should_minimize_to_tray("normal", tray_supported=True)
    assert not should_minimize_to_tray("withdrawn", tray_supported=True)
    assert not should_minimize_to_tray("iconic", tray_supported=False)


def test_helper_gui_uses_dark_navy_theme():
    assert HELPER_BG == "#03041D"
    assert HELPER_FG == "#F4F0FF"


def test_normalize_color_choice_accepts_rgb_tuple():
    assert normalize_color_choice((18, 52, 86), fallback="#000000") == "#123456"


def test_normalize_color_choice_falls_back_for_invalid_values():
    assert normalize_color_choice(None, fallback="#11143A") == "#11143A"
    assert normalize_color_choice((18, 52), fallback="#11143A") == "#11143A"


def test_active_color_for_lightens_dark_accent():
    assert active_color_for("#11143A") == "#2A2D53"


class FakeWidget:
    def __init__(self) -> None:
        self.options = {}

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)


def test_apply_outline_color_updates_buttons_and_log_outline_only():
    button = FakeWidget()
    log = FakeWidget()

    result = apply_outline_color([button], log, "#0E3D32")

    assert result == {
        "accent": "#0E3D32",
        "activeAccent": "#27564B",
    }
    assert button.options == {
        "bg": "#0E3D32",
        "activebackground": "#27564B",
    }
    assert log.options == {
        "highlightbackground": "#0E3D32",
        "highlightcolor": "#27564B",
        "selectbackground": "#27564B",
    }


def test_apply_background_color_updates_background_widgets_and_log_panel():
    root = FakeWidget()
    label = FakeWidget()
    log = FakeWidget()

    result = apply_background_color([root, label], log, "#102030")

    assert result == "#102030"
    assert root.options == {"bg": "#102030"}
    assert label.options == {"bg": "#102030"}
    assert log.options == {
        "bg": "#102030",
        "insertbackground": HELPER_FG,
    }


def test_helper_color_settings_path_uses_appdata_folder(tmp_path):
    path = helper_color_settings_path(env={"APPDATA": str(tmp_path)})

    assert path == tmp_path / "Stake-GPT Helper" / "settings.json"


def test_load_helper_color_settings_returns_defaults_when_missing(tmp_path):
    settings = load_helper_color_settings(tmp_path / "missing.json")

    assert settings == {
        "outlineColor": HELPER_BUTTON_BG,
        "backgroundColor": HELPER_BG,
    }


def test_save_helper_color_settings_overwrites_last_choices(tmp_path):
    settings_path = tmp_path / "settings.json"

    save_helper_color_settings(
        {
            "outlineColor": "#0E3D32",
            "backgroundColor": "#102030",
        },
        settings_path,
    )
    save_helper_color_settings(
        {
            "outlineColor": "#AA0000",
            "backgroundColor": "#001122",
        },
        settings_path,
    )

    assert load_helper_color_settings(settings_path) == {
        "outlineColor": "#AA0000",
        "backgroundColor": "#001122",
    }


def test_load_helper_color_settings_ignores_invalid_colors(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"outlineColor": "not-a-color", "backgroundColor": "#102030"}',
        encoding="utf-8",
    )

    assert load_helper_color_settings(settings_path) == {
        "outlineColor": HELPER_BUTTON_BG,
        "backgroundColor": "#102030",
    }
