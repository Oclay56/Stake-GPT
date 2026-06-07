from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.local_ui_bridge import LocalUiBridgeTimeout, SupabaseLocalUiJobStore, _row_to_job
from app.gpt_action import build_gpt_action_openapi_schema
from app.main import (
    app,
    get_local_ui_job_store,
    get_mlb_engine,
    get_stake_client,
    _compact_stake_ui_sgm_board,
)
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


class _FixedUtcNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 5, 31, 12, 0, 30, tzinfo=timezone.utc)


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


class FakeCompletedMlbMoneylinesJobStore(FakeCompletedUiJobStore):
    async def find_recent_completed_job(self, **kwargs):
        return None

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
                "source": "stake_ui_mlb_moneylines_raw",
                "capturedAt": "2026-05-31T12:00:00Z",
                "url": "https://stake.com/sports/baseball/usa/mlb",
                "games": [
                    {
                        "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                        "matchup": "New York Yankees vs Toronto Blue Jays",
                        "status": "pregame",
                        "marketLabel": "Winner (incl. Extra Innings)",
                        "selections": [
                            {
                                "team": "New York Yankees",
                                "odds": 1.72,
                                "rowId": "mlb_ml_yankees",
                            },
                            {
                                "team": "Toronto Blue Jays",
                                "odds": 2.08,
                                "rowId": "mlb_ml_blue-jays",
                            },
                        ],
                        "warnings": [],
                    }
                ],
                "warnings": [],
            },
            "error": None,
        }


class FakeMoneylineMLBEngine:
    async def get_teams(self, season=None):
        return {
            "teams": [
                {"mlbId": 147, "name": "New York Yankees", "key": "new-york-yankees"},
                {"mlbId": 141, "name": "Toronto Blue Jays", "key": "toronto-blue-jays"},
            ]
        }

    async def get_schedule(self, game_date: str):
        return {
            "games": [
                {
                    "gamePk": 900001,
                    "gameDate": f"{game_date}T17:05:00Z",
                    "status": "Scheduled",
                    "awayTeam": {
                        "mlbId": 147,
                        "name": "New York Yankees",
                        "key": "new-york-yankees",
                        "score": None,
                        "isWinner": None,
                        "probablePitcher": {"mlbId": 1001, "name": "Yankees Starter"},
                    },
                    "homeTeam": {
                        "mlbId": 141,
                        "name": "Toronto Blue Jays",
                        "key": "toronto-blue-jays",
                        "score": None,
                        "isWinner": None,
                        "probablePitcher": {"mlbId": 1002, "name": "Blue Jays Starter"},
                    },
                }
            ]
        }

    async def get_schedule_range(self, start_date: str, end_date: str):
        return {"games": []}

    async def get_standings(self, season: int):
        return {
            "teamsById": {
                147: {
                    "mlbId": 147,
                    "name": "New York Yankees",
                    "key": "new-york-yankees",
                    "wins": 34,
                    "losses": 22,
                    "pct": ".607",
                },
                141: {
                    "mlbId": 141,
                    "name": "Toronto Blue Jays",
                    "key": "toronto-blue-jays",
                    "wins": 29,
                    "losses": 27,
                    "pct": ".518",
                },
            }
        }


class FakeCompletedCandidatePoolJobStore(FakeCompletedUiJobStore):
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
                "source": "stake_ui_sgm_board_batch",
                "fixtureCount": 1,
                "succeeded": 1,
                "failed": 0,
                "boards": [
                    {
                        "source": "stake_ui_sgm",
                        "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                        "capturedAt": "2026-05-20T20:00:00Z",
                        "playerProps": [
                            {
                                "team": "Toronto Blue Jays",
                                "player": "Strong Under",
                                "scope": "player",
                                "market": "Singles",
                                "line": 0.5,
                                "over": 1.35,
                                "under": 2.25,
                                "playable": True,
                                "customBet": True,
                                "liveCustomBetAvailable": True,
                                "marketId": "market-singles",
                                "lineId": "line-strong-under-singles",
                                "swishStatId": 302,
                                "playerId": "swish-1001",
                            }
                        ],
                        "teamMarkets": [],
                    }
                ],
                "errors": [],
            },
            "error": None,
        }


