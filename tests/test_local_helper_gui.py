from __future__ import annotations

import pytest

from app.local_helper_gui import (
    AzpHelperGui,
    HELPER_BG,
    HELPER_CARD_BORDER,
    HELPER_FG,
    HELPER_BUTTON_BG,
    STAKE_LOGO_TEXT,
    STAKE_LOGO_RENDER_TEXT,
    active_color_for,
    apply_background_color,
    apply_border_color,
    apply_cmd_color,
    apply_outline_color,
    helper_color_presets_dir,
    helper_color_settings_path,
    list_helper_color_presets,
    load_helper_color_settings,
    load_helper_color_preset,
    logo_fill_fraction,
    logo_liquid_fill_polygon,
    normalize_color_choice,
    render_stake_logo_frame,
    save_helper_color_settings,
    save_helper_color_preset,
    should_minimize_to_tray,
)


def test_should_minimize_to_tray_only_for_iconic_windows_with_tray_support():
    assert should_minimize_to_tray("iconic", tray_supported=True)
    assert not should_minimize_to_tray("normal", tray_supported=True)
    assert not should_minimize_to_tray("withdrawn", tray_supported=True)
    assert not should_minimize_to_tray("iconic", tray_supported=False)


def test_helper_gui_uses_dark_fluent_theme():
    assert HELPER_BG == "#101418"
    assert HELPER_FG == "#F4F6F8"


def test_animated_logo_text_uses_stake_gpt_wordmark():
    assert STAKE_LOGO_TEXT == "Stake-GPT"


def test_rendered_logo_spaces_dash_from_letters():
    assert STAKE_LOGO_RENDER_TEXT == "Stake - GPT"


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


class FakeCard:
    def __init__(self) -> None:
        self.border_color = None

    def set_border_color(self, color: str) -> None:
        self.border_color = color


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
        "highlightbackground": "#0E3D32",
        "highlightcolor": "#27564B",
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


def test_apply_border_color_updates_rounded_card_borders():
    card = FakeCard()

    result = apply_border_color([card], "#445566")

    assert result == "#445566"
    assert card.border_color == "#445566"


def test_apply_cmd_color_updates_log_text_and_header_widgets():
    title = FakeWidget()
    icon = FakeWidget()
    log = FakeWidget()

    result = apply_cmd_color([title, icon], log, "#FEDCBA")

    assert result == "#FEDCBA"
    assert title.options == {"fg": "#FEDCBA"}
    assert icon.options == {"fg": "#FEDCBA"}
    assert log.options == {
        "fg": "#FEDCBA",
        "insertbackground": "#FEDCBA",
        "selectforeground": "#FEDCBA",
    }


def test_helper_color_settings_path_uses_appdata_folder(tmp_path):
    path = helper_color_settings_path(env={"APPDATA": str(tmp_path)})

    assert path == tmp_path / "Stake-GPT Helper" / "settings.json"


def test_helper_color_presets_dir_uses_appdata_folder(tmp_path):
    path = helper_color_presets_dir(env={"APPDATA": str(tmp_path)})

    assert path == tmp_path / "Stake-GPT Helper" / "color-presets"


def test_load_helper_color_settings_returns_defaults_when_missing(tmp_path):
    settings = load_helper_color_settings(tmp_path / "missing.json")

    assert settings == {
        "outlineColor": HELPER_BUTTON_BG,
        "backgroundColor": HELPER_BG,
        "borderColor": HELPER_CARD_BORDER,
        "cmdColor": HELPER_FG,
    }


def test_save_helper_color_settings_overwrites_last_choices(tmp_path):
    settings_path = tmp_path / "settings.json"

    save_helper_color_settings(
        {
            "outlineColor": "#0E3D32",
            "backgroundColor": "#102030",
            "borderColor": "#223344",
            "cmdColor": "#AABBCC",
        },
        settings_path,
    )
    save_helper_color_settings(
        {
            "outlineColor": "#AA0000",
            "backgroundColor": "#001122",
            "borderColor": "#334455",
            "cmdColor": "#CCDDEE",
        },
        settings_path,
    )

    assert load_helper_color_settings(settings_path) == {
        "outlineColor": "#AA0000",
        "backgroundColor": "#001122",
        "borderColor": "#334455",
        "cmdColor": "#CCDDEE",
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
        "borderColor": HELPER_CARD_BORDER,
        "cmdColor": HELPER_FG,
    }


def test_save_and_load_named_color_preset(tmp_path):
    preset_dir = tmp_path / "color-presets"

    path = save_helper_color_preset(
        "Red Dark",
        {
            "outlineColor": "#DD0011",
            "backgroundColor": "#101010",
            "borderColor": "#222222",
            "cmdColor": "#EEEEEE",
        },
        preset_dir,
    )

    assert path == preset_dir / "Red Dark.json"
    assert load_helper_color_preset("Red Dark", preset_dir) == {
        "outlineColor": "#DD0011",
        "backgroundColor": "#101010",
        "borderColor": "#222222",
        "cmdColor": "#EEEEEE",
    }


def test_named_color_presets_are_listed_without_json_extension(tmp_path):
    preset_dir = tmp_path / "color-presets"
    default_colors = {
        "outlineColor": "#DD0011",
        "backgroundColor": "#101010",
        "borderColor": "#222222",
        "cmdColor": "#EEEEEE",
    }
    save_helper_color_preset("Red Dark", default_colors, preset_dir)
    save_helper_color_preset("Blue Dark", default_colors, preset_dir)

    assert list_helper_color_presets(preset_dir) == ["Blue Dark", "Red Dark"]


