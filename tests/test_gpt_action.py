import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.gpt_action import (
    build_gpt_decision_result,
    build_gpt_action_openapi_schema,
    build_matchup_picks,
    build_matchup_prop_board,
    build_player_mlb_context,
    require_gpt_api_key_value,
    validate_gpt_selections,
)
from app.main import app, get_mlb_engine, get_snapshot_store, get_stake_client
from app.mlb_bridge import clear_mlb_bridge_cache
from app.storage import SnapshotStore


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


class FakeStakeClientWithSuspiciousOdds(FakeStakeClient):
    async def get_odds(self, fixture_slug: str):
        payload = await super().get_odds(fixture_slug)
        if fixture_slug == "blue-jays-angels":
            payload["swishMarkets"]["playerProps"].append(
                {
                    "competitorName": "Bo Bichette",
                    "teamName": "Toronto Blue Jays",
                    "marketName": "runs",
                    "sportStatType": "player",
                    "outcomes": [
                        {
                            "line": 0.5,
                            "over": 2.3,
                            "under": 0.9804882831650161,
                        }
                    ],
                }
            )
        return payload


class FakeStakeClientWithRunFlood(FakeStakeClient):
    async def get_odds(self, fixture_slug: str):
        payload = await super().get_odds(fixture_slug)
        if fixture_slug == "blue-jays-angels":
            payload["swishMarkets"]["playerProps"].extend(
                [
                    {
                        "competitorName": "Bo Bichette",
                        "teamName": "Toronto Blue Jays",
                        "marketName": "runs",
                        "sportStatType": "player",
                        "outcomes": [{"line": 0.5, "over": 2.2, "under": 1.78}],
                    },
                    {
                        "competitorName": "Anthony Santander",
                        "teamName": "Toronto Blue Jays",
                        "marketName": "runs",
                        "sportStatType": "player",
                        "outcomes": [{"line": 0.5, "over": 2.15, "under": 1.8}],
                    },
                    {
                        "competitorName": "Luis Rengifo",
                        "teamName": "Los Angeles Angels",
                        "marketName": "runs",
                        "sportStatType": "player",
                        "outcomes": [{"line": 0.5, "over": 2.1, "under": 1.77}],
                    },
                    {
                        "competitorName": "Nolan Schanuel",
                        "teamName": "Los Angeles Angels",
                        "marketName": "total-bases",
                        "sportStatType": "player",
                        "outcomes": [{"line": 1.5, "over": 2.05, "under": 1.85}],
                    },
                ]
            )
        return payload


