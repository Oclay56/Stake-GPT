from app.cli import (
    build_parser,
    format_pick_board,
    format_daily_slate,
    format_parlay_candidates,
    format_backtest,
    format_slate_run,
    format_settlement,
    format_analyze_stored,
    format_enriched_props,
    format_latest_props,
    format_match_audit,
    format_movement,
    format_player_search,
    format_refresh_result,
    format_stored_props,
    render_menu,
    _write_report,
)


def test_render_menu_is_numbered_and_includes_core_workflows():
    menu = render_menu()

    assert "Stake + MLB Test CLI" in menu
    assert "1. Stake MLB props" in menu
    assert "2. Enriched props" in menu
    assert "3. Match audit" in menu
    assert "4. MLB player search" in menu
    assert "7. Refresh stored props" in menu
    assert "8. Stored props" in menu
    assert "9. Prop movement" in menu
    assert "10. Latest props" in menu
    assert "11. Analyze stored props" in menu
    assert "12. Settle stored props" in menu
    assert "13. Backtest stored props" in menu
    assert "14. Run slate workflow" in menu
    assert "15. Build parlays" in menu
    assert "16. Pick board" in menu
    assert "17. Daily slate workflow" in menu
    assert "0. Exit" in menu


def test_format_enriched_props_shows_match_and_stat_context():
    payload = {
        "date": "2026-05-08",
        "propCount": 1,
        "matchedPropCount": 1,
        "unmatchedPropCount": 0,
        "props": [
            {
                "player": {
                    "name": "Jose Altuve",
                    "mlbId": 514888,
                    "matchStatus": "matched_exact_name_team",
                },
                "team": {"name": "Houston Astros"},
                "market": {"name": "hits"},
                "line": 0.5,
                "odds": {"over": 1.8, "under": 1.9},
                "statContext": {
                    "statKey": "hits",
                    "recentPerGame": 1.6667,
                    "seasonValue": 51,
                    "gamesUsed": 3,
                },
            }
        ],
    }

    formatted = format_enriched_props(payload)

    assert "Props: 1 | matched: 1 | unmatched: 0" in formatted
    assert "Jose Altuve | Houston Astros | hits 0.5 | O 1.8 / U 1.9" in formatted
    assert "MLB 514888 | matched_exact_name_team | hits recent/gm 1.6667 | season 51" in formatted


def test_format_match_audit_highlights_issues():
    audit = {
        "date": "2026-05-08",
        "propCount": 2,
        "matchedPropCount": 1,
        "unmatchedPropCount": 1,
        "issueCounts": {"unmatched": 1, "low_confidence": 1},
        "rows": [
            {
                "player": "Unknown",
                "team": "Houston Astros",
                "market": "hits",
                "status": "unmatched",
                "confidence": 0.0,
                "candidateCount": 0,
                "matchedPlayer": None,
                "issues": ["unmatched", "low_confidence"],
            }
        ],
    }

    formatted = format_match_audit(audit)

    assert "Props: 2 | matched: 1 | unmatched: 1" in formatted
    assert "Issues: low_confidence=1, unmatched=1" in formatted
    assert "Unknown | Houston Astros | hits | unmatched | issues: unmatched, low_confidence" in formatted


def test_format_player_search_lists_player_options():
    payload = {
        "query": "Aaron Judge",
        "playerCount": 1,
        "players": [
            {
                "mlbId": 592450,
                "name": "Aaron Judge",
                "position": "RF",
                "team": {"name": "New York Yankees"},
            }
        ],
    }

    formatted = format_player_search(payload)

    assert "Search: Aaron Judge | results: 1" in formatted
    assert "592450 | Aaron Judge | RF | New York Yankees" in formatted


