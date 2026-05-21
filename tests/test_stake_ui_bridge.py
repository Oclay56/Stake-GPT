from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.gpt_action import build_gpt_action_openapi_schema
from app.main import app, get_local_ui_job_store, get_stake_client, _compact_stake_ui_sgm_board
from app.stake_sgm_browser import match_sgm_review_selections, normalize_sgm_response


class FakeStakeClient:
    async def get_tournament_schedule(self, sport: str, category: str, tournament: str):
        return {
            "schedule": [
                {
                    "fixtures": [
                        {
                            "slug": "46450286-miami-marlins-atlanta-braves",
                            "name": "Miami Marlins - Atlanta Braves",
                            "date": 1779221400000,
                            "status": "active",
                            "type": "match",
                        }
                    ]
                }
            ]
        }


class FakeSlugNameStakeClient:
    async def get_tournament_schedule(self, sport: str, category: str, tournament: str):
        return {
            "schedule": [
                {
                    "fixtures": [
                        {
                            "slug": "46575343-miami-marlins-atlanta-braves",
                            "name": "46575343-miami-marlins-atlanta-braves",
                            "date": 1779314400000,
                            "status": "active",
                            "type": "match",
                        }
                    ]
                }
            ]
        }


class FakeCompletedUiJobStore:
    def __init__(self) -> None:
        self.created_jobs: list[dict] = []
        self.cached_job: dict | None = None

    def enabled(self) -> bool:
        return True

    async def find_recent_completed_job(
        self,
        *,
        job_type: str,
        fixture_slug: str,
        max_age_seconds: int,
        limit: int = 20,
    ):
        return self.cached_job

    async def create_job(self, *, job_type: str, request: dict, timeout_seconds: int):
        job = {
            "jobId": "job-123",
            "jobType": job_type,
            "status": "pending",
            "request": request,
        }
        self.created_jobs.append(job)
        return job

    async def wait_for_completed_result(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: float = 1.0,
    ):
        assert job_id == "job-123"
        return {
            "jobId": job_id,
            "status": "completed",
            "result": {
                "source": "stake_ui_sgm",
                "fixtureSlug": "46450286-miami-marlins-atlanta-braves",
                "counts": {"playerPropsPlayable": 3},
                "playerProps": [
                    {
                        "team": "Atlanta Braves",
                        "player": "Ronald Acuna Jr.",
                        "market": "Hits",
                        "line": 0.5,
                        "under": 2.1,
                        "over": 1.62,
                        "playable": True,
                    }
                    for _ in range(3)
                ],
                "teamMarkets": [],
            },
            "error": None,
        }


class FakeCompletedBuildJobStore(FakeCompletedUiJobStore):
    async def wait_for_completed_result(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: float = 1.0,
    ):
        assert job_id == "job-123"
        return {
            "jobId": job_id,
            "status": "completed",
            "workerId": "azp-local-test",
            "result": {
                "source": "stake_ui_sgm_build_slip",
                "fixtureSlug": "46450286-miami-marlins-atlanta-braves",
                "status": "built_for_review",
                "reviewOnly": True,
                "clickedLegs": 2,
                "selectedRows": [
                    {
                        "player": "Ronald Acuna Jr.",
                        "team": "Atlanta Braves",
                        "market": "Hits",
                        "side": "under",
                        "line": 0.5,
                        "odds": 2.1,
                    },
                    {
                        "player": "Ozzie Albies",
                        "team": "Atlanta Braves",
                        "market": "Total Bases",
                        "side": "under",
                        "line": 1.5,
                        "odds": 1.8,
                    },
                ],
                "missingSelections": [],
                "safety": {
                    "enteredStakeAmount": False,
                    "clickedPlaceBet": False,
                },
            },
            "error": None,
        }


class FakeCompletedMlbGamesJobStore(FakeCompletedUiJobStore):
    async def wait_for_completed_result(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: float = 1.0,
    ):
        assert job_id == "job-123"
        return {
            "jobId": job_id,
            "status": "completed",
            "workerId": "azp-local-test",
            "result": {
                "source": "stake_ui_mlb_games",
                "capturedAt": "2026-05-20T20:00:00Z",
                "games": [
                    {
                        "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                        "url": "https://stake.com/de/sports/baseball/usa/mlb/46575351-new-york-yankees-toronto-blue-jays",
                        "matchup": "New York Yankees vs Toronto Blue Jays",
                        "teams": ["New York Yankees", "Toronto Blue Jays"],
                        "statusText": "NOT STARTED",
                    },
                    {
                        "fixtureSlug": "46575562-washington-nationals-new-york-mets",
                        "url": "https://stake.com/de/sports/baseball/usa/mlb/46575562-washington-nationals-new-york-mets",
                        "matchup": "Washington Nationals vs New York Mets",
                        "teams": ["Washington Nationals", "New York Mets"],
                        "statusText": "NOT STARTED",
                    },
                ],
                "warnings": [],
            },
            "error": None,
        }


