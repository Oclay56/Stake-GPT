from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.sgm_candidate_pool import (
    _apply_game_level_contest,
    _apply_within_player_market_contest,
    _market_concentration_diagnostics,
    _select_ranked_candidates,
    build_sgm_candidate_pool_from_boards,
    compact_sgm_candidate_pool_response,
    normalize_sgm_market_key,
    score_sgm_candidate,
)
from app.mlb_bridge import stat_mapping_for_market, stat_value_from_stats


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


def _fresh_captured_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scored_candidate(
    row_id: str,
    player: str,
    market: str,
    score: float,
    *,
    fixture_slug: str = "fixture-a",
):
    return {
        "fixtureSlug": fixture_slug,
        "team": "Test A",
        "player": player,
        "market": market,
        "normalizedMarketKey": normalize_sgm_market_key(market),
        "side": "under",
        "line": 0.5,
        "odds": 2.0,
        "rowId": row_id,
        "score": score,
        "contextQuality": "full",
        "reasonTags": [],
        "riskFlags": [],
    }


def test_normalizes_sgm_only_market_aliases():
    assert normalize_sgm_market_key("Steals") == "stolen_bases"
    assert normalize_sgm_market_key("BB") == "batter_walks"
    assert normalize_sgm_market_key("Batter Ks") == "batter_strikeouts"
    assert normalize_sgm_market_key("Singles") == "singles"
    assert normalize_sgm_market_key("Hits + Runs + RBIs") == "hits_runs_rbis"
    mapping = stat_mapping_for_market("hits-runs-rbis")
    assert mapping["supported"] is True
    assert stat_value_from_stats(mapping, {"hits": 1, "runs": 2, "rbi": 3}) == 6


def test_probability_assessment_uses_formula_inputs_and_labels_edge():
    scored = score_sgm_candidate(
        {
            "odds": 1.9,
            "side": "under",
            "line": 0.5,
            "contextQuality": "full",
            "seasonSample": {"status": "robust"},
            "context": {
                "last10": {"sideHitRate": 0.8, "gamesUsed": 10},
                "last15": {"sideHitRate": 0.8, "gamesUsed": 15},
                "season": {"sideMargin": 0.5, "gamesUsed": 60},
            },
            "normalizedMarketKey": "hits",
            "opponentPitcherContext": {
                "status": "available",
                "season": {
                    "gamesStarted": 20,
                    "hitsAllowed": 100,
                    "walksAllowed": 40,
                    "strikeOuts": 120,
                    "homeRunsAllowed": 12,
                },
                "recent": {"perGame": {"hits": 5.0}},
            },
        },
        mode="best_available",
    )

    probability = scored["probabilityAssessment"]

    assert probability["impliedProbability"] == 0.5263
    assert probability["inputs"]["seasonRateSource"] == "season_average_proxy"
    assert probability["inputs"]["last15Rate"] == 0.8
    assert probability["inputs"]["matchupFactor"] > 0.5
    assert probability["edgeStatus"] == "clear_possible_edge"
    assert scored["probabilityScoreAdjustment"] > 0


def test_candidate_score_uses_sample_gated_historical_signal():
    base_candidate = {
        "odds": 1.9,
        "side": "under",
        "line": 0.5,
        "contextQuality": "full",
        "seasonSample": {"status": "robust"},
        "context": {
            "last10": {"sideHitRate": 0.65, "gamesUsed": 10},
            "last15": {"sideHitRate": 0.65, "gamesUsed": 15},
            "season": {"sideMargin": 0.2, "gamesUsed": 60},
        },
        "normalizedMarketKey": "singles",
    }
    without_history = score_sgm_candidate(base_candidate)
    with_history = score_sgm_candidate(
        {
            **base_candidate,
            "historicalSignal": {
                "status": "positive_history_signal",
                "applied": {
                    "status": "positive_history_signal",
                    "bucket": "player_market_line",
                    "gradedLegs": 15,
                    "hitRate": 0.7,
                    "averageBreakEvenRate": 0.52,
                    "historicalEdge": 0.18,
                    "probabilityAdjustment": 0.08,
                    "scoreAdjustment": 2.8,
                    "reason": "usable historical sample",
                },
                "notes": ["historical_signal_score_adjusted"],
            },
        }
    )

    assert with_history["historicalScoreAdjustment"] == 2.8
    assert with_history["score"] > without_history["score"]
    assert with_history["probabilityAssessment"]["historicalCalibration"]["bucket"] == "player_market_line"
    assert "historical_positive_signal" in with_history["reasonTags"]
    assert "historical_sample_gated_score_adjustment" in with_history["reasonTags"]