class FakeStakeClientWithStrongRunFlood(FakeStakeClient):
    async def get_odds(self, fixture_slug: str):
        payload = await super().get_odds(fixture_slug)
        if fixture_slug == "blue-jays-angels":
            payload["swishMarkets"]["playerProps"].extend(
                [
                    {
                        "competitorName": "Bo Bichette",
                        "teamName": "Toronto Blue Jays",
                        "marketName": "runs",
                        "sportStatType": "player",
                        "outcomes": [{"line": 0.5, "over": 1.9, "under": 1.78}],
                    },
                    {
                        "competitorName": "Anthony Santander",
                        "teamName": "Toronto Blue Jays",
                        "marketName": "runs",
                        "sportStatType": "player",
                        "outcomes": [{"line": 0.5, "over": 1.88, "under": 1.8}],
                    },
                    {
                        "competitorName": "Luis Rengifo",
                        "teamName": "Los Angeles Angels",
                        "marketName": "runs",
                        "sportStatType": "player",
                        "outcomes": [{"line": 0.5, "over": 1.86, "under": 1.77}],
                    },
                    {
                        "competitorName": "Nolan Schanuel",
                        "teamName": "Los Angeles Angels",
                        "marketName": "total-bases",
                        "sportStatType": "player",
                        "outcomes": [{"line": 0.5, "over": 1.52, "under": 2.35}],
                    },
                ]
            )
        return payload


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
            "Bo Bichette": {
                "mlbId": 666182,
                "name": "Bo Bichette",
                "key": "bo-bichette",
                "team": {
                    "mlbId": 141,
                    "name": "Toronto Blue Jays",
                    "key": "toronto-blue-jays",
                },
            },
            "Anthony Santander": {
                "mlbId": 623993,
                "name": "Anthony Santander",
                "key": "anthony-santander",
                "team": {
                    "mlbId": 141,
                    "name": "Toronto Blue Jays",
                    "key": "toronto-blue-jays",
                },
            },
            "Luis Rengifo": {
                "mlbId": 650859,
                "name": "Luis Rengifo",
                "key": "luis-rengifo",
                "team": {
                    "mlbId": 108,
                    "name": "Los Angeles Angels",
                    "key": "los-angeles-angels",
                },
            },
            "Nolan Schanuel": {
                "mlbId": 694384,
                "name": "Nolan Schanuel",
                "key": "nolan-schanuel",
                "team": {
                    "mlbId": 108,
                    "name": "Los Angeles Angels",
                    "key": "los-angeles-angels",
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
            666182: {"runs": 3, "gamesPlayed": 20},
            623993: {"runs": 2, "gamesPlayed": 20},
            650859: {"runs": 4, "gamesPlayed": 20},
            694384: {"totalBases": 8, "gamesPlayed": 20},
        }
        names = {
            543807: "George Springer",
            665489: "Vladimir Guerrero Jr.",
            545361: "Mike Trout",
            700712: "Walbert Urena",
            686799: "Jack Kochanowicz",
            514888: "Jose Altuve",
            666182: "Bo Bichette",
            623993: "Anthony Santander",
            650859: "Luis Rengifo",
            694384: "Nolan Schanuel",
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
            666182: 0.0,
            623993: 0.0,
            650859: 0.0,
            694384: 0.0,
        }[player_id]
        stat_key = {
            700712: "strikeOuts",
            686799: "strikeOuts",
            666182: "runs",
            623993: "runs",
            650859: "runs",
            694384: "totalBases",
        }.get(player_id, "hits")
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


class FakeMLBEngineWithStrongRuns(FakeMLBEngine):
    async def get_player_profile(self, player_id: int, season=None, group: str = "hitting"):
        payload = await super().get_player_profile(player_id, season=season, group=group)
        if player_id in {666182, 623993, 650859}:
            payload["player"]["stats"]["runs"] = 36
        if player_id == 694384:
            payload["player"]["stats"]["totalBases"] = 20
        return payload

    async def get_player_recent_history(
        self,
        player_id: int,
        group: str = "hitting",
        season=None,
        limit: int = 10,
    ):
        payload = await super().get_player_recent_history(
            player_id,
            group=group,
            season=season,
            limit=limit,
        )
        if player_id in {666182, 623993, 650859}:
            payload["totals"] = {"runs": 15}
            payload["perGame"] = {"runs": 3.0}
            for game in payload["games"]:
                game["stats"] = {"runs": 3.0}
        if player_id == 694384:
            payload["totals"] = {"totalBases": 5}
            payload["perGame"] = {"totalBases": 1.0}
            for game in payload["games"]:
                game["stats"] = {"totalBases": 1.0}
        return payload


@pytest.fixture(autouse=True)
def override_clients():
    clear_mlb_bridge_cache()
    app.dependency_overrides[get_stake_client] = lambda: FakeStakeClient()
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMLBEngine()
    yield
    app.dependency_overrides.clear()
    clear_mlb_bridge_cache()


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
    parameters = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
    }
    assert parameters["diversityMode"]["schema"]["enum"] == [
        "balanced",
        "best_available",
        "strict_diversity",
        "longshot",
    ]


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


def test_build_matchup_picks_rejects_unplayable_feed_odds(monkeypatch):
    monkeypatch.setenv("AZP_MIN_PLAYABLE_ODDS", "1.10")

    result = asyncio.run(
        build_matchup_picks(
            stake_client=FakeStakeClientWithSuspiciousOdds(),
            mlb_engine=FakeMLBEngine(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="runs",
            side="under",
            legs=2,
            mode="sgp",
            season=2026,
            history_limit=5,
        )
    )

    assert result["recommendationCount"] == 0
    assert result["recommendationDiagnostics"]["discardedInvalidOdds"] == 1
    assert "Bo Bichette" not in {
        pick["player"]["name"] for pick in result["recommendations"]
    }
    assert any("playable odds" in note for note in result["notes"])


def test_build_matchup_picks_rejects_whole_market_when_one_side_has_unplayable_odds(monkeypatch):
    monkeypatch.setenv("AZP_MIN_PLAYABLE_ODDS", "1.10")

    result = asyncio.run(
        build_matchup_picks(
            stake_client=FakeStakeClientWithSuspiciousOdds(),
            mlb_engine=FakeMLBEngineWithStrongRuns(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="runs",
            side="over",
            legs=2,
            mode="sgp",
            season=2026,
            history_limit=5,
        )
    )

    assert result["recommendationCount"] == 0
    assert result["recommendationDiagnostics"]["discardedUnavailableMarket"] == 1
    assert "Bo Bichette" not in {
        pick["player"]["name"] for pick in result["recommendations"]
    }


def test_build_matchup_picks_includes_blended_form_context():
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
            history_limit=10,
        )
    )

    springer = next(
        pick
        for pick in result["recommendations"]
        if pick["player"]["name"] == "George Springer"
    )
    assert springer["formContext"]["recent5PerGame"] == 0.2
    assert springer["formContext"]["recent10PerGame"] == 0.2
    assert springer["formContext"]["seasonPerGame"] == 0.35
    assert springer["formContext"]["blendedPerGame"] == 0.2375
    assert springer["formContext"]["blendedEdge"] == 0.2625