def test_format_refresh_result_shows_saved_counts_and_path():
    formatted = format_refresh_result(
        {
            "dbPath": "C:\\Users\\farne\\Desktop\\AZP\\data\\azp.sqlite",
            "propSnapshotsInserted": 30,
            "playerStatSnapshotsInserted": 12,
            "date": "2026-05-08",
            "snapshotPhase": "pregame",
            "snapshotLabel": "morning board",
        }
    )

    assert "Refresh saved: 30 prop snapshots | 12 MLB stat snapshots" in formatted
    assert "Date: 2026-05-08" in formatted
    assert "Phase: pregame" in formatted
    assert "Label: morning board" in formatted
    assert "DB: C:\\Users\\farne\\Desktop\\AZP\\data\\azp.sqlite" in formatted


def test_format_stored_props_lists_saved_rows():
    formatted = format_stored_props(
        {
            "dbPath": "C:\\data\\azp.sqlite",
            "count": 1,
            "props": [
                {
                    "capturedAt": "2026-05-08T16:30:00+00:00",
                    "playerName": "Jose Altuve",
                    "teamName": "Houston Astros",
                    "marketKey": "hits",
                    "line": 0.5,
                    "overOdds": 1.8,
                    "underOdds": 1.9,
                    "matchStatus": "matched_exact_name_team",
                    "snapshotPhase": "pregame",
                    "snapshotLabel": "morning board",
                }
            ],
        }
    )

    assert "Stored props: 1" in formatted
    assert "Jose Altuve | Houston Astros | hits 0.5 | O 1.8 / U 1.9" in formatted
    assert "matched_exact_name_team | pregame | morning board | 2026-05-08T16:30:00+00:00" in formatted


def test_format_movement_lists_snapshot_changes():
    formatted = format_movement(
        {
            "propId": "reds-astros:jose-altuve:houston-astros:hits",
            "snapshotCount": 2,
            "snapshots": [
                {
                    "capturedAt": "2026-05-08T16:30:00+00:00",
                    "line": 0.5,
                    "overOdds": 1.8,
                    "underOdds": 1.9,
                },
                {
                    "capturedAt": "2026-05-08T16:45:00+00:00",
                    "line": 1.5,
                    "overOdds": 2.05,
                    "underOdds": 1.72,
                    "snapshotPhase": "live",
                    "snapshotLabel": "midgame",
                },
            ],
            "changes": [
                {
                    "fromCapturedAt": "2026-05-08T16:30:00+00:00",
                    "toCapturedAt": "2026-05-08T16:45:00+00:00",
                    "line": 1.0,
                    "overOdds": 0.25,
                    "underOdds": -0.18,
                }
            ],
        }
    )

    assert "Movement for reds-astros:jose-altuve:houston-astros:hits" in formatted
    assert "Snapshots: 2" in formatted
    assert "Latest: 2026-05-08T16:45:00+00:00 | live | midgame" in formatted
    assert "2026-05-08T16:30:00+00:00 -> 2026-05-08T16:45:00+00:00" in formatted
    assert "line 1.0 | over 0.25 | under -0.18" in formatted


def test_format_latest_props_lists_latest_rows():
    formatted = format_latest_props(
        {
            "dbPath": "C:\\data\\azp.sqlite",
            "count": 1,
            "props": [
                {
                    "capturedAt": "2026-05-08T16:45:00+00:00",
                    "playerName": "Jose Altuve",
                    "teamName": "Houston Astros",
                    "marketKey": "hits",
                    "line": 1.5,
                    "overOdds": 2.05,
                    "underOdds": 1.72,
                    "matchStatus": "matched_exact_name_team",
                    "snapshotPhase": "pregame",
                    "snapshotLabel": "latest board",
                }
            ],
        }
    )

    assert "Latest props: 1" in formatted
    assert "Jose Altuve | Houston Astros | hits 1.5 | O 2.05 / U 1.72" in formatted
    assert "matched_exact_name_team | pregame | latest board | 2026-05-08T16:45:00+00:00" in formatted