class FakeCandidatePoolMLBEngine:
    async def search_players(self, query: str, limit: int = 10):
        if query != "Strong Under":
            return {"players": []}
        return {
            "players": [
                {
                    "mlbId": 1001,
                    "name": "Strong Under",
                    "key": "strong-under",
                    "team": {
                        "mlbId": 141,
                        "name": "Toronto Blue Jays",
                        "key": "toronto-blue-jays",
                    },
                }
            ]
        }

    async def get_player_profile(self, player_id: int, season=None, group: str = "hitting"):
        return {
            "player": {
                "mlbId": player_id,
                "name": "Strong Under",
                "key": "strong-under",
                "stats": {
                    "hits": 40,
                    "doubles": 12,
                    "triples": 1,
                    "homeRuns": 5,
                    "gamesPlayed": 50,
                },
            },
            "season": season,
            "group": group,
        }

    async def get_player_recent_history(self, player_id: int, group: str = "hitting", season=None, limit: int = 15):
        values = [0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1]
        return {
            "playerId": player_id,
            "group": group,
            "gamesUsed": limit,
            "games": [
                {"stats": {"hits": value, "doubles": 0, "triples": 0, "homeRuns": 0}}
                for value in values[:limit]
            ],
        }

    async def get_schedule(self, game_date: str):
        return {
            "date": game_date,
            "games": [
                {
                    "gamePk": 1,
                    "awayTeam": {
                        "mlbId": 141,
                        "name": "Toronto Blue Jays",
                        "key": "toronto-blue-jays",
                    },
                    "homeTeam": {
                        "mlbId": 147,
                        "name": "New York Yankees",
                        "key": "new-york-yankees",
                    },
                }
            ],
        }

    async def get_team_roster(self, team_id: int, season=None):
        return {"teamId": team_id, "players": []}


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


class FakeTimeoutBuildJobStore(FakeCompletedUiJobStore):
    async def wait_for_completed_result(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: float = 1.0,
    ):
        assert job_id == "job-123"
        raise LocalUiBridgeTimeout("Timed out waiting for the local helper.")


class FakeCompletedStateJobStore(FakeCompletedUiJobStore):
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
                "source": "stake_ui_state",
                "capturedAt": "2026-05-20T20:00:00Z",
                "status": "ok",
                "url": "https://stake.com/sports/baseball/usa/mlb/46575351-new-york-yankees-toronto-blue-jays",
                "currentFixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                "sgmVisible": True,
                "access": {
                    "regionBlocked": False,
                    "cloudflareRequired": False,
                    "loginRequired": False,
                },
                "slip": {
                    "rightPanelFound": True,
                    "rightPanelEmpty": False,
                    "rightPanelSelectionCount": 2,
                },
                "warnings": [],
            },
            "error": None,
        }


class FakeCompletedClearSelectionsJobStore(FakeCompletedUiJobStore):
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
                "source": "stake_ui_sgm_clear_selections",
                "status": "cleared",
                "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                "sgmVisible": True,
                "clearedWorkingSelection": True,
                "slip": {
                    "rightPanelFound": True,
                    "rightPanelEmpty": False,
                    "rightPanelSelectionCount": 2,
                },
            },
            "error": None,
        }


class FakeCompletedRemoveSidebarGroupJobStore(FakeCompletedUiJobStore):
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
                "source": "stake_ui_remove_sidebar_group",
                "capturedAt": "2026-05-20T20:00:00Z",
                "status": "removed",
                "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                "matchup": "New York Yankees vs Toronto Blue Jays",
                "teams": ["New York Yankees", "Toronto Blue Jays"],
                "removeResult": {
                    "status": "clicked",
                    "targetStillVisible": False,
                },
                "slip": {
                    "rightPanelFound": True,
                    "rightPanelEmpty": False,
                    "rightPanelSelectionCount": 2,
                },
                "safety": {
                    "enteredStakeAmount": False,
                    "clickedPlaceBet": False,
                    "removedSidebarGroupOnly": True,
                },
            },
            "error": None,
        }


