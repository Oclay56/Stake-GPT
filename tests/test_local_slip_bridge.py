from __future__ import annotations

import asyncio

import httpx

from app.local_slip_bridge import (
    LocalBridgeConfig,
    SlipJobApiClient,
    build_dry_run_result,
    format_job_summary,
    run_once,
    watch_message,
)


def test_format_job_summary_keeps_review_information_compact():
    summary = format_job_summary(
        {
            "jobId": "job-1",
            "matchup": "Blue Jays vs Angels",
            "date": "2026-05-08",
            "selections": [
                {
                    "player": {"name": "George Springer"},
                    "market": {"name": "Hits"},
                    "side": "under",
                    "line": 0.5,
                    "odds": 2.9,
                }
            ],
        }
    )

    assert "job-1" in summary
    assert "Blue Jays vs Angels" in summary
    assert "George Springer under 0.5 Hits @ 2.9" in summary


def test_build_dry_run_result_never_marks_legs_clicked():
    result = build_dry_run_result(
        {
            "jobId": "job-1",
            "selections": [
                {
                    "player": {"name": "George Springer"},
                    "market": {"name": "Hits"},
                    "side": "under",
                    "line": 0.5,
                    "odds": 2.9,
                }
            ],
        }
    )

    assert result["mode"] == "dry_run"
    assert result["matched"] == 0
    assert result["blocked"] == 0
    assert result["requiresManualReview"] is True
    assert result["legs"][0]["action"] == "review_only_not_clicked"


def test_slip_job_api_client_claims_and_updates_job():
    asyncio.run(_run_slip_job_api_client_claims_and_updates_job())


async def _run_slip_job_api_client_claims_and_updates_job():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/slip-jobs/next":
            return httpx.Response(
                200,
                json={
                    "job": {
                        "jobId": "job-1",
                        "status": "claimed",
                        "selections": [],
                    }
                },
            )
        if request.url.path == "/slip-jobs/job-1/status":
            return httpx.Response(200, json={"jobId": "job-1", "status": "dry_run_ready"})
        return httpx.Response(404, json={"detail": "missing"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://azp.example",
    ) as http_client:
        api_client = SlipJobApiClient(
            http_client=http_client,
            config=LocalBridgeConfig(
                api_url="https://azp.example",
                api_key="secret",
                bridge_id="bridge-test",
            ),
        )
        job = await api_client.claim_next_job()
        update = await api_client.update_job_status(
            "job-1",
            "dry_run_ready",
            message="Dry-run complete.",
            result={"matched": 0},
        )

    assert job["jobId"] == "job-1"
    assert update["status"] == "dry_run_ready"
    assert requests[0].url.params["bridgeId"] == "bridge-test"
    assert requests[0].headers["X-AZP-API-Key"] == "secret"
    assert requests[1].headers["X-AZP-API-Key"] == "secret"


def test_run_once_uses_ui_builder_and_marks_complete():
    asyncio.run(_run_once_uses_ui_builder_and_marks_complete())


async def _run_once_uses_ui_builder_and_marks_complete():
    updates: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slip-jobs/next":
            return httpx.Response(
                200,
                json={
                    "job": {
                        "jobId": "job-1",
                        "status": "claimed",
                        "matchup": "Blue Jays vs Angels",
                        "selections": [
                            {
                                "selectionId": "springer-hits:under",
                                "player": {"name": "George Springer"},
                                "market": {"name": "Hits"},
                                "side": "under",
                                "line": 0.5,
                                "odds": 2.9,
                            }
                        ],
                    }
                },
            )
        if request.url.path == "/slip-jobs/job-1/status":
            updates.append(request.read())
            return httpx.Response(200, json={"jobId": "job-1", "status": "built"})
        return httpx.Response(404, json={"detail": "missing"})

    async def fake_ui_runner(job, config):
        assert job["jobId"] == "job-1"
        assert config.mode == "click"
        return {
            "mode": "click",
            "matched": 1,
            "clicked": 1,
            "blocked": 0,
            "requiresManualReview": True,
            "message": "Clicked 1 leg.",
            "legs": [{"status": "clicked"}],
        }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://azp.example",
    ) as http_client:
        outcome = await run_once(
            LocalBridgeConfig(
                api_url="https://azp.example",
                bridge_id="bridge-test",
                ui_mode="click",
            ),
            http_client=http_client,
            ui_runner=fake_ui_runner,
        )

    assert outcome["status"] == "processed"
    assert updates
    assert b'"status":"built"' in updates[0]
    assert b'"clicked":1' in updates[0]


def test_run_once_marks_blocked_when_any_ui_leg_blocks():
    asyncio.run(_run_once_marks_blocked_when_any_ui_leg_blocks())


async def _run_once_marks_blocked_when_any_ui_leg_blocks():
    updates: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slip-jobs/next":
            return httpx.Response(
                200,
                json={
                    "job": {
                        "jobId": "job-2",
                        "status": "claimed",
                        "selections": [
                            {
                                "player": {"name": "George Springer"},
                                "market": {"name": "Hits"},
                                "side": "under",
                                "line": 0.5,
                            }
                        ],
                    }
                },
            )
        if request.url.path == "/slip-jobs/job-2/status":
            updates.append(request.read())
            return httpx.Response(200, json={"jobId": "job-2", "status": "blocked"})
        return httpx.Response(404, json={"detail": "missing"})

    async def fake_ui_runner(job, config):
        return {
            "mode": "click",
            "matched": 0,
            "clicked": 0,
            "blocked": 1,
            "requiresManualReview": True,
            "message": "Blocked 1 leg.",
            "legs": [{"status": "blocked"}],
        }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://azp.example",
    ) as http_client:
        outcome = await run_once(
            LocalBridgeConfig(
                api_url="https://azp.example",
                bridge_id="bridge-test",
                ui_mode="click",
            ),
            http_client=http_client,
            ui_runner=fake_ui_runner,
        )

    assert outcome["status"] == "processed"
    assert b'"status":"blocked"' in updates[0]


def test_watch_message_prints_waiting_once_then_stays_quiet():
    first = watch_message({"status": "idle"}, was_waiting=False)
    second = watch_message({"status": "idle"}, was_waiting=True)

    assert first == ("Waiting for slip...", True)
    assert second == (None, True)


def test_watch_message_reports_built_slip_then_returns_to_waiting_state():
    message, waiting = watch_message(
        {"status": "processed", "job": {"status": "built"}},
        was_waiting=True,
    )

    assert message == "One slip created. Waiting for next slip..."
    assert waiting is False