def test_format_analyze_stored_shows_buckets_and_reasons():
    formatted = format_analyze_stored(
        {
            "date": "2026-05-08",
            "market": "hits",
            "snapshotPhase": "pregame",
            "counts": {"watchlist": 1, "neutral": 0, "avoid": 1},
            "buckets": {
                "watchlist": [
                    {
                        "playerName": "Jose Altuve",
                        "teamName": "Houston Astros",
                        "marketKey": "hits",
                        "line": 0.5,
                        "lean": "over",
                        "recentPerGame": 1.6667,
                        "seasonValue": 51.0,
                        "overOdds": 1.8,
                        "underOdds": 1.9,
                        "reasons": ["recent_per_game_above_line"],
                        "score": 82,
                        "confidence": "high",
                        "riskFlags": ["market_moved_against_over"],
                    }
                ],
                "neutral": [],
                "avoid": [
                    {
                        "playerName": "Unknown",
                        "teamName": "Houston Astros",
                        "marketKey": "hits",
                        "line": 0.5,
                        "lean": "none",
                        "recentPerGame": None,
                        "seasonValue": None,
                        "overOdds": 2.1,
                        "underOdds": 1.7,
                        "reasons": ["weak_match"],
                    }
                ],
            },
        }
    )

    assert "Stored Analyzer | date 2026-05-08 | market hits | phase pregame" in formatted
    assert "Watchlist (1)" in formatted
    assert "Jose Altuve | Houston Astros | hits 0.5 | lean over | score 82 | confidence high" in formatted
    assert "reasons: recent_per_game_above_line" in formatted
    assert "risk: market_moved_against_over" in formatted
    assert "Avoid / Needs Review (1)" in formatted
    assert "Unknown | Houston Astros | hits 0.5 | lean none" in formatted
    assert "reasons: weak_match" in formatted


def test_format_settlement_shows_grading_rows():
    formatted = format_settlement(
        {
            "date": "2026-05-08",
            "market": "hits",
            "snapshotPhase": "pregame",
            "counts": {
                "settled": 1,
                "pending": 1,
                "ungraded": 0,
                "over": 1,
                "under": 0,
                "push": 0,
            },
            "rows": [
                {
                    "playerName": "Jose Altuve",
                    "teamName": "Houston Astros",
                    "marketKey": "hits",
                    "line": 0.5,
                    "actualValue": 2.0,
                    "actualResult": "over",
                    "overOutcome": "win",
                    "reasons": [],
                },
                {
                    "playerName": "Pending Player",
                    "teamName": "Houston Astros",
                    "marketKey": "hits",
                    "line": 0.5,
                    "actualValue": None,
                    "actualResult": "pending",
                    "overOutcome": "pending",
                    "reasons": ["no_game_log_for_date"],
                },
            ],
        }
    )

    assert "Settlement | date 2026-05-08 | market hits | phase pregame" in formatted
    assert "Counts: settled=1 | pending=1 | ungraded=0 | over=1 | under=0 | push=0" in formatted
    assert "Jose Altuve | Houston Astros | hits 0.5 | actual 2.0 | result over | over win" in formatted
    assert "Pending Player | Houston Astros | hits 0.5 | actual - | result pending | over pending" in formatted
    assert "reasons: no_game_log_for_date" in formatted