def test_compact_response_preserves_historical_enrichment_status():
    compact = compact_sgm_candidate_pool_response(
        {
            "rankedCandidates": [
                {
                    "fixtureSlug": "toronto-blue-jays-new-york-yankees",
                    "matchup": "New York Yankees - Toronto Blue Jays",
                    "rowId": "row-1",
                    "player": "George Springer",
                    "market": "Hits",
                    "side": "under",
                    "line": 0.5,
                    "odds": 1.91,
                    "score": 72.0,
                    "historicalSignal": {
                        "status": "low_sample",
                        "enrichment": {
                            "status": "available",
                            "coverageRate": 1.0,
                            "enrichedLegs": 1,
                        },
                        "applied": {},
                    },
                    "reasonTags": [],
                    "riskFlags": [],
                }
            ],
            "notes": [],
        }
    )

    row = compact["rankedCandidates"][0]
    assert row["historicalEnrichmentStatus"] == "available"
    assert row["historicalEnrichmentCoverage"] == 1.0
    assert row["historicalEnrichedLegs"] == 1


def test_stale_board_requires_refetch_and_blocks_candidate_score():
    scored = score_sgm_candidate(
        {
            "odds": 1.9,
            "side": "under",
            "line": 0.5,
            "contextQuality": "full",
            "seasonSample": {"status": "robust"},
            "context": {
                "last10": {"sideHitRate": 0.9, "gamesUsed": 10},
                "last15": {"sideHitRate": 0.9, "gamesUsed": 15},
                "season": {"sideMargin": 0.5, "gamesUsed": 60},
            },
            "normalizedMarketKey": "hits",
            "boardFreshness": {"status": "stale", "refetchRequired": True},
        },
        mode="best_available",
    )

    assert "stake_board_stale_refetch_required" in scored["riskFlags"]
    assert scored["score"] == 0.0


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


def test_within_player_market_contest_prioritizes_player_winners_before_alternatives():
    rows = [
        _scored_candidate("row-same-singles", "Same Player", "Singles", 94),
        _scored_candidate("row-same-rbi", "Same Player", "RBI", 88),
        _scored_candidate("row-other-walks", "Other Player", "Batter Walks", 72),
    ]

    summary = _apply_within_player_market_contest(rows)
    selected = _select_ranked_candidates(
        rows,
        mode="best_available",
        legs_per_game=None,
        max_total_legs=2,
        max_candidates_per_game=8,
        max_total_candidates=10,
    )

    same_winner = rows[0]
    same_alternative = rows[1]

    assert summary["policy"] == "within_player_winner_first"
    assert summary["playerGroups"] == 2
    assert summary["contestedPlayerGroups"] == 1
    assert same_winner["marketContestRank"] == 1
    assert same_winner["marketContestWinner"] is True
    assert same_winner["marketContest"]["topAlternatives"][0]["rowId"] == "row-same-rbi"
    assert "player_market_fit_winner" in same_winner["reasonTags"]
    assert same_alternative["marketContestRank"] == 2
    assert "player_market_fit_alternative" in same_alternative["reasonTags"]
    assert [row["rowId"] for row in selected] == [
        "row-same-singles",
        "row-other-walks",
    ]


def test_singles_abundant_but_alternative_markets_win_when_scores_are_better():
    rows = [
        _scored_candidate("row-a-singles", "Player A", "Singles", 82),
        _scored_candidate("row-a-rbi", "Player A", "RBI", 91),
        _scored_candidate("row-b-singles", "Player B", "Singles", 84),
        _scored_candidate("row-b-runs", "Player B", "Runs", 93),
        _scored_candidate("row-c-singles", "Player C", "Singles", 81),
        _scored_candidate("row-c-hrr", "Player C", "Hits + Runs + RBIs", 92),
    ]

    _apply_within_player_market_contest(rows)
    _apply_game_level_contest(rows, legs_per_game=3, max_candidates_per_game=8)
    selected = _select_ranked_candidates(
        rows,
        mode="per_game",
        legs_per_game=3,
        max_total_legs=3,
        max_candidates_per_game=8,
        max_total_candidates=10,
    )

    assert [row["market"] for row in selected] == [
        "Runs",
        "Hits + Runs + RBIs",
        "RBI",
    ]
    assert all(row["marketContestWinner"] is True for row in selected)
    assert all(row["selectionProof"]["closestAlternativeMarket"] == "Singles" for row in selected)


def test_singles_can_dominate_when_they_win_on_merit_and_only_warns_diagnostically():
    rows = []
    for index in range(5):
        player = f"Singles Player {index}"
        rows.append(_scored_candidate(f"row-{index}-singles", player, "Singles", 94 - index))
        rows.append(_scored_candidate(f"row-{index}-hits", player, "Hits", 80 - index))

    _apply_within_player_market_contest(rows)
    _apply_game_level_contest(rows, legs_per_game=5, max_candidates_per_game=8)
    selected = _select_ranked_candidates(
        rows,
        mode="per_game",
        legs_per_game=5,
        max_total_legs=5,
        max_candidates_per_game=8,
        max_total_candidates=10,
    )
    before = [row["rowId"] for row in selected]
    concentration = _market_concentration_diagnostics(selected)
    after = [row["rowId"] for row in selected]

    assert [row["market"] for row in selected] == ["Singles"] * 5
    assert concentration["diagnosticOnly"] is True
    assert concentration["warnings"]
    assert before == after
    assert all("beat closest alternative" in row["selectionProof"]["whySelectedBeatAlternative"] for row in selected)
    assert all(row["selectionProof"]["closestAlternativeMarket"] == "Hits" for row in selected)


