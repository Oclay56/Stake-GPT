import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.gpt_action import (
    build_gpt_action_openapi_schema,
    build_matchup_picks,
    require_gpt_api_key_value,
)
from app.main import app, get_mlb_engine, get_stake_client


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
                        },
                        {
                            "slug": "reds-astros",
                            "name": "Cincinnati Reds - Houston Astros",
                            "date": 1778277600000,
                            "status": "active",
                            "type": "match",
                        },
                    ],
                }
            ],
        }

    async def get_odds(self, fixture_slug: str):
        fixture_names = {
            "blue-jays-angels": "Toronto Blue Jays - Los Angeles Angels",
            "reds-astros": "Cincinnati Reds - Houston Astros",
        }
        props = {
            "blue-jays-angels": [
                {
                    "competitorName": "George Springer",
                    "teamName": "Toronto Blue Jays",
                    "marketName": "hits",
                    "sportStatType": "player",
                    "outcomes": [
                        {"line": 1.5, "over": 2.57, "under": 1.35},
                        {"line": 0.5, "over": 1.34, "under": 2.9},
                    ],
                },
                {
                    "competitorName": "Vladimir Guerrero Jr.",
                    "teamName": "Toronto Blue Jays",
                    "marketName": "hits",
                    "sportStatType": "player",
                    "outcomes": [{"line": 0.5, "over": 1.62, "under": 2.1}],
                },
                {
                    "competitorName": "Mike Trout",
                    "teamName": "Los Angeles Angels",
                    "marketName": "hits",
                    "sportStatType": "player",
                    "outcomes": [{"line": 0.5, "over": 1.74, "under": 1.95}],
                },
                {
                    "competitorName": "Walbert Urena",
                    "teamName": "Los Angeles Angels",
                    "marketName": "strikeouts",
                    "sportStatType": "player",
                    "outcomes": [{"line": 0.5, "over": 2.24, "under": 1.55}],
                },
                {
                    "competitorName": "Jack Kochanowicz",
                    "teamName": "Los Angeles Angels",
                    "marketName": "strikeouts",
                    "sportStatType": "player",
                    "outcomes": [{"line": 0.5, "over": 1.82, "under": 1.9}],
                },
            ],
            "reds-astros": [
                {
                    "competitorName": "Jose Altuve",
                    "teamName": "Houston Astros",
                    "marketName": "hits",
                    "sportStatType": "player",
                    "outcomes": [{"line": 0.5, "over": 1.55, "under": 2.25}],
                },
            ],
        }
        return {
            "fixture": {
                "slug": fixture_slug,
                "name": fixture_names[fixture_slug],
                "startTime": 1778277600000,
                "status": "active",
                "type": "match",
            },
            "groups": [],
            "swishMarkets": {"playerProps": props[fixture_slug]},
        }


