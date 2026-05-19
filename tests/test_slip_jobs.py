from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, get_gpt_store, get_mlb_engine, get_stake_client
from app.storage import GptActionStore


class FakeStakeClient:
    async def get_tournament_schedule(self, sport: str, category: str, tournament: str):
        return {
            "sport": {"slug": sport, "name": "Baseball"},
            "schedule": [
                {
                    "date": 1778277600000,
                    "fixtures": [
                        {
                            "slug": "blue-jays-angels",
                            "name": "Toronto Blue Jays - Los Angeles Angels",
                            "date": 1778277600000,
                            "status": "active",
                            "type": "match",
                        }
                    ],
                }
            ],
        }


class FakeMLBEngine:
    async def get_schedule(self, game_date: str):
        return {
            "date": game_date,
            "gameCount": 1,
            "games": [
                {
                    "gamePk": 1,
                    "gameDate": f"{game_date}T23:07:00Z",
                    "status": "Scheduled",
                    "awayTeam": {
                        "mlbId": 141,
                        "name": "Toronto Blue Jays",
                        "key": "toronto-blue-jays",
                        "probablePitcher": {
                            "mlbId": 702056,
                            "name": "Trey Yesavage",
                            "key": "trey-yesavage",
                        },
                    },
                    "homeTeam": {
                        "mlbId": 108,
                        "name": "Los Angeles Angels",
                        "key": "los-angeles-angels",
                        "probablePitcher": {
                            "mlbId": 686799,
                            "name": "Jack Kochanowicz",
                            "key": "jack-kochanowicz",
                        },
                    },
                }
            ],
        }


def test_mlb_schedule_route_returns_official_mlb_games(tmp_path):
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMLBEngine()
    app.dependency_overrides[get_gpt_store] = lambda: GptActionStore(
        tmp_path / "gpt.sqlite"
    )
    try:
        with TestClient(app) as client:
            response = client.get("/mlb/schedule", params={"date": "2026-05-08"})
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert response.status_code == 200
    assert body["source"] == "mlb_stats_api"
    assert body["gameCount"] == 1
    assert body["games"][0]["gamePk"] == 1
    assert body["games"][0]["matchup"] == "Toronto Blue Jays vs Los Angeles Angels"
    assert body["games"][0]["probablePitchers"]["away"]["name"] == "Trey Yesavage"


def test_mlb_schedule_stake_map_links_official_game_to_stake_fixture(tmp_path):
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMLBEngine()
    app.dependency_overrides[get_stake_client] = lambda: FakeStakeClient()
    app.dependency_overrides[get_gpt_store] = lambda: GptActionStore(
        tmp_path / "gpt.sqlite"
    )
    try:
        with TestClient(app) as client:
            response = client.get(
                "/mlb/schedule/stake-map",
                params={"date": "2026-05-08"},
            )
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert response.status_code == 200
    assert body["source"] == "mlb_stats_api_plus_stake_odds_api"
    assert body["games"][0]["stake"]["available"] is True
    assert body["games"][0]["stake"]["fixtureSlug"] == "blue-jays-angels"
    assert body["games"][0]["stake"]["name"] == "Toronto Blue Jays - Los Angeles Angels"


def test_slip_job_lifecycle_routes_create_claim_and_update(tmp_path):
    store = GptActionStore(tmp_path / "gpt.sqlite")
    app.dependency_overrides[get_gpt_store] = lambda: store
    try:
        with TestClient(app) as client:
            create_response = client.post(
                "/slip-jobs",
                json={
                    "source": "custom_gpt",
                    "prompt": "Build one six-leg under slip.",
                    "slipType": "same_game",
                    "matchup": "Blue Jays vs Angels",
                    "date": "2026-05-08",
                    "mode": "mega_under",
                    "target": {"legCount": 6, "side": "under"},
                    "selections": [
                        {
                            "selectionId": "sel-1",
                            "player": {"name": "George Springer"},
                            "market": {"key": "hits", "name": "Hits"},
                            "side": "under",
                            "line": 0.5,
                            "odds": 2.9,
                        }
                    ],
                },
            )
            next_response = client.get(
                "/slip-jobs/next",
                params={"bridgeId": "local-bridge-test"},
            )
            job_id = create_response.json()["jobId"]
            status_response = client.post(
                f"/slip-jobs/{job_id}/status",
                json={
                    "status": "dry_run_ready",
                    "bridgeId": "local-bridge-test",
                    "message": "Stake UI dry-run completed.",
                    "result": {"matched": 1, "blocked": 0},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert create_response.status_code == 200
    assert create_response.json()["status"] == "pending"
    assert create_response.json()["legCount"] == 1
    assert next_response.status_code == 200
    assert next_response.json()["job"]["jobId"] == create_response.json()["jobId"]
    assert next_response.json()["job"]["status"] == "claimed"
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "dry_run_ready"
