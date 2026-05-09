import asyncio
from datetime import datetime, timezone

from app.settlement import settle_stored_props
from app.storage import SnapshotStore


class FakeSettlementEngine:
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


def _prop_payload(props):
    return {
        "date": "2026-05-08",
        "timezone": "America/New_York",
        "props": props,
    }


def _prop(
    prop_id,
    player_id,
    player_name,
    market_key="hits",
    stat_key="hits",
    line=0.5,
    game_pk=None,
):
    prop = {
        "propId": prop_id,
        "fixtureSlug": "reds-astros",
        "game": "Cincinnati Reds - Houston Astros",
        "player": {
            "name": player_name,
            "key": player_name.lower().replace(" ", "-"),
            "mlbId": player_id,
            "matchStatus": "matched_exact_name_team",
        },
        "team": {
            "name": "Houston Astros",
            "key": "houston-astros",
            "mlbId": 117,
        },
        "market": {"name": market_key, "key": market_key},
        "line": line,
        "odds": {"over": 1.8, "under": 1.9},
        "mlbProfile": {
            "player": {
                "mlbId": player_id,
                "name": player_name,
                "stats": {stat_key: 51} if stat_key else {},
            },
            "season": 2026,
            "group": "hitting",
        },
        "recentHistory": {
            "playerId": player_id,
            "season": 2026,
            "group": "hitting",
            "gamesUsed": 3,
            "totals": {stat_key: 4.0} if stat_key else {},
            "perGame": {stat_key: 1.3333} if stat_key else {},
        },
        "statContext": {
            "statKey": stat_key,
            "seasonValue": 51,
            "recentTotal": 4.0,
            "recentPerGame": 1.3333,
            "gamesUsed": 3,
        },
    }
    if game_pk is not None:
        prop["mlbGame"] = {
            "gamePk": game_pk,
            "awayTeam": {"mlbId": 113, "name": "Cincinnati Reds"},
            "homeTeam": {"mlbId": 117, "name": "Houston Astros"},
        }
    return prop


def test_settle_stored_props_grades_latest_saved_prop_against_game_log(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_enriched_props(
        _prop_payload([_prop("hit-one", 514888, "Jose Altuve", line=0.5)]),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    engine = FakeSettlementEngine(
        {
            514888: {
                "games": [
                    {
                        "date": "2026-05-08",
                        "stats": {"hits": 2},
                    }
                ]
            }
        }
    )

    result = asyncio.run(
        settle_stored_props(
            store,
            engine,
            date_text="2026-05-08",
            market="hits",
            snapshot_phase="pregame",
        )
    )

    assert result["counts"] == {
        "settled": 1,
        "pending": 0,
        "ungraded": 0,
        "over": 1,
        "under": 0,
        "push": 0,
    }
    row = result["rows"][0]
    assert row["playerName"] == "Jose Altuve"
    assert row["actualValue"] == 2.0
    assert row["actualResult"] == "over"
    assert row["overOutcome"] == "win"
    assert row["reasons"] == []
    assert engine.calls == [
        {"playerId": 514888, "group": "hitting", "season": 2026, "limit": 30}
    ]


def test_settle_stored_props_marks_missing_game_log_pending(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_enriched_props(
        _prop_payload([_prop("hit-one", 514888, "Jose Altuve", line=0.5)]),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    engine = FakeSettlementEngine({514888: {"games": []}})

    result = asyncio.run(
        settle_stored_props(
            store,
            engine,
            date_text="2026-05-08",
            market="hits",
            snapshot_phase="pregame",
        )
    )

    assert result["counts"]["pending"] == 1
    assert result["rows"][0]["overOutcome"] == "pending"
    assert result["rows"][0]["reasons"] == ["no_game_log_for_date"]


def test_settle_stored_props_marks_doubleheaders_ungraded(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_enriched_props(
        _prop_payload([_prop("hit-one", 514888, "Jose Altuve", line=0.5)]),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    engine = FakeSettlementEngine(
        {
            514888: {
                "games": [
                    {"date": "2026-05-08", "stats": {"hits": 1}},
                    {"date": "2026-05-08", "stats": {"hits": 2}},
                ]
            }
        }
    )

    result = asyncio.run(
        settle_stored_props(
            store,
            engine,
            date_text="2026-05-08",
            market="hits",
            snapshot_phase="pregame",
        )
    )

    assert result["counts"]["ungraded"] == 1
    assert result["rows"][0]["overOutcome"] == "ungraded"
    assert result["rows"][0]["reasons"] == ["multiple_game_logs_for_date"]


def test_settle_stored_props_uses_stored_game_pk_for_doubleheaders(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_enriched_props(
        _prop_payload(
            [_prop("hit-one", 514888, "Jose Altuve", line=0.5, game_pk=222)]
        ),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    engine = FakeSettlementEngine(
        {
            514888: {
                "games": [
                    {"gamePk": 111, "date": "2026-05-08", "stats": {"hits": 0}},
                    {"gamePk": 222, "date": "2026-05-08", "stats": {"hits": 2}},
                ]
            }
        }
    )

    result = asyncio.run(
        settle_stored_props(
            store,
            engine,
            date_text="2026-05-08",
            market="hits",
            snapshot_phase="pregame",
        )
    )

    assert result["counts"]["settled"] == 1
    row = result["rows"][0]
    assert row["mlbGamePk"] == 222
    assert row["actualValue"] == 2.0
    assert row["overOutcome"] == "win"


def test_settle_stored_props_reuses_history_for_same_player(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_enriched_props(
        _prop_payload(
            [
                _prop("hit-one", 514888, "Jose Altuve", line=0.5),
                _prop("run-one", 514888, "Jose Altuve", "runs", "runs", line=0.5),
            ]
        ),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    engine = FakeSettlementEngine(
        {
            514888: {
                "games": [
                    {
                        "date": "2026-05-08",
                        "stats": {"hits": 1, "runs": 1},
                    }
                ]
            }
        }
    )

    result = asyncio.run(
        settle_stored_props(
            store,
            engine,
            date_text="2026-05-08",
            snapshot_phase="pregame",
        )
    )

    assert result["counts"]["settled"] == 2
    assert len(engine.calls) == 1