class FakeCompletedClearSidebarJobStore(FakeCompletedUiJobStore):
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
                "source": "stake_ui_clear_sidebar",
                "capturedAt": "2026-05-20T20:00:00Z",
                "status": "cleared",
                "clearResult": {
                    "status": "clicked",
                    "clickedButtonText": "clear bets",
                },
                "slip": {
                    "rightPanelFound": True,
                    "rightPanelEmpty": True,
                    "rightPanelSelectionCount": 0,
                },
                "safety": {
                    "enteredStakeAmount": False,
                    "clickedPlaceBet": False,
                    "clearedEntireSidebar": True,
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
    assert properties["rowIds"]["minItems"] == 2
    assert properties["requiredLegs"]["minimum"] == 2
    selection_schema = properties["selections"]["items"]
    assert "rowId" in selection_schema["properties"]


def test_gpt_schema_exposes_stake_ui_mlb_games_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operation = schema["paths"]["/mlb/stake-ui/mlb-games"]["post"]

    assert operation["operationId"] == "getStakeUiMlbGames"
    assert "MLB" in operation["summary"]


def test_gpt_schema_exposes_stake_ui_mlb_moneylines_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operation = schema["paths"]["/mlb/stake-ui/mlb-moneylines"]["post"]
    properties = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]

    assert operation["operationId"] == "getStakeUiMlbMoneylines"
    assert "fixtureSlugs" in properties
    assert "matchups" in properties
    assert "date" in properties


def test_gpt_schema_exposes_stake_ui_mlb_moneyline_review_slip_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operation = schema["paths"]["/mlb/stake-ui/moneyline-review-slip"]["post"]
    properties = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]

    assert operation["operationId"] == "buildStakeUiMoneylineReviewSlip"
    assert properties["reviewOnly"]["const"] is True
    assert properties["selections"]["items"]["properties"]["rowId"]["type"] == "string"
    assert properties["selections"]["items"]["properties"]["fixtureSlug"]["type"] == "string"
    assert properties["selections"]["items"]["properties"]["team"]["type"] == "string"


def test_gpt_schema_exposes_stake_ui_sgm_candidate_pool_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operation = schema["paths"]["/mlb/stake-ui/sgm-candidate-pool"]["post"]

    assert operation["operationId"] == "buildStakeUiSgmCandidatePool"
    properties = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "fixtureSlugs" in properties
    assert "compact" in properties
    assert "maxSgmGroupOdds" in properties
    assert properties["mode"]["enum"] == [
        "best_available",
        "safe",
        "balanced",
        "longshot",
        "per_game",
    ]
    assert properties["legsPerGame"]["minimum"] == 2
    assert properties["maxCandidatesPerGame"]["minimum"] == 2


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
    assert group_schema["properties"]["rowIds"]["minItems"] == 2
    assert group_schema["properties"]["requiredLegs"]["minimum"] == 2
    assert "rowId" in group_schema["properties"]["selections"]["items"]["properties"]
    assert "continueOnGroupFailure" in properties
    assert "minGroupsRequired" in properties