def test_format_backtest_shows_accuracy_summary_and_rows():
    formatted = format_backtest(
        {
            "date": "2026-05-08",
            "market": "hits",
            "snapshotPhase": "pregame",
            "counts": {
                "rows": 2,
                "settled": 2,
                "pending": 0,
                "ungraded": 0,
                "decisions": 2,
                "correct": 1,
                "incorrect": 1,
                "push": 0,
                "noDecision": 0,
            },
            "byBucket": {
                "watchlist": {
                    "total": 1,
                    "decisions": 1,
                    "correct": 1,
                    "incorrect": 0,
                    "push": 0,
                    "noDecision": 0,
                    "notSettled": 0,
                    "accuracy": 1.0,
                },
                "neutral": {
                    "total": 0,
                    "decisions": 0,
                    "correct": 0,
                    "incorrect": 0,
                    "push": 0,
                    "noDecision": 0,
                    "notSettled": 0,
                    "accuracy": None,
                },
                "avoid": {
                    "total": 1,
                    "decisions": 1,
                    "correct": 0,
                    "incorrect": 1,
                    "push": 0,
                    "noDecision": 0,
                    "notSettled": 0,
                    "accuracy": 0.0,
                },
            },
            "rows": [
                {
                    "playerName": "Watch Winner",
                    "teamName": "Houston Astros",
                    "marketKey": "hits",
                    "line": 0.5,
                    "bucket": "watchlist",
                    "lean": "over",
                    "score": 84,
                    "confidence": "high",
                    "actualValue": 2.0,
                    "overOutcome": "win",
                    "decisionOutcome": "correct",
                }
            ],
        }
    )

    assert "Backtest | date 2026-05-08 | market hits | phase pregame" in formatted
    assert "Counts: rows=2 | settled=2 | decisions=2 | correct=1 | incorrect=1 | accuracy=50.0%" in formatted
    assert "watchlist: total 1 | decisions 1 | correct 1 | incorrect 0 | accuracy 100.0%" in formatted
    assert "avoid: total 1 | decisions 1 | correct 0 | incorrect 1 | accuracy 0.0%" in formatted
    assert "Watch Winner | Houston Astros | hits 0.5 | watchlist/over | score 84 | actual 2.0 | over win | decision correct" in formatted


def test_format_slate_run_shows_refresh_and_top_candidates():
    formatted = format_slate_run(
        {
            "date": "2026-05-08",
            "preset": "mlb-core",
            "markets": ["hits", "runs"],
            "snapshotPhase": "pregame",
            "snapshotLabel": "morning board",
            "refresh": {
                "propSnapshotsInserted": 10,
                "playerStatSnapshotsInserted": 8,
            },
            "analysisSaved": {"analysisSnapshotsInserted": 10},
            "analysis": {
                "counts": {"watchlist": 1, "neutral": 8, "avoid": 1},
                "buckets": {
                    "watchlist": [
                        {
                            "playerName": "Jose Altuve",
                            "teamName": "Houston Astros",
                            "marketKey": "hits",
                            "line": 0.5,
                            "lean": "over",
                            "score": 84,
                            "confidence": "high",
                            "overOdds": 1.8,
                            "underOdds": 1.9,
                            "reasons": ["recent_per_game_above_line"],
                        }
                    ],
                    "neutral": [],
                    "avoid": [],
                },
            },
        }
    )

    assert "Slate Run | date 2026-05-08 | preset mlb-core | phase pregame" in formatted
    assert "Markets: hits, runs" in formatted
    assert "Saved: 10 props | 8 MLB stat snapshots | 10 analyzer decisions" in formatted
    assert "Analyzer: watchlist=1 | neutral=8 | avoid=1" in formatted
    assert "Jose Altuve | Houston Astros | hits 0.5 | lean over | score 84 | confidence high" in formatted


