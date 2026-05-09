from datetime import datetime, timezone

from app.analyzer import analyze_stored_props
from app.storage import SnapshotStore


def _prop_payload(prop):
    return {
        "date": "2026-05-08",
        "timezone": "America/New_York",
        "props": [prop],
    }


def _prop(
    prop_id,
    player_name,
    line,
    recent_per_game,
    match_status="matched_exact_name_team",
    stat_key="hits",
    market_key="hits",
    over_odds=1.8,
    under_odds=1.9,
    games_used=3,
    season_value=51,
    season_games=51,
):
    season_stats = {stat_key: season_value, "gamesPlayed": season_games} if stat_key else {}
    return {
        "propId": prop_id,
        "fixtureSlug": "reds-astros",
        "game": "Cincinnati Reds - Houston Astros",
        "player": {
            "name": player_name,
            "key": player_name.lower().replace(" ", "-"),
            "mlbId": 514888,
            "matchStatus": match_status,
        },
        "team": {
            "name": "Houston Astros",
            "key": "houston-astros",
            "mlbId": 117,
        },
        "market": {"name": market_key, "key": market_key},
        "line": line,
        "odds": {"over": over_odds, "under": under_odds},
        "mlbProfile": {
            "player": {
                "mlbId": 514888,
                "name": player_name,
                "stats": season_stats,
            },
            "season": 2026,
            "group": "hitting",
        },
        "recentHistory": {
            "playerId": 514888,
            "season": 2026,
            "group": "hitting",
            "gamesUsed": games_used,
            "games": [
                {
                    "date": "2026-05-07",
                    "opponent": "Cincinnati Reds",
                    "stats": {stat_key: 2} if stat_key else {},
                }
            ],
            "totals": (
                {stat_key: recent_per_game * games_used}
                if stat_key and recent_per_game is not None
                else {}
            ),
            "perGame": {stat_key: recent_per_game} if stat_key else {},
        },
        "statContext": {
            "statKey": stat_key,
            "seasonValue": season_value,
            "recentTotal": recent_per_game * games_used if recent_per_game is not None else None,
            "recentPerGame": recent_per_game,
            "gamesUsed": games_used,
        },
    }


def test_analyze_stored_props_buckets_latest_snapshots(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    captured_at = datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc)

    store.save_enriched_props(
        _prop_payload(_prop("one", "Watch Player", 0.5, 1.0)),
        captured_at=captured_at,
        snapshot_phase="pregame",
    )
    store.save_enriched_props(
        _prop_payload(_prop("two", "Neutral Player", 0.5, 0.6)),
        captured_at=captured_at,
        snapshot_phase="pregame",
    )
    store.save_enriched_props(
        _prop_payload(
            _prop(
                "three",
                "Weak Player",
                0.5,
                None,
                match_status="matched_exact_name",
            )
        ),
        captured_at=captured_at,
        snapshot_phase="pregame",
    )

    result = analyze_stored_props(
        store,
        date_text="2026-05-08",
        market="hits",
        snapshot_phase="pregame",
        min_edge=0.25,
    )

    assert result["counts"] == {"watchlist": 1, "neutral": 1, "avoid": 1}
    assert result["buckets"]["watchlist"][0]["playerName"] == "Watch Player"
    assert result["buckets"]["watchlist"][0]["lean"] == "over"
    assert result["buckets"]["watchlist"][0]["edge"] == 0.5
    assert result["buckets"]["watchlist"][0]["score"] >= 75
    assert result["buckets"]["watchlist"][0]["confidence"] == "high"
    assert result["buckets"]["watchlist"][0]["gamesUsed"] == 3
    assert result["buckets"]["watchlist"][0]["recentGames"][0]["stats"]["hits"] == 2
    assert result["buckets"]["watchlist"][0]["seasonStats"]["hits"] == 51
    assert result["buckets"]["watchlist"][0]["seasonPerGame"] == 1.0
    assert result["buckets"]["neutral"][0]["playerName"] == "Neutral Player"
    assert result["buckets"]["neutral"][0]["reasons"] == ["near_line"]
    assert result["buckets"]["avoid"][0]["playerName"] == "Weak Player"
    assert "weak_match" in result["buckets"]["avoid"][0]["reasons"]