def test_gpt_schema_exposes_optional_stake_ui_state_actions():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    state_operation = schema["paths"]["/mlb/stake-ui/state"]["post"]
    clear_operation = schema["paths"]["/mlb/stake-ui/clear-sgm-selections"]["post"]
    remove_operation = schema["paths"]["/mlb/stake-ui/remove-sidebar-group"]["post"]
    clear_sidebar_operation = schema["paths"]["/mlb/stake-ui/clear-sidebar"]["post"]

    assert state_operation["operationId"] == "readStakeUiState"
    assert clear_operation["operationId"] == "clearStakeUiSgmSelections"
    assert remove_operation["operationId"] == "removeStakeUiSidebarGroup"
    assert clear_sidebar_operation["operationId"] == "clearStakeUiSidebar"
    properties = clear_sidebar_operation["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert properties["reviewOnly"]["const"] is True


def test_local_ui_bridge_row_to_job_clamps_completed_at_before_created_at():
    job = _row_to_job(
        {
            "job_id": "job-123",
            "job_type": "stake_sgm_board",
            "status": "completed",
            "request_json": {},
            "result_json": {},
            "created_at": "2026-05-22T15:48:17.352361+00:00",
            "completed_at": "2026-05-22T15:48:14.575827+00:00",
            "updated_at": "2026-05-22T15:48:14.575827+00:00",
        }
    )

    assert job["completedAt"] == job["createdAt"]


def test_local_ui_bridge_finds_recent_completed_job_by_cache_key(monkeypatch):
    async def fake_request(*args, **kwargs):
        return [
            {
                "job_id": "job-moneylines",
                "job_type": "stake_ui_mlb_moneylines",
                "status": "completed",
                "request_json": {"cacheKey": "mlb-moneylines:2026-05-31:50"},
                "result_json": {"source": "stake_ui_mlb_moneylines_raw"},
                "created_at": "2026-05-31T12:00:00+00:00",
                "completed_at": "2026-05-31T12:00:01+00:00",
                "updated_at": "2026-05-31T12:00:01+00:00",
            }
        ]

    monkeypatch.setattr("app.local_ui_bridge.datetime", _FixedUtcNow)
    store = SupabaseLocalUiJobStore(
        supabase_url="https://example.supabase.co",
        service_key="secret",
    )
    monkeypatch.setattr(store, "_request", fake_request)

    cached = asyncio.run(
        store.find_recent_completed_job(
            job_type="stake_ui_mlb_moneylines",
            cache_key="mlb-moneylines:2026-05-31:50",
            max_age_seconds=60,
        )
    )

    assert cached["jobType"] == "stake_ui_mlb_moneylines"


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


def test_stake_ui_mlb_moneylines_route_returns_enriched_read_only_board():
    fake_store = FakeCompletedMlbMoneylinesJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMoneylineMLBEngine()

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/mlb-moneylines",
            json={
                "date": "2026-05-31",
                "fixtureSlugs": ["123-new-york-yankees-toronto-blue-jays"],
                "timeoutSeconds": 2,
                "limit": 50,
            },
        )

    result = response.json()
    created = fake_store.created_jobs[0]

    assert response.status_code == 200
    assert created["jobType"] == "stake_ui_mlb_moneylines"
    assert created["request"]["purpose"] == "stake_ui_mlb_moneyline_research"
    assert result["source"] == "stake_ui_mlb_moneylines"
    assert result["decisionOwner"] == "custom_gpt"
    assert result["builderRole"] == "read_only_moneyline_research_not_final_recommendation"
    assert result["games"][0]["selections"][0]["teamContext"]["mlbTeamId"] == 147
    assert result["bridge"]["cacheHit"] is False


def test_stake_ui_mlb_moneyline_review_slip_route_creates_helper_job():
    class FakeMoneylineBuildJobStore(FakeCompletedMlbMoneylinesJobStore):
        async def create_job(self, *, job_type, request, timeout_seconds):
            self.created_jobs.append(
                {
                    "jobId": "job-moneyline-build",
                    "jobType": job_type,
                    "request": request,
                    "timeoutSeconds": timeout_seconds,
                }
            )
            return {"jobId": "job-moneyline-build"}

        async def wait_for_completed_result(self, job_id, *, timeout_seconds):
            return {
                "jobId": job_id,
                "status": "completed",
                "workerId": "helper-1",
                "createdAt": "2026-05-31T12:00:00+00:00",
                "completedAt": "2026-05-31T12:00:01+00:00",
                "result": {
                    "source": "stake_ui_mlb_moneyline_review_slip",
                    "status": "built_for_review",
                    "reviewOnly": True,
                    "requestedSelections": 1,
                    "addedSelections": [
                        {
                            "rowId": "mlb_ml_yankees",
                            "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                            "team": "New York Yankees",
                        }
                    ],
                    "alreadyPresentSelections": [],
                    "remainingSelections": [],
                    "safety": {
                        "enteredStakeAmount": False,
                        "clickedPlaceBet": False,
                    },
                },
            }

    fake_store = FakeMoneylineBuildJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/moneyline-review-slip",
            json={
                "reviewOnly": True,
                "timeoutSeconds": 2,
                "selections": [
                    {
                        "rowId": "mlb_ml_yankees",
                        "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                        "team": "New York Yankees",
                        "odds": 1.72,
                    }
                ],
            },
        )

    result = response.json()
    created = fake_store.created_jobs[0]

    assert response.status_code == 200
    assert created["jobType"] == "stake_ui_mlb_moneyline_build_slip"
    assert created["request"]["purpose"] == "stake_ui_mlb_moneyline_review_slip"
    assert created["request"]["forbiddenActions"] == ["enter_stake_amount", "click_place_bet"]
    assert result["source"] == "stake_ui_mlb_moneyline_review_slip_via_local_helper"
    assert result["result"]["status"] == "built_for_review"


