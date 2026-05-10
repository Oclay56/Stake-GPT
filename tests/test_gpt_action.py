from __future__ import annotations

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.gpt_action import (
    build_gpt_action_openapi_schema,
    build_matchup_prop_board,
    build_player_mlb_context,
    require_gpt_api_key_value,
    validate_gpt_selections,
)
from app.main import app, get_gpt_store, get_mlb_engine, get_stake_client
from app.storage import GptActionStore


class FakeStakeClient:
    async def get_tournament_schedule(self, sport: str, category: str, tournament: str):
        assert (sport, category, tournament) == ("baseball", "usa", "mlb")
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
        fixtures = {
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
                    "competitorName": "Mike Trout",
                    "teamName": "Los Angeles Angels",
                    "marketName": "hits",
                    "sportStatType": "player",
                    "outcomes": [{"line": 0.5, "over": 1.74, "under": 1.95}],
                },
                {
                    "competitorName": "Jack Kochanowicz",
                    "teamName": "Los Angeles Angels",
                    "marketName": "strikeouts",
                    "sportStatType": "player",
                    "outcomes": [{"line": 5.5, "over": 1.82, "under": 1.9}],
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
                "name": fixtures[fixture_slug],
                "startTime": 1778277600000,
                "status": "active",
                "type": "match",
            },
            "groups": [],
            "swishMarkets": {"playerProps": props[fixture_slug]},
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

    async def get_team_roster(self, team_id: int, season=None):
        return {"teamId": team_id, "season": season, "playerCount": 0, "players": []}

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
        }
        player = players.get(query)
        return {"query": query, "playerCount": 1 if player else 0, "players": [player] if player else []}

    async def get_player_profile(self, player_id: int, season=None, group: str = "hitting"):
        stats = {
            543807: {"gamesPlayed": 20, "hits": 15},
            545361: {"gamesPlayed": 18, "hits": 20},
            686799: {"gamesStarted": 5, "strikeOuts": 24},
        }
        names = {
            543807: "George Springer",
            545361: "Mike Trout",
            686799: "Jack Kochanowicz",
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
        stat_key = "strikeOuts" if player_id == 686799 else "hits"
        value = 4.8 if player_id == 686799 else 0.4
        games = [
            {
                "gamePk": index,
                "date": f"2026-05-{7 - index:02d}",
                "opponent": "Test",
                "stats": {stat_key: value},
            }
            for index in range(min(limit, 5))
        ]
        return {
            "playerId": player_id,
            "group": group,
            "season": season,
            "gamesUsed": len(games),
            "games": games,
            "totals": {stat_key: round(value * len(games), 4)},
            "perGame": {stat_key: value},
        }


@pytest.fixture(autouse=True)
def override_clients(tmp_path):
    app.dependency_overrides[get_stake_client] = lambda: FakeStakeClient()
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMLBEngine()
    app.dependency_overrides[get_gpt_store] = lambda: GptActionStore(
        tmp_path / "gpt.sqlite"
    )
    yield
    app.dependency_overrides.clear()


def test_gpt_schema_exposes_gpt_owned_data_actions_only():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")
    path_ids = {
        path: next(iter(methods.values()))["operationId"]
        for path, methods in schema["paths"].items()
    }

    assert schema["servers"] == [{"url": "https://azp-test.example"}]
    assert "/mlb/matchup/{matchup}/props" in schema["paths"]
    assert path_ids["/mlb/matchup/{matchup}/props"] == "getMatchupPropBoard"
    assert path_ids["/mlb/player/{playerId}/context"] == "getPlayerMlbContext"
    assert path_ids["/mlb/validate-selections"] == "validateSelections"
    assert path_ids["/mlb/save-gpt-decision"] == "saveGptDecision"
    assert "/gpt/mlb/matchup-picks" not in schema["paths"]


def test_gpt_api_key_is_optional_until_env_var_is_set(monkeypatch):
    monkeypatch.delenv("AZP_GPT_API_KEY", raising=False)
    assert require_gpt_api_key_value(None) is None

    monkeypatch.setenv("AZP_GPT_API_KEY", "secret")
    assert require_gpt_api_key_value("secret") is None
    with pytest.raises(Exception):
        require_gpt_api_key_value("wrong")


def test_matchup_prop_board_returns_stake_backed_primary_lines_without_scores():
    result = asyncio.run(
        build_matchup_prop_board(
            stake_client=FakeStakeClient(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="hits",
            side="under",
        )
    )

    springer = next(
        selection
        for selection in result["selections"]
        if selection["player"]["name"] == "George Springer"
    )
    assert result["decisionOwner"] == "custom_gpt"
    assert result["matchedFixtureCount"] == 1
    assert result["availableSelectionCount"] == 2
    assert springer["line"] == 0.5
    assert springer["odds"] == 2.9
    assert springer["side"] == "under"
    assert springer["playable"] is True
    assert "score" not in springer
    assert "recommendations" not in result


def test_player_prop_context_adds_mlb_recent_and_season_context():
    board = asyncio.run(
        build_matchup_prop_board(
            stake_client=FakeStakeClient(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="hits",
            side="under",
        )
    )
    selection_id = board["selections"][0]["selectionId"]

    result = asyncio.run(
        build_player_mlb_context(
            stake_client=FakeStakeClient(),
            mlb_engine=FakeMLBEngine(),
            matchup="Blue Jays vs Angels",
            prop_id=selection_id,
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="hits",
            season=2026,
            history_limit=15,
        )
    )

    assert result["selection"]["selectionId"] == selection_id
    assert result["player"]["mlbId"] is not None
    assert result["statContext"]["statKey"] == "hits"
    assert result["recent"]["windows"]["5"]["gamesUsed"] == 5
    assert result["season"]["stats"]
    assert result["matchupGame"]["gamePk"] == 1


def test_validate_gpt_selections_checks_current_stake_line_side_and_odds():
    board = asyncio.run(
        build_matchup_prop_board(
            stake_client=FakeStakeClient(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="hits",
            side="under",
        )
    )
    springer = next(
        selection
        for selection in board["selections"]
        if selection["player"]["name"] == "George Springer"
    )

    result = asyncio.run(
        validate_gpt_selections(
            stake_client=FakeStakeClient(),
            matchup="Blue Jays vs Angels",
            selections=[
                {
                    "selectionId": springer["selectionId"],
                    "side": "under",
                    "line": 1.5,
                    "odds": springer["odds"],
                }
            ],
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="hits",
        )
    )

    assert result["valid"] is False
    assert result["results"][0]["status"] == "line_mismatch"
    assert result["results"][0]["current"]["line"] == 0.5


def test_save_gpt_decision_route_persists_gpt_choice_without_azp_ledger():
    with TestClient(app) as client:
        board_response = client.get(
            "/mlb/matchup/Blue Jays vs Angels/props",
            params={"date": "2026-05-08", "market": "hits", "side": "under"},
        )
        selection = board_response.json()["selections"][0]
        response = client.post(
            "/mlb/save-gpt-decision",
            json={
                "matchup": "Blue Jays vs Angels",
                "date": "2026-05-08",
                "prompt": "Pick two under hits props.",
                "selections": [
                    {
                        "selectionId": selection["selectionId"],
                        "side": selection["side"],
                        "line": selection["line"],
                        "odds": selection["odds"],
                    }
                ],
                "reasoning": ["Stake board confirmed exact line."],
                "riskFlags": ["small_sample"],
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["decisionOwner"] == "custom_gpt"
    assert body["validation"]["valid"] is True
    assert body["gptDecisionLedger"]["saved"] is True
    assert "recommendationLedger" not in body