class FakeCompletedBatchBuildJobStore(FakeCompletedUiJobStore):
    async def wait_for_completed_result(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: float = 1.0,
    ):
        assert job_id == "job-123"
        return {
            "jobId": job_id,
            "status": "completed",
            "workerId": "azp-local-test",
            "result": {
                "source": "stake_ui_sgm_review_slip_batch",
                "status": "built_for_review",
                "reviewOnly": True,
                "fixtureCount": 2,
                "clickedGroups": 2,
                "clickedLegs": 4,
                "groups": [
                    {
                        "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                        "status": "built_for_review",
                        "clickedLegs": 2,
                    },
                    {
                        "fixtureSlug": "46575562-washington-nationals-new-york-mets",
                        "status": "built_for_review",
                        "clickedLegs": 2,
                    },
                ],
                "safety": {
                    "enteredStakeAmount": False,
                    "clickedPlaceBet": False,
                },
            },
            "error": None,
        }


@pytest.fixture
def fake_ui_store():
    return FakeCompletedUiJobStore()


@pytest.fixture(autouse=True)
def override_dependencies(fake_ui_store):
    app.dependency_overrides[get_stake_client] = lambda: FakeStakeClient()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_ui_store
    yield
    app.dependency_overrides.clear()


def test_gpt_schema_exposes_stake_ui_sgm_board_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operation = schema["paths"]["/mlb/stake-ui/sgm-board"]["post"]

    assert operation["operationId"] == "getStakeUiSgmBoard"
    assert "Stake UI" in operation["summary"]


def test_gpt_schema_exposes_review_slip_build_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operation = schema["paths"]["/mlb/stake-ui/review-slip"]["post"]

    assert operation["operationId"] == "buildStakeUiReviewSlip"
    assert "review" in operation["summary"].lower()
    properties = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert properties["reviewOnly"]["const"] is True
    assert "rowIds" in properties
    selection_schema = properties["selections"]["items"]
    assert "rowId" in selection_schema["properties"]


def test_gpt_schema_exposes_stake_ui_mlb_games_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operation = schema["paths"]["/mlb/stake-ui/mlb-games"]["post"]

    assert operation["operationId"] == "getStakeUiMlbGames"
    assert "MLB" in operation["summary"]


def test_gpt_schema_exposes_batch_review_slip_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operation = schema["paths"]["/mlb/stake-ui/review-slip-batch"]["post"]

    assert operation["operationId"] == "buildStakeUiReviewSlipBatch"
    assert "batch" in operation["summary"].lower()
    properties = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert properties["reviewOnly"]["const"] is True
    assert "groups" in properties
    group_schema = properties["groups"]["items"]
    assert "rowIds" in group_schema["properties"]
    assert "rowId" in group_schema["properties"]["selections"]["items"]["properties"]


def test_compact_sgm_board_returns_stable_row_ids_for_duplicate_odds():
    board = {
        "source": "stake_ui_sgm",
        "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
        "playerProps": [
            {
                "team": "New York Yankees",
                "player": "Austin Wells",
                "scope": "player",
                "market": "Hits",
                "line": 0.5,
                "under": 2.1,
                "over": 1.62,
                "playable": True,
                "lineId": "line-a",
                "marketId": "market-hits",
                "playerId": "player-a",
            },
            {
                "team": "Toronto Blue Jays",
                "player": "George Springer",
                "scope": "player",
                "market": "Hits",
                "line": 0.5,
                "under": 2.1,
                "over": 1.62,
                "playable": True,
                "lineId": "line-b",
                "marketId": "market-hits",
                "playerId": "player-b",
            },
        ],
        "teamMarkets": [],
    }

    compact = _compact_stake_ui_sgm_board(
        board,
        limit=10,
        side="under",
        market="",
        scope="",
        playable_only=True,
    )

    rows = compact["rows"]
    assert len(rows) == 2
    assert all(row["rowId"] for row in rows)
    assert rows[0]["odds"] == rows[1]["odds"]
    assert rows[0]["rowId"] != rows[1]["rowId"]