def test_stake_ui_sgm_candidate_pool_returns_ranked_support_rows():
    fake_store = FakeCompletedCandidatePoolJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store
    app.dependency_overrides[get_mlb_engine] = lambda: FakeCandidatePoolMLBEngine()

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/sgm-candidate-pool",
            json={
                "date": "2026-05-25",
                "fixtureSlugs": ["46575351-new-york-yankees-toronto-blue-jays"],
                "side": "under",
                "markets": "singles",
                "qualityFloor": 50,
                "timeoutSeconds": 45,
            },
        )

    result = response.json()

    assert response.status_code == 200
    assert result["source"] == "stake_ui_sgm_candidate_pool"
    assert result["decisionOwner"] == "custom_gpt"
    assert result["builderRole"] == "candidate_support_not_final_recommendation"
    assert result["rankedCandidates"][0]["rowId"].startswith("sgm_")
    assert result["rankedCandidates"][0]["normalizedMarketKey"] == "singles"
    assert result["rankedCandidates"][0]["mlbPersonId"] == 1001
    assert fake_store.created_jobs[0]["jobType"] == "stake_ui_sgm_board_batch"


def test_stake_ui_sgm_candidate_pool_compact_mode_trims_nested_context():
    fake_store = FakeCompletedCandidatePoolJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store
    app.dependency_overrides[get_mlb_engine] = lambda: FakeCandidatePoolMLBEngine()

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/sgm-candidate-pool",
            json={
                "date": "2026-05-25",
                "fixtureSlugs": ["46575351-new-york-yankees-toronto-blue-jays"],
                "side": "under",
                "markets": "singles",
                "qualityFloor": 50,
                "compact": True,
                "timeoutSeconds": 45,
            },
        )

    result = response.json()
    row = result["rankedCandidates"][0]

    assert response.status_code == 200
    assert result["compact"] is True
    assert set(row) == {
        "fixtureSlug",
        "matchup",
        "rowId",
        "player",
        "team",
        "market",
        "side",
        "line",
        "odds",
        "contextQuality",
        "score",
        "marketContestRank",
        "gameContestRank",
        "selectedMarket",
        "selectedScore",
        "marketsCompared",
        "closestAlternativeMarket",
        "closestAlternativeScore",
        "whySelectedBeatAlternative",
        "availabilityRole",
        "reasonTags",
        "riskFlags",
    }
    assert len(row["reasonTags"]) <= 3
    assert row["availabilityRole"] == "eligibility_only"
    assert row["selectedMarket"] == row["market"]
    assert "context" not in row
    assert "last15" not in row