def test_format_daily_slate_combines_workflow_board_and_parlays():
    formatted = format_daily_slate(
        {
            "slateRun": {
                "date": "2026-05-09",
                "preset": "mlb-core",
                "markets": ["hits", "runs"],
                "snapshotPhase": "pregame",
                "snapshotLabel": "daily",
                "refresh": {
                    "propSnapshotsInserted": 12,
                    "playerStatSnapshotsInserted": 10,
                },
                "analysisSaved": {"analysisSnapshotsInserted": 12},
                "analysis": {
                    "counts": {"watchlist": 2, "neutral": 9, "avoid": 1},
                    "buckets": {"watchlist": [], "neutral": [], "avoid": []},
                },
            },
            "pickBoard": {
                "requested": {
                    "date": "2026-05-09",
                    "snapshotPhase": "pregame",
                    "markets": ["hits"],
                    "profile": "safe-ish",
                },
                "eligibleCount": 1,
                "picks": [
                    {
                        "pickNumber": 1,
                        "playerName": "Jose Altuve",
                        "teamName": "Houston Astros",
                        "fixtureSlug": "reds-astros",
                        "game": "Cincinnati Reds - Houston Astros",
                        "marketKey": "hits",
                        "line": 0.5,
                        "lean": "over",
                        "odds": 1.8,
                        "overOdds": 1.8,
                        "underOdds": 1.9,
                        "score": 86,
                        "confidence": "high",
                        "recentPerGame": 1.6667,
                        "seasonValue": 51,
                        "seasonPerGame": 1.02,
                        "gamesUsed": 5,
                        "recentGames": [],
                        "whyIncluded": ["recent form clears market line"],
                        "riskFlags": [],
                    }
                ],
            },
            "parlays": {
                "requested": {
                    "legs": 3,
                    "oddsMin": 2.0,
                    "oddsMax": 6.0,
                    "mode": "standard",
                },
                "eligibleCount": 3,
                "candidateCount": 0,
                "warnings": [],
                "candidates": [],
            },
        },
        max_picks=1,
        max_parlays=1,
    )

    assert "Daily Slate Workflow" in formatted
    assert "Slate Run | date 2026-05-09 | preset mlb-core | phase pregame" in formatted
    assert "Pick Board | date 2026-05-09 | phase pregame | markets hits | profile safe-ish" in formatted
    assert "Parlay Builder | legs 3 | odds 2.0-6.0 | mode standard" in formatted


def test_format_parlay_candidates_shows_candidates_and_legs():
    formatted = format_parlay_candidates(
        {
            "requested": {
                "legs": 3,
                "oddsMin": 3.0,
                "oddsMax": 8.0,
                "mode": "standard",
            },
            "eligibleCount": 3,
            "candidateCount": 1,
            "warnings": [],
            "candidates": [
                {
                    "rank": 1,
                    "legCount": 3,
                    "totalOdds": 5.22,
                    "rawProductOdds": 5.22,
                    "score": 255,
                    "withinRange": True,
                    "conflictWarnings": ["same_game_correlation:reds-astros:3"],
                    "correlationRisk": {
                        "riskLevel": "high",
                        "quoteNeeded": True,
                        "warning": "SGP repricing likely; Stake quote needed before treating raw odds as final.",
                        "tags": ["same_fixture", "pitcher_vs_batter"],
                        "gameScriptTags": ["low-scoring game script"],
                    },
                    "legs": [
                        {
                            "playerName": "Jose Altuve",
                            "teamName": "Houston Astros",
                            "fixtureSlug": "reds-astros",
                            "marketKey": "hits",
                            "line": 0.5,
                            "lean": "over",
                            "odds": 1.8,
                            "score": 86,
                            "confidence": "high",
                            "reasons": ["recent_per_game_above_line"],
                            "whyIncluded": ["recent form clears market line"],
                            "whyNotStronger": ["same-game legs can be correlated"],
                        }
                    ],
                }
            ],
        }
    )

    assert "Parlay Builder | legs 3 | odds 3.0-8.0 | mode standard" in formatted
    assert "Eligible legs: 3 | candidates: 1" in formatted
    assert "#1 | total odds 5.22 | score 255 | in range" in formatted
    assert "raw product 5.22 | correlation high | quote needed" in formatted
    assert "SGP repricing likely; Stake quote needed before treating raw odds as final." in formatted
    assert "correlation tags: same_fixture, pitcher_vs_batter" in formatted
    assert "conflicts: same_game_correlation:reds-astros:3" in formatted
    assert "Jose Altuve | Houston Astros | reds-astros | hits 0.5 | over @ 1.8 | score 86 | high" in formatted
    assert "why: recent form clears market line" in formatted
    assert "watch: same-game legs can be correlated" in formatted