def test_analyze_stored_props_applies_market_specific_risk_flags(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    captured_at = datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc)

    store.save_enriched_props(
        _prop_payload(
            _prop(
                "hr-one",
                "Power Hitter",
                0.5,
                0.9,
                stat_key="homeRuns",
                market_key="home-runs",
                over_odds=4.5,
                under_odds=1.15,
                games_used=2,
            )
        ),
        captured_at=captured_at,
        snapshot_phase="pregame",
    )

    result = analyze_stored_props(
        store,
        date_text="2026-05-08",
        market="home-runs",
        snapshot_phase="pregame",
        min_edge=0.25,
    )

    row = result["buckets"]["watchlist"][0]
    assert row["playerName"] == "Power Hitter"
    assert row["marketProfile"] == "sparse_power"
    assert row["confidence"] == "medium"
    assert "sparse_market" in row["riskFlags"]
    assert "small_recent_sample" in row["riskFlags"]
    assert "long_over_odds" in row["riskFlags"]
    assert "recent_per_game_above_market_threshold" in row["reasons"]


def test_analyze_stored_props_flags_market_movement_against_over(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    first = _prop(
        "move-one",
        "Moving Player",
        0.5,
        1.0,
        over_odds=1.8,
        under_odds=1.9,
    )
    second = _prop(
        "move-one",
        "Moving Player",
        0.5,
        1.0,
        over_odds=2.25,
        under_odds=1.6,
    )

    store.save_enriched_props(
        _prop_payload(first),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    store.save_enriched_props(
        _prop_payload(second),
        captured_at=datetime(2026, 5, 8, 16, 45, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )

    result = analyze_stored_props(
        store,
        date_text="2026-05-08",
        market="hits",
        snapshot_phase="pregame",
        min_edge=0.25,
    )

    row = result["buckets"]["watchlist"][0]
    assert row["movement"]["overOdds"] == 0.45
    assert "market_moved_against_over" in row["riskFlags"]


def test_line_above_season_baseline_lowers_confidence_without_hiding_pick(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")

    store.save_enriched_props(
        _prop_payload(
            _prop(
                "alt-line",
                "Line Sensitive Hitter",
                1.5,
                1.9,
                season_value=42,
                season_games=50,
            )
        ),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )

    result = analyze_stored_props(
        store,
        date_text="2026-05-08",
        market="hits",
        snapshot_phase="pregame",
        min_edge=0.25,
    )

    row = result["buckets"]["watchlist"][0]
    assert row["lean"] == "over"
    assert row["edge"] == 0.4
    assert row["seasonPerGame"] == 0.84
    assert row["confidence"] == "medium"
    assert "high_line" in row["riskFlags"]
    assert "season_baseline_below_line" in row["riskFlags"]
    assert "recent_form_clears_line_but_season_does_not" in row["reasons"]


def test_season_and_recent_agreement_boosts_score_and_confidence(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")

    store.save_enriched_props(
        _prop_payload(
            _prop(
                "supported",
                "Supported Hitter",
                0.5,
                1.4,
                season_value=60,
                season_games=50,
            )
        ),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )

    result = analyze_stored_props(
        store,
        date_text="2026-05-08",
        market="hits",
        snapshot_phase="pregame",
        min_edge=0.25,
    )

    row = result["buckets"]["watchlist"][0]
    assert row["confidence"] == "high"
    assert row["score"] >= 88
    assert "season_baseline_supports_over" in row["reasons"]
    assert "recent_and_season_agree" in row["reasons"]


def test_pitcher_strikeouts_use_pitching_specific_thresholds_and_season_games(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")

    store.save_enriched_props(
        _prop_payload(
            _prop(
                "pitcher-k",
                "Strikeout Pitcher",
                5.5,
                6.6,
                stat_key="strikeOuts",
                market_key="pitcher-strikeouts",
                season_value=66,
                season_games=10,
                games_used=4,
            )
        ),
        captured_at=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )

    result = analyze_stored_props(
        store,
        date_text="2026-05-08",
        market="pitcher-strikeouts",
        snapshot_phase="pregame",
        min_edge=0.25,
    )

    row = result["buckets"]["watchlist"][0]
    assert row["marketProfile"] == "pitching_count"
    assert row["marketThreshold"] == 0.75
    assert row["seasonPerGame"] == 6.6
    assert row["confidence"] == "high"
    assert "pitching_recent_average_clears_strikeout_line" in row["reasons"]