def test_stake_ui_review_slip_batch_route_creates_one_batch_job_with_guardrails():
    fake_store = FakeCompletedBatchBuildJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/review-slip-batch",
            json={
                "reviewOnly": True,
                "timeoutSeconds": 2,
                "continueOnGroupFailure": True,
                "minGroupsRequired": 1,
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
    assert created_request["continueOnGroupFailure"] is True
    assert created_request["minGroupsRequired"] == 1
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


def test_stake_ui_review_slip_batch_route_rejects_one_leg_group():
    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/review-slip-batch",
            json={
                "reviewOnly": True,
                "groups": [
                    {
                        "matchup": "Yankees vs Blue Jays",
                        "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                        "rowIds": ["sgm_abc123"],
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert "at least 2" in response.json()["detail"]


def test_stake_ui_state_route_creates_diagnostic_job():
    fake_store = FakeCompletedStateJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/state",
            json={
                "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                "timeoutSeconds": 2,
            },
        )

    body = response.json()
    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert body["source"] == "stake_ui_state_via_local_helper"
    assert body["purpose"] == "stake_ui_diagnostics"
    assert body["state"]["currentFixtureSlug"] == "46575351-new-york-yankees-toronto-blue-jays"
    assert body["state"]["sgmVisible"] is True
    assert created_request["purpose"] == "stake_ui_diagnostics"


def test_stake_ui_clear_sgm_selections_route_creates_recovery_job():
    fake_store = FakeCompletedClearSelectionsJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/clear-sgm-selections",
            json={
                "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                "timeoutSeconds": 2,
            },
        )

    body = response.json()
    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert body["source"] == "stake_ui_sgm_clear_selections_via_local_helper"
    assert body["result"]["status"] == "cleared"
    assert body["result"]["clearedWorkingSelection"] is True
    assert created_request["purpose"] == "stake_ui_sgm_recovery_clear_selection"


def test_stake_ui_remove_sidebar_group_route_creates_safe_recovery_job():
    fake_store = FakeCompletedRemoveSidebarGroupJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/remove-sidebar-group",
            json={
                "matchup": "Yankees vs Blue Jays",
                "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                "timeoutSeconds": 2,
                "reviewOnly": True,
            },
        )

    body = response.json()
    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert body["source"] == "stake_ui_remove_sidebar_group_via_local_helper"
    assert body["purpose"] == "stake_ui_review_slip_sidebar_removal"
    assert body["result"]["status"] == "removed"
    assert body["result"]["safety"]["clickedPlaceBet"] is False
    assert body["result"]["safety"]["removedSidebarGroupOnly"] is True
    assert created_request["purpose"] == "stake_ui_remove_sidebar_group"
    assert created_request["reviewOnly"] is True
    assert created_request["forbiddenActions"] == ["enter_stake_amount", "click_place_bet"]


def test_stake_ui_remove_sidebar_group_route_passes_moneyline_identity():
    fake_store = FakeCompletedRemoveSidebarGroupJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/remove-sidebar-group",
            json={
                "rowId": "mlb_ml_yankees",
                "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
                "team": "New York Yankees",
                "timeoutSeconds": 2,
                "reviewOnly": True,
            },
        )

    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert created_request["rowId"] == "mlb_ml_yankees"
    assert created_request["team"] == "New York Yankees"


def test_stake_ui_clear_sidebar_route_creates_safe_recovery_job():
    fake_store = FakeCompletedClearSidebarJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/clear-sidebar",
            json={
                "timeoutSeconds": 2,
                "reviewOnly": True,
            },
        )

    body = response.json()
    created_request = fake_store.created_jobs[0]["request"]

    assert response.status_code == 200
    assert body["source"] == "stake_ui_clear_sidebar_via_local_helper"
    assert body["purpose"] == "stake_ui_review_slip_sidebar_clear"
    assert body["result"]["status"] == "cleared"
    assert body["result"]["safety"]["clickedPlaceBet"] is False
    assert body["result"]["safety"]["clearedEntireSidebar"] is True
    assert created_request["purpose"] == "stake_ui_clear_sidebar"
    assert created_request["reviewOnly"] is True
    assert created_request["forbiddenActions"] == ["enter_stake_amount", "click_place_bet"]


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


def test_stake_ui_review_slip_timeout_returns_structured_terminal_state():
    fake_store = FakeTimeoutBuildJobStore()
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

    body = response.json()

    assert response.status_code == 504
    assert body["detail"]["status"] == "timeout"
    assert body["detail"]["phase"] == "local_helper_wait"
    assert body["detail"]["clickedLegs"] == 0
    assert body["detail"]["lastKnownFixtureSlug"] == "46450286-miami-marlins-atlanta-braves"