def test_build_matchup_picks_soft_diversity_prefers_close_market_spread(monkeypatch):
    monkeypatch.setenv("AZP_MIN_PLAYABLE_ODDS", "1.10")
    monkeypatch.setenv("AZP_MAX_RECOMMENDATIONS_PER_MARKET", "2")

    result = asyncio.run(
        build_matchup_picks(
            stake_client=FakeStakeClientWithRunFlood(),
            mlb_engine=FakeMLBEngine(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets=None,
            side="under",
            legs=2,
            mode="sgp",
            diversity_mode="balanced",
            season=2026,
            history_limit=5,
            recommendation_limit=4,
        )
    )

    market_counts = result["recommendationDiagnostics"]["marketCounts"]
    assert market_counts["runs"] == 2
    assert market_counts["hits"] == 1
    assert result["recommendationDiagnostics"]["softDiversityPromotions"] >= 1
    assert result["recommendationDiagnostics"]["discardedByMarketDiversity"] == 0
    assert any("Soft diversity" in note for note in result["notes"])


def test_build_matchup_picks_soft_diversity_keeps_clearly_stronger_repeated_market(monkeypatch):
    monkeypatch.setenv("AZP_MIN_PLAYABLE_ODDS", "1.10")
    monkeypatch.setenv("AZP_MAX_RECOMMENDATIONS_PER_MARKET", "2")
    monkeypatch.setenv("AZP_SOFT_DIVERSITY_SCORE_GAP", "8")

    result = asyncio.run(
        build_matchup_picks(
            stake_client=FakeStakeClientWithStrongRunFlood(),
            mlb_engine=FakeMLBEngineWithStrongRuns(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets=None,
            side="over",
            legs=2,
            mode="sgp",
            diversity_mode="balanced",
            season=2026,
            history_limit=5,
            recommendation_limit=5,
        )
    )

    market_counts = result["recommendationDiagnostics"]["marketCounts"]
    assert market_counts["runs"] == 3
    assert result["recommendationDiagnostics"]["softDiversityOverrides"] >= 1
    assert "market_concentration:runs" in result["recommendationDiagnostics"]["concentrationTags"]
    assert any("Concentration flagged" in note for note in result["notes"])


def test_build_matchup_picks_strict_diversity_still_hard_caps_repeated_markets(monkeypatch):
    monkeypatch.setenv("AZP_MIN_PLAYABLE_ODDS", "1.10")
    monkeypatch.setenv("AZP_MAX_RECOMMENDATIONS_PER_MARKET", "2")

    result = asyncio.run(
        build_matchup_picks(
            stake_client=FakeStakeClientWithRunFlood(),
            mlb_engine=FakeMLBEngine(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets=None,
            side="under",
            legs=2,
            mode="sgp",
            diversity_mode="strict_diversity",
            season=2026,
            history_limit=5,
            recommendation_limit=10,
        )
    )

    market_counts = result["recommendationDiagnostics"]["marketCounts"]
    assert market_counts["runs"] == 2
    assert result["recommendationDiagnostics"]["discardedByMarketDiversity"] == 1
    assert any("Strict diversity capped" in note for note in result["notes"])


def test_build_matchup_picks_does_not_cap_explicit_single_market(monkeypatch):
    monkeypatch.setenv("AZP_MIN_PLAYABLE_ODDS", "1.10")
    monkeypatch.setenv("AZP_MAX_RECOMMENDATIONS_PER_MARKET", "2")

    result = asyncio.run(
        build_matchup_picks(
            stake_client=FakeStakeClientWithRunFlood(),
            mlb_engine=FakeMLBEngine(),
            matchup="Blue Jays vs Angels",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            markets="runs",
            side="under",
            legs=2,
            mode="sgp",
            diversity_mode="balanced",
            season=2026,
            history_limit=5,
            recommendation_limit=10,
        )
    )

    market_counts = result["recommendationDiagnostics"]["marketCounts"]
    assert market_counts["runs"] == 3
    assert result["recommendationDiagnostics"]["discardedByMarketDiversity"] == 0


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


def test_gpt_route_saves_exact_recommendation_response(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    app.dependency_overrides[get_snapshot_store] = lambda: store

    with TestClient(app) as client:
        response = client.get(
            "/gpt/mlb/matchup-picks",
            params={
                "matchup": "Blue Jays vs Angels",
                "date": "2026-05-08",
                "markets": "hits",
                "side": "under",
                "legs": 2,
                "mode": "sgp",
            },
        )

    assert response.status_code == 200
    legs = store.list_recommendation_legs(date_text="2026-05-08")
    assert len(legs) == response.json()["recommendationCount"]
    assert legs[0]["matchup"] == "Blue Jays vs Angels"
    assert legs[0]["selection"].endswith("hits")
    assert legs[0]["odds"] >= 1.1


def test_gpt_schema_exposes_performance_summary_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    assert "/gpt/mlb/performance-summary" in schema["paths"]
    operation = schema["paths"]["/gpt/mlb/performance-summary"]["get"]
    assert operation["operationId"] == "getMlbPerformanceSummary"
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert "date" in parameters
    assert "market" in parameters
    assert "diversityMode" in parameters


def test_gpt_schema_exposes_settlement_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    assert "/gpt/mlb/settle-recommendations" in schema["paths"]
    operation = schema["paths"]["/gpt/mlb/settle-recommendations"]["get"]
    assert operation["operationId"] == "settleMlbRecommendations"
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert "date" in parameters
    assert "requestId" in parameters
    assert "diversityMode" in parameters


def test_gpt_schema_exposes_board_context_validation_and_decision_actions():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    assert schema["paths"]["/gpt/mlb/matchup-prop-board"]["get"]["operationId"] == (
        "getMatchupPropBoard"
    )
    assert schema["paths"]["/gpt/mlb/player-context"]["get"]["operationId"] == (
        "getPlayerMlbContext"
    )
    assert schema["paths"]["/gpt/mlb/validate-selections"]["post"]["operationId"] == (
        "validateSelections"
    )
    assert schema["paths"]["/gpt/mlb/gpt-decisions"]["post"]["operationId"] == (
        "saveGptDecision"
    )


def test_build_matchup_prop_board_returns_stake_options_without_azp_scores():
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

    assert result["source"] == "live_stake_prop_board"
    assert result["decisionOwner"] == "chatgpt"
    assert result["matchedFixtureCount"] == 1
    assert result["propCount"] == 3
    assert result["selectionCount"] == 3
    springer = next(
        selection
        for selection in result["selections"]
        if selection["player"]["name"] == "George Springer"
    )
    assert springer["selection"] == "George Springer under 0.5 hits"
    assert springer["propId"] == "blue-jays-angels:george-springer:toronto-blue-jays:hits"
    assert springer["selectionId"].endswith(":under")
    assert springer["line"] == 0.5
    assert springer["odds"] == 2.9
    assert springer["availability"]["stakeReturned"] is True
    assert springer["availability"]["playable"] is True
    assert "score" not in springer
    assert "recommendations" not in result


def test_build_player_mlb_context_enriches_selected_board_prop():
    result = asyncio.run(
        build_player_mlb_context(
            stake_client=FakeStakeClient(),
            mlb_engine=FakeMLBEngine(),
            matchup="Blue Jays vs Angels",
            prop_id="blue-jays-angels:george-springer:toronto-blue-jays:hits",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            season=2026,
            history_limit=15,
        )
    )

    assert result["source"] == "stake_prop_plus_mlb_context"
    assert result["player"]["name"] == "George Springer"
    assert result["stakeProp"]["line"] == 0.5
    assert result["stakeProp"]["odds"]["under"] == 2.9
    assert result["statContext"]["statKey"] == "hits"
    assert result["recent"]["last5"]["perGame"] == 0.2
    assert result["recent"]["last10"]["perGame"] == 0.2
    assert result["season"]["perGame"] == 0.35
    assert result["matchupGame"]["gamePk"] == 1
    assert result["matchupGame"]["homeTeam"]["probablePitcher"]["name"] == (
        "Jack Kochanowicz"
    )


def test_validate_gpt_selections_checks_current_stake_line_and_odds():
    result = asyncio.run(
        validate_gpt_selections(
            stake_client=FakeStakeClient(),
            matchup="Blue Jays vs Angels",
            selections=[
                {
                    "propId": "blue-jays-angels:george-springer:toronto-blue-jays:hits",
                    "side": "under",
                    "line": 0.5,
                    "odds": 2.9,
                },
                {
                    "propId": "blue-jays-angels:george-springer:toronto-blue-jays:hits",
                    "side": "under",
                    "line": 1.5,
                    "odds": 1.35,
                },
            ],
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
        )
    )

    assert result["valid"] is False
    assert result["validCount"] == 1
    assert result["invalidCount"] == 1
    assert result["results"][0]["valid"] is True
    assert result["results"][1]["valid"] is False
    assert "line_mismatch" in result["results"][1]["issues"]


def test_build_gpt_decision_result_keeps_chatgpt_choices_separate_from_azp():
    result = asyncio.run(
        build_gpt_decision_result(
            stake_client=FakeStakeClient(),
            matchup="Blue Jays vs Angels",
            selections=[
                {
                    "propId": "blue-jays-angels:george-springer:toronto-blue-jays:hits",
                    "side": "under",
                    "line": 0.5,
                    "odds": 2.9,
                    "rationale": "GPT preferred the low hit line after checking context.",
                }
            ],
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
            prompt="Pick two players yourself.",
            notes=["GPT made the final choice."],
        )
    )

    assert result["source"] == "chatgpt_decision"
    assert result["decisionOwner"] == "chatgpt"
    assert result["azpRecommendation"] is False
    assert result["validation"]["valid"] is True
    assert result["selections"][0]["selection"] == "George Springer under 0.5 hits"
    assert result["selections"][0]["rationale"] == (
        "GPT preferred the low hit line after checking context."
    )


def test_gpt_decision_route_saves_separate_decision_ledger(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    app.dependency_overrides[get_snapshot_store] = lambda: store

    with TestClient(app) as client:
        response = client.post(
            "/gpt/mlb/gpt-decisions",
            json={
                "matchup": "Blue Jays vs Angels",
                "date": "2026-05-08",
                "prompt": "Pick two players yourself.",
                "selections": [
                    {
                        "propId": "blue-jays-angels:george-springer:toronto-blue-jays:hits",
                        "side": "under",
                        "line": 0.5,
                        "odds": 2.9,
                        "rationale": "GPT preferred the low hit line.",
                    }
                ],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["gptDecisionLedger"]["saved"] is True
    assert body["gptDecisionLedger"]["legsSaved"] == 1
    saved = store.list_gpt_decision_legs(date_text="2026-05-08")
    assert len(saved) == 1
    assert saved[0]["selection"] == "George Springer under 0.5 hits"
    assert saved[0]["source"] == "chatgpt_decision"


def test_gpt_performance_summary_prefers_supabase_when_configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret")

    async def fake_fetch(**kwargs):
        return {
            "source": "supabase",
            "date": kwargs["date_text"],
            "counts": {"legs": 3, "correct": 2},
            "rows": [],
        }

    monkeypatch.setattr(
        "app.main.fetch_recommendation_performance_from_supabase",
        fake_fetch,
    )

    with TestClient(app) as client:
        response = client.get(
            "/gpt/mlb/performance-summary",
            params={"date": "2026-05-08", "market": "hits"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "supabase"
    assert body["date"] == "2026-05-08"
    assert body["counts"]["legs"] == 3


def test_gpt_settlement_route_saves_and_syncs_recommendation_results(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret")
    store = SnapshotStore(tmp_path / "azp.sqlite")
    app.dependency_overrides[get_snapshot_store] = lambda: store
    synced = {}

    async def fake_sync(rows):
        synced["rows"] = rows
        return {"enabled": True, "synced": True, "settlementsSynced": len(rows)}

    monkeypatch.setattr(
        "app.main.sync_recommendation_settlements_to_supabase",
        fake_sync,
    )

    with TestClient(app) as client:
        saved_response = client.get(
            "/gpt/mlb/matchup-picks",
            params={
                "matchup": "Blue Jays vs Angels",
                "date": "2026-05-08",
                "markets": "hits",
                "side": "under",
                "legs": 2,
                "mode": "sgp",
            },
        )
        assert saved_response.status_code == 200

        response = client.get(
            "/gpt/mlb/settle-recommendations",
            params={"date": "2026-05-08"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "local_sqlite_plus_mlb_stats"
    assert body["settlementLedger"]["supabaseSynced"] is True
    assert body["legCount"] == len(synced["rows"])


def test_gpt_privacy_route_gives_action_privacy_policy_url_target():
    with TestClient(app) as client:
        response = client.get("/gpt/privacy")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "AZP Suite GPT Action Privacy Policy"
    assert "does not place bets" in " ".join(body["dataUse"])
