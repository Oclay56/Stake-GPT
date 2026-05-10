from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, get_mlb_engine, get_stake_client


class FakeStakeClient:
    async def get_tournament_schedule(self, sport: str, category: str, tournament: str):
        return {
            "sport": {"slug": sport},
            "schedule": [
                {
                    "date": 1778277600000,
                    "fixtures": [
                        {
                            "slug": "reds-astros",
                            "name": "Cincinnati Reds - Houston Astros",
                            "date": 1778277600000,
                            "status": "active",
                            "type": "match",
                        }
                    ],
                }
            ],
        }

    async def get_odds(self, fixture_slug: str):
        return {
            "fixture": {
                "slug": fixture_slug,
                "name": "Cincinnati Reds - Houston Astros",
                "startTime": 1778277600000,
                "status": "active",
            },
            "groups": [],
            "swishMarkets": {
                "playerProps": [
                    {
                        "competitorName": "Jose Altuve",
                        "teamName": "Houston Astros",
                        "marketName": "hits",
                        "sportStatType": "player",
                        "outcomes": [{"line": 0.5, "over": 1.55, "under": 2.25}],
                    }
                ]
            },
        }


class FakeMLBEngine:
    async def search_players(self, query: str, limit: int = 10):
        return {
            "query": query,
            "playerCount": 1,
            "players": [
                {
                    "mlbId": 514888,
                    "name": query,
                    "key": query.lower().replace(" ", "-"),
                    "team": {"name": "Houston Astros", "key": "houston-astros"},
                }
            ],
        }

    async def get_schedule(self, game_date: str):
        return {
            "date": game_date,
            "games": [
                {
                    "gamePk": 1,
                    "status": "Scheduled",
                    "awayTeam": {"name": "Cincinnati Reds", "key": "cincinnati-reds"},
                    "homeTeam": {"name": "Houston Astros", "key": "houston-astros"},
                }
            ],
        }

    async def get_team_roster(self, team_id: int, season=None):
        return {"players": []}

    async def get_player_profile(self, player_id: int, season=None, group: str = "hitting"):
        return {
            "player": {
                "mlbId": player_id,
                "name": "Jose Altuve",
                "stats": {"gamesPlayed": 10, "hits": 12},
            },
            "season": season,
            "group": group,
        }

    async def get_player_recent_history(
        self,
        player_id: int,
        group: str = "hitting",
        season=None,
        limit: int = 10,
    ):
        return {
            "gamesUsed": min(limit, 5),
            "games": [
                {"stats": {"hits": 1}}
                for _ in range(min(limit, 5))
            ],
            "totals": {"hits": min(limit, 5)},
            "perGame": {"hits": 1.0},
        }


def test_health_route():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_schema_is_gpt_data_only():
    with TestClient(app) as client:
        response = client.get("/gpt/openapi.json")

    schema = response.json()
    assert response.status_code == 200
    assert "/mlb/matchup/{matchup}/props" in schema["paths"]
    assert "/gpt/mlb/matchup-picks" not in schema["paths"]


def test_matchup_props_route_uses_stake_board():
    app.dependency_overrides[get_stake_client] = lambda: FakeStakeClient()
    try:
        with TestClient(app) as client:
            response = client.get(
                "/mlb/matchup/Reds vs Astros/props",
                params={"date": "2026-05-08", "market": "hits", "side": "under"},
            )
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert response.status_code == 200
    assert body["decisionOwner"] == "custom_gpt"
    assert body["selections"][0]["player"]["name"] == "Jose Altuve"
    assert body["selections"][0]["line"] == 0.5


def test_gpt_navigation_routes_are_available():
    app.dependency_overrides[get_stake_client] = lambda: FakeStakeClient()
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMLBEngine()
    try:
        with TestClient(app) as client:
            summary = client.get(
                "/mlb/matchup/Reds vs Astros/board-summary",
                params={"date": "2026-05-08"},
            )
            page = client.get(
                "/mlb/matchup/Reds vs Astros/prop-page",
                params={"date": "2026-05-08", "market": "hits", "side": "under"},
            )
            comparison = client.get(
                "/mlb/matchup/Reds vs Astros/comparison-board",
                params={"date": "2026-05-08", "market": "hits", "side": "under"},
            )
    finally:
        app.dependency_overrides.clear()

    assert summary.status_code == 200
    assert summary.json()["purpose"] == "board_navigation_summary"
    assert page.status_code == 200
    assert page.json()["rows"][0]["player"]["name"] == "Jose Altuve"
    assert comparison.status_code == 200
    assert comparison.json()["rows"][0]["metrics"]["recentAverage"] == 1.0
