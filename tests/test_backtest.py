import asyncio
from datetime import datetime, timezone

from app.backtest import backtest_stored_props
from app.storage import SnapshotStore


class FakeBacktestEngine:
    def __init__(self, histories):
        self.histories = histories

    async def get_player_recent_history(self, player_id, group="hitting", season=None, limit=30):
        return self.histories.get(player_id, {"games": []})


def _prop_payload(props):
    return {
        "date": "2026-05-08",
        "timezone": "America/New_York",
        "props": props,
    }


def _prop(prop_id, player_id, name, line, recent_per_game):
    return {
        "propId": prop_id,
        "fixtureSlug": "reds-astros",
        "game": "Cincinnati Reds - Houston Astros",
        "player": {
            "name": name,
            "key": name.lower().replace(" ", "-"),
            "mlbId": player_id,
            "matchStatus": "matched_exact_name_team",
        },
        "team": {
            "name": "Houston Astros",
            "key": "houston-astros",
            "mlbId": 117,
        },
        "market": {"name": "hits", "key": "hits"},
        "line": line,
        "odds": {"over": 1.8, "under": 1.9},
        "mlbGame": {
            "gamePk": 824522,
            "awayTeam": {"mlbId": 113, "name": "Cincinnati Reds"},
            "homeTeam": {"mlbId": 117, "name": "Houston Astros"},
        },
        "mlbProfile": {
            "player": {
                "mlbId": player_id,
                "name": name,
                "stats": {"hits": 51},
            },
            "season": 2026,
            "group": "hitting",
        },
        "recentHistory": {
            "playerId": player_id,
            "season": 2026,
            "group": "hitting",
            "gamesUsed": 3,
            "totals": {"hits": recent_per_game * 3},
            "perGame": {"hits": recent_per_game},
        },
        "statContext": {
            "statKey": "hits",
            "seasonValue": 51,
            "recentTotal": recent_per_game * 3,
            "recentPerGame": recent_per_game,
            "gamesUsed": 3,
        },
    }


def test_backtest_stored_props_summarizes_analyzer_accuracy(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_enriched_props(
        _prop_payload(
            [
                _prop("watch-win", 1, "Watch Winner", 0.5, 1.2),
                _prop("avoid-win", 2, "Avoid Winner", 1.5, 0.3),
                _prop("neutral-row", 3, "Neutral Row", 0.5, 0.6),
            ]
        ),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    engine = FakeBacktestEngine(
        {
            1: {"games": [{"gamePk": 824522, "date": "2026-05-08", "stats": {"hits": 2}}]},
            2: {"games": [{"gamePk": 824522, "date": "2026-05-08", "stats": {"hits": 0}}]},
            3: {"games": [{"gamePk": 824522, "date": "2026-05-08", "stats": {"hits": 1}}]},
        }
    )

    result = asyncio.run(
        backtest_stored_props(
            store,
            engine,
            date_text="2026-05-08",
            market="hits",
            snapshot_phase="pregame",
            min_edge=0.25,
        )
    )

    assert result["counts"] == {
        "rows": 3,
        "settled": 3,
        "pending": 0,
        "ungraded": 0,
        "decisions": 2,
        "correct": 2,
        "incorrect": 0,
        "push": 0,
        "noDecision": 1,
    }
    assert result["byBucket"]["watchlist"]["accuracy"] == 1.0
    assert result["byBucket"]["avoid"]["accuracy"] == 1.0
    assert result["byBucket"]["neutral"]["accuracy"] is None
    assert result["byConfidence"]["high"]["correct"] == 2
    assert result["rows"][0]["playerName"] == "Watch Winner"
    assert result["rows"][0]["bucket"] == "watchlist"
    assert result["rows"][0]["decisionOutcome"] == "correct"
    assert result["rows"][1]["playerName"] == "Avoid Winner"
    assert result["rows"][1]["lean"] == "under_or_avoid_over"
    assert result["rows"][1]["decisionOutcome"] == "correct"
    assert result["rows"][2]["decisionOutcome"] == "no_decision"


def test_backtest_stored_props_marks_pending_rows_without_accuracy(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_enriched_props(
        _prop_payload([_prop("pending", 1, "Pending Player", 0.5, 1.2)]),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    engine = FakeBacktestEngine({1: {"games": []}})

    result = asyncio.run(
        backtest_stored_props(
            store,
            engine,
            date_text="2026-05-08",
            market="hits",
            snapshot_phase="pregame",
        )
    )

    assert result["counts"]["pending"] == 1
    assert result["counts"]["decisions"] == 0
    assert result["rows"][0]["decisionOutcome"] == "not_settled"
    assert result["byBucket"]["watchlist"]["accuracy"] is None


def test_backtest_prefers_saved_analysis_decisions(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_enriched_props(
        _prop_payload([_prop("saved-decision", 1, "Saved Decision", 0.5, 1.2)]),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    store.save_analysis_result(
        {
            "date": "2026-05-08",
            "snapshotPhase": "pregame",
            "buckets": {
                "watchlist": [],
                "neutral": [],
                "avoid": [
                    {
                        "propId": "saved-decision",
                        "playerName": "Saved Decision",
                        "teamName": "Houston Astros",
                        "marketKey": "hits",
                        "line": 0.5,
                        "bucket": "avoid",
                        "lean": "under_or_avoid_over",
                        "edge": -0.5,
                        "score": 25,
                        "confidence": "high",
                        "marketProfile": "standard_count",
                        "marketThreshold": 0.25,
                        "riskFlags": [],
                        "reasons": ["saved_test_decision"],
                    }
                ],
            },
        },
        captured_at=datetime(2026, 5, 8, 16, 31, tzinfo=timezone.utc),
    )
    engine = FakeBacktestEngine(
        {
            1: {"games": [{"gamePk": 824522, "date": "2026-05-08", "stats": {"hits": 0}}]},
        }
    )

    result = asyncio.run(
        backtest_stored_props(
            store,
            engine,
            date_text="2026-05-08",
            market="hits",
            snapshot_phase="pregame",
            min_edge=99,
        )
    )

    assert result["source"] == "saved_analysis"
    assert result["rows"][0]["bucket"] == "avoid"
    assert result["rows"][0]["decisionOutcome"] == "correct"
