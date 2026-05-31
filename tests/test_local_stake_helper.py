from __future__ import annotations

import asyncio
from pathlib import Path

from app import local_stake_helper


class FakeJobStore:
    def __init__(self) -> None:
        self.completed: list[tuple[str, dict]] = []
        self.failed: list[tuple[str, str]] = []
        self.fail_raises: Exception | None = None

    async def complete_job(self, job_id: str, result: dict):
        self.completed.append((job_id, result))

    async def fail_job(self, job_id: str, error_message: str):
        if self.fail_raises:
            raise self.fail_raises
        self.failed.append((job_id, error_message))


def test_process_job_runs_sync_browser_reader_outside_event_loop(monkeypatch):
    def fake_read_stake_sgm_board(fixture_slug: str, *, cdp_url: str):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("sync browser reader ran inside the async event loop")

        return {
            "source": "stake_ui_sgm",
            "fixtureSlug": fixture_slug,
            "counts": {"playerPropsPlayable": 1},
        }

    monkeypatch.setattr(
        local_stake_helper,
        "read_stake_sgm_board",
        fake_read_stake_sgm_board,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-123",
        "request": {"fixtureSlug": "46575343-miami-marlins-atlanta-braves"},
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-123"
    assert store.completed[0][1]["fixtureSlug"] == "46575343-miami-marlins-atlanta-braves"
    assert store.completed[0][1]["request"] == job["request"]


def test_process_job_runs_mlb_game_reader(monkeypatch):
    def fake_read_stake_mlb_games(*, cdp_url: str, limit: int):
        assert cdp_url == "http://127.0.0.1:9222"
        assert limit == 12
        return {
            "source": "stake_ui_mlb_games",
            "games": [
                {
                    "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                    "matchup": "New York Yankees vs Toronto Blue Jays",
                }
            ],
        }

    monkeypatch.setattr(
        local_stake_helper,
        "read_stake_mlb_games",
        fake_read_stake_mlb_games,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-games",
        "jobType": "stake_ui_mlb_games",
        "request": {"limit": 12},
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-games"
    assert store.completed[0][1]["games"][0]["fixtureSlug"] == "46575351-new-york-yankees-toronto-blue-jays"


def test_process_job_runs_mlb_moneyline_reader(monkeypatch):
    def fake_read_stake_mlb_moneylines(*, cdp_url: str, limit: int):
        assert cdp_url == "http://127.0.0.1:9222"
        assert limit == 40
        return {
            "source": "stake_ui_mlb_moneylines_raw",
            "games": [{"fixtureSlug": "123-yankees-blue-jays"}],
        }

    monkeypatch.setattr(
        local_stake_helper,
        "read_stake_mlb_moneylines",
        fake_read_stake_mlb_moneylines,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-moneylines",
        "jobType": "stake_ui_mlb_moneylines",
        "request": {"limit": 40},
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][1]["source"] == "stake_ui_mlb_moneylines_raw"


def test_review_mode_claims_read_only_mlb_moneyline_jobs():
    assert "stake_ui_mlb_moneylines" in local_stake_helper._job_types_for_mode("review")


def test_process_job_runs_mlb_moneyline_review_slip_builder(monkeypatch):
    def fake_build_stake_mlb_moneyline_review_slip(
        selections: list[dict[str, object]],
        *,
        execution_timeout_seconds,
        cdp_url: str,
    ):
        assert cdp_url == "http://127.0.0.1:9222"
        assert execution_timeout_seconds == 55
        assert selections == [
            {
                "rowId": "mlb_ml_yankees",
                "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                "team": "New York Yankees",
                "odds": 1.72,
            }
        ]
        return {
            "source": "stake_ui_mlb_moneyline_review_slip",
            "status": "built_for_review",
        }

    monkeypatch.setattr(
        local_stake_helper,
        "build_stake_mlb_moneyline_review_slip",
        fake_build_stake_mlb_moneyline_review_slip,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-moneyline-build",
        "jobType": "stake_ui_mlb_moneyline_build_slip",
        "request": {
            "selections": [
                {
                    "rowId": "mlb_ml_yankees",
                    "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                    "team": "New York Yankees",
                    "odds": 1.72,
                }
            ],
            "localExecutionTimeoutSeconds": 55,
        },
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-moneyline-build"
    assert store.completed[0][1]["status"] == "built_for_review"


def test_review_mode_claims_moneyline_review_slip_jobs():
    assert "stake_ui_mlb_moneyline_build_slip" in local_stake_helper._job_types_for_mode("review")


def test_process_job_runs_batch_sgm_board_reader(monkeypatch):
    def fake_read_stake_sgm_boards_batch(*, fixture_slugs: list[str], cdp_url: str, max_fixtures: int):
        assert cdp_url == "http://127.0.0.1:9222"
        assert fixture_slugs == [
            "46575351-new-york-yankees-toronto-blue-jays",
            "46575562-washington-nationals-new-york-mets",
        ]
        assert max_fixtures == 20
        return {
            "source": "stake_ui_sgm_board_batch",
            "fixtureCount": 2,
            "succeeded": 1,
            "failed": 1,
            "boards": [{"fixtureSlug": fixture_slugs[0], "playerProps": []}],
            "errors": [{"fixtureSlug": fixture_slugs[1], "message": "SGM tab not visible"}],
        }

    monkeypatch.setattr(
        local_stake_helper,
        "read_stake_sgm_boards_batch",
        fake_read_stake_sgm_boards_batch,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-board-batch",
        "jobType": "stake_ui_sgm_board_batch",
        "request": {
            "fixtureSlugs": [
                "46575351-new-york-yankees-toronto-blue-jays",
                "46575562-washington-nationals-new-york-mets",
            ],
            "maxFixtures": 20,
        },
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-board-batch"
    assert store.completed[0][1]["source"] == "stake_ui_sgm_board_batch"
    assert store.completed[0][1]["failed"] == 1


def test_process_job_runs_batch_review_builder(monkeypatch):
    def fake_build_stake_sgm_review_slip_batch(
        groups: list[dict],
        *,
        continue_on_group_failure: bool,
        min_groups_required: int | None,
        execution_timeout_seconds: int | None,
        cdp_url: str,
    ):
        assert cdp_url == "http://127.0.0.1:9222"
        assert continue_on_group_failure is True
        assert min_groups_required == 1
        assert execution_timeout_seconds == 175
        assert groups[0]["fixtureSlug"] == "46575351-new-york-yankees-toronto-blue-jays"
        return {
            "source": "stake_ui_sgm_review_slip_batch",
            "status": "built_for_review",
            "clickedGroups": 1,
            "clickedLegs": 2,
        }

    monkeypatch.setattr(
        local_stake_helper,
        "build_stake_sgm_review_slip_batch",
        fake_build_stake_sgm_review_slip_batch,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-batch",
        "jobType": "stake_ui_sgm_build_slip_batch",
        "request": {
            "continueOnGroupFailure": True,
            "minGroupsRequired": 1,
            "localExecutionTimeoutSeconds": 175,
            "groups": [
                {
                    "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                    "selections": [
                        {"market": "Play Home Runs", "side": "under", "line": 2.5, "odds": 1.72},
                        {"market": "Match Total Bases", "side": "under", "line": 25.5, "odds": 2.47},
                    ],
                }
            ]
        },
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-batch"
    assert store.completed[0][1]["status"] == "built_for_review"


def test_process_job_runs_stake_ui_state_reader(monkeypatch):
    def fake_read_stake_ui_state(*, cdp_url: str, fixture_slug: str | None = None):
        assert cdp_url == "http://127.0.0.1:9222"
        assert fixture_slug == "46575351-new-york-yankees-toronto-blue-jays"
        return {
            "source": "stake_ui_state",
            "status": "ok",
            "currentFixtureSlug": fixture_slug,
            "sgmVisible": True,
        }

    monkeypatch.setattr(
        local_stake_helper,
        "read_stake_ui_state",
        fake_read_stake_ui_state,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-state",
        "jobType": "stake_ui_state",
        "request": {"fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays"},
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-state"
    assert store.completed[0][1]["source"] == "stake_ui_state"


def test_process_job_runs_sgm_selection_clearer(monkeypatch):
    def fake_clear_stake_sgm_selections(*, cdp_url: str, fixture_slug: str | None = None):
        assert cdp_url == "http://127.0.0.1:9222"
        assert fixture_slug == "46575351-new-york-yankees-toronto-blue-jays"
        return {
            "source": "stake_ui_sgm_clear_selections",
            "status": "cleared",
            "fixtureSlug": fixture_slug,
        }

    monkeypatch.setattr(
        local_stake_helper,
        "clear_stake_sgm_selections",
        fake_clear_stake_sgm_selections,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-clear",
        "jobType": "stake_ui_sgm_clear_selections",
        "request": {"fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays"},
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-clear"
    assert store.completed[0][1]["status"] == "cleared"


def test_process_job_runs_sidebar_group_remover(monkeypatch):
    def fake_remove_stake_sidebar_group(
        *,
        cdp_url: str,
        fixture_slug: str | None = None,
        matchup: str | None = None,
        row_id: str | None = None,
        team: str | None = None,
    ):
        assert cdp_url == "http://127.0.0.1:9222"
        assert fixture_slug == "46575351-new-york-yankees-toronto-blue-jays"
        assert matchup == "Yankees vs Blue Jays"
        assert row_id is None
        assert team is None
        return {
            "source": "stake_ui_remove_sidebar_group",
            "status": "removed",
            "fixtureSlug": fixture_slug,
            "matchup": matchup,
        }

    monkeypatch.setattr(
        local_stake_helper,
        "remove_stake_sidebar_group",
        fake_remove_stake_sidebar_group,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-remove",
        "jobType": "stake_ui_remove_sidebar_group",
        "request": {
            "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
            "matchup": "Yankees vs Blue Jays",
        },
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-remove"
    assert store.completed[0][1]["status"] == "removed"


def test_process_job_runs_sidebar_clearer(monkeypatch):
    def fake_clear_stake_sidebar(*, cdp_url: str):
        assert cdp_url == "http://127.0.0.1:9222"
        return {
            "source": "stake_ui_clear_sidebar",
            "status": "cleared",
        }

    monkeypatch.setattr(
        local_stake_helper,
        "clear_stake_sidebar",
        fake_clear_stake_sidebar,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-clear-sidebar",
        "jobType": "stake_ui_clear_sidebar",
        "request": {},
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-clear-sidebar"
    assert store.completed[0][1]["status"] == "cleared"


def test_process_job_does_not_crash_when_failure_reporting_fails(monkeypatch, capsys):
    def fake_read_stake_sgm_board(fixture_slug: str, *, cdp_url: str):
        raise RuntimeError("Stake is still region-blocked in this browser session.")

    monkeypatch.setattr(
        local_stake_helper,
        "read_stake_sgm_board",
        fake_read_stake_sgm_board,
    )
    store = FakeJobStore()
    store.fail_raises = OSError("[Errno 11001] getaddrinfo failed")
    job = {
        "jobId": "job-456",
        "request": {"fixtureSlug": "46575343-miami-marlins-atlanta-braves"},
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    output = capsys.readouterr().out
    assert "Failed job job-456" in output
    assert "Could not report failed job job-456" in output


def test_debug_chrome_launch_opens_stake_in_visible_window(monkeypatch, tmp_path):
    monkeypatch.delenv("AZP_STAKE_START_URL", raising=False)

    args = local_stake_helper._debug_chrome_args(
        Path("C:/Chrome/chrome.exe"),
        tmp_path / "profile",
        9222,
    )

    assert Path(args[0]) == Path("C:/Chrome/chrome.exe")
    assert "--new-window" in args
    assert "--start-maximized" in args
    assert "--window-position=80,40" in args
    assert "--window-position=-32000,-32000" not in args
    assert "about:blank" not in args
    assert args[-1] == "https://stake.com"


def test_debug_chrome_launch_allows_start_url_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AZP_STAKE_START_URL", "https://stake.com/sports")

    args = local_stake_helper._debug_chrome_args(
        Path("C:/Chrome/chrome.exe"),
        tmp_path / "profile",
        9222,
    )

    assert args[-1] == "https://stake.com/sports"


def test_supabase_cache_cleanup_sync_uses_env_settings(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run_cleanup(**kwargs):
        seen.update(kwargs)
        return {
            "expiredJobs": 2,
            "deletedJobs": 3,
        }

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("AZP_LOCAL_UI_JOB_TABLE", "local_ui_jobs")
    monkeypatch.setenv("AZP_SUPABASE_JOB_RETENTION_HOURS", "4")
    monkeypatch.setenv("AZP_SUPABASE_STALE_JOB_MINUTES", "9")
    monkeypatch.setattr(local_stake_helper, "run_cleanup", fake_run_cleanup)

    result = local_stake_helper._run_supabase_cache_cleanup_sync()

    assert result == {"expiredJobs": 2, "deletedJobs": 3}
    assert seen["supabase_url"] == "https://example.supabase.co"
    assert seen["service_key"] == "service-key"
    assert seen["table_name"] == "local_ui_jobs"
    assert seen["retention_hours"] == 4
    assert seen["stale_running_minutes"] == 9