def test_stake_ui_mlb_games_route_creates_job_and_returns_completed_result():
    fake_store = FakeCompletedMlbGamesJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/mlb-games",
            json={"timeoutSeconds": 2, "limit": 10},
        )

    body = response.json()
    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert body["source"] == "stake_ui_mlb_games_via_local_helper"
    assert body["bridge"]["status"] == "completed"
    assert body["uiGames"]["returnedGames"] == 2
    assert body["uiGames"]["games"][0]["fixtureSlug"] == "46575351-new-york-yankees-toronto-blue-jays"
    assert created_request["purpose"] == "stake_ui_mlb_game_index"
    assert created_request["limit"] == 10


def test_stake_ui_review_slip_batch_route_creates_one_batch_job_with_guardrails():
    fake_store = FakeCompletedBatchBuildJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/review-slip-batch",
            json={
                "reviewOnly": True,
                "timeoutSeconds": 2,
                "groups": [
                    {
                        "matchup": "Yankees vs Blue Jays",
                        "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                        "selections": [
                            {
                                "market": "Play Home Runs",
                                "side": "under",
                                "line": 2.5,
                                "odds": 1.72,
                            },
                            {
                                "market": "Match Total Bases",
                                "side": "under",
                                "line": 25.5,
                                "odds": 2.47,
                            },
                        ],
                    },
                    {
                        "matchup": "Nationals vs Mets",
                        "fixtureSlug": "46575562-washington-nationals-new-york-mets",
                        "selections": [
                            {
                                "player": "Zack Littell",
                                "market": "Failed Attempts",
                                "side": "under",
                                "line": 2.5,
                                "odds": 2.15,
                            },
                            {
                                "market": "Play Home Runs",
                                "side": "under",
                                "line": 2.5,
                                "odds": 1.72,
                            },
                        ],
                    },
                ],
            },
        )

    body = response.json()
    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert body["source"] == "stake_ui_sgm_review_slip_batch_via_local_helper"
    assert body["result"]["status"] == "built_for_review"
    assert body["result"]["clickedGroups"] == 2
    assert body["result"]["safety"]["enteredStakeAmount"] is False
    assert created_request["reviewOnly"] is True
    assert created_request["forbiddenActions"] == ["enter_stake_amount", "click_place_bet"]
    assert len(created_request["groups"]) == 2


def test_stake_ui_review_slip_batch_route_accepts_row_ids_without_reconstructed_fields():
    fake_store = FakeCompletedBatchBuildJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/review-slip-batch",
            json={
                "reviewOnly": True,
                "timeoutSeconds": 2,
                "groups": [
                    {
                        "matchup": "Yankees vs Blue Jays",
                        "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                        "rowIds": ["sgm_abc123", "sgm_def456"],
                    }
                ],
            },
        )

    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert created_request["groups"][0]["selections"] == [
        {"rowId": "sgm_abc123"},
        {"rowId": "sgm_def456"},
    ]


def test_stake_ui_sgm_board_route_creates_job_and_returns_completed_result(fake_ui_store):
    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/sgm-board",
            json={
                "matchup": "Braves vs Marlins",
                "date": "2026-05-19",
                "timeoutSeconds": 2,
            },
        )

    body = response.json()
    created_request = fake_ui_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert body["decisionOwner"] == "custom_gpt"
    assert body["source"] == "stake_ui_sgm_via_local_helper"
    assert body["fixtureSlug"] == "46450286-miami-marlins-atlanta-braves"
    assert body["bridge"]["jobId"] == "job-123"
    assert body["bridge"]["status"] == "completed"
    assert body["uiBoard"]["counts"]["playerPropsPlayable"] == 3
    assert "playerProps" not in body["uiBoard"]
    assert "teamMarkets" not in body["uiBoard"]
    assert len(body["uiBoard"]["rows"]) == 6
    assert created_request["fixtureSlug"] == "46450286-miami-marlins-atlanta-braves"
    assert created_request["matchup"] == "Braves vs Marlins"
    assert body["bridge"]["cacheHit"] is False