def test_color_preset_name_rejects_empty_name(tmp_path):
    with pytest.raises(ValueError):
        save_helper_color_preset("   ", {}, tmp_path)


def test_color_preset_name_replaces_invalid_filename_characters(tmp_path):
    path = save_helper_color_preset(
        'Red/Blue: "Night"',
        {
            "outlineColor": "#DD0011",
            "backgroundColor": "#101010",
            "borderColor": "#222222",
            "cmdColor": "#EEEEEE",
        },
        tmp_path,
    )

    assert path.name == "Red-Blue- -Night-.json"


def test_logo_fill_fraction_fills_then_drains():
    assert logo_fill_fraction(0, cycle_frames=80) == 0
    assert logo_fill_fraction(20, cycle_frames=80) == 0.5
    assert logo_fill_fraction(40, cycle_frames=80) == 1
    assert logo_fill_fraction(60, cycle_frames=80) == 0.5
    assert logo_fill_fraction(80, cycle_frames=80) == 0


def test_logo_liquid_fill_polygon_uses_vertical_wave_fill():
    points = logo_liquid_fill_polygon(240, 96, 0.25, frame=0, amplitude=0)

    assert points[0] == (0, 72)
    assert points[-2:] == [(240, 96), (0, 96)]
    assert all(y == 72 for _x, y in points[:7])


def test_render_stake_logo_frame_uses_requested_background():
    image = render_stake_logo_frame(
        width=320,
        height=110,
        background="#102030",
        frame=0,
    )

    assert image.size == (320, 110)
    assert image.getpixel((0, 0))[:3] == (16, 32, 48)


def test_color_button_toggles_existing_palette_dialog():
    try:
        app = AzpHelperGui()
    except Exception as exc:
        pytest.skip(f"Tkinter GUI unavailable: {exc}")

    app.root.withdraw()
    try:
        app.choose_button_color()
        first_dialog = app.color_dialog

        assert first_dialog is not None
        assert first_dialog.winfo_exists()

        app.choose_button_color()

        assert app.color_dialog is None
        assert not first_dialog.winfo_exists()
    finally:
        dialog = getattr(app, "color_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()
        app.tray_icon.close()
        app.root.destroy()


def test_color_dialog_offers_all_color_targets():
    try:
        app = AzpHelperGui()
    except Exception as exc:
        pytest.skip(f"Tkinter GUI unavailable: {exc}")

    app.root.withdraw()
    try:
        app.choose_button_color()
        assert app.color_dialog is not None

        labels = sorted(
            child.cget("text")
            for child in app.color_dialog.winfo_children()[1].winfo_children()
        )

        assert labels == ["Background", "Border", "CMD", "Outline"]
    finally:
        dialog = getattr(app, "color_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()
        app.tray_icon.close()
        app.root.destroy()


def test_color_dialog_offers_named_preset_controls():
    try:
        app = AzpHelperGui()
    except Exception as exc:
        pytest.skip(f"Tkinter GUI unavailable: {exc}")

    app.root.withdraw()
    try:
        app.choose_button_color()
        assert app.color_dialog is not None

        def child_texts(parent):
            values = []
            for child in parent.winfo_children():
                try:
                    text = str(child.cget("text"))
                except Exception:
                    continue
                if text:
                    values.append(text)
            return values

        preset_body = app.color_dialog.winfo_children()[2]
        dialog_texts = child_texts(preset_body)
        control_texts = child_texts(preset_body.winfo_children()[2])

        assert "Preset name" in dialog_texts
        assert "Save Preset" in control_texts
        assert "Load Preset" in control_texts
    finally:
        dialog = getattr(app, "color_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()
        app.tray_icon.close()
        app.root.destroy()


def test_loading_color_preset_prefers_dropdown_selection():
    try:
        app = AzpHelperGui()
    except Exception as exc:
        pytest.skip(f"Tkinter GUI unavailable: {exc}")

    app.root.withdraw()
    try:
        app.choose_button_color()
        assert app.color_preset_name_var is not None
        assert app.color_preset_choice_var is not None

        app.color_preset_name_var.set("Typed Name")
        app.color_preset_choice_var.set("Dropdown Name")

        assert app._preset_name_for_load() == "Dropdown Name"
    finally:
        dialog = getattr(app, "color_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()
        app.tray_icon.close()
        app.root.destroy()


def test_color_picker_keeps_target_dialog_open_after_selecting_color(monkeypatch):
    try:
        app = AzpHelperGui()
    except Exception as exc:
        pytest.skip(f"Tkinter GUI unavailable: {exc}")

    app.root.withdraw()
    monkeypatch.setattr("app.local_helper_gui.colorchooser.askcolor", lambda **_kwargs: ((1, 2, 3), "#010203"))
    app._save_helper_color_settings = lambda: None
    try:
        app.choose_button_color()
        dialog = app.color_dialog

        assert dialog is not None
        assert dialog.winfo_exists()

        app._choose_helper_color("outline", dialog)

        assert app.color_dialog is dialog
        assert dialog.winfo_exists()
    finally:
        dialog = getattr(app, "color_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()
        app.tray_icon.close()
        app.root.destroy()
