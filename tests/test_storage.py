from datetime import datetime, timezone

from app.storage import SnapshotStore


def _enriched_payload():
    return {
        "date": "2026-05-08",
        "timezone": "America/New_York",
        "fixtureCount": 1,
        "propCount": 1,
        "matchedPropCount": 1,
        "unmatchedPropCount": 0,
        "props": [
            {
                "propId": "reds-astros:jose-altuve:houston-astros:hits",
                "fixtureSlug": "reds-astros",
                "game": "Cincinnati Reds - Houston Astros",
                "player": {
                    "name": "Jose Altuve",
                    "key": "jose-altuve",
                    "mlbId": 514888,
                    "matchStatus": "matched_exact_name_team",
                },
                "team": {
                    "name": "Houston Astros",
                    "key": "houston-astros",
                    "mlbId": 117,
                },
                "market": {"name": "hits", "key": "hits"},
                "line": 0.5,
                "odds": {"over": 1.8, "under": 1.9},
                "mlbGame": {
                    "gamePk": 824522,
                    "gameDate": "2026-05-08T23:05:00Z",
                    "status": "Final",
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
                "mlbProfile": {
                    "player": {
                        "mlbId": 514888,
                        "name": "Jose Altuve",
                        "stats": {"hits": 51},
                    },
                    "season": 2026,
                    "group": "hitting",
                },
                "recentHistory": {
                    "playerId": 514888,
                    "season": 2026,
                    "group": "hitting",
                    "gamesUsed": 3,
                    "games": [
                        {
                            "date": "2026-05-07",
                            "opponent": "Cincinnati Reds",
                            "stats": {"hits": 2},
                        }
                    ],
                    "totals": {"hits": 5.0},
                    "perGame": {"hits": 1.6667},
                },
                "statContext": {
                    "statKey": "hits",
                    "seasonValue": 51,
                    "recentTotal": 5.0,
                    "recentPerGame": 1.6667,
                    "gamesUsed": 3,
                },
            }
        ],
    }


def test_snapshot_store_writes_enriched_props_and_player_stats(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    captured_at = datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc)

    result = store.save_enriched_props(
        _enriched_payload(),
        captured_at=captured_at,
        snapshot_phase="pregame",
        snapshot_label="morning board",
    )

    assert result == {
        "propSnapshotsInserted": 1,
        "playerStatSnapshotsInserted": 1,
    }
    assert (tmp_path / "azp.sqlite").exists()

    props = store.list_prop_snapshots(date_text="2026-05-08")
    assert len(props) == 1
    assert props[0]["capturedAt"] == "2026-05-08T16:30:00+00:00"
    assert props[0]["snapshotPhase"] == "pregame"
    assert props[0]["snapshotLabel"] == "morning board"
    assert props[0]["propId"] == "reds-astros:jose-altuve:houston-astros:hits"
    assert props[0]["mlbGamePk"] == 824522
    assert props[0]["playerName"] == "Jose Altuve"
    assert props[0]["playerMlbId"] == 514888
    assert props[0]["teamName"] == "Houston Astros"
    assert props[0]["teamMlbId"] == 117
    assert props[0]["marketKey"] == "hits"
    assert props[0]["line"] == 0.5
    assert props[0]["overOdds"] == 1.8
    assert props[0]["underOdds"] == 1.9
    assert props[0]["matchStatus"] == "matched_exact_name_team"
    assert props[0]["statKey"] == "hits"
    assert props[0]["recentPerGame"] == 1.6667
    assert props[0]["recentGames"][0]["stats"]["hits"] == 2
    assert props[0]["seasonStats"]["hits"] == 51

    stats = store.list_player_stat_snapshots(player_mlb_id=514888)
    assert len(stats) == 1
    assert stats[0]["playerName"] == "Jose Altuve"
    assert stats[0]["snapshotPhase"] == "pregame"
    assert stats[0]["snapshotLabel"] == "morning board"
    assert stats[0]["season"] == 2026
    assert stats[0]["groupName"] == "hitting"
    assert stats[0]["stats"]["hits"] == 51
    assert stats[0]["recentHistory"]["perGame"]["hits"] == 1.6667


def test_snapshot_store_returns_line_movement_for_prop(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    first = _enriched_payload()
    second = _enriched_payload()
    second["props"][0]["line"] = 1.5
    second["props"][0]["odds"] = {"over": 2.05, "under": 1.72}

    store.save_enriched_props(
        first,
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    store.save_enriched_props(
        second,
        captured_at=datetime(2026, 5, 8, 16, 45, tzinfo=timezone.utc),
        snapshot_phase="live",
        snapshot_label="line moved",
    )

    movement = store.get_prop_movement(
        "reds-astros:jose-altuve:houston-astros:hits"
    )

    assert movement["propId"] == "reds-astros:jose-altuve:houston-astros:hits"
    assert movement["snapshotCount"] == 2
    assert movement["snapshots"][0]["line"] == 0.5
    assert movement["snapshots"][0]["snapshotPhase"] == "pregame"
    assert movement["snapshots"][1]["line"] == 1.5
    assert movement["snapshots"][1]["snapshotPhase"] == "live"
    assert movement["snapshots"][1]["snapshotLabel"] == "line moved"
    assert movement["changes"] == [
        {
            "fromCapturedAt": "2026-05-08T16:30:00+00:00",
            "toCapturedAt": "2026-05-08T16:45:00+00:00",
            "line": 1.0,
            "overOdds": 0.25,
            "underOdds": -0.18,
        }
    ]


def test_snapshot_store_dedupes_same_prop_id_inside_one_refresh(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    payload = _enriched_payload()
    duplicate = _enriched_payload()["props"][0]
    duplicate["odds"] = {"over": 2.05, "under": 1.72}
    payload["props"] = [payload["props"][0], duplicate]
    payload["propCount"] = 2

    result = store.save_enriched_props(
        payload,
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
    )

    props = store.list_prop_snapshots(date_text="2026-05-08")
    assert result["propSnapshotsInserted"] == 1
    assert len(props) == 1
    assert props[0]["overOdds"] == 2.05
    assert props[0]["underOdds"] == 1.72


def test_snapshot_store_prefers_balanced_primary_line_when_duplicate_lines_are_saved(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    payload = _enriched_payload()
    alternate = _enriched_payload()["props"][0]
    alternate["line"] = 1.5
    alternate["odds"] = {"over": 3.2, "under": 1.2}
    payload["props"] = [payload["props"][0], alternate]
    payload["propCount"] = 2

    result = store.save_enriched_props(
        payload,
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
    )

    props = store.list_prop_snapshots(date_text="2026-05-08")
    assert result["propSnapshotsInserted"] == 1
    assert len(props) == 1
    assert props[0]["line"] == 0.5
    assert props[0]["overOdds"] == 1.8
    assert props[0]["underOdds"] == 1.9


def test_snapshot_store_filters_prop_snapshots_by_phase(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    store.save_enriched_props(
        _enriched_payload(),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    store.save_enriched_props(
        _enriched_payload(),
        captured_at=datetime(2026, 5, 8, 23, 30, tzinfo=timezone.utc),
        snapshot_phase="postgame",
        snapshot_label="after final",
    )

    props = store.list_prop_snapshots(
        date_text="2026-05-08",
        snapshot_phase="postgame",
    )

    assert len(props) == 1
    assert props[0]["snapshotPhase"] == "postgame"
    assert props[0]["snapshotLabel"] == "after final"


def test_snapshot_store_returns_latest_snapshot_per_prop(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    first = _enriched_payload()
    second = _enriched_payload()
    second["props"][0]["line"] = 1.5
    second["props"][0]["odds"] = {"over": 2.05, "under": 1.72}

    store.save_enriched_props(
        first,
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
        snapshot_label="first board",
    )
    store.save_enriched_props(
        second,
        captured_at=datetime(2026, 5, 8, 16, 45, tzinfo=timezone.utc),
        snapshot_phase="pregame",
        snapshot_label="latest board",
    )

    props = store.list_latest_prop_snapshots(
        date_text="2026-05-08",
        market="hits",
        snapshot_phase="pregame",
    )

    assert len(props) == 1
    assert props[0]["line"] == 1.5
    assert props[0]["overOdds"] == 2.05
    assert props[0]["snapshotLabel"] == "latest board"


def test_snapshot_store_writes_latest_analysis_decisions(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    first = {
        "date": "2026-05-08",
        "market": "hits",
        "snapshotPhase": "pregame",
        "buckets": {
            "watchlist": [
                {
                    "propId": "one",
                    "playerName": "Jose Altuve",
                        "teamName": "Houston Astros",
                        "fixtureSlug": "reds-astros",
                        "marketKey": "hits",
                        "line": 0.5,
                        "recentPerGame": 1.6667,
                        "seasonValue": 51,
                        "seasonPerGame": 1.02,
                        "seasonEdge": 0.52,
                        "gamesUsed": 3,
                        "recentGames": [
                            {
                                "date": "2026-05-07",
                                "opponent": "Cincinnati Reds",
                                "stats": {"hits": 2},
                            }
                        ],
                    "bucket": "watchlist",
                    "lean": "over",
                    "edge": 0.5,
                    "score": 84,
                    "confidence": "high",
                    "marketProfile": "standard_count",
                        "marketThreshold": 0.25,
                        "overOdds": 1.8,
                        "underOdds": 1.9,
                        "riskFlags": [],
                    "reasons": ["recent_per_game_above_line"],
                }
            ],
            "neutral": [],
            "avoid": [],
        },
    }
    second = {
        **first,
        "buckets": {
            "watchlist": [
                {
                    **first["buckets"]["watchlist"][0],
                    "score": 91,
                    "riskFlags": ["market_moved_against_over"],
                }
            ],
            "neutral": [],
            "avoid": [],
        },
    }

    saved_first = store.save_analysis_result(
        first,
        captured_at=datetime(2026, 5, 8, 16, 31, tzinfo=timezone.utc),
        snapshot_label="first pass",
    )
    store.save_analysis_result(
        second,
        captured_at=datetime(2026, 5, 8, 16, 45, tzinfo=timezone.utc),
        snapshot_label="latest pass",
    )

    rows = store.list_latest_analysis_snapshots(
        date_text="2026-05-08",
        market="hits",
        snapshot_phase="pregame",
    )

    assert saved_first == {"analysisSnapshotsInserted": 1}
    assert len(rows) == 1
    assert rows[0]["propId"] == "one"
    assert rows[0]["score"] == 91
    assert rows[0]["fixtureSlug"] == "reds-astros"
    assert rows[0]["overOdds"] == 1.8
    assert rows[0]["recentPerGame"] == 1.6667
    assert rows[0]["seasonValue"] == 51
    assert rows[0]["seasonPerGame"] == 1.02
    assert rows[0]["seasonEdge"] == 0.52
    assert rows[0]["gamesUsed"] == 3
    assert rows[0]["recentGames"][0]["stats"]["hits"] == 2
    assert rows[0]["snapshotLabel"] == "latest pass"
    assert rows[0]["riskFlags"] == ["market_moved_against_over"]
    assert rows[0]["reasons"] == ["recent_per_game_above_line"]