def test_stake_ui_review_slip_route_rejects_one_leg_sgm_group():
    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/review-slip",
            json={
                "matchup": "Braves vs Marlins",
                "fixtureSlug": "46450286-miami-marlins-atlanta-braves",
                "reviewOnly": True,
                "rowIds": ["sgm_row_1"],
            },
        )

    assert response.status_code == 422
    assert "at least 2" in response.json()["detail"]


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
                    },
                    {
                        "player": "Ozzie Albies",
                        "market": "Total Bases",
                        "side": "under",
                        "line": 1.5,
                        "odds": 1.8,
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
                                        "trading": {"betFactor": 0.85},
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
                                                "push": None,
                                                "balanced": True,
                                                "suspended": False,
                                            },
                                            {
                                                "id": "line-2",
                                                "line": 1.5,
                                                "over": 3.5,
                                                "under": 1.2,
                                                "balanced": False,
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
    assert board["playerProps"][0]["nonPlayableReasons"] == []
    assert board["playerProps"][0]["betFactor"] == 0.85
    assert board["playerProps"][0]["balanced"] is True
    assert board["playerProps"][1]["playable"] is False
    assert board["playerProps"][1]["nonPlayableReasons"] == ["suspended"]
    assert board["marketCatalog"]["markets"][0]["market"] == "Hits"
    assert board["marketCatalog"]["markets"][0]["playableRowCount"] == 1


def test_normalize_sgm_response_allows_pregame_custom_bet_rows_without_live_flag():
    raw = {
        "data": {
            "slugFixture": {
                "id": "fixture-1",
                "status": "active",
                "provider": "betradar",
                "swishGame": {"id": "game-1", "status": "PreGame"},
                "swishGameTeams": [
                    {
                        "id": "team-1",
                        "name": "Pittsburgh Pirates",
                        "markets": [],
                        "players": [
                            {
                                "id": "player-1",
                                "name": "Nick Gonzales",
                                "position": "2B",
                                "markets": [
                                    {
                                        "id": "market-singles",
                                        "stat": {
                                            "id": "stat-singles",
                                            "type": "player",
                                            "swishStatId": 302,
                                            "name": "Singles",
                                            "customBet": True,
                                            "liveCustomBetAvailable": False,
                                        },
                                        "lines": [
                                            {
                                                "id": "line-singles",
                                                "line": 0.5,
                                                "over": 2.65,
                                                "under": 1.42,
                                                "suspended": False,
                                            }
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

    board = normalize_sgm_response("fixture-1", raw)
    row = board["playerProps"][0]
    diagnostics = {
        item["market"]: item for item in board["marketDiagnostics"]["playerTargets"]
    }

    assert row["playable"] is True
    assert row["liveCustomBetAvailable"] is False
    assert row["nonPlayableReasons"] == []
    assert row["playabilityMode"] == "pregame_custom_bet"
    assert row["playabilityWarnings"] == ["liveCustomBetAvailable_false"]
    assert diagnostics["singles"]["status"] == "market_parsed_with_row_id"
    assert diagnostics["singles"]["rowIdCount"] == 2
    assert diagnostics["singles"]["sampleRows"][0]["rowIds"]["under"].startswith("sgm_")


def test_normalize_sgm_response_keeps_live_rows_blocked_without_live_flag():
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
                        "name": "Pittsburgh Pirates",
                        "markets": [],
                        "players": [
                            {
                                "id": "player-1",
                                "name": "Nick Gonzales",
                                "position": "2B",
                                "markets": [
                                    {
                                        "id": "market-singles",
                                        "stat": {
                                            "id": "stat-singles",
                                            "type": "player",
                                            "swishStatId": 302,
                                            "name": "Singles",
                                            "customBet": True,
                                            "liveCustomBetAvailable": False,
                                        },
                                        "lines": [
                                            {
                                                "id": "line-singles",
                                                "line": 0.5,
                                                "over": 2.65,
                                                "under": 1.42,
                                                "suspended": False,
                                            }
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

    board = normalize_sgm_response("fixture-1", raw)
    row = board["playerProps"][0]
    diagnostics = {
        item["market"]: item for item in board["marketDiagnostics"]["playerTargets"]
    }

    assert row["playable"] is False
    assert row["nonPlayableReasons"] == ["liveCustomBetAvailable_false"]
    assert row["playabilityMode"] == "blocked"
    assert diagnostics["singles"]["status"] == "market_parsed_not_playable"
    assert diagnostics["singles"]["rowIdCount"] == 0


def test_sgm_board_market_filter_supports_batter_and_steal_aliases():
    board = {
        "fixtureSlug": "fixture-1",
        "playerProps": [
            {
                "team": "Atlanta Braves",
                "player": "Hitter One",
                "position": "CF",
                "playerId": "hitter-1",
                "scope": "player",
                "market": "Strikeouts",
                "line": 0.5,
                "under": 1.82,
                "over": 1.91,
                "playable": True,
                "marketId": "market-hitter-k",
                "lineId": "line-hitter-k",
                "swishStatId": "strikeouts",
            },
            {
                "team": "Atlanta Braves",
                "player": "Pitcher One",
                "position": "P",
                "playerId": "pitcher-1",
                "scope": "player",
                "market": "Strikeouts",
                "line": 4.5,
                "under": 2.1,
                "over": 1.7,
                "playable": True,
                "marketId": "market-pitcher-k",
                "lineId": "line-pitcher-k",
                "swishStatId": "strikeouts",
            },
            {
                "team": "Atlanta Braves",
                "player": "Speedster One",
                "position": "SS",
                "playerId": "speed-1",
                "scope": "player",
                "market": "Steals",
                "line": 0.5,
                "under": 1.25,
                "over": 3.4,
                "playable": True,
                "marketId": "market-steals",
                "lineId": "line-steals",
                "swishStatId": "steals",
            },
        ],
        "teamMarkets": [],
    }

    batter_ks = _compact_stake_ui_sgm_board(
        board,
        limit=10,
        side="under",
        market="batter strikeouts",
        scope="",
        playable_only=True,
    )["rows"]
    steals = _compact_stake_ui_sgm_board(
        board,
        limit=10,
        side="over",
        market="stolen bases",
        scope="",
        playable_only=True,
    )["rows"]

    assert [row["player"] for row in batter_ks] == ["Hitter One"]
    assert batter_ks[0]["rowId"].startswith("sgm_")
    assert [row["player"] for row in steals] == ["Speedster One"]
    assert steals[0]["rowId"].startswith("sgm_")


def test_normalize_sgm_response_reports_target_player_market_diagnostics():
    raw = {
        "data": {
            "slugFixture": {
                "id": "fixture-1",
                "status": "active",
                "provider": "betradar",
                "swishGame": {"id": "game-1", "status": "PreGame"},
                "swishGameTeams": [
                    {
                        "id": "team-1",
                        "name": "Atlanta Braves",
                        "markets": [],
                        "players": [
                            {
                                "id": "player-1",
                                "name": "Hitter One",
                                "position": "CF",
                                "markets": [
                                    {
                                        "id": "market-singles",
                                        "stat": {
                                            "id": "stat-singles",
                                            "type": "player",
                                            "swishStatId": "singles",
                                            "name": "Singles",
                                            "customBet": True,
                                            "liveCustomBetAvailable": True,
                                        },
                                        "lines": [
                                            {
                                                "id": "line-singles",
                                                "line": 0.5,
                                                "over": 2.6,
                                                "under": 1.42,
                                                "suspended": False,
                                            }
                                        ],
                                    },
                                    {
                                        "id": "market-steals",
                                        "stat": {
                                            "id": "stat-steals",
                                            "type": "player",
                                            "swishStatId": "steals",
                                            "name": "Steals",
                                            "customBet": False,
                                            "liveCustomBetAvailable": True,
                                        },
                                        "lines": [
                                            {
                                                "id": None,
                                                "line": 0.5,
                                                "over": 3.1,
                                                "under": 1.3,
                                                "suspended": False,
                                            }
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        }
    }

    board = normalize_sgm_response(
        "fixture-1",
        raw,
        visible_market_text="Same Game Multi Batter Walks",
    )
    diagnostics = {
        item["market"]: item for item in board["marketDiagnostics"]["playerTargets"]
    }

    assert diagnostics["singles"]["status"] == "market_parsed_with_row_id"
    assert diagnostics["singles"]["rowIdCount"] == 2
    assert diagnostics["stolen bases"]["status"] == "market_parsed_not_playable"
    assert diagnostics["stolen bases"]["sampleRows"][0]["nonPlayableReasons"] == [
        "customBet_false"
    ]
    assert diagnostics["stolen bases"]["sampleRows"][0]["identifierWarnings"] == [
        "missing_line_id"
    ]
    assert diagnostics["stolen bases"]["sampleRows"][0]["rowIds"] == {}
    assert diagnostics["batter walks"]["status"] == "market_visible_but_not_parsed"
    assert diagnostics["batter strikeouts"]["status"] == "market_not_offered"


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
