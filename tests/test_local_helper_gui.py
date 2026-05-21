from __future__ import annotations

from app.local_helper_gui import should_minimize_to_tray


def test_should_minimize_to_tray_only_for_iconic_windows_with_tray_support():
    assert should_minimize_to_tray("iconic", tray_supported=True)
    assert not should_minimize_to_tray("normal", tray_supported=True)
    assert not should_minimize_to_tray("withdrawn", tray_supported=True)
    assert not should_minimize_to_tray("iconic", tray_supported=False)
