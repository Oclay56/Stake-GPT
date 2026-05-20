from __future__ import annotations

import asyncio

from app import local_stake_helper


class FakeJobStore:
    def __init__(self) -> None:
        self.completed: list[tuple[str, dict]] = []
        self.failed: list[tuple[str, str]] = []

    async def complete_job(self, job_id: str, result: dict):
        self.completed.append((job_id, result))

    async def fail_job(self, job_id: str, error_message: str):
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