def test_build_parser_accepts_parlay_locks_and_mode():
    args = build_parser().parse_args(
        [
            "build-parlay",
            "--date",
            "2026-05-09",
            "--snapshot-phase",
            "pregame",
            "--legs",
            "4",
            "--odds-min",
            "5",
            "--odds-max",
            "20",
            "--mode",
            "sgp",
            "--preset",
            "power",
            "--profile",
            "longshot",
            "--lock-players",
            "Aaron Judge,Juan Soto",
            "--lock-prop-ids",
            "game-a:locked-prop",
            "--lock-picks",
            "2,4",
        ]
    )

    assert args.command == "build-parlay"
    assert args.mode == "sgp"
    assert args.preset == "power"
    assert args.profile == "longshot"
    assert args.lock_players == "Aaron Judge,Juan Soto"
    assert args.lock_prop_ids == "game-a:locked-prop"
    assert args.lock_picks == "2,4"


def test_build_parser_accepts_pick_board_preset_and_profile():
    args = build_parser().parse_args(
        [
            "pick-board",
            "--date",
            "2026-05-09",
            "--snapshot-phase",
            "pregame",
            "--preset",
            "mlb-core",
            "--profile",
            "safe-ish",
        ]
    )

    assert args.command == "pick-board"
    assert args.preset == "mlb-core"
    assert args.profile == "safe-ish"


def test_build_parser_accepts_daily_slate_options():
    args = build_parser().parse_args(
        [
            "daily-slate",
            "--date",
            "2026-05-09",
            "--limit",
            "3",
            "--preset",
            "mlb-core",
            "--profile",
            "safe-ish",
            "--mode",
            "sgp",
            "--legs",
            "4",
            "--count",
            "2",
            "--pick-limit",
            "8",
        ]
    )

    assert args.command == "daily-slate"
    assert args.date == "2026-05-09"
    assert args.limit == 3
    assert args.preset == "mlb-core"
    assert args.profile == "safe-ish"
    assert args.mode == "sgp"
    assert args.legs == 4
    assert args.count == 2
    assert args.pick_limit == 8


def test_write_report_saves_output_when_path_is_provided(tmp_path):
    report_path = tmp_path / "reports" / "daily.txt"

    _write_report(str(report_path), "daily output")

    assert report_path.read_text(encoding="utf-8") == "daily output"


def test_format_pick_board_shows_numbered_selectable_legs():
    formatted = format_pick_board(
        {
            "requested": {
                "date": "2026-05-09",
                "snapshotPhase": "pregame",
                "markets": ["hits"],
                "profile": "safe-ish",
            },
            "eligibleCount": 1,
            "picks": [
                {
                    "pickNumber": 1,
                    "playerName": "Jose Altuve",
                    "teamName": "Houston Astros",
                    "fixtureSlug": "reds-astros",
                    "game": "Cincinnati Reds - Houston Astros",
                    "marketKey": "hits",
                    "line": 0.5,
                    "lean": "over",
                    "odds": 1.8,
                    "overOdds": 1.8,
                    "underOdds": 1.9,
                    "score": 86,
                    "confidence": "high",
                    "recentPerGame": 1.6667,
                    "seasonValue": 51,
                    "seasonPerGame": 1.02,
                    "gamesUsed": 5,
                    "recentGames": [
                        {"date": "2026-05-08", "opponent": "Reds", "stats": {"hits": 2}},
                        {"date": "2026-05-07", "opponent": "Rangers", "stats": {"hits": 1}},
                    ],
                    "whyIncluded": ["recent form clears market line"],
                    "riskFlags": [],
                }
            ],
        }
    )

    assert "Pick Board | date 2026-05-09 | phase pregame | markets hits | profile safe-ish" in formatted
    assert "Eligible legs: 1" in formatted
    assert "1. Jose Altuve | Houston Astros | Cincinnati Reds - Houston Astros | hits 0.5 | over @ 1.8 | score 86 | high" in formatted
    assert "detail: recent5/gm 1.67 | season 51 | season/gm 1.02 | Stake O 1.8 / U 1.9" in formatted
    assert "recent games: 2026-05-08 2 vs Reds, 2026-05-07 1 vs Rangers" in formatted
    assert "why: recent form clears market line" in formatted
    assert "risk: none" in formatted
