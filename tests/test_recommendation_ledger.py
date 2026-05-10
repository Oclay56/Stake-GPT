import asyncio
from datetime import datetime, timezone

from app.recommendations import (
    settle_recommendation_legs,
    summarize_recommendation_performance,
)
from app.storage import SnapshotStore


class FakeRecommendationSettlementEngine:
    def __init__(self, history_by_player):
        self.history_by_player = history_by_player
        self.calls = []

    async def get_player_recent_history(self, player_id, group="hitting", season=None, limit=30):
        self.calls.append(
            {
                "playerId": player_id,
                "group": group,
                "season": season,
                "limit": limit,
            }
        )
        return self.history_by_player.get(player_id, {"games": []})


def _gpt_response():
    return {
        "source": "live_stake_odds_plus_mlb_stats",
        "readOnly": True,
        "matchup": "Blue Jays vs Angels",
        "date": "2026-05-08",
        "timezone": "America/New_York",
        "filters": {
            "markets": ["hits", "runs"],
            "side": "under",
            "legs": 2,
            "mode": "sgp",
            "diversityMode": "balanced",
        },
        "matchedFixtureCount": 1,
        "availablePropCount": 12,
        "matchedPropCount": 10,
        "unmatchedPropCount": 2,
        "recommendationCount": 2,
        "recommendationDiagnostics": {
            "concentrationTags": ["same_side_cluster:under", "low_scoring_script"],
            "marketCounts": {"hits": 1, "runs": 1},
            "softDiversityPromotions": 1,
        },
        "recommendations": [
            {
                "rank": 1,
                "bucket": "watchlist",
                "propId": "blue-jays-angels:george-springer:toronto-blue-jays:hits",
                "fixtureSlug": "blue-jays-angels",
                "game": "Toronto Blue Jays - Los Angeles Angels",
                "playerName": "George Springer",
                "teamName": "Toronto Blue Jays",
                "marketKey": "hits",
                "statKey": "hits",
                "line": 0.5,
                "lean": "under_or_avoid_over",
                "side": "under",
                "odds": 2.9,
                "overOdds": 1.34,
                "underOdds": 2.9,
                "edge": 0.5,
                "score": 87,
                "confidence": "high",
                "selection": "George Springer under 0.5 hits",
                "player": {
                    "name": "George Springer",
                    "key": "george-springer",
                    "mlbId": 543807,
                    "matchStatus": "matched_exact_name_team",
                },
                "team": {
                    "name": "Toronto Blue Jays",
                    "key": "toronto-blue-jays",
                    "mlbId": 141,
                },
                "mlbGame": {"gamePk": 111},
                "recent5": {
                    "gamesUsed": 5,
                    "total": 0,
                    "perGame": 0,
                    "games": [],
                },
                "season": {"value": 12, "perGame": 0.5, "stats": {"gamesPlayed": 24}},
                "riskFlags": [],
                "reasons": ["recent_per_game_below_line", "recent_and_season_agree"],
                "contextualEdge": {
                    "tags": ["hit_distribution_clustered_0_1"],
                    "notes": [],
                    "scoreAdjustment": 2,
                    "deferredLayers": ["umpire_impact"],
                },
            },
            {
                "rank": 2,
                "bucket": "watchlist",
                "propId": "blue-jays-angels:bo-bichette:toronto-blue-jays:runs",
                "fixtureSlug": "blue-jays-angels",
                "game": "Toronto Blue Jays - Los Angeles Angels",
                "playerName": "Bo Bichette",
                "teamName": "Toronto Blue Jays",
                "marketKey": "runs",
                "statKey": "runs",
                "line": 0.5,
                "lean": "under_or_avoid_over",
                "side": "under",
                "odds": 1.78,
                "overOdds": 2.2,
                "underOdds": 1.78,
                "edge": 0.4,
                "score": 78,
                "confidence": "medium",
                "selection": "Bo Bichette under 0.5 runs",
                "player": {
                    "name": "Bo Bichette",
                    "key": "bo-bichette",
                    "mlbId": 666182,
                    "matchStatus": "matched_exact_name_team",
                },
                "team": {
                    "name": "Toronto Blue Jays",
                    "key": "toronto-blue-jays",
                    "mlbId": 141,
                },
                "mlbGame": {"gamePk": 111},
                "recent5": {
                    "gamesUsed": 5,
                    "total": 0,
                    "perGame": 0,
                    "games": [],
                },
                "season": {"value": 20, "perGame": 0.6, "stats": {"gamesPlayed": 33}},
                "riskFlags": ["game_script_dependent_counting_stat"],
                "reasons": ["recent_per_game_below_line"],
                "contextualEdge": {
                    "tags": ["run_production_market"],
                    "notes": [],
                    "scoreAdjustment": -4,
                    "deferredLayers": ["umpire_impact"],
                },
            },
        ],
        "parlay": {
            "requested": {"legs": 2, "mode": "sgp"},
            "candidates": [],
        },
        "notes": ["Concentration flagged: same_side_cluster:under."],
    }