def test_hits_do_not_win_by_default_when_total_bases_or_hrr_scores_better():
    rows = [
        _scored_candidate("row-power-hits", "Power Bat", "Hits", 87),
        _scored_candidate("row-power-total-bases", "Power Bat", "Total Bases", 93),
        _scored_candidate("row-table-hits", "Table Setter", "Hits", 86),
        _scored_candidate("row-table-hrr", "Table Setter", "Hits + Runs + RBIs", 92),
    ]

    _apply_within_player_market_contest(rows)
    selected = [row for row in rows if row.get("marketContestWinner")]

    assert [row["market"] for row in sorted(selected, key=lambda row: row["player"])] == [
        "Total Bases",
        "Hits + Runs + RBIs",
    ]


def test_market_concentration_and_exposure_never_change_scores_or_picks_by_themselves():
    candidate = {
        "odds": 2.0,
        "side": "under",
        "context": {
            "last10": {"sideHitRate": 0.8},
            "last15": {"sideHitRate": 0.8},
            "season": {"sideSupported": True},
        },
        "normalizedMarketKey": "singles",
    }

    base = score_sgm_candidate(candidate, mode="best_available", market_exposure_count=0)
    repeated = score_sgm_candidate(
        candidate,
        mode="best_available",
        market_exposure_count=99,
        max_market_repeats=1,
    )

    assert repeated["score"] == base["score"]
    assert repeated["marketExposurePenalty"] == 0.0
    assert "market_repeat_cap_reached" not in repeated["riskFlags"]
    assert "market_exposure_soft_penalty" not in repeated["reasonTags"]


def test_selected_leg_proof_includes_closest_alternative_and_rejected_alternatives():
    rows = [
        _scored_candidate("row-proof-singles", "Proof Player", "Singles", 95),
        _scored_candidate("row-proof-hits", "Proof Player", "Hits", 89),
        _scored_candidate("row-proof-rbi", "Proof Player", "RBI", 80),
    ]

    _apply_within_player_market_contest(rows)
    winner = next(row for row in rows if row["rowId"] == "row-proof-singles")
    proof = winner["selectionProof"]

    assert proof["selectedMarket"] == "Singles"
    assert proof["selectedScore"] == 95
    assert proof["closestAlternativeMarket"] == "Hits"
    assert proof["closestAlternativeScore"] == 89
    assert proof["availabilityRole"] == "eligibility_only"
    assert proof["contextQuality"] == "full"
    assert proof["rejectedAlternatives"][0]["market"] == "Hits"
    assert proof["rejectedAlternatives"][0]["reasonLost"]
    assert proof["rejectedAlternatives"][0]["lossType"] == "lower_merit"


def test_availability_clickability_and_data_depth_are_not_direct_merit_bonuses():
    context = {
        "last10": {"sideHitRate": 0.75},
        "last15": {"sideHitRate": 0.75},
        "season": {"sideSupported": True},
    }
    base = score_sgm_candidate(
        {
            "odds": 2.0,
            "side": "under",
            "context": context,
            "normalizedMarketKey": "singles",
            "rowId": "clickable-row",
            "playable": True,
            "customBet": True,
            "contextQuality": "full",
        },
        mode="best_available",
    )
    changed_availability_metadata = score_sgm_candidate(
        {
            "odds": 2.0,
            "side": "under",
            "context": context,
            "normalizedMarketKey": "singles",
            "rowId": "",
            "playable": False,
            "customBet": False,
            "contextQuality": "unsupported",
        },
        mode="best_available",
    )

    assert changed_availability_metadata["score"] == base["score"]
    assert changed_availability_metadata["availabilityRole"] == "eligibility_only"
    assert changed_availability_metadata["dataDepthRole"] == "confidence_cap_not_direct_merit_bonus"


def test_candidate_pool_ranks_context_backed_rows_and_skips_weak_per_game():
    boards = [
        {
            "source": "stake_ui_sgm",
            "fixtureSlug": "fixture-a",
            "capturedAt": _fresh_captured_at(),
            "playerProps": [
                _row("Strong Under", "Test A", "Singles", 2.25),
                _row("Diverse Under", "Test A", "Batter Walks", 2.05),
            ],
            "teamMarkets": [],
        },
        {
            "source": "stake_ui_sgm",
            "fixtureSlug": "fixture-b",
            "capturedAt": _fresh_captured_at(),
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
            use_bet_history_signals=False,
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