class FakeMLBEngine:
    async def search_players(self, query: str, limit: int = 10):
        players = {
            "George Springer": {
                "mlbId": 543807,
                "name": "George Springer",
                "key": "george-springer",
                "team": {
                    "mlbId": 141,
                    "name": "Toronto Blue Jays",
                    "key": "toronto-blue-jays",
                },
            },
            "Vladimir Guerrero Jr.": {
                "mlbId": 665489,
                "name": "Vladimir Guerrero Jr.",
                "key": "vladimir-guerrero-jr",
                "team": {
                    "mlbId": 141,
                    "name": "Toronto Blue Jays",
                    "key": "toronto-blue-jays",
                },
            },
            "Mike Trout": {
                "mlbId": 545361,
                "name": "Mike Trout",
                "key": "mike-trout",
                "team": {
                    "mlbId": 108,
                    "name": "Los Angeles Angels",
                    "key": "los-angeles-angels",
                },
            },
            "Walbert Urena": {
                "mlbId": 700712,
                "name": "Walbert Urena",
                "key": "walbert-urena",
                "team": {
                    "mlbId": 108,
                    "name": "Los Angeles Angels",
                    "key": "los-angeles-angels",
                },
            },
            "Jack Kochanowicz": {
                "mlbId": 686799,
                "name": "Jack Kochanowicz",
                "key": "jack-kochanowicz",
                "team": {
                    "mlbId": 108,
                    "name": "Los Angeles Angels",
                    "key": "los-angeles-angels",
                },
            },
            "Jose Altuve": {
                "mlbId": 514888,
                "name": "Jose Altuve",
                "key": "jose-altuve",
                "team": {
                    "mlbId": 117,
                    "name": "Houston Astros",
                    "key": "houston-astros",
                },
            },
        }
        return {"query": query, "playerCount": 1, "players": [players[query]]}

    async def get_schedule(self, game_date: str):
        return {
            "date": game_date,
            "gameCount": 2,
            "games": [
                {
                    "gamePk": 1,
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
                },
                {
                    "gamePk": 2,
                    "awayTeam": {
                        "mlbId": 113,
                        "name": "Cincinnati Reds",
                        "key": "cincinnati-reds",
                    },
                    "homeTeam": {
                        "mlbId": 117,
                        "name": "Houston Astros",
                        "key": "houston-astros",
                    },
                },
            ],
        }

    async def get_team_roster(self, team_id: int, season=None):
        return {"teamId": team_id, "season": season, "playerCount": 0, "players": []}

    async def get_player_profile(self, player_id: int, season=None, group: str = "hitting"):
        stats = {
            543807: {"hits": 7, "gamesPlayed": 20},
            665489: {"hits": 18, "gamesPlayed": 20},
            545361: {"hits": 6, "gamesPlayed": 18},
            700712: {"strikeOuts": 22, "gamesStarted": 4},
            686799: {"strikeOuts": 24, "gamesStarted": 5},
            514888: {"hits": 22, "gamesPlayed": 20},
        }
        names = {
            543807: "George Springer",
            665489: "Vladimir Guerrero Jr.",
            545361: "Mike Trout",
            700712: "Walbert Urena",
            686799: "Jack Kochanowicz",
            514888: "Jose Altuve",
        }
        return {
            "player": {
                "mlbId": player_id,
                "name": names[player_id],
                "stats": stats[player_id],
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
        per_game = {
            543807: 0.2,
            665489: 1.4,
            545361: 0.2,
            700712: 4.2,
            686799: 4.8,
            514888: 1.2,
        }[player_id]
        stat_key = "strikeOuts" if player_id in {700712, 686799} else "hits"
        return {
            "playerId": player_id,
            "group": group,
            "season": season,
            "gamesUsed": 5,
            "games": [
                {"date": "2026-05-07", "opponent": "Test", "stats": {stat_key: per_game}}
            ],
            "totals": {stat_key: round(per_game * 5, 4)},
            "perGame": {stat_key: per_game},
        }


@pytest.fixture(autouse=True)
def override_clients():
    app.dependency_overrides[get_stake_client] = lambda: FakeStakeClient()
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMLBEngine()
    yield
    app.dependency_overrides.clear()


def test_gpt_schema_exposes_read_only_matchup_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    assert schema["servers"] == [{"url": "https://azp-test.example"}]
    assert "/gpt/mlb/matchup-picks" in schema["paths"]
    operation = schema["paths"]["/gpt/mlb/matchup-picks"]["get"]
    assert operation["operationId"] == "getMlbMatchupPicks"
    assert "Stake-offered" in operation["description"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert "properties" in response_schema
    assert "recommendations" in response_schema["properties"]


def test_gpt_api_key_is_optional_until_env_var_is_set(monkeypatch):
    monkeypatch.delenv("AZP_GPT_API_KEY", raising=False)
    assert require_gpt_api_key_value(None) is None

    monkeypatch.setenv("AZP_GPT_API_KEY", "secret")
    assert require_gpt_api_key_value("secret") is None
    with pytest.raises(Exception):
        require_gpt_api_key_value("wrong")


def test_build_matchup_picks_filters_to_requested_stake_matchup_and_side():
    result = asyncio.run(
        build_matchup_picks(
            stake_client=FakeStakeClient(),
            mlb_engine=FakeMLBEngine(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="hits",
            side="under",
            legs=2,
            mode="sgp",
            season=2026,
            history_limit=5,
        )
    )

    players = {pick["player"]["name"] for pick in result["recommendations"]}
    assert result["availablePropCount"] == 3
    assert result["matchedFixtureCount"] == 1
    assert players == {"George Springer", "Mike Trout"}
    springer = next(
        pick
        for pick in result["recommendations"]
        if pick["player"]["name"] == "George Springer"
    )
    assert springer["selection"] == "George Springer under 0.5 hits"
    assert springer["line"] == 0.5
    assert springer["odds"] == 2.9
    assert "Jose Altuve" not in players


def test_build_matchup_picks_filters_pitcher_props_to_probable_pitchers():
    result = asyncio.run(
        build_matchup_picks(
            stake_client=FakeStakeClient(),
            mlb_engine=FakeMLBEngine(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="strikeouts",
            side="over",
            legs=2,
            mode="sgp",
            season=2026,
            history_limit=5,
        )
    )

    players = {pick["player"]["name"] for pick in result["recommendations"]}
    assert "Jack Kochanowicz" in players
    assert "Walbert Urena" not in players


def test_gpt_route_returns_only_stake_backed_picks():
    with TestClient(app) as client:
        response = client.get(
            "/gpt/mlb/matchup-picks",
            params={
                "matchup": "Blue Jays vs Angels",
                "date": "2026-05-08",
                "markets": "hits",
                "side": "over",
                "legs": 2,
                "mode": "sgp",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "live_stake_odds_plus_mlb_stats"
    assert body["matchedFixtureCount"] == 1
    assert {pick["player"]["name"] for pick in body["recommendations"]} == {
        "Vladimir Guerrero Jr."
    }


def test_gpt_privacy_route_gives_action_privacy_policy_url_target():
    with TestClient(app) as client:
        response = client.get("/gpt/privacy")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "AZP Suite GPT Action Privacy Policy"
    assert "does not place bets" in " ".join(body["dataUse"])