def test_stake_ui_sgm_board_route_reuses_fresh_completed_ui_job(fake_ui_store):
    fake_ui_store.cached_job = {
        "jobId": "job-cached",
        "status": "completed",
        "workerId": "azp-local-test",
        "result": {
            "source": "stake_ui_sgm",
            "fixtureSlug": "46450286-miami-marlins-atlanta-braves",
            "counts": {"playerPropsPlayable": 1},
            "playerProps": [
                {
                    "team": "Atlanta Braves",
                    "player": "Ronald Acuna Jr.",
                    "market": "Hits",
                    "line": 0.5,
                    "under": 2.1,
                    "over": 1.62,
                    "playable": True,
                }
            ],
            "teamMarkets": [],
        },
        "error": None,
    }

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/sgm-board",
            json={
                "matchup": "Braves vs Marlins",
                "date": "2026-05-19",
                "timeoutSeconds": 2,
            },
        )

    body = response.json()

    assert response.status_code == 200
    assert body["bridge"]["jobId"] == "job-cached"
    assert body["bridge"]["cacheHit"] is True
    assert body["uiBoard"]["returnedRows"] == 2
    assert fake_ui_store.created_jobs == []


def test_stake_ui_review_slip_route_creates_build_job_with_review_only_guardrails():
    fake_store = FakeCompletedBuildJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/review-slip",
            json={
                "matchup": "Braves vs Marlins",
                "date": "2026-05-19",
                "timeoutSeconds": 2,
                "reviewOnly": True,
                "selections": [
                    {
                        "player": "Ronald Acuna Jr.",
                        "team": "Atlanta Braves",
                        "market": "Hits",
                        "side": "under",
                        "line": 0.5,
                        "odds": 2.1,
                    },
                    {
                        "player": "Ozzie Albies",
                        "team": "Atlanta Braves",
                        "market": "Total Bases",
                        "side": "under",
                        "line": 1.5,
                        "odds": 1.8,
                    },
                ],
            },
        )

    body = response.json()
    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert body["source"] == "stake_ui_sgm_review_slip_via_local_helper"
    assert body["result"]["status"] == "built_for_review"
    assert body["result"]["safety"]["clickedPlaceBet"] is False
    assert created_request["reviewOnly"] is True
    assert created_request["forbiddenActions"] == ["enter_stake_amount", "click_place_bet"]
    assert len(created_request["selections"]) == 2


def test_stake_ui_review_slip_route_accepts_row_ids_without_reconstructed_fields():
    fake_store = FakeCompletedBuildJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/review-slip",
            json={
                "matchup": "Braves vs Marlins",
                "fixtureSlug": "46450286-miami-marlins-atlanta-braves",
                "timeoutSeconds": 2,
                "reviewOnly": True,
                "rowIds": ["sgm_row_1", "sgm_row_2"],
            },
        )

    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert created_request["selections"] == [{"rowId": "sgm_row_1"}, {"rowId": "sgm_row_2"}]


