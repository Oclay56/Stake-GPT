from __future__ import annotations

from datetime import datetime, timezone

from app.supabase_cache import (
    LOCAL_CLEANUP_TARGETS,
    LocalCleanupTarget,
    _content_range_total,
    chrome_cleanup_targets,
    cleanup_local_cache,
    cleanup_operations,
)


def test_cleanup_operations_only_target_local_ui_cache_rows():
    operations = cleanup_operations(
        now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        retention_hours=6,
        stale_running_minutes=15,
    )

    names = [operation.name for operation in operations]
    assert names == [
        "expire pending/running jobs past expires_at",
        "expire stale running jobs",
        "delete jobs past expires_at",
        "delete old completed/failed/expired jobs",
    ]
    assert operations[0].filters == {
        "status": "in.(pending,running)",
        "expires_at": "lt.2026-05-21T12:00:00Z",
    }
    assert operations[1].filters == {
        "status": "eq.running",
        "updated_at": "lt.2026-05-21T11:45:00Z",
    }
    assert operations[2].filters == {
        "expires_at": "lt.2026-05-21T12:00:00Z",
    }
    assert operations[3].filters == {
        "status": "in.(completed,failed,expired)",
        "updated_at": "lt.2026-05-21T06:00:00Z",
    }


def test_content_range_total_handles_postgrest_headers():
    assert _content_range_total("0-0/23") == 23
    assert _content_range_total("*/0") == 0
    assert _content_range_total(None) == 0
    assert _content_range_total("bad") == 0


def test_cleanup_local_cache_deletes_only_rebuildable_targets(tmp_path):
    cache_file = tmp_path / "data" / "chrome-stake-ui" / "Default" / "Cache" / "entry.bin"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"x" * 128)
    temp_file = tmp_path / ".tmp" / "scan.json"
    temp_file.parent.mkdir(parents=True)
    temp_file.write_text("temporary\n", encoding="utf-8")
    cookie_file = tmp_path / "data" / "chrome-stake-ui" / "Default" / "Cookies"
    cookie_file.write_text("keep-login-state\n", encoding="utf-8")

    result = cleanup_local_cache(
        root_dir=tmp_path,
        targets=[
            LocalCleanupTarget("temporary workspace files", ".tmp"),
            LocalCleanupTarget("Chrome HTTP cache", "data/chrome-stake-ui/Default/Cache"),
        ],
    )

    assert result["errors"] == []
    assert result["deletedFiles"] == 2
    assert result["bytesFreed"] >= 128
    assert not cache_file.exists()
    assert not temp_file.exists()
    assert cookie_file.exists()


def test_default_cleanup_targets_include_both_stake_chrome_profiles():
    paths = {target.relative_path for target in chrome_cleanup_targets()}

    assert "data/chrome-stake-ui/Default/Cache" in paths
    assert "data/chrome-stake-ui/Default/Code Cache" in paths
    assert "data/chrome-stake-ui-bet/Profile 1/Cache" in paths
    assert "data/chrome-stake-ui-bet/Profile 1/Code Cache" in paths
    assert "data/chrome-stake-ui-bet/BrowserMetrics-spare.pma" in paths
    assert not any(path.endswith("/Cookies") for path in paths)
    assert not any(path.endswith("/Local Storage") for path in paths)
    assert not any(path.endswith("/Sessions") for path in paths)


def test_default_cleanup_deletes_stake_com_and_stake_bet_cache_without_login_state(tmp_path):
    stake_com_cache = tmp_path / "data" / "chrome-stake-ui" / "Default" / "Cache" / "entry.bin"
    stake_bet_cache = tmp_path / "data" / "chrome-stake-ui-bet" / "Profile 1" / "Cache" / "entry.bin"
    stake_bet_metrics = tmp_path / "data" / "chrome-stake-ui-bet" / "BrowserMetrics-spare.pma"
    stake_com_cookie = tmp_path / "data" / "chrome-stake-ui" / "Default" / "Cookies"
    stake_bet_cookie = tmp_path / "data" / "chrome-stake-ui-bet" / "Profile 1" / "Cookies"

    for path, content in (
        (stake_com_cache, b"x" * 32),
        (stake_bet_cache, b"y" * 64),
        (stake_bet_metrics, b"z" * 16),
        (stake_com_cookie, b"keep-com-login"),
        (stake_bet_cookie, b"keep-bet-login"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    result = cleanup_local_cache(root_dir=tmp_path, targets=LOCAL_CLEANUP_TARGETS)

    assert result["errors"] == []
    assert result["deletedFiles"] == 3
    assert not stake_com_cache.exists()
    assert not stake_bet_cache.exists()
    assert not stake_bet_metrics.exists()
    assert stake_com_cookie.exists()
    assert stake_bet_cookie.exists()


def test_cleanup_local_cache_dry_run_counts_without_deleting(tmp_path):
    cache_file = tmp_path / "data" / "chrome-stake-ui" / "Default" / "Cache" / "entry.bin"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"x" * 64)

    result = cleanup_local_cache(
        root_dir=tmp_path,
        dry_run=True,
        targets=[LocalCleanupTarget("Chrome HTTP cache", "data/chrome-stake-ui/Default/Cache")],
    )

    assert result["wouldDeleteFiles"] == 1
    assert result["wouldFreeBytes"] == 64
    assert result["deletedFiles"] == 0
    assert cache_file.exists()