def test_snapshot_store_saves_exact_gpt_recommendation_response(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")

    result = store.save_recommendation_result(
        _gpt_response(),
        captured_at=datetime(2026, 5, 8, 16, 0, tzinfo=timezone.utc),
        request_params={"matchup": "Blue Jays vs Angels", "side": "under"},
    )

    assert result["recommendationRequestsInserted"] == 1
    assert result["recommendationLegsInserted"] == 2

    requests = store.list_recommendation_requests(date_text="2026-05-08")
    assert len(requests) == 1
    assert requests[0]["matchup"] == "Blue Jays vs Angels"
    assert requests[0]["diversityMode"] == "balanced"
    assert requests[0]["concentrationTags"] == [
        "same_side_cluster:under",
        "low_scoring_script",
    ]

    legs = store.list_recommendation_legs(date_text="2026-05-08")
    assert [leg["selection"] for leg in legs] == [
        "George Springer under 0.5 hits",
        "Bo Bichette under 0.5 runs",
    ]
    assert legs[0]["requestId"] == requests[0]["requestId"]
    assert legs[0]["playerMlbId"] == 543807
    assert legs[0]["mlbGamePk"] == 111
    assert legs[0]["marketKey"] == "hits"
    assert legs[0]["side"] == "under"
    assert legs[0]["odds"] == 2.9
    assert legs[0]["score"] == 87
    assert legs[0]["contextualTags"] == ["hit_distribution_clustered_0_1"]
    assert legs[0]["deferredLayers"] == ["umpire_impact"]


def test_settle_recommendation_legs_grades_exact_gpt_picks(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_recommendation_result(
        _gpt_response(),
        captured_at=datetime(2026, 5, 8, 16, 0, tzinfo=timezone.utc),
    )
    engine = FakeRecommendationSettlementEngine(
        {
            543807: {
                "games": [
                    {"gamePk": 111, "date": "2026-05-08", "stats": {"hits": 0}},
                ]
            },
            666182: {
                "games": [
                    {"gamePk": 111, "date": "2026-05-08", "stats": {"runs": 1}},
                ]
            },
        }
    )

    result = asyncio.run(
        settle_recommendation_legs(
            store,
            engine,
            date_text="2026-05-08",
        )
    )

    assert result["counts"]["settled"] == 2
    assert result["counts"]["correct"] == 1
    assert result["counts"]["incorrect"] == 1
    assert result["rows"][0]["decisionOutcome"] == "correct"
    assert result["rows"][1]["decisionOutcome"] == "incorrect"

    saved = store.list_recommendation_settlements(date_text="2026-05-08")
    assert len(saved) == 2
    assert saved[0]["actualValue"] == 0.0
    assert saved[0]["decisionOutcome"] == "correct"


def test_recommendation_performance_summary_groups_by_market_side_and_tags(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_recommendation_result(
        _gpt_response(),
        captured_at=datetime(2026, 5, 8, 16, 0, tzinfo=timezone.utc),
    )
    engine = FakeRecommendationSettlementEngine(
        {
            543807: {
                "games": [
                    {"gamePk": 111, "date": "2026-05-08", "stats": {"hits": 0}},
                ]
            },
            666182: {
                "games": [
                    {"gamePk": 111, "date": "2026-05-08", "stats": {"runs": 1}},
                ]
            },
        }
    )
    asyncio.run(settle_recommendation_legs(store, engine, date_text="2026-05-08"))

    summary = summarize_recommendation_performance(store, date_text="2026-05-08")

    assert summary["counts"]["legs"] == 2
    assert summary["counts"]["settled"] == 2
    assert summary["counts"]["correct"] == 1
    assert summary["byMarket"]["hits"]["accuracy"] == 1.0
    assert summary["byMarket"]["runs"]["accuracy"] == 0.0
    assert summary["bySide"]["under"]["decisions"] == 2
    assert summary["byRiskFlag"]["game_script_dependent_counting_stat"]["incorrect"] == 1
    assert summary["byContextualTag"]["run_production_market"]["incorrect"] == 1
    assert summary["byDiversityMode"]["balanced"]["decisions"] == 2