def test_stake_ui_review_slip_route_rejects_missing_exact_selection_fields():
    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/review-slip",
            json={
                "matchup": "Braves vs Marlins",
                "date": "2026-05-19",
                "reviewOnly": True,
                "selections": [
                    {
                        "player": "Ronald Acuna Jr.",
                        "market": "Hits",
                        "side": "under",
                        "line": 0.5,
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert "odds" in response.json()["detail"]


def test_stake_ui_sgm_board_route_returns_compact_limited_under_rows(fake_ui_store):
    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/sgm-board",
            json={
                "matchup": "Braves vs Marlins",
                "date": "2026-05-19",
                "timeoutSeconds": 2,
                "side": "under",
                "limit": 2,
            },
        )

    body = response.json()

    assert response.status_code == 200
    assert body["uiBoard"]["filters"]["side"] == "under"
    assert body["uiBoard"]["returnedRows"] == 2
    assert len(body["uiBoard"]["rows"]) == 2
    assert all(row["side"] == "under" for row in body["uiBoard"]["rows"])
    assert all(set(row) >= {"player", "team", "market", "side", "line", "odds"} for row in body["uiBoard"]["rows"])


def test_stake_ui_sgm_board_resolves_slug_only_schedule_names(fake_ui_store):
    app.dependency_overrides[get_stake_client] = lambda: FakeSlugNameStakeClient()

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/sgm-board",
            json={
                "matchup": "Marlins vs Braves",
                "date": "2026-05-20",
                "timeoutSeconds": 2,
            },
        )

    body = response.json()
    created_request = fake_ui_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert body["fixtureSlug"] == "46575343-miami-marlins-atlanta-braves"
    assert created_request["fixtureSlug"] == "46575343-miami-marlins-atlanta-braves"


def test_normalize_sgm_response_marks_only_unsuspended_available_lines_playable():
    raw = {
        "data": {
            "slugFixture": {
                "id": "fixture-1",
                "status": "live",
                "provider": "betradar",
                "swishGame": {"id": "game-1", "status": "InProgress"},
                "swishGameTeams": [
                    {
                        "id": "team-1",
                        "name": "Atlanta Braves",
                        "markets": [],
                        "players": [
                            {
                                "id": "player-1",
                                "name": "Ronald Acuna Jr.",
                                "position": "OF",
                                "markets": [
                                    {
                                        "id": "market-1",
                                        "stat": {
                                            "id": "stat-1",
                                            "type": "player",
                                            "swishStatId": "hits",
                                            "name": "Hits",
                                            "customBet": True,
                                            "liveCustomBetAvailable": True,
                                        },
                                        "lines": [
                                            {
                                                "id": "line-1",
                                                "line": 0.5,
                                                "over": 1.62,
                                                "under": 2.1,
                                                "suspended": False,
                                            },
                                            {
                                                "id": "line-2",
                                                "line": 1.5,
                                                "over": 3.5,
                                                "under": 1.2,
                                                "suspended": True,
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        }
    }

    board = normalize_sgm_response(
        "46450286-miami-marlins-atlanta-braves",
        raw,
        warnings=["browser appears logged out"],
    )

    assert board["source"] == "stake_ui_sgm"
    assert board["fixtureSlug"] == "46450286-miami-marlins-atlanta-braves"
    assert board["counts"]["playerProps"] == 2
    assert board["counts"]["playerPropsPlayable"] == 1
    assert board["playerProps"][0]["player"] == "Ronald Acuna Jr."
    assert board["playerProps"][0]["playable"] is True
    assert board["playerProps"][1]["playable"] is False


def test_match_sgm_review_selections_requires_exact_playable_ui_rows():
    board = {
        "playerProps": [
            {
                "team": "Atlanta Braves",
                "player": "Ronald Acuna Jr.",
                "market": "Hits",
                "line": 0.5,
                "under": 2.1,
                "over": 1.62,
                "playable": True,
                "lineId": "line-1",
            },
            {
                "team": "Atlanta Braves",
                "player": "Ozzie Albies",
                "market": "Hits",
                "line": 1.5,
                "under": 1.4,
                "over": 2.8,
                "playable": False,
                "lineId": "line-2",
            },
        ],
        "teamMarkets": [],
    }

    result = match_sgm_review_selections(
        board,
        [
            {
                "player": "Ronald Acuna Jr.",
                "team": "Atlanta Braves",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 2.1,
            },
            {
                "player": "Ozzie Albies",
                "team": "Atlanta Braves",
                "market": "Hits",
                "side": "under",
                "line": 1.5,
                "odds": 1.4,
            },
        ],
    )

    assert len(result["matchedRows"]) == 1
    assert result["matchedRows"][0]["lineId"] == "line-1"
    assert result["missingSelections"][0]["reason"] == "no exact playable UI row matched"


def test_match_sgm_review_selections_can_match_by_row_id_only():
    board = {
        "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
        "playerProps": [
            {
                "team": "New York Yankees",
                "player": "Austin Wells",
                "scope": "player",
                "market": "Hits",
                "line": 0.5,
                "under": 2.1,
                "over": 1.62,
                "playable": True,
                "lineId": "line-a",
                "marketId": "market-hits",
                "playerId": "player-a",
            },
            {
                "team": "Toronto Blue Jays",
                "player": "George Springer",
                "scope": "player",
                "market": "Hits",
                "line": 0.5,
                "under": 2.1,
                "over": 1.62,
                "playable": True,
                "lineId": "line-b",
                "marketId": "market-hits",
                "playerId": "player-b",
            },
        ],
        "teamMarkets": [],
    }
    compact = _compact_stake_ui_sgm_board(
        board,
        limit=10,
        side="under",
        market="",
        scope="",
        playable_only=True,
    )
    target_row_id = compact["rows"][1]["rowId"]

    result = match_sgm_review_selections(board, [{"rowId": target_row_id}])

    assert result["missingSelections"] == []
    assert len(result["matchedRows"]) == 1
    assert result["matchedRows"][0]["player"] == "George Springer"
    assert result["matchedRows"][0]["side"] == "under"
    assert result["matchedRows"][0]["odds"] == 2.1
    assert result["matchedRows"][0]["rowId"] == target_row_id
