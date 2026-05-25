from __future__ import annotations

import asyncio

from app.sgm_candidate_pool import (
    build_sgm_candidate_pool_from_boards,
    normalize_sgm_market_key,
    score_sgm_candidate,
)


class CandidateFakeMLBEngine:
    async def search_players(self, query: str, limit: int = 10):
        players = {
            "Strong Under": {
                "mlbId": 1001,
                "name": "Strong Under",
                "key": "strong-under",
                "team": {"mlbId": 111, "name": "Test A", "key": "test-a"},
            },
            "Weak Under": {
                "mlbId": 1002,
                "name": "Weak Under",
                "key": "weak-under",
                "team": {"mlbId": 222, "name": "Test C", "key": "test-c"},
            },
            "Diverse Under": {
                "mlbId": 1003,
                "name": "Diverse Under",
                "key": "diverse-under",
                "team": {"mlbId": 111, "name": "Test A", "key": "test-a"},
            },
        }
        return {"players": [players[query]] if query in players else []}

    async def get_player_profile(self, player_id: int, season=None, group: str = "hitting"):
        profiles = {
            1001: {"hits": 40, "doubles": 12, "triples": 1, "homeRuns": 5, "gamesPlayed": 50},
            1002: {"hits": 75, "doubles": 12, "triples": 1, "homeRuns": 5, "gamesPlayed": 50},
            1003: {"baseOnBalls": 9, "gamesPlayed": 50},
        }
        return {
            "player": {
                "mlbId": player_id,
                "name": f"Player {player_id}",
                "key": f"player-{player_id}",
                "stats": profiles[player_id],
            },
            "season": season,
            "group": group,
        }

    async def get_player_recent_history(self, player_id: int, group: str = "hitting", season=None, limit: int = 15):
        if player_id == 1001:
            values = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
            games = [
                {"stats": {"hits": value, "doubles": 0, "triples": 0, "homeRuns": 0}}
                for value in values[:limit]
            ]
        elif player_id == 1002:
            games = [
                {"stats": {"hits": 2, "doubles": 0, "triples": 0, "homeRuns": 0}}
                for _ in range(limit)
            ]
        else:
            games = [{"stats": {"baseOnBalls": 0}} for _ in range(limit)]
        return {"playerId": player_id, "group": group, "gamesUsed": len(games), "games": games}

    async def get_schedule(self, game_date: str):
        return {
            "date": game_date,
            "games": [
                {
                    "gamePk": 1,
                    "awayTeam": {"mlbId": 111, "name": "Test A", "key": "test-a"},
                    "homeTeam": {"mlbId": 112, "name": "Test B", "key": "test-b"},
                },
                {
                    "gamePk": 2,
                    "awayTeam": {"mlbId": 222, "name": "Test C", "key": "test-c"},
                    "homeTeam": {"mlbId": 223, "name": "Test D", "key": "test-d"},
                },
            ],
        }

    async def get_team_roster(self, team_id: int, season=None):
        return {"teamId": team_id, "players": []}


def _row(player: str, team: str, market: str, under: float, *, line: float = 0.5, fixture_slug: str = "fixture-a"):
    return {
        "fixtureSlug": fixture_slug,
        "team": team,
        "player": player,
        "scope": "player",
        "market": market,
        "line": line,
        "over": 1.35,
        "under": under,
        "playable": True,
        "customBet": True,
        "liveCustomBetAvailable": True,
        "marketId": f"market-{market}",
        "lineId": f"line-{player}-{market}",
        "swishStatId": 302 if market == "Singles" else 318,
        "playerId": f"swish-{player}",
    }


def test_normalizes_sgm_only_market_aliases():
    assert normalize_sgm_market_key("Steals") == "stolen_bases"
    assert normalize_sgm_market_key("BB") == "batter_walks"
    assert normalize_sgm_market_key("Batter Ks") == "batter_strikeouts"
    assert normalize_sgm_market_key("Singles") == "singles"


def test_scoring_penalizes_short_filler_and_high_odds_without_support():
    short = score_sgm_candidate(
        {
            "odds": 1.1,
            "side": "under",
            "context": {"last10": {"sideHitRate": 0.8}, "last15": {"sideHitRate": 0.8}, "season": {"sideSupported": True}},
            "normalizedMarketKey": "singles",
        },
        mode="best_available",
    )
    assert short["quotaFillerPenalty"] > 0
    assert "short_odds_quota_filler" in short["riskFlags"]

    unsupported_longshot = score_sgm_candidate(
        {
            "odds": 7.0,
            "side": "under",
            "context": {"last10": {"sideHitRate": 0.2}, "last15": {"sideHitRate": 0.2}, "season": {"sideSupported": False}},
            "normalizedMarketKey": "singles",
        },
        mode="longshot",
    )
    assert unsupported_longshot["riskFlags"] == ["high_odds_no_stat_support"]
    assert unsupported_longshot["score"] < 50


def test_candidate_pool_ranks_context_backed_rows_and_skips_weak_per_game():
    boards = [
        {
            "source": "stake_ui_sgm",
            "fixtureSlug": "fixture-a",
            "capturedAt": "2026-05-25T12:00:00Z",
            "playerProps": [
                _row("Strong Under", "Test A", "Singles", 2.25),
                _row("Diverse Under", "Test A", "Batter Walks", 2.05),
            ],
            "teamMarkets": [],
        },
        {
            "source": "stake_ui_sgm",
            "fixtureSlug": "fixture-b",
            "capturedAt": "2026-05-25T12:00:00Z",
            "playerProps": [
                _row("Weak Under", "Test C", "Singles", 4.5, fixture_slug="fixture-b"),
            ],
            "teamMarkets": [],
        },
    ]

    result = asyncio.run(
        build_sgm_candidate_pool_from_boards(
            boards,
            CandidateFakeMLBEngine(),
            date="2026-05-25",
            side="under",
            mode="per_game",
            legs_per_game=2,
            quality_floor=55,
            max_total_candidates=10,
            history_limit=15,
        )
    )

    assert result["source"] == "stake_ui_sgm_candidate_pool"
    assert result["decisionOwner"] == "custom_gpt"
    assert result["builderRole"] == "candidate_support_not_final_recommendation"
    assert result["guardrails"]["maxLegsPerGameGroup"] == 16
    assert result["guardrails"]["maxSgmGroupOdds"] == 501
    assert [candidate["player"] for candidate in result["rankedCandidates"]] == [
        "Strong Under",
        "Diverse Under",
    ]
    assert result["perGame"]["fixture-b"]["skipped"] is True
    assert result["rejectedSummary"]["score_below_quality_floor"] >= 1
