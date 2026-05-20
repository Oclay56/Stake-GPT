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


class FakeSlugNameStakeClient:
    async def get_tournament_schedule(self, sport: str, category: str, tournament: str):
        return {
            "sport": {"slug": sport, "name": "Baseball"},
            "schedule": [
                {
                    "date": 1778277600000,
                    "fixtures": [
                        {
                            "slug": "46450286-toronto-blue-jays-los-angeles-angels",
                            "name": "46450286-toronto-blue-jays-los-angeles-angels",
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


def test_mlb_schedule_stake_map_links_slug_only_stake_fixture(tmp_path):
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMLBEngine()
    app.dependency_overrides[get_stake_client] = lambda: FakeSlugNameStakeClient()
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
    assert body["games"][0]["stake"]["available"] is True
    assert (
        body["games"][0]["stake"]["fixtureSlug"]
        == "46450286-toronto-blue-jays-los-angeles-angels"
    )
    assert body["games"][0]["stake"]["teams"] == [
        "Toronto Blue Jays",
        "Los Angeles Angels",
    ]
