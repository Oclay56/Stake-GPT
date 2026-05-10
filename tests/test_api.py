from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, get_stake_client


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
