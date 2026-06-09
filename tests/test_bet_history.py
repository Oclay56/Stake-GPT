from __future__ import annotations

import asyncio
import json
import sqlite3

from app.bet_history import (
    format_backtest_report,
    format_backtest_rich_report,
    format_dataset_report,
    format_model_report,
    format_report,
    format_review_report,
    format_sync_report,
    list_import_files,
    normalize_market,
    parse_history_file,
    parse_history_rows,
    parse_odds,
    settle_result,
    sync_import_folder,
)
from app.bet_history_enrichment import enrich_bet_history, format_enrichment_report
from app.market_normalization import SUPPORTED_MLB_PROP_MARKETS, normalize_mlb_prop_market_key
from app.mlb_bridge import MARKET_STAT_MAP
from app.storage import GptActionStore


def test_bet_history_parser_normalizes_csv_rows(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text(
        "\n".join(
            [
                "Date,Player,Market,Side,Line,Odds,Result,Actual Stat,Ticket ID",
                "2026-05-08,George Springer,Hits,Under,0.5,2.90,Won,0,ticket-1",
                "2026-05-08,Vladimir Guerrero Jr.,HRR,Under,1.5,2.15,Lost,3,ticket-1",
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_history_file(path)

    assert parsed["sourceFormat"] == "csv"
    assert parsed["report"]["rawRows"] == 2
    assert parsed["report"]["parsedLegs"] == 2
    assert parsed["report"]["needsReview"] == 0
    assert parsed["report"]["parserVersion"]
    assert parsed["report"]["eligibilityVersion"]
    assert parsed["report"]["fingerprintVersion"]
    assert parsed["legs"][0]["marketKey"] == "hits"
    assert parsed["legs"][0]["parserVersion"]
    assert parsed["legs"][0]["eligibilityVersion"]
    assert parsed["legs"][0]["side"] == "under"
    assert parsed["legs"][0]["odds"] == 2.9
    assert parsed["legs"][1]["marketKey"] == "hits_runs_rbis"
    assert parsed["report"]["trainingEligible"] == 2


def test_bet_history_parser_ignores_noisy_ui_columns():
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "Under",
                "line": "0.5",
                "odds": "2.90",
                "result": "Won",
                "cashout_button": "Cashout",
                "promo_banner": "Boost available",
                "decorative_icon": "baseball",
            }
        ],
        source_format="csv",
    )

    leg = parsed["legs"][0]

    assert leg["marketKey"] == "hits"
    assert leg["trainingEligible"] is True
    assert leg["ignoredFields"] == ["cashout_button", "decorative_icon", "promo_banner"]
    assert parsed["report"]["ignoredFields"] == {
        "cashout_button": 1,
        "decorative_icon": 1,
        "promo_banner": 1,
    }


def test_bet_history_parser_extracts_canonical_fields_from_raw_text(tmp_path):
    path = tmp_path / "raw-paste.txt"
    path.write_text(
        "\n".join(
            [
                "Stake Sports",
                "2026-05-08 George Springer Hits Under 0.5 2.90 Won",
                "Cashout unavailable",
                "2026-05-08 Bo Bichette Total Bases Over 1.5 1.83 Lost",
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_history_file(path)
    clean_legs = [leg for leg in parsed["legs"] if leg["trainingEligible"]]

    assert parsed["sourceFormat"] == "text"
    assert len(parsed["legs"]) == 4
    assert len(clean_legs) == 2
    assert clean_legs[0]["playerName"] == "George Springer"
    assert clean_legs[0]["marketKey"] == "hits"
    assert clean_legs[0]["side"] == "under"
    assert clean_legs[0]["line"] == 0.5
    assert clean_legs[0]["odds"] == 2.9
    assert clean_legs[0]["resultStatus"] == "won"
    assert clean_legs[1]["playerName"] == "Bo Bichette"
    assert clean_legs[1]["marketKey"] == "total_bases"
    assert parsed["report"]["trainingEligible"] == 2
    assert parsed["report"]["needsReview"] == 2


def test_bet_history_parser_extracts_stake_settled_ui_blocks(tmp_path):
    path = tmp_path / "stake-settled.txt"
    path.write_text(
        "\n".join(
            [
                "Sun, Jun 07",
                "4:10 PM",
                "Los Angeles Dodgers",
                "Los Angeles Angels",
                "5",
                "13",
                "Under 0.5 Hits",
                "",
                "Sebastian Rivero",
                "",
                "5",
                "1",
                "Under 1.5 Batter Strikeouts",
                "",
                "Zach Neto",
                "",
                "4",
                "2",
                "Under 0.5 Singles",
                "",
                "Ryan Ward",
                "",
                "0",
                "1",
                "Under 0.5 Batter Walks",
                "",
                "Max Muncy",
                "",
                "0",
                "1",
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_history_file(path)

    assert parsed["sourceFormat"] == "text"
    assert parsed["report"]["rawRows"] == 4
    assert parsed["report"]["parsedLegs"] == 4
    assert parsed["report"]["parseDiagnostics"]["textMode"] == "stake_ui_blocks"
    assert parsed["report"]["parseDiagnostics"]["parsedBlocks"] == 4
    assert parsed["report"]["needsReview"] == 0
    assert parsed["report"]["trainingEligible"] == 4
    assert [leg["playerName"] for leg in parsed["legs"]] == [
        "Sebastian Rivero",
        "Zach Neto",
        "Ryan Ward",
        "Max Muncy",
    ]
    assert [leg["marketKey"] for leg in parsed["legs"]] == [
        "hits",
        "batter_strikeouts",
        "singles",
        "batter_walks",
    ]
    assert [leg["actualStat"] for leg in parsed["legs"]] == [5.0, 4.0, 0.0, 0.0]
    assert [leg["resultStatus"] for leg in parsed["legs"]] == ["lost", "lost", "won", "won"]
    assert all(leg["odds"] is None for leg in parsed["legs"])
    assert all(leg["matchup"] == "Los Angeles Dodgers - Los Angeles Angels" for leg in parsed["legs"])


def test_bet_history_parser_groups_sgm_by_declared_leg_count(tmp_path):
    path = tmp_path / "stake-sgm.txt"
    path.write_text(
        "\n".join(
            [
                "3 Leg Same Game Multi",
                "2.50",
                "Sun, Jun 07",
                "Los Angeles Dodgers",
                "Los Angeles Angels",
                "5",
                "13",
                "Under 0.5 Hits",
                "Sebastian Rivero",
                "5",
                "1",
                "Under 1.5 Batter Strikeouts",
                "Zach Neto",
                "4",
                "2",
                "Under 0.5 Singles",
                "Ryan Ward",
                "0",
                "1",
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_history_file(path)
    ticket_ids = {leg["ticketId"] for leg in parsed["legs"]}

    assert parsed["report"]["parsedLegs"] == 3
    assert len(ticket_ids) == 1
    assert next(iter(ticket_ids)).startswith("sgm_")
    assert {leg["ticketOdds"] for leg in parsed["legs"]} == {2.5}
    assert parsed["report"]["ignoredFieldCount"] == 0


def test_bet_history_parser_accepts_high_decimal_sgm_ticket_odds(tmp_path):
    path = tmp_path / "high-odds-sgm.txt"
    path.write_text(
        "\n".join(
            [
                "10 Leg Same Game Multi",
                "301.00",
                "Sun, Jun 07",
                "San Diego Padres - New York Mets",
                "Under 0.5 RBIs",
                "Juan Soto",
                "1",
                "Under 0.5 Batter Strikeouts",
                "Marcus Semien",
                "0",
                "1",
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_history_file(path)

    assert parsed["report"]["parsedLegs"] == 2
    assert {leg["ticketOdds"] for leg in parsed["legs"]} == {301.0}


def test_bet_history_parser_keeps_multi_sgm_contexts_separate(tmp_path):
    path = tmp_path / "multi-sgm.txt"
    path.write_text(
        "\n".join(
            [
                "28 Leg Multi",
                "2 Leg Same Game Multi",
                "2.39",
                "Toronto Blue Jays - Baltimore Orioles",
                "Sun, Jun 07 1:37 PM",
                "Toronto Blue Jays",
                "Baltimore Orioles",
                "6",
                "4",
                "Under 1.5 Batter Strikeouts",
                "Pete Alonso",
                "0",
                "2",
                "Under 0.5 Batter Strikeouts",
                "Vladimir Guerrero Jr.",
                "1",
                "2 Leg Same Game Multi",
                "2.28",
                "Detroit Tigers - Seattle Mariners",
                "Live",
                "9th inning bottom",
                "Detroit Tigers",
                "Seattle Mariners",
                "5",
                "4",
                "Under 0.5 Singles",
                "Patrick Wisdom",
                "1",
                "1",
                "Under 0.5 Singles",
                "Zach McKinstry",
                "1",
                "1",
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_history_file(path)

    first_group = parsed["legs"][:2]
    second_group = parsed["legs"][2:]

    assert parsed["report"]["parsedLegs"] == 4
    assert {leg["matchup"] for leg in first_group} == {"Toronto Blue Jays - Baltimore Orioles"}
    assert {leg["matchup"] for leg in second_group} == {"Detroit Tigers - Seattle Mariners"}
    assert {leg["ticketId"] for leg in first_group} != {leg["ticketId"] for leg in second_group}
    assert all(leg["trainingEligible"] for leg in first_group)
    assert all(not leg["liveAtImport"] for leg in first_group)
    assert all(leg["trainingEligible"] for leg in second_group)
    assert all(not leg["liveAtImport"] for leg in second_group)


def test_bet_history_parser_converts_void_without_result_mismatch(tmp_path):
    path = tmp_path / "void-sgm.txt"
    path.write_text(
        "\n".join(
            [
                "2 Leg Same Game Multi",
                "2.55",
                "Miami Marlins - Tampa Bay Rays",
                "Sun, Jun 07 1:40 PM",
                "Miami Marlins",
                "Tampa Bay Rays",
                "4",
                "1",
                "Under 0.5 Singles",
                "Connor Norby",
                "Void",
                "0",
                "1",
                "Under 0.5 Singles",
                "Esteury Ruiz",
                "Void",
                "0",
                "1",
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_history_file(path)

    assert parsed["report"]["parsedLegs"] == 2
    assert parsed["report"]["needsReview"] == 0
    assert parsed["report"]["trainingEligible"] == 2
    assert {leg["resultStatus"] for leg in parsed["legs"]} == {"void"}
    assert all("result_actual_mismatch" not in leg["parseNotes"] for leg in parsed["legs"])


def test_bet_history_parser_keeps_ambiguous_rows_for_review():
    parsed = parse_history_rows(
        [
            {
                "date": "5/8/26",
                "selection": "Random Player Strikeouts Under 3.5",
                "odds": "1.83",
                "result": "loss",
            }
        ],
        source_format="json",
    )

    leg = parsed["legs"][0]

    assert leg["marketKey"] == "strikeouts"
    assert leg["side"] == "under"
    assert leg["line"] == 3.5
    assert leg["resultStatus"] == "lost"
    assert leg["needsReview"] is True
    assert leg["trainingEligible"] is False
    assert "ambiguous_strikeouts_market" in leg["parseNotes"]


def test_bet_history_parser_infers_result_from_actual_stat():
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "Zach Neto",
                "market": "Batter Strikeouts",
                "side": "Under",
                "line": 1.5,
                "odds": 1.91,
                "actual_stat": 3,
            },
            {
                "date": "2026-05-08",
                "player": "Max Muncy",
                "market": "Batter Walks",
                "side": "Under",
                "line": 0.5,
                "odds": 1.80,
                "actual_stat": 0,
            },
        ],
        source_format="csv",
    )

    lost, won = parsed["legs"]

    assert lost["resultStatus"] == "lost"
    assert lost["settledResultStatus"] == "lost"
    assert lost["resultSource"] == "actual_stat_math"
    assert "result_inferred_from_actual_stat" in lost["parseNotes"]
    assert won["resultStatus"] == "won"
    assert won["settledResultStatus"] == "won"
    assert parsed["report"]["trainingEligible"] == 2


def test_bet_history_parser_flags_source_result_that_disagrees_with_actual_stat():
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "Sebastian Rivero",
                "market": "Hits",
                "side": "Under",
                "line": 0.5,
                "odds": 1.75,
                "actual_stat": 1,
                "result": "won",
            }
        ],
        source_format="csv",
    )

    leg = parsed["legs"][0]

    assert leg["sourceResultStatus"] == "won"
    assert leg["settledResultStatus"] == "lost"
    assert leg["resultStatus"] == "won"
    assert leg["needsReview"] is True
    assert leg["trainingEligible"] is False
    assert "result_actual_mismatch" in leg["parseNotes"]


def test_bet_history_parser_ignores_live_rows_for_training():
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "actual_stat": 0,
                "live_at_import": True,
            }
        ],
        source_format="json",
    )

    leg = parsed["legs"][0]

    assert leg["resultStatus"] == "won"
    assert leg["liveAtImport"] is False
    assert leg["needsReview"] is False
    assert leg["trainingEligible"] is True
    assert "live_or_in_progress_at_import" not in leg["parseNotes"]


def test_bet_history_parser_blocks_unknown_markets_from_training():
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Promo Super Boost",
                "side": "under",
                "line": 0.5,
                "odds": 2.0,
                "result": "won",
            }
        ],
        source_format="csv",
    )

    leg = parsed["legs"][0]

    assert leg["marketKey"] is None
    assert leg["trainingEligible"] is False
    assert "unknown_market" in leg["parseNotes"]
    assert "missing_market" in leg["parseNotes"]


def test_bet_history_fingerprint_changes_when_settlement_changes():
    first = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "actual_stat": 1,
                "result": "lost",
            }
        ],
        source_format="json",
    )
    corrected = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "actual_stat": 0,
                "result": "won",
            }
        ],
        source_format="json",
    )

    assert first["sourceFingerprint"] != corrected["sourceFingerprint"]


def test_bet_history_parser_splits_json_ticket_legs(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps(
            {
                "bets": [
                    {
                        "ticket_id": "ticket-2",
                        "date": "2026-05-08",
                        "result": "won",
                        "legs": [
                            {
                                "player": "Bo Bichette",
                                "market": "Singles",
                                "side": "under",
                                "line": 0.5,
                                "odds": 1.8,
                            },
                            {
                                "player": "Jose Berrios",
                                "market": "Pitcher Strikeouts",
                                "side": "over",
                                "line": 4.5,
                                "odds": 2.1,
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_history_file(path)

    assert parsed["report"]["parsedLegs"] == 2
    assert [leg["ticketId"] for leg in parsed["legs"]] == ["ticket-2", "ticket-2"]
    assert [leg["marketKey"] for leg in parsed["legs"]] == ["singles", "pitcher_strikeouts"]


def test_bet_history_storage_saves_raw_and_normalized_legs(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 2.9,
                "result": "won",
                "actual_stat": 0,
            }
        ],
        source_format="json",
        source_path="inline",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")

    result = store.save_bet_history_import(parsed)
    report = store.bet_history_report()
    rows = store.list_bet_history_legs()
    imports = store.list_bet_history_imports()

    assert result["legsImported"] == 1
    assert report["parsedLegs"] == 1
    assert report["needsReview"] == 0
    assert report["trainingEligible"] == 1
    assert rows[0]["playerName"] == "George Springer"
    assert rows[0]["marketKey"] == "hits"
    assert rows[0]["resultStatus"] == "won"
    assert rows[0]["needsReview"] is False
    assert rows[0]["trainingEligible"] is True
    assert rows[0]["parserVersion"]
    assert imports[0]["parserVersion"]
    assert imports[0]["eligibilityVersion"]


def test_bet_history_storage_skips_duplicate_imports(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 2.9,
                "result": "won",
                "actual_stat": 0,
            }
        ],
        source_format="json",
        source_path="inline",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")

    first = store.save_bet_history_import(parsed)
    second = store.save_bet_history_import(parsed)
    forced = store.save_bet_history_import(parsed, force=True)
    report = store.bet_history_report()

    assert first["legsImported"] == 1
    assert second["duplicateSkipped"] is True
    assert second["legsImported"] == 0
    assert second["importId"] == first["importId"]
    assert forced["force"] is True
    assert forced["legsImported"] == 1
    assert report["parsedLegs"] == 2


def test_bet_history_storage_refreshes_ticket_odds_on_duplicate_import(tmp_path):
    base_rows = [
        {
            "date": "2026-05-08",
            "ticket_id": "sgm_1",
            "player": "George Springer",
            "market": "Hits",
            "side": "under",
            "line": 0.5,
            "result": "won",
            "actual_stat": 0,
        }
    ]
    first_parsed = parse_history_rows(base_rows, source_format="json", source_path="inline")
    refreshed_parsed = parse_history_rows(
        [{**base_rows[0], "ticket_odds": 301.0}],
        source_format="json",
        source_path="inline",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")

    first = store.save_bet_history_import(first_parsed)
    second = store.save_bet_history_import(refreshed_parsed)
    rows = store.list_bet_history_legs(import_id=first["importId"])

    assert second["duplicateSkipped"] is True
    assert second["refreshedLegs"] == 1
    assert second["legsImported"] == 0
    assert rows[0]["normalized"]["ticketOdds"] == 301.0
    assert store.bet_history_report()["parsedLegs"] == 1


def test_bet_history_storage_skips_legacy_fingerprint_duplicates(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 2.9,
                "result": "won",
                "actual_stat": 0,
            }
        ],
        source_format="json",
        source_path="inline",
    )
    legacy_parsed = {**parsed, "sourceFingerprint": "legacy-fingerprint"}
    store = GptActionStore(tmp_path / "gpt.sqlite")

    first = store.save_bet_history_import(legacy_parsed)
    second = store.save_bet_history_import(parsed)
    report = store.bet_history_report()

    assert first["legsImported"] == 1
    assert second["duplicateSkipped"] is True
    assert second["importId"] == first["importId"]
    assert report["parsedLegs"] == 1


def test_bet_history_storage_lists_and_deletes_imports(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 2.9,
                "result": "won",
                "actual_stat": 0,
            }
        ],
        source_format="json",
        source_path="inline",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")
    saved = store.save_bet_history_import(parsed)

    imports = store.list_bet_history_imports()
    deleted = store.delete_bet_history_import(saved["importId"])
    report = store.bet_history_report()

    assert imports[0]["importId"] == saved["importId"]
    assert deleted["deleted"] is True
    assert deleted["legsDeleted"] == 1
    assert deleted["rawRowsDeleted"] == 1
    assert report["parsedLegs"] == 0


def test_bet_history_review_explains_ineligible_rows(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "selection": "Random Player Strikeouts Under 3.5",
                "odds": "1.83",
                "result": "loss",
            },
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "actual_stat": 0,
                "live_at_import": True,
            },
        ],
        source_format="json",
        source_path="inline",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    review = store.bet_history_review()
    screen = format_review_report(review)

    assert review["needsReview"] == 1
    assert review["reasonCounts"]["ambiguous_strikeouts_market"] == 1
    assert "live_or_in_progress_at_import" not in review["reasonCounts"]
    assert "Do not guess batter vs pitcher strikeouts" in screen
    assert "blocked_until_final" not in screen


def test_bet_history_storage_repairs_legacy_live_only_blockers(tmp_path):
    db_path = tmp_path / "gpt.sqlite"
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "actual_stat": 0,
            }
        ],
        source_format="json",
    )
    store = GptActionStore(db_path)
    store.save_bet_history_import(parsed)
    row = store.list_bet_history_legs()[0]

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE bet_history_legs
            SET parse_notes_json = ?,
                normalized_json = ?,
                needs_review = 1,
                training_eligible = 0
            WHERE history_leg_id = ?
            """,
            (
                json.dumps(["missing_odds", "live_or_in_progress_at_import"]),
                json.dumps({**row["normalized"], "liveAtImport": True}),
                row["historyLegId"],
            ),
        )

    repaired_store = GptActionStore(db_path)
    repaired = repaired_store.list_bet_history_legs()[0]

    assert repaired["needsReview"] is False
    assert repaired["trainingEligible"] is True
    assert repaired["parseNotes"] == ["missing_odds"]
    assert "liveAtImport" not in repaired["normalized"]


def test_bet_history_backtest_summarizes_hit_rate_and_roi(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 2.5,
                "result": "won",
                "actual_stat": 0,
            },
            {
                "date": "2026-05-08",
                "player": "Bo Bichette",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 1.8,
                "result": "lost",
                "actual_stat": 1,
            },
            {
                "date": "2026-05-08",
                "player": "Max Muncy",
                "market": "Batter Walks",
                "side": "under",
                "line": 0.5,
                "actual_stat": 0,
            },
        ],
        source_format="json",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    report = store.bet_history_backtest()

    assert report["overall"]["legs"] == 3
    assert report["overall"]["won"] == 2
    assert report["overall"]["lost"] == 1
    assert report["overall"]["hitRate"] == 0.6667
    assert report["overall"]["oddsLegs"] == 2
    assert report["overall"]["roiEligible"] == 2
    assert report["overall"]["roiExcludedMissingOdds"] == 1
    assert report["overall"]["missingOdds"] == 1
    assert report["overall"]["profitPerUnit"] == 0.5
    assert report["overall"]["roi"] == 0.25
    assert report["byMarket"][0]["label"] == "hits"

    # New stake-aware fields (no stake data in this import -> None / 0)
    assert report["overall"].get("stakeAwareEligible") == 0
    assert report["overall"].get("totalStaked") is None
    assert report["overall"].get("realizedRoi") is None
    assert "totalProfit" not in report["overall"] or report["overall"].get("totalProfit") is None


def test_bet_history_backtest_respects_stake_amount_for_roi(tmp_path):
    # Two legs with real stakes and odds. Third has stake+payout but no odds (realized path).
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-08",
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 2.5,
                "stake": 2.0,
                "result": "won",
            },
            {
                "date": "2026-05-08",
                "player": "Bo Bichette",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 1.8,
                "stake": 1.0,
                "result": "lost",
            },
            {
                "date": "2026-05-08",
                "player": "Max Muncy",
                "market": "Batter Walks",
                "side": "under",
                "line": 0.5,
                "stakeAmount": 5.0,
                "payoutAmount": 0.0,
                "result": "lost",
            },
        ],
        source_format="json",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    report = store.bet_history_backtest()

    overall = report["overall"]
    # Legacy unit view still works
    assert overall["roiEligible"] == 2
    assert overall["profitPerUnit"] == 0.5  # (2.5-1) + (-1) flat
    assert overall["roi"] == 0.25

    # New stake-weighted + realized
    assert overall["stakeAwareEligible"] == 2
    # Stakes: won 2u @ +1.5 = +3, lost 1u = -1, payout/stake row = -5.
    assert overall["totalStaked"] == 8.0
    assert overall["totalProfit"] == -3.0
    assert overall["realizedRoi"] == round(-3.0 / 8.0, 4)
    assert overall["realizedPnlRows"] == 1


def test_bet_history_ticket_backtest_groups_sgm_results_and_roi(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-06-01",
                "ticket_id": "ticket-win",
                "ticket_odds": 3.0,
                "player": "George Springer",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "result": "won",
                "actual_stat": 0,
            },
            {
                "date": "2026-06-01",
                "ticket_id": "ticket-win",
                "ticket_odds": 3.0,
                "player": "Bo Bichette",
                "market": "Singles",
                "side": "under",
                "line": 0.5,
                "result": "won",
                "actual_stat": 0,
            },
            {
                "date": "2026-06-02",
                "ticket_id": "ticket-loss",
                "ticket_odds": 4.5,
                "player": "Max Muncy",
                "market": "Batter Walks",
                "side": "under",
                "line": 0.5,
                "result": "lost",
                "actual_stat": 1,
            },
            {
                "date": "2026-06-02",
                "ticket_id": "ticket-loss",
                "ticket_odds": 4.5,
                "player": "Mookie Betts",
                "market": "Runs",
                "side": "under",
                "line": 0.5,
                "result": "won",
                "actual_stat": 0,
            },
            {
                "date": "2026-06-03",
                "ticket_id": "ticket-partial-void",
                "ticket_odds": 2.2,
                "player": "Alex Call",
                "market": "Batter Strikeouts",
                "side": "under",
                "line": 0.5,
                "result": "won",
                "actual_stat": 0,
            },
            {
                "date": "2026-06-03",
                "ticket_id": "ticket-partial-void",
                "ticket_odds": 2.2,
                "player": "Ryan Ward",
                "market": "Singles",
                "side": "under",
                "line": 0.5,
                "result": "void",
            },
        ],
        source_format="json",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    report = store.bet_history_backtest(view="tickets")

    tickets = report["tickets"]
    overall = tickets["overall"]
    assert overall["tickets"] == 3
    assert overall["sgmTickets"] == 3
    assert overall["won"] == 2
    assert overall["lost"] == 1
    assert overall["hitRate"] == 0.6667
    assert overall["roiEligible"] == 2
    assert overall["roiExcludedAdjustedOdds"] == 1
    assert overall["profitPerUnit"] == 1.0
    assert overall["roi"] == 0.5
    assert tickets["failureContributors"]["byMarket"][0]["label"] == "batter_walks"

    partial_void = next(row for row in tickets["ticketRows"] if row["ticketId"] == "ticket-partial-void")
    assert partial_void["resultStatus"] == "won"
    assert partial_void["roiBlocker"] == "adjusted_odds_unknown_after_push_or_void"


def test_bet_history_backtest_filters_player_market_date_and_ticket(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-05-30",
                "ticket_id": "old-ticket",
                "player": "Max Muncy",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "result": "won",
                "actual_stat": 0,
            },
            {
                "date": "2026-06-02",
                "ticket_id": "target-ticket",
                "player": "Max Muncy",
                "market": "Batter Walks",
                "side": "under",
                "line": 0.5,
                "result": "lost",
                "actual_stat": 1,
            },
            {
                "date": "2026-06-02",
                "ticket_id": "target-ticket",
                "player": "Mookie Betts",
                "market": "Batter Walks",
                "side": "under",
                "line": 0.5,
                "result": "won",
                "actual_stat": 0,
            },
        ],
        source_format="json",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    report = store.bet_history_backtest(
        market_key="Batter Walks",
        player_name="muncy",
        from_date="2026-06-01",
        ticket_id="target-ticket",
    )

    assert report["filters"]["marketKey"] == "batter_walks"
    assert report["filters"]["playerName"] == "muncy"
    assert report["overall"]["legs"] == 1
    assert report["overall"]["lost"] == 1
    assert report["tickets"]["overall"]["tickets"] == 1
    assert report["tickets"]["overall"]["lost"] == 1


def test_bet_history_backtest_signals_and_calibration_are_sample_aware(tmp_path):
    rows = []
    for index in range(10):
        rows.append(
            {
                "date": "2026-06-02",
                "ticket_id": f"ticket-{index}",
                "ticket_odds": 2.0,
                "player": "George Springer",
                "market": "Singles",
                "side": "under",
                "line": 0.5,
                "odds": 2.0,
                "result": "won" if index < 8 else "lost",
                "actual_stat": 0 if index < 8 else 1,
            }
        )
    parsed = parse_history_rows(rows, source_format="json")
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    report = store.bet_history_backtest(view="calibration")

    signal = report["signals"]["byMarket"][0]
    assert signal["label"] == "singles"
    assert signal["signal"] == "positive_history"
    calibration = report["calibration"]["marketSideLine"][0]
    assert calibration["label"] == "singles | under | line 0.5"
    assert calibration["status"] == "usable"
    assert calibration["historicalEdge"] == 0.3
    assert calibration["recommendedAdjustment"] == 0.08

    screen = format_backtest_report(report)
    assert "Calibration by market-side-line:" in screen
    assert "adjustment +0.0800" in screen


def test_bet_history_backtest_dashboard_includes_automated_final_outcome(tmp_path):
    rows = []
    for index in range(12):
        rows.append(
            {
                "date": "2026-06-02",
                "ticket_id": f"ticket-{index // 2}",
                "ticket_odds": 2.4,
                "player": "George Springer" if index % 2 == 0 else "Bo Bichette",
                "market": "Singles" if index % 2 == 0 else "Batter Strikeouts",
                "side": "under",
                "line": 0.5 if index % 2 == 0 else 1.5,
                "odds": 2.0,
                "result": "won" if index < 7 else "lost",
                "actual_stat": 0 if index < 7 else 1 if index % 2 == 0 else 2,
            }
        )
    parsed = parse_history_rows(rows, source_format="json")
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    report = store.bet_history_backtest()
    screen = format_backtest_report(report)

    assert report["flow"][0]["step"] == "load_history"
    assert report["flow"][-1]["step"] == "final_outcome"
    assert report["finalOutcome"]["legSample"]["gradedLegs"] == 12
    assert report["finalOutcome"]["ticketSample"]["tickets"] == 6
    assert report["signals"]["byMarketLine"][0]["label"] in {
        "singles | line 0.5",
        "batter_strikeouts | line 1.5",
    }
    assert report["signals"]["underOnly"]["overall"]["gradedLegs"] == 12
    assert report["enrichedBuckets"]["byLongshotOdds"][0]["label"] == "ticket odds under 10"
    assert report["enrichedBuckets"]["byLegCount"][0]["label"] == "2-5 legs"
    assert report["finalOutcome"]["modelReadiness"]["status"] == "backtest_ready_ml_dataset_forming"
    assert "Automated flow:" in screen
    assert "Final Outcome" in screen
    assert "Enriched / ticket-structure buckets:" in screen
    assert "Verdict:" in screen
    assert "Next action:" in screen


def test_bet_history_backtest_rich_report_uses_boxed_dashboard(tmp_path):
    rows = []
    for index in range(12):
        rows.append(
            {
                "date": "2026-06-02",
                "ticket_id": f"ticket-{index // 2}",
                "ticket_odds": 4.5,
                "player": "George Springer" if index % 2 == 0 else "Bo Bichette",
                "market": "Singles" if index % 2 == 0 else "Batter Strikeouts",
                "side": "under",
                "line": 0.5 if index % 2 == 0 else 1.5,
                "odds": 2.0,
                "result": "won" if index < 8 else "lost",
                "actual_stat": 0 if index < 8 else 2,
            }
        )
    parsed = parse_history_rows(rows, source_format="json")
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    report = store.bet_history_backtest()
    screen = format_backtest_rich_report(report, width=150)

    assert "Stake-GPT Historic Analysis" in screen
    assert "Final Outcome" in screen
    assert "Leg Sample" in screen
    assert "Ticket Sample" in screen
    assert "Strongest Markets" in screen
    assert "Calibration Preview" in screen
    assert "Context Buckets" in screen
    assert any(char in screen for char in ("╭", "┌"))


def test_bet_history_model_readiness_stays_forming_without_enrichment(tmp_path):
    rows = []
    for ticket_index in range(45):
        ticket_won = ticket_index % 3 != 0
        for leg_index in range(6):
            market = "Singles" if leg_index % 2 == 0 else "Batter Strikeouts"
            line = 0.5 if market == "Singles" else 1.5
            won_leg = ticket_won or leg_index < 5
            rows.append(
                {
                    "date": "2026-06-02",
                    "ticket_id": f"ticket-{ticket_index}",
                    "ticket_odds": 85.0,
                    "player": f"Player {leg_index}",
                    "market": market,
                    "side": "under",
                    "line": line,
                    "result": "won" if won_leg else "lost",
                    "actual_stat": 0 if won_leg else 2,
                }
            )
    parsed = parse_history_rows(rows, source_format="json")
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    report = store.bet_history_backtest()
    outcome = report["finalOutcome"]

    assert outcome["legSample"]["gradedLegs"] == 270
    assert outcome["legSample"]["oddsLegs"] == 0
    assert outcome["ticketSample"]["gradedTickets"] == 45
    assert outcome["modelReadiness"]["status"] == "backtest_ready_ml_dataset_forming"
    assert outcome["modelReadiness"]["label"] == "Historic-analysis-ready, ML dataset forming"
    assert "historical MLB enrichment coverage is 0.0%" in outcome["modelReadiness"]["reason"]
    assert outcome["modelReadiness"]["gates"]["enrichmentCoverage"] == 0.0
    assert "historic enrich --missing-only" in outcome["nextAction"]


def test_bet_history_candidate_signal_returns_sample_gated_adjustment(tmp_path):
    rows = []
    for index in range(15):
        rows.append(
            {
                "date": "2026-06-02",
                "ticket_id": f"ticket-{index}",
                "ticket_odds": 2.0,
                "player": "George Springer",
                "market": "Singles",
                "side": "under",
                "line": 0.5,
                "odds": 2.0,
                "result": "won" if index < 11 else "lost",
                "actual_stat": 0 if index < 11 else 1,
            }
        )
    parsed = parse_history_rows(rows, source_format="json")
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    signal = store.bet_history_candidate_signal(
        player_name="George Springer",
        market_key="Singles",
        side="under",
        line=0.5,
    )

    assert signal["status"] == "positive_history_signal"
    assert signal["applied"]["bucket"] == "player_market_line"
    assert signal["applied"]["gradedLegs"] == 15
    assert signal["applied"]["probabilityAdjustment"] == 0.08
    assert signal["applied"]["scoreAdjustment"] == 2.8
    assert signal["buckets"]["marketSideLine"]["status"] == "low_sample"
    assert "historical_signal_score_adjusted" in signal["notes"]


class FakeHistoricalMlbEngine:
    def __init__(self) -> None:
        self.schedule_calls: list[str] = []
        self.context_calls: list[int] = []

    async def get_schedule(self, game_date: str):
        self.schedule_calls.append(game_date)
        return {
            "date": game_date,
            "gameCount": 1,
            "games": [
                {
                    "gamePk": 999001,
                    "gameDate": f"{game_date}T23:07:00Z",
                    "status": "Final",
                    "awayTeam": {
                        "mlbId": 147,
                        "name": "New York Yankees",
                        "key": "new-york-yankees",
                        "probablePitcher": {
                            "mlbId": 111,
                            "name": "Yankees Starter",
                            "key": "yankees-starter",
                        },
                    },
                    "homeTeam": {
                        "mlbId": 141,
                        "name": "Toronto Blue Jays",
                        "key": "toronto-blue-jays",
                        "probablePitcher": {
                            "mlbId": 222,
                            "name": "Blue Jays Starter",
                            "key": "blue-jays-starter",
                        },
                    },
                }
            ],
        }

    async def get_game_context(self, game_pk: int):
        self.context_calls.append(game_pk)
        batting_stats = {
            "hits": 0,
            "doubles": 0,
            "triples": 0,
            "homeRuns": 0,
            "runs": 0,
            "rbi": 0,
            "baseOnBalls": 1,
            "strikeOuts": 2,
            "totalBases": 0,
            "stolenBases": 0,
        }
        springer = {
            "mlbId": 543807,
            "name": "George Springer",
            "key": "george-springer",
            "batSide": "R",
            "pitchHand": None,
            "position": "RF",
            "battingOrder": 1,
            "confirmedStarter": True,
            "stats": {"batting": batting_stats},
        }
        return {
            "gamePk": game_pk,
            "gameDate": "2026-06-02T23:07:00Z",
            "officialDate": "2026-06-02",
            "status": {
                "abstractGameState": "Final",
                "detailedState": "Final",
                "codedGameState": "F",
                "statusCode": "F",
                "reason": None,
                "startTimeTBD": False,
            },
            "statusRiskFlags": [],
            "gameInfo": {"gameType": "R", "doubleHeader": "N", "gameNumber": 1, "dayNight": "night"},
            "venue": {"mlbId": 14, "name": "Rogers Centre", "roofType": "retractable"},
            "weather": {"condition": "Clear", "temp": "72", "wind": "5 mph"},
            "teams": {
                "away": {
                    "team": {"mlbId": 147, "name": "New York Yankees", "key": "new-york-yankees"},
                    "lineupConfirmed": True,
                    "lineup": [],
                    "playersById": {},
                    "teamStats": {},
                },
                "home": {
                    "team": {"mlbId": 141, "name": "Toronto Blue Jays", "key": "toronto-blue-jays"},
                    "lineupConfirmed": True,
                    "lineup": [springer],
                    "playersById": {"543807": springer},
                    "teamStats": {},
                },
            },
        }


def test_bet_history_enrichment_stores_snapshots_and_feeds_backtest(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-06-02",
                "ticket_id": "ticket-1",
                "player": "George Springer",
                "team": "Toronto Blue Jays",
                "opponent": "New York Yankees",
                "matchup": "New York Yankees - Toronto Blue Jays",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "odds": 1.91,
                "result": "won",
            }
        ],
        source_format="json",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)
    engine = FakeHistoricalMlbEngine()

    report = asyncio.run(
        enrich_bet_history(
            store=store,
            mlb_engine=engine,
            missing_only=True,
        )
    )

    assert report["targets"] == 1
    assert report["legsEnriched"] == 1
    assert report["snapshotsCreated"] == 1
    assert report["resultMismatches"] == 0
    assert engine.schedule_calls == ["2026-06-02"]
    assert engine.context_calls == [999001]

    screen = format_enrichment_report(report)
    assert "Game snapshots: 1" in screen
    assert "George Springer | hits under 0.5" in screen

    backtest = store.bet_history_backtest()
    assert backtest["enrichment"]["status"] == "enriched"
    assert backtest["enrichment"]["enrichedLegs"] == 1
    assert backtest["enrichment"]["gradedBySnapshot"] == 1
    assert backtest["enrichedBuckets"]["byLineupSpot"][0]["label"] == "top third | batting 1"
    assert backtest["enrichedBuckets"]["byStarterRole"][0]["label"] == "confirmed starter"
    assert backtest["enrichedBuckets"]["byVenue"][0]["label"] == "Rogers Centre"
    assert "Historical enrichment: enriched" in format_backtest_report(backtest)

    signal = store.bet_history_candidate_signal(
        player_name="George Springer",
        market_key="Hits",
        side="under",
        line=0.5,
    )
    assert signal["enrichment"]["status"] == "available"
    assert signal["enrichment"]["enrichedLegs"] == 1
    assert "historical_enrichment_available" in signal["notes"]

    dataset = store.build_bet_history_dataset()
    assert dataset["datasetVersion"] == "historic_dataset_v1"
    assert dataset["rows"] == 1
    assert dataset["trainingRows"] == 1
    assert dataset["enrichedRows"] == 1
    assert dataset["underRows"] == 1
    assert dataset["buckets"]["lineupSpots"][0]["label"] == "top third | batting 1"
    assert dataset["preview"][0]["marketKey"] == "hits"
    assert dataset["readiness"]["status"] == "ml_dataset_forming"
    assert "Bet Historic Dataset" in format_dataset_report(dataset)

    latest = store.latest_bet_history_dataset()
    assert latest is not None
    assert latest["datasetRunId"] == dataset["datasetRunId"]
    assert latest["trainingRows"] == 1


def test_bet_history_enrichment_missing_only_skips_existing_rows(tmp_path):
    parsed = parse_history_rows(
        [
            {
                "date": "2026-06-02",
                "player": "George Springer",
                "team": "Toronto Blue Jays",
                "opponent": "New York Yankees",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
                "result": "won",
            }
        ],
        source_format="json",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)
    engine = FakeHistoricalMlbEngine()

    first = asyncio.run(enrich_bet_history(store=store, mlb_engine=engine, missing_only=True))
    second = asyncio.run(enrich_bet_history(store=store, mlb_engine=engine, missing_only=True))

    assert first["legsEnriched"] == 1
    assert second["targets"] == 0
    assert second["legsEnriched"] == 0
    assert len(engine.context_calls) == 1


def test_bet_history_model_trains_and_stores_offline_baseline(tmp_path):
    rows = []
    for index in range(160):
        market = "Hits" if index % 2 else "RBI"
        won = index % 5 != 0 if market == "Hits" else index % 4 != 0
        rows.append(
            {
                "date": f"2026-06-{(index % 28) + 1:02d}",
                "ticket_id": f"ticket-{index // 4}",
                "player": f"Player {index % 12}",
                "team": "Toronto Blue Jays",
                "opponent": "New York Yankees",
                "market": market,
                "side": "under",
                "line": 0.5,
                "ticket_odds": 25.0 if index % 8 else 15000.0,
                "result": "won" if won else "lost",
                "actual_stat": 0 if won else 1,
            }
        )

    parsed = parse_history_rows(rows, source_format="json")
    store = GptActionStore(tmp_path / "gpt.sqlite")
    store.save_bet_history_import(parsed)

    report = store.build_bet_history_model()

    assert report["modelVersion"] == "historic_bucket_baseline_v1"
    assert report["strategyProfile"] == "under_longshot_sgm_leg_baseline"
    assert report["target"] == "leg_win_probability"
    assert report["sideFilter"] == "under"
    assert report["trainingRows"] >= 80
    assert report["holdoutRows"] >= 30
    assert report["validation"]["canInfluenceBuilds"] is False
    assert report["metrics"]["holdout"]["rows"] == report["holdoutRows"]
    assert "marketKey" in report["featureNames"]
    assert "Bet Historic Model" in format_model_report(report)

    latest = store.latest_bet_history_model()
    assert latest is not None
    assert latest["modelRunId"] == report["modelRunId"]
    assert latest["validation"]["canInfluenceBuilds"] is False

    rebuilt_dataset = store.build_bet_history_dataset()
    assert rebuilt_dataset["rows"] == 160
    assert store.latest_bet_history_model() is None


def test_bet_history_empty_report_points_to_import_folder(tmp_path):
    import_dir = tmp_path / "data" / "bet-history" / "imports"
    import_dir.mkdir(parents=True)
    (import_dir / "p1.txt").write_text("raw", encoding="utf-8")
    (import_dir / ".gitkeep").write_text("", encoding="utf-8")

    screen = format_report(
        {
            "sourcePath": "local_database",
            "rawRows": 0,
            "parsedLegs": 0,
            "needsReview": 0,
            "trainingEligible": 0,
            "confidence": {},
            "markets": {},
            "results": {},
            "missingOrAmbiguous": {},
            "ignoredFields": {},
            "importFiles": list_import_files(import_dir),
            "reviewSamples": [],
        },
        storage_path=tmp_path / "gpt.sqlite",
        import_dir=import_dir,
    )

    assert screen.startswith("Bet Historic")
    assert "Database:" in screen
    assert "Import folder:" in screen
    assert "No bet historic has been imported yet." in screen
    assert "Training eligible: 0" in screen
    assert "Pending import files:" in screen
    assert "- p1.txt" in screen


def test_bet_history_sync_imports_new_folder_files_and_skips_duplicates(tmp_path):
    import_dir = tmp_path / "data" / "bet-history" / "imports"
    import_dir.mkdir(parents=True)
    (import_dir / "p1.csv").write_text(
        "\n".join(
            [
                "Date,Player,Market,Side,Line,Odds,Result,Actual Stat",
                "2026-06-02,George Springer,Hits,Under,0.5,1.91,Won,0",
            ]
        ),
        encoding="utf-8",
    )
    (import_dir / "p2.csv").write_text(
        "\n".join(
            [
                "Date,Player,Market,Side,Line,Odds,Result,Actual Stat",
                "2026-06-02,Ryan Ward,Singles,Under,0.5,1.85,Won,0",
            ]
        ),
        encoding="utf-8",
    )
    (import_dir / "p10.csv").write_text(
        "\n".join(
            [
                "Date,Player,Market,Side,Line,Odds,Result,Actual Stat",
                "2026-06-02,Zach Neto,Batter Strikeouts,Under,1.5,1.82,Lost,2",
            ]
        ),
        encoding="utf-8",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")

    first = sync_import_folder(store, import_dir)
    second = sync_import_folder(store, import_dir)

    assert first["filesConsidered"] == 3
    assert first["filesImported"] == 3
    assert first["filesSkippedDuplicate"] == 0
    assert first["history"]["importFiles"] == ["p1.csv", "p2.csv", "p10.csv"]
    assert first["history"]["parsedLegs"] == 3
    assert second["filesImported"] == 0
    assert second["filesSkippedDuplicate"] == 3
    assert second["history"]["parsedLegs"] == 3

    screen = format_sync_report(second, storage_path=store.db_path)
    assert "Bet Historic Sync" in screen
    assert "Latest checked files: p1.csv, p2.csv, p10.csv" in screen
    assert "Skipped duplicates: 3" in screen
    assert "Parsed legs: 3" in screen
    assert "Import folder files:" in screen


def test_bet_history_sync_refreshes_existing_file_without_duplicate_rows(tmp_path):
    import_dir = tmp_path / "data" / "bet-history" / "imports"
    import_dir.mkdir(parents=True)
    path = import_dir / "p1.csv"
    path.write_text(
        "\n".join(
            [
                "Date,Ticket Id,Player,Market,Side,Line,Result,Actual Stat",
                "2026-06-02,sgm_1,George Springer,Hits,Under,0.5,Won,0",
            ]
        ),
        encoding="utf-8",
    )
    store = GptActionStore(tmp_path / "gpt.sqlite")

    first = sync_import_folder(store, import_dir)
    path.write_text(
        "\n".join(
            [
                "Date,Ticket Id,Ticket Odds,Player,Market,Side,Line,Result,Actual Stat",
                "2026-06-02,sgm_1,301.00,George Springer,Hits,Under,0.5,Won,0",
            ]
        ),
        encoding="utf-8",
    )
    second = sync_import_folder(store, import_dir)
    third = sync_import_folder(store, import_dir)
    rows = store.list_bet_history_legs(limit=10)

    assert first["filesImported"] == 1
    assert second["filesImported"] == 0
    assert second["filesSkippedDuplicate"] == 1
    assert second["refreshedLegs"] == 1
    assert third["refreshedLegs"] == 0
    assert store.bet_history_report()["parsedLegs"] == 1
    assert rows[0]["normalized"]["ticketOdds"] == 301.0


def test_market_and_odds_helpers_cover_common_variants():
    assert normalize_market("Hits + Runs + RBIs")[0] == "hits_runs_rbis"
    assert normalize_market("Batter Ks")[0] == "batter_strikeouts"
    assert normalize_market("Runs + RBI")[0] == "runs_rbis"
    assert normalize_market("BB")[0] == "batter_walks"
    assert normalize_market("Steals")[0] == "stolen_bases"
    assert normalize_market("Hitter Strikeouts")[0] == "batter_strikeouts"
    assert normalize_market("Pitcher Ks")[0] == "pitcher_strikeouts"
    assert normalize_market("Outs Recorded")[0] == "outs_recorded"
    assert normalize_market("Hits Allowed")[0] == "hits_allowed"
    assert normalize_market("Earned Runs Allowed")[0] == "earned_runs"
    assert normalize_market("Walks Allowed")[0] == "walks_allowed"
    assert normalize_market("Strikeouts")[1] == ["ambiguous_strikeouts_market"]
    assert parse_odds("2.50") == 2.5
    assert parse_odds("1.83") == 1.83
    assert parse_odds("20.00") == 20.0


def test_history_market_support_covers_backend_mlb_prop_markets():
    unsupported = {
        market_key: normalize_mlb_prop_market_key(market_key)
        for market_key in MARKET_STAT_MAP
        if normalize_mlb_prop_market_key(market_key) not in SUPPORTED_MLB_PROP_MARKETS
    }

    assert unsupported == {}


def test_settle_result_handles_over_under_and_push():
    assert settle_result(side="under", line=1.5, actual_stat=3) == "lost"
    assert settle_result(side="under", line=0.5, actual_stat=0) == "won"
    assert settle_result(side="over", line=1.5, actual_stat=3) == "won"
    assert settle_result(side="over", line=1.5, actual_stat=1) == "lost"
    assert settle_result(side="over", line=2.0, actual_stat=2) == "push"
