from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bet_history_fingerprint import FINGERPRINT_VERSION, history_fingerprint
from .market_normalization import SUPPORTED_MLB_PROP_MARKETS, normalize_mlb_prop_market_key
from .supabase_history import (
    delete_supabase_history_import,
    supabase_history_enabled,
    sync_sqlite_history_to_supabase,
    sync_supabase_history_to_sqlite,
)


DEFAULT_DB_PATH = Path("data") / "gpt_action.sqlite"
_REMOTE_HISTORY_PULL_CACHE: dict[str, float] = {}


class GptActionStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        explicit_db_path = db_path is not None
        configured_path = db_path or os.getenv("AZP_DB_PATH") or DEFAULT_DB_PATH
        self.db_path = Path(configured_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._history_remote_enabled = supabase_history_enabled(explicit_db_path=explicit_db_path)
        self._history_remote_loaded = False
        self._history_remote_last_error: str | None = None
        self._ensure_schema()

    def save_gpt_decision_result(
        self,
        response: dict[str, Any],
        request_body: dict[str, Any],
    ) -> dict[str, Any]:
        decision_id = str(uuid.uuid4())
        captured_at = _utc_now()
        selections = response.get("selections") or []
        validation = response.get("validation") or {}

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gpt_decision_requests (
                    decision_id, captured_at, source, matchup, slate_date,
                    prompt, request_json, response_json, validation_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    captured_at,
                    "custom_gpt",
                    response.get("matchup") or request_body.get("matchup"),
                    response.get("date") or request_body.get("date"),
                    request_body.get("prompt"),
                    _json_dumps(request_body),
                    _json_dumps(response),
                    _json_dumps(validation),
                    _json_dumps(_decision_metadata(response, request_body)),
                ),
            )
            for rank, selection in enumerate(selections, start=1):
                conn.execute(
                    """
                    INSERT INTO gpt_decision_legs (
                        leg_id, decision_id, rank, captured_at, slate_date, matchup,
                        selection_id, prop_id, fixture_slug, player_name, team_name,
                        market_key, market_name, side, line, odds, playable, status,
                        selection_json, decision_profile_json, risk_flags_json,
                        settlement_status, actual_stat, settled_at,
                        settlement_confidence, settlement_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _decision_leg_values(
                        decision_id=decision_id,
                        captured_at=captured_at,
                        slate_date=response.get("date") or request_body.get("date"),
                        matchup=response.get("matchup") or request_body.get("matchup"),
                        rank=rank,
                        selection=selection,
                    ),
                )
            conn.commit()

        return {
            "decisionId": decision_id,
            "capturedAt": captured_at,
            "gptDecisionLegsInserted": len(selections),
        }

    def list_gpt_decision_legs(
        self,
        date_text: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM gpt_decision_legs"
        params: list[Any] = []
        if date_text:
            sql += " WHERE slate_date = ?"
            params.append(date_text)
        sql += " ORDER BY captured_at DESC, rank ASC LIMIT ?"
        params.append(_clean_limit(limit))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_leg_row(row) for row in rows]

    def save_market_mappings(self, mappings: list[dict[str, Any]]) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            for mapping in mappings:
                conn.execute(
                    """
                    INSERT INTO market_mappings (
                        sport, stake_display_name, internal_market_key, stat_key,
                        group_name, last_seen_at, active, examples_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sport, stake_display_name, internal_market_key)
                    DO UPDATE SET
                        stat_key = excluded.stat_key,
                        group_name = excluded.group_name,
                        last_seen_at = excluded.last_seen_at,
                        active = excluded.active,
                        examples_json = excluded.examples_json
                    """,
                    (
                        mapping.get("sport") or "mlb",
                        mapping.get("stakeDisplayName"),
                        mapping.get("internalMarketKey"),
                        mapping.get("statKey"),
                        mapping.get("group"),
                        now,
                        1 if mapping.get("active", True) else 0,
                        _json_dumps(mapping.get("examples") or []),
                    ),
                )
            conn.commit()
        return {"marketMappingsSaved": len(mappings), "capturedAt": now}

    def save_bet_history_import(self, parsed: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        self._load_remote_bet_history_once()
        import_id = str(uuid.uuid4())
        imported_at = _utc_now()
        report = parsed.get("report") or {}
        raw_rows = list(parsed.get("rawRows") or [])
        legs = list(parsed.get("legs") or [])
        source_fingerprint = parsed.get("sourceFingerprint") or history_fingerprint(legs)
        raw_id_by_row: dict[int, str] = {}

        with self._connect() as conn:
            if not force:
                duplicate = _find_duplicate_bet_history_import(
                    conn,
                    source_fingerprint=source_fingerprint,
                    legs=legs,
                )
                if duplicate:
                    refreshed_legs = _refresh_duplicate_bet_history_metadata(
                        conn,
                        import_id=duplicate["importId"],
                        legs=legs,
                    )
                    if refreshed_legs:
                        conn.execute(
                            """
                            UPDATE bet_history_imports
                            SET parser_version = ?,
                                eligibility_version = ?,
                                report_json = ?
                            WHERE import_id = ?
                            """,
                            (
                                parsed.get("parserVersion"),
                                parsed.get("eligibilityVersion"),
                                _json_dumps(report),
                                duplicate["importId"],
                            ),
                        )
                        conn.commit()
                    return {
                        "importId": duplicate["importId"],
                        "importedAt": duplicate.get("importedAt"),
                        "rawRowsImported": 0,
                        "legsImported": 0,
                        "needsReview": 0,
                        "duplicateSkipped": True,
                        "duplicateReason": "same_normalized_history",
                        "refreshedLegs": refreshed_legs,
                        "supabaseHistorySync": self.sync_bet_history_to_supabase(
                            table_names=("bet_history_imports", "bet_history_legs")
                        ),
                    }
            conn.execute(
                """
                INSERT INTO bet_history_imports (
                    import_id, imported_at, source_path, source_format,
                    source_fingerprint, fingerprint_version, parser_version,
                    eligibility_version, raw_row_count, parsed_leg_count,
                    needs_review_count, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    import_id,
                    imported_at,
                    parsed.get("sourcePath"),
                    parsed.get("sourceFormat"),
                    source_fingerprint,
                    parsed.get("fingerprintVersion") or FINGERPRINT_VERSION,
                    parsed.get("parserVersion"),
                    parsed.get("eligibilityVersion"),
                    len(raw_rows),
                    len(legs),
                    int(report.get("needsReview") or 0),
                    _json_dumps(report),
                ),
            )
            for raw in raw_rows:
                raw_id = str(uuid.uuid4())
                row_number = int(raw.get("sourceRowNumber") or 0)
                raw_id_by_row[row_number] = raw_id
                conn.execute(
                    """
                    INSERT INTO bet_history_raw (
                        raw_id, import_id, source_row_number, source_format,
                        raw_text, raw_json, parse_status, parse_notes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        raw_id,
                        import_id,
                        row_number,
                        raw.get("sourceFormat") or parsed.get("sourceFormat"),
                        raw.get("rawText"),
                        _json_dumps(raw.get("rawJson") or {}),
                        "parsed",
                        _json_dumps([]),
                    ),
                )
            for leg in legs:
                conn.execute(
                    """
                    INSERT INTO bet_history_legs (
                        history_leg_id, import_id, raw_id, ticket_id, leg_index,
                        bet_date, settled_date, sport, league, player_name, team_name,
                        opponent_name, fixture_slug, matchup, market_key, market_name,
                        side, line, odds, stake_amount, payout_amount, result_status,
                        actual_stat, parse_confidence, parse_confidence_label,
                        needs_review, training_eligible, parser_version,
                        eligibility_version, parse_notes_json, ignored_fields_json,
                        normalized_json, raw_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _bet_history_leg_values(
                        import_id=import_id,
                        raw_id=raw_id_by_row.get(int(leg.get("sourceRowNumber") or 0)),
                        leg=leg,
                        created_at=imported_at,
                    ),
                )
            conn.commit()

        result = {
            "importId": import_id,
            "importedAt": imported_at,
            "rawRowsImported": len(raw_rows),
            "legsImported": len(legs),
            "needsReview": int(report.get("needsReview") or 0),
            "force": bool(force),
        }
        result["supabaseHistorySync"] = self.sync_bet_history_to_supabase(
            table_names=("bet_history_imports", "bet_history_raw", "bet_history_legs")
        )
        return result

    def list_bet_history_legs(
        self,
        *,
        needs_review: bool | None = None,
        import_id: str | None = None,
        training_eligible: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._load_remote_bet_history_once()
        sql = "SELECT * FROM bet_history_legs"
        params: list[Any] = []
        where: list[str] = []
        if import_id:
            where.append("import_id = ?")
            params.append(str(import_id))
        if needs_review is not None:
            where.append("needs_review = ?")
            params.append(1 if needs_review else 0)
        if training_eligible is not None:
            where.append("training_eligible = ?")
            params.append(1 if training_eligible else 0)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, bet_date DESC, leg_index ASC LIMIT ?"
        params.append(_clean_limit(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_bet_history_leg_row(row) for row in rows]

    def bet_history_review(
        self,
        *,
        import_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        review_rows = self.list_bet_history_legs(
            needs_review=True,
            import_id=import_id,
            limit=limit,
        )
        unresolved_by_reason: dict[str, int] = {}
        for row in review_rows:
            reasons = _history_review_reasons(row)
            for reason in reasons:
                unresolved_by_reason[reason] = unresolved_by_reason.get(reason, 0) + 1
        return {
            "sourcePath": "local_database",
            "importId": import_id,
            "limit": limit,
            "needsReview": len(review_rows),
            "reasonCounts": dict(sorted(unresolved_by_reason.items())),
            "reviewRows": [_bet_history_review_row(row) for row in review_rows],
        }

    def list_bet_history_imports(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self._load_remote_bet_history_once()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM bet_history_imports
                ORDER BY imported_at DESC
                LIMIT ?
                """,
                (_clean_limit(limit),),
            ).fetchall()
        return [_bet_history_import_row(row) for row in rows]

    def delete_bet_history_import(self, import_id: str) -> dict[str, Any]:
        self._load_remote_bet_history_once()
        clean_id = str(import_id or "").strip()
        if not clean_id:
            return {"deleted": False, "reason": "missing_import_id"}
        with self._connect() as conn:
            found = conn.execute(
                "SELECT import_id FROM bet_history_imports WHERE import_id = ?",
                (clean_id,),
            ).fetchone()
            if not found:
                return {"deleted": False, "importId": clean_id, "reason": "not_found"}
            leg_count = conn.execute(
                "SELECT COUNT(*) AS count FROM bet_history_legs WHERE import_id = ?",
                (clean_id,),
            ).fetchone()["count"]
            raw_count = conn.execute(
                "SELECT COUNT(*) AS count FROM bet_history_raw WHERE import_id = ?",
                (clean_id,),
            ).fetchone()["count"]
            conn.execute("DELETE FROM bet_history_legs WHERE import_id = ?", (clean_id,))
            conn.execute("DELETE FROM bet_history_raw WHERE import_id = ?", (clean_id,))
            conn.execute("DELETE FROM bet_history_imports WHERE import_id = ?", (clean_id,))
            conn.commit()
        remote_delete: dict[str, Any] | None = None
        if self._history_remote_enabled:
            try:
                remote_delete = delete_supabase_history_import(clean_id)
            except Exception as exc:
                if _fail_on_supabase_history_error():
                    raise
                remote_delete = {"deleted": False, "error": str(exc)}
        result = {
            "deleted": True,
            "importId": clean_id,
            "legsDeleted": int(leg_count or 0),
            "rawRowsDeleted": int(raw_count or 0),
        }
        if remote_delete is not None:
            result["supabaseHistoryDelete"] = remote_delete
        return result

    def bet_history_report(self, *, review_limit: int = 25) -> dict[str, Any]:
        self._load_remote_bet_history_once()
        with self._connect() as conn:
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS parsed_legs,
                    SUM(CASE WHEN needs_review = 1 THEN 1 ELSE 0 END) AS needs_review,
                    SUM(CASE WHEN training_eligible = 1 THEN 1 ELSE 0 END) AS training_eligible
                FROM bet_history_legs
                """
            ).fetchone()
            confidence = conn.execute(
                """
                SELECT parse_confidence_label AS label, COUNT(*) AS count
                FROM bet_history_legs
                GROUP BY parse_confidence_label
                """
            ).fetchall()
            markets = conn.execute(
                """
                SELECT COALESCE(market_key, 'unknown') AS label, COUNT(*) AS count
                FROM bet_history_legs
                GROUP BY COALESCE(market_key, 'unknown')
                ORDER BY count DESC
                """
            ).fetchall()
            results = conn.execute(
                """
                SELECT COALESCE(result_status, 'unknown') AS label, COUNT(*) AS count
                FROM bet_history_legs
                GROUP BY COALESCE(result_status, 'unknown')
                ORDER BY count DESC
                """
            ).fetchall()
        review = self.list_bet_history_legs(needs_review=True, limit=review_limit)
        return {
            "sourcePath": "local_database",
            "rawRows": self._bet_history_raw_count(),
            "parsedLegs": int((totals or {})["parsed_legs"] or 0),
            "needsReview": int((totals or {})["needs_review"] or 0),
            "trainingEligible": int((totals or {})["training_eligible"] or 0),
            "confidence": {row["label"]: row["count"] for row in confidence},
            "markets": {row["label"]: row["count"] for row in markets},
            "results": {row["label"]: row["count"] for row in results},
            "missingOrAmbiguous": _notes_counter(review),
            "ignoredFields": _ignored_fields_counter(review),
            "ignoredFieldCount": sum(len(row.get("ignoredFields") or []) for row in review),
            "enrichment": self.bet_history_enrichment_report(),
            "reviewSamples": [_bet_history_review_sample(row) for row in review],
        }

    def bet_history_enrichment_targets(
        self,
        *,
        import_id: str | None = None,
        from_date: str | None = None,
        missing_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self._load_remote_bet_history_once()
        sql = """
            SELECT
                l.*,
                e.game_pk AS enrichment_game_pk,
                e.player_mlb_id AS enrichment_player_mlb_id,
                e.stat_value AS enrichment_stat_value,
                e.enriched_result_status AS enrichment_result_status,
                e.context_quality AS enrichment_context_quality,
                e.notes_json AS enrichment_notes_json,
                e.enriched_at AS enrichment_enriched_at
            FROM bet_history_legs l
            LEFT JOIN bet_history_leg_enrichments e
                ON e.history_leg_id = l.history_leg_id
            WHERE l.sport = 'mlb'
              AND l.bet_date IS NOT NULL
              AND l.market_key IS NOT NULL
              AND l.side IN ('over', 'under')
              AND l.line IS NOT NULL
              AND l.player_name IS NOT NULL
        """
        params: list[Any] = []
        if import_id:
            sql += " AND l.import_id = ?"
            params.append(import_id)
        if from_date:
            sql += " AND l.bet_date >= ?"
            params.append(from_date)
        if missing_only:
            sql += " AND e.history_leg_id IS NULL"
        sql += " ORDER BY l.bet_date ASC, l.ticket_id ASC, l.leg_index ASC LIMIT ?"
        params.append(_clean_history_limit(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_bet_history_leg_row(row) for row in rows]

    def get_bet_history_game_snapshot(self, game_pk: int) -> dict[str, Any] | None:
        self._load_remote_bet_history_once()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bet_history_game_snapshots WHERE game_pk = ?",
                (int(game_pk),),
            ).fetchone()
        return _bet_history_game_snapshot_row(row) if row else None

    def save_bet_history_game_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        game_pk = int(snapshot.get("gamePk") or 0)
        if game_pk <= 0:
            raise ValueError("gamePk is required for a historical game snapshot.")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO bet_history_game_snapshots (
                    game_pk, official_date, game_date, matchup_key,
                    away_team_name, home_team_name, final_status,
                    venue_json, probable_pitchers_json, pregame_context_json,
                    grading_context_json, raw_context_json, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_pk,
                    snapshot.get("officialDate"),
                    snapshot.get("gameDate"),
                    snapshot.get("matchupKey"),
                    snapshot.get("awayTeamName"),
                    snapshot.get("homeTeamName"),
                    snapshot.get("finalStatus"),
                    _json_dumps(snapshot.get("venue") or {}),
                    _json_dumps(snapshot.get("probablePitchers") or {}),
                    _json_dumps(snapshot.get("pregameContext") or {}),
                    _json_dumps(snapshot.get("gradingContext") or {}),
                    _json_dumps(snapshot.get("rawContext") or {}),
                    snapshot.get("source") or "mlb_stats_api",
                    snapshot.get("fetchedAt") or now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM bet_history_game_snapshots WHERE game_pk = ?",
                (game_pk,),
            ).fetchone()
            conn.commit()
        return _bet_history_game_snapshot_row(row)

    def save_bet_history_leg_enrichment(self, enrichment: dict[str, Any]) -> dict[str, Any]:
        history_leg_id = str(enrichment.get("historyLegId") or "").strip()
        game_pk = int(enrichment.get("gamePk") or 0)
        if not history_leg_id:
            raise ValueError("historyLegId is required for leg enrichment.")
        if game_pk <= 0:
            raise ValueError("gamePk is required for leg enrichment.")
        enriched_at = enrichment.get("enrichedAt") or _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bet_history_leg_enrichments (
                    history_leg_id, game_pk, player_mlb_id, player_team_side,
                    player_team_name, lineup_confirmed, confirmed_starter,
                    batting_order, bat_side, pitch_hand, position,
                    stat_key, stat_value, enriched_result_status,
                    context_quality, pregame_context_json, grading_context_json,
                    notes_json, source, enriched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(history_leg_id) DO UPDATE SET
                    game_pk = excluded.game_pk,
                    player_mlb_id = excluded.player_mlb_id,
                    player_team_side = excluded.player_team_side,
                    player_team_name = excluded.player_team_name,
                    lineup_confirmed = excluded.lineup_confirmed,
                    confirmed_starter = excluded.confirmed_starter,
                    batting_order = excluded.batting_order,
                    bat_side = excluded.bat_side,
                    pitch_hand = excluded.pitch_hand,
                    position = excluded.position,
                    stat_key = excluded.stat_key,
                    stat_value = excluded.stat_value,
                    enriched_result_status = excluded.enriched_result_status,
                    context_quality = excluded.context_quality,
                    pregame_context_json = excluded.pregame_context_json,
                    grading_context_json = excluded.grading_context_json,
                    notes_json = excluded.notes_json,
                    source = excluded.source,
                    enriched_at = excluded.enriched_at
                """,
                (
                    history_leg_id,
                    game_pk,
                    enrichment.get("playerMlbId"),
                    enrichment.get("playerTeamSide"),
                    enrichment.get("playerTeamName"),
                    1 if enrichment.get("lineupConfirmed") else 0,
                    1 if enrichment.get("confirmedStarter") else 0,
                    enrichment.get("battingOrder"),
                    enrichment.get("batSide"),
                    enrichment.get("pitchHand"),
                    enrichment.get("position"),
                    enrichment.get("statKey"),
                    _float_or_none(enrichment.get("statValue")),
                    enrichment.get("enrichedResultStatus"),
                    enrichment.get("contextQuality"),
                    _json_dumps(enrichment.get("pregameContext") or {}),
                    _json_dumps(enrichment.get("gradingContext") or {}),
                    _json_dumps(enrichment.get("notes") or []),
                    enrichment.get("source") or "mlb_stats_api_snapshot",
                    enriched_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM bet_history_leg_enrichments WHERE history_leg_id = ?",
                (history_leg_id,),
            ).fetchone()
            conn.commit()
        return _bet_history_enrichment_row(row)

    def bet_history_enrichment_report(self) -> dict[str, Any]:
        self._load_remote_bet_history_once()
        with self._connect() as conn:
            snapshots = conn.execute(
                "SELECT COUNT(*) AS count FROM bet_history_game_snapshots"
            ).fetchone()
            enrichments = conn.execute(
                """
                SELECT
                    COUNT(*) AS count,
                    SUM(CASE WHEN stat_value IS NOT NULL THEN 1 ELSE 0 END) AS stat_values,
                    SUM(CASE WHEN enriched_result_status IS NOT NULL THEN 1 ELSE 0 END) AS graded,
                    SUM(CASE WHEN context_quality = 'full' THEN 1 ELSE 0 END) AS full_context,
                    SUM(CASE WHEN context_quality = 'partial' THEN 1 ELSE 0 END) AS partial_context
                FROM bet_history_leg_enrichments
                """
            ).fetchone()
        return {
            "snapshots": int((snapshots or {})["count"] or 0),
            "legEnrichments": int((enrichments or {})["count"] or 0),
            "statValues": int((enrichments or {})["stat_values"] or 0),
            "gradedBySnapshot": int((enrichments or {})["graded"] or 0),
            "fullContext": int((enrichments or {})["full_context"] or 0),
            "partialContext": int((enrichments or {})["partial_context"] or 0),
        }

    def bet_history_backtest(
        self,
        *,
        market_key: str | None = None,
        side: str | None = None,
        player_name: str | None = None,
        from_date: str | None = None,
        ticket_id: str | None = None,
        import_id: str | None = None,
        limit: int = 10000,
        view: str = "dashboard",
    ) -> dict[str, Any]:
        self._load_remote_bet_history_once()
        clean_market = _clean_market_filter(market_key)
        clean_player = _clean_text_filter(player_name)
        clean_from_date = _clean_text_filter(from_date)
        clean_ticket = _clean_text_filter(ticket_id)
        sql = """
            SELECT
                l.*,
                e.game_pk AS enrichment_game_pk,
                e.player_mlb_id AS enrichment_player_mlb_id,
                e.player_team_side AS enrichment_player_team_side,
                e.player_team_name AS enrichment_player_team_name,
                e.lineup_confirmed AS enrichment_lineup_confirmed,
                e.confirmed_starter AS enrichment_confirmed_starter,
                e.batting_order AS enrichment_batting_order,
                e.bat_side AS enrichment_bat_side,
                e.pitch_hand AS enrichment_pitch_hand,
                e.position AS enrichment_position,
                e.stat_key AS enrichment_stat_key,
                e.stat_value AS enrichment_stat_value,
                e.enriched_result_status AS enrichment_result_status,
                e.context_quality AS enrichment_context_quality,
                e.pregame_context_json AS enrichment_pregame_context_json,
                e.grading_context_json AS enrichment_grading_context_json,
                e.notes_json AS enrichment_notes_json,
                e.source AS enrichment_source,
                e.enriched_at AS enrichment_enriched_at
            FROM bet_history_legs l
            LEFT JOIN bet_history_leg_enrichments e
                ON e.history_leg_id = l.history_leg_id
            WHERE l.training_eligible = 1
              AND l.result_status IN ('won', 'lost', 'push', 'void')
        """
        params: list[Any] = []
        if import_id:
            sql += " AND l.import_id = ?"
            params.append(import_id)
        if clean_market:
            sql += " AND l.market_key = ?"
            params.append(clean_market)
        if side:
            sql += " AND l.side = ?"
            params.append(side)
        if clean_player:
            sql += " AND LOWER(COALESCE(l.player_name, '')) LIKE ?"
            params.append(f"%{clean_player.lower()}%")
        if clean_from_date:
            sql += " AND l.bet_date >= ?"
            params.append(clean_from_date)
        if clean_ticket:
            sql += " AND l.ticket_id = ?"
            params.append(clean_ticket)
        sql += " ORDER BY l.bet_date DESC, l.created_at DESC, l.leg_index ASC LIMIT ?"
        params.append(_clean_history_limit(limit))
        with self._connect() as conn:
            rows = [_bet_history_leg_row(row) for row in conn.execute(sql, params).fetchall()]
        return _bet_history_backtest_report(
            rows,
            filters={
                "marketKey": clean_market,
                "side": side,
                "playerName": clean_player,
                "fromDate": clean_from_date,
                "ticketId": clean_ticket,
                "importId": import_id,
                "limit": limit,
                "view": _clean_backtest_view(view),
            },
        )

    def bet_history_candidate_signal(
        self,
        *,
        player_name: str | None,
        market_key: str | None,
        side: str | None,
        line: float | int | str | None = None,
        limit: int = 50000,
    ) -> dict[str, Any]:
        self._load_remote_bet_history_once()
        clean_market = _clean_market_filter(market_key)
        clean_player = _clean_text_filter(player_name)
        clean_side = str(side or "").strip().lower() or None
        clean_line = _float_or_none(line)
        if clean_side not in {"over", "under"}:
            clean_side = None
        if not clean_market or not clean_side:
            return _empty_candidate_history_signal(
                player_name=clean_player,
                market_key=clean_market,
                side=clean_side,
                line=clean_line,
                status="missing_candidate_market_or_side",
                source=self.bet_history_source_label(),
            )

        market_side_rows = self._bet_history_signal_rows(
            market_key=clean_market,
            side=clean_side,
            limit=limit,
        )
        market_side_line_rows = [
            row for row in market_side_rows
            if clean_line is not None and _same_line(row.get("line"), clean_line)
        ]
        player_market_rows: list[dict[str, Any]] = []
        player_market_line_rows: list[dict[str, Any]] = []
        if clean_player:
            player_market_rows = [
                row for row in market_side_rows
                if _same_history_subject(row.get("playerName"), clean_player)
            ]
            player_market_line_rows = [
                row for row in player_market_rows
                if clean_line is not None and _same_line(row.get("line"), clean_line)
            ]

        buckets = {
            "playerMarketLine": _candidate_history_bucket(
                "player_market_line",
                player_market_line_rows,
                min_usable=10,
            ),
            "playerMarket": _candidate_history_bucket(
                "player_market",
                player_market_rows,
                min_usable=15,
            ),
            "marketSideLine": _candidate_history_bucket(
                "market_side_line",
                market_side_line_rows,
                min_usable=30,
            ),
            "marketSide": _candidate_history_bucket(
                "market_side",
                market_side_rows,
                min_usable=40,
            ),
        }
        ticket_failure = _candidate_history_ticket_failure(market_side_rows, player_market_rows)
        applied = _candidate_history_applied_signal(buckets, ticket_failure=ticket_failure)
        enrichment = _candidate_history_enrichment_summary(
            market_side_rows=market_side_rows,
            player_market_rows=player_market_rows,
        )
        return {
            "source": self.bet_history_source_label(),
            "status": applied.get("status"),
            "playerName": clean_player,
            "marketKey": clean_market,
            "side": clean_side,
            "line": clean_line,
            "buckets": buckets,
            "ticketFailure": ticket_failure,
            "enrichment": enrichment,
            "applied": applied,
            "notes": _candidate_history_signal_notes(buckets, applied, enrichment=enrichment),
        }

    def bet_history_source_label(self) -> str:
        if self._history_remote_enabled:
            return "supabase_bet_history_with_sqlite_cache"
        return "local_sqlite_bet_history_fallback"

    def _bet_history_signal_rows(
        self,
        *,
        market_key: str,
        side: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT
                l.*,
                e.game_pk AS enrichment_game_pk,
                e.player_mlb_id AS enrichment_player_mlb_id,
                e.player_team_side AS enrichment_player_team_side,
                e.player_team_name AS enrichment_player_team_name,
                e.lineup_confirmed AS enrichment_lineup_confirmed,
                e.confirmed_starter AS enrichment_confirmed_starter,
                e.batting_order AS enrichment_batting_order,
                e.bat_side AS enrichment_bat_side,
                e.pitch_hand AS enrichment_pitch_hand,
                e.position AS enrichment_position,
                e.stat_key AS enrichment_stat_key,
                e.stat_value AS enrichment_stat_value,
                e.enriched_result_status AS enrichment_result_status,
                e.context_quality AS enrichment_context_quality,
                e.pregame_context_json AS enrichment_pregame_context_json,
                e.grading_context_json AS enrichment_grading_context_json,
                e.notes_json AS enrichment_notes_json,
                e.source AS enrichment_source,
                e.enriched_at AS enrichment_enriched_at
            FROM bet_history_legs l
            LEFT JOIN bet_history_leg_enrichments e
                ON e.history_leg_id = l.history_leg_id
            WHERE l.training_eligible = 1
              AND l.result_status IN ('won', 'lost', 'push', 'void')
              AND l.market_key = ?
              AND l.side = ?
            ORDER BY l.bet_date DESC, l.created_at DESC, l.leg_index ASC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(
                sql,
                (market_key, side, _clean_history_limit(limit)),
            ).fetchall()
        return [_bet_history_leg_row(row) for row in rows]

    def _bet_history_raw_count(self) -> int:
        self._load_remote_bet_history_once()
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM bet_history_raw").fetchone()
        return int(row["count"] or 0)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS gpt_decision_requests (
                    decision_id TEXT PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    matchup TEXT,
                    slate_date TEXT,
                    prompt TEXT,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS gpt_decision_legs (
                    leg_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    slate_date TEXT,
                    matchup TEXT,
                    selection_id TEXT,
                    prop_id TEXT,
                    fixture_slug TEXT,
                    player_name TEXT,
                    team_name TEXT,
                    market_key TEXT,
                    market_name TEXT,
                    side TEXT,
                    line REAL,
                    odds REAL,
                    playable INTEGER NOT NULL DEFAULT 0,
                    status TEXT,
                    selection_json TEXT NOT NULL,
                    decision_profile_json TEXT NOT NULL DEFAULT '{}',
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    settlement_status TEXT NOT NULL DEFAULT 'unsettled',
                    actual_stat REAL,
                    settled_at TEXT,
                    settlement_confidence REAL,
                    settlement_source TEXT,
                    FOREIGN KEY(decision_id) REFERENCES gpt_decision_requests(decision_id)
                );

                CREATE TABLE IF NOT EXISTS market_mappings (
                    sport TEXT NOT NULL,
                    stake_display_name TEXT NOT NULL,
                    internal_market_key TEXT NOT NULL,
                    stat_key TEXT,
                    group_name TEXT,
                    last_seen_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    examples_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(sport, stake_display_name, internal_market_key)
                );

                CREATE TABLE IF NOT EXISTS bet_history_imports (
                    import_id TEXT PRIMARY KEY,
                    imported_at TEXT NOT NULL,
                    source_path TEXT,
                    source_format TEXT NOT NULL,
                    source_fingerprint TEXT,
                    fingerprint_version TEXT,
                    parser_version TEXT,
                    eligibility_version TEXT,
                    raw_row_count INTEGER NOT NULL DEFAULT 0,
                    parsed_leg_count INTEGER NOT NULL DEFAULT 0,
                    needs_review_count INTEGER NOT NULL DEFAULT 0,
                    report_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS bet_history_raw (
                    raw_id TEXT PRIMARY KEY,
                    import_id TEXT NOT NULL,
                    source_row_number INTEGER NOT NULL,
                    source_format TEXT NOT NULL,
                    raw_text TEXT,
                    raw_json TEXT NOT NULL,
                    parse_status TEXT NOT NULL DEFAULT 'parsed',
                    parse_notes_json TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(import_id) REFERENCES bet_history_imports(import_id)
                );

                CREATE TABLE IF NOT EXISTS bet_history_legs (
                    history_leg_id TEXT PRIMARY KEY,
                    import_id TEXT NOT NULL,
                    raw_id TEXT,
                    ticket_id TEXT,
                    leg_index INTEGER NOT NULL,
                    bet_date TEXT,
                    settled_date TEXT,
                    sport TEXT NOT NULL DEFAULT 'mlb',
                    league TEXT,
                    player_name TEXT,
                    team_name TEXT,
                    opponent_name TEXT,
                    fixture_slug TEXT,
                    matchup TEXT,
                    market_key TEXT,
                    market_name TEXT,
                    side TEXT,
                    line REAL,
                    odds REAL,
                    stake_amount REAL,
                    payout_amount REAL,
                    result_status TEXT,
                    actual_stat REAL,
                    parse_confidence REAL NOT NULL DEFAULT 0,
                    parse_confidence_label TEXT NOT NULL DEFAULT 'low',
                    needs_review INTEGER NOT NULL DEFAULT 1,
                    training_eligible INTEGER NOT NULL DEFAULT 0,
                    parser_version TEXT,
                    eligibility_version TEXT,
                    parse_notes_json TEXT NOT NULL DEFAULT '[]',
                    ignored_fields_json TEXT NOT NULL DEFAULT '[]',
                    normalized_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(import_id) REFERENCES bet_history_imports(import_id),
                    FOREIGN KEY(raw_id) REFERENCES bet_history_raw(raw_id)
                );

                CREATE INDEX IF NOT EXISTS bet_history_legs_market_idx
                    ON bet_history_legs (market_key, side);

                CREATE INDEX IF NOT EXISTS bet_history_legs_date_idx
                    ON bet_history_legs (bet_date);

                CREATE INDEX IF NOT EXISTS bet_history_legs_review_idx
                    ON bet_history_legs (needs_review, parse_confidence_label);

                CREATE INDEX IF NOT EXISTS bet_history_legs_ticket_idx
                    ON bet_history_legs (ticket_id);

                CREATE INDEX IF NOT EXISTS bet_history_legs_player_idx
                    ON bet_history_legs (player_name);

                CREATE TABLE IF NOT EXISTS bet_history_game_snapshots (
                    game_pk INTEGER PRIMARY KEY,
                    official_date TEXT,
                    game_date TEXT,
                    matchup_key TEXT,
                    away_team_name TEXT,
                    home_team_name TEXT,
                    final_status TEXT,
                    venue_json TEXT NOT NULL DEFAULT '{}',
                    probable_pitchers_json TEXT NOT NULL DEFAULT '{}',
                    pregame_context_json TEXT NOT NULL DEFAULT '{}',
                    grading_context_json TEXT NOT NULL DEFAULT '{}',
                    raw_context_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'mlb_stats_api',
                    fetched_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS bet_history_game_snapshots_date_idx
                    ON bet_history_game_snapshots (official_date);

                CREATE TABLE IF NOT EXISTS bet_history_leg_enrichments (
                    history_leg_id TEXT PRIMARY KEY,
                    game_pk INTEGER NOT NULL,
                    player_mlb_id INTEGER,
                    player_team_side TEXT,
                    player_team_name TEXT,
                    lineup_confirmed INTEGER NOT NULL DEFAULT 0,
                    confirmed_starter INTEGER NOT NULL DEFAULT 0,
                    batting_order INTEGER,
                    bat_side TEXT,
                    pitch_hand TEXT,
                    position TEXT,
                    stat_key TEXT,
                    stat_value REAL,
                    enriched_result_status TEXT,
                    context_quality TEXT NOT NULL DEFAULT 'unknown',
                    pregame_context_json TEXT NOT NULL DEFAULT '{}',
                    grading_context_json TEXT NOT NULL DEFAULT '{}',
                    notes_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT 'mlb_stats_api_snapshot',
                    enriched_at TEXT NOT NULL,
                    FOREIGN KEY(history_leg_id) REFERENCES bet_history_legs(history_leg_id) ON DELETE CASCADE,
                    FOREIGN KEY(game_pk) REFERENCES bet_history_game_snapshots(game_pk)
                );

                CREATE INDEX IF NOT EXISTS bet_history_leg_enrichments_game_idx
                    ON bet_history_leg_enrichments (game_pk);

                """
            )
            _ensure_column(conn, "gpt_decision_requests", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(conn, "gpt_decision_legs", "decision_profile_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(conn, "gpt_decision_legs", "risk_flags_json", "TEXT NOT NULL DEFAULT '[]'")
            _ensure_column(conn, "gpt_decision_legs", "settlement_status", "TEXT NOT NULL DEFAULT 'unsettled'")
            _ensure_column(conn, "gpt_decision_legs", "actual_stat", "REAL")
            _ensure_column(conn, "gpt_decision_legs", "settled_at", "TEXT")
            _ensure_column(conn, "gpt_decision_legs", "settlement_confidence", "REAL")
            _ensure_column(conn, "gpt_decision_legs", "settlement_source", "TEXT")
            _ensure_column(conn, "bet_history_legs", "training_eligible", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "bet_history_legs", "parser_version", "TEXT")
            _ensure_column(conn, "bet_history_legs", "eligibility_version", "TEXT")
            _ensure_column(conn, "bet_history_legs", "ignored_fields_json", "TEXT NOT NULL DEFAULT '[]'")
            _ensure_column(conn, "bet_history_imports", "source_fingerprint", "TEXT")
            _ensure_column(conn, "bet_history_imports", "fingerprint_version", "TEXT")
            _ensure_column(conn, "bet_history_imports", "parser_version", "TEXT")
            _ensure_column(conn, "bet_history_imports", "eligibility_version", "TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS bet_history_imports_fingerprint_idx
                    ON bet_history_imports (source_fingerprint)
                """
            )
            _normalize_legacy_live_history_flags(conn)

    def sync_bet_history_from_supabase(self, *, force: bool = False) -> dict[str, Any]:
        if not self._history_remote_enabled:
            return {"enabled": False, "reason": "supabase_history_disabled"}
        cache_key = str(self.db_path.resolve())
        ttl = _remote_history_pull_ttl()
        now = time.monotonic()
        if (
            not force
            and self._history_remote_loaded
            and ttl > 0
            and now - _REMOTE_HISTORY_PULL_CACHE.get(cache_key, 0.0) < ttl
        ):
            return {"enabled": True, "skipped": True, "reason": "already_loaded"}
        if (
            not force
            and ttl > 0
            and now - _REMOTE_HISTORY_PULL_CACHE.get(cache_key, 0.0) < ttl
        ):
            self._history_remote_loaded = True
            return {"enabled": True, "skipped": True, "reason": "pull_ttl_active"}
        try:
            result = sync_supabase_history_to_sqlite(self.db_path)
            self._history_remote_loaded = True
            self._history_remote_last_error = None
            _REMOTE_HISTORY_PULL_CACHE[cache_key] = now
            return result
        except Exception as exc:
            self._history_remote_loaded = True
            self._history_remote_last_error = str(exc)
            if _fail_on_supabase_history_error():
                raise
            return {
                "enabled": True,
                "error": str(exc),
                "fallback": "sqlite_local_cache",
            }

    def sync_bet_history_to_supabase(
        self,
        *,
        table_names: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        if not self._history_remote_enabled:
            return {"enabled": False, "reason": "supabase_history_disabled"}
        try:
            result = sync_sqlite_history_to_supabase(self.db_path, table_names=table_names)
            self._history_remote_last_error = None
            return result
        except Exception as exc:
            self._history_remote_last_error = str(exc)
            if _fail_on_supabase_history_error():
                raise
            return {
                "enabled": True,
                "error": str(exc),
                "fallback": "sqlite_local_cache",
            }

    def _load_remote_bet_history_once(self) -> dict[str, Any]:
        if not self._history_remote_enabled:
            return {"enabled": False, "reason": "supabase_history_disabled"}
        if self._history_remote_loaded:
            return {"enabled": True, "skipped": True, "reason": "already_loaded"}
        return self.sync_bet_history_from_supabase()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


SnapshotStore = GptActionStore


def default_db_path() -> Path:
    return Path(os.getenv("AZP_DB_PATH") or DEFAULT_DB_PATH)


def _decision_leg_values(
    decision_id: str,
    captured_at: str,
    slate_date: str | None,
    matchup: str | None,
    rank: int,
    selection: dict[str, Any],
) -> tuple[Any, ...]:
    player = selection.get("player") or {}
    team = selection.get("team") or {}
    market = selection.get("market") or {}
    availability = selection.get("availability") or {}
    return (
        str(uuid.uuid4()),
        decision_id,
        rank,
        captured_at,
        slate_date,
        matchup,
        selection.get("selectionId"),
        selection.get("propId"),
        selection.get("fixtureSlug"),
        player.get("name"),
        team.get("name"),
        market.get("key"),
        market.get("name"),
        selection.get("side"),
        _float_or_none(selection.get("line")),
        _float_or_none(selection.get("odds")),
        1 if selection.get("playable") else 0,
        availability.get("status") or selection.get("status"),
        _json_dumps(selection),
        _json_dumps(selection.get("decisionProfile") or {}),
        _json_dumps(selection.get("riskFlags") or (selection.get("decisionProfile") or {}).get("riskFlags") or []),
        "unsettled",
        None,
        None,
        None,
        None,
    )


def _leg_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "legId": row["leg_id"],
        "decisionId": row["decision_id"],
        "rank": row["rank"],
        "capturedAt": row["captured_at"],
        "date": row["slate_date"],
        "matchup": row["matchup"],
        "selectionId": row["selection_id"],
        "propId": row["prop_id"],
        "fixtureSlug": row["fixture_slug"],
        "player": row["player_name"],
        "team": row["team_name"],
        "marketKey": row["market_key"],
        "market": row["market_name"],
        "side": row["side"],
        "line": row["line"],
        "odds": row["odds"],
        "playable": bool(row["playable"]),
        "status": row["status"],
        "selection": _json_loads(row["selection_json"]),
        "decisionProfile": _json_loads(row["decision_profile_json"]),
        "riskFlags": _json_loads(row["risk_flags_json"]),
        "settlement": {
            "status": row["settlement_status"],
            "actualStat": row["actual_stat"],
            "settledAt": row["settled_at"],
            "confidence": row["settlement_confidence"],
            "source": row["settlement_source"],
        },
    }


def _bet_history_leg_values(
    *,
    import_id: str,
    raw_id: str | None,
    leg: dict[str, Any],
    created_at: str,
) -> tuple[Any, ...]:
    return (
        str(uuid.uuid4()),
        import_id,
        raw_id,
        leg.get("ticketId"),
        int(leg.get("legIndex") or 1),
        leg.get("betDate"),
        leg.get("settledDate"),
        leg.get("sport") or "mlb",
        leg.get("league"),
        leg.get("playerName"),
        leg.get("teamName"),
        leg.get("opponentName"),
        leg.get("fixtureSlug"),
        leg.get("matchup"),
        leg.get("marketKey"),
        leg.get("marketName"),
        leg.get("side"),
        _float_or_none(leg.get("line")),
        _float_or_none(leg.get("odds")),
        _float_or_none(leg.get("stakeAmount")),
        _float_or_none(leg.get("payoutAmount")),
        leg.get("resultStatus"),
        _float_or_none(leg.get("actualStat")),
        _float_or_none(leg.get("parseConfidence")) or 0.0,
        leg.get("parseConfidenceLabel") or "low",
        1 if leg.get("needsReview") else 0,
        1 if leg.get("trainingEligible") else 0,
        leg.get("parserVersion"),
        leg.get("eligibilityVersion"),
        _json_dumps(leg.get("parseNotes") or []),
        _json_dumps(leg.get("ignoredFields") or []),
        _json_dumps(_normalized_history_leg(leg)),
        _json_dumps(leg.get("rawJson") or {}),
        created_at,
    )


def _bet_history_leg_row(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "historyLegId": row["history_leg_id"],
        "importId": row["import_id"],
        "rawId": row["raw_id"],
        "ticketId": row["ticket_id"],
        "legIndex": row["leg_index"],
        "betDate": row["bet_date"],
        "settledDate": row["settled_date"],
        "sport": row["sport"],
        "league": row["league"],
        "playerName": row["player_name"],
        "teamName": row["team_name"],
        "opponentName": row["opponent_name"],
        "fixtureSlug": row["fixture_slug"],
        "matchup": row["matchup"],
        "marketKey": row["market_key"],
        "marketName": row["market_name"],
        "side": row["side"],
        "line": row["line"],
        "odds": row["odds"],
        "stakeAmount": row["stake_amount"],
        "payoutAmount": row["payout_amount"],
        "resultStatus": row["result_status"],
        "actualStat": row["actual_stat"],
        "parseConfidence": row["parse_confidence"],
        "parseConfidenceLabel": row["parse_confidence_label"],
        "needsReview": bool(row["needs_review"]),
        "trainingEligible": bool(row["training_eligible"]),
        "parserVersion": row["parser_version"],
        "eligibilityVersion": row["eligibility_version"],
        "parseNotes": _json_loads(row["parse_notes_json"]) or [],
        "ignoredFields": _json_loads(row["ignored_fields_json"]) or [],
        "normalized": _json_loads(row["normalized_json"]) or {},
        "raw": _json_loads(row["raw_json"]) or {},
        "createdAt": row["created_at"],
    }
    if _row_has_column(row, "enrichment_game_pk"):
        result["historicalEnrichment"] = _joined_bet_history_enrichment(row)
    return result


def _joined_bet_history_enrichment(row: sqlite3.Row) -> dict[str, Any] | None:
    game_pk = row["enrichment_game_pk"]
    if game_pk is None:
        return None
    return {
        "gamePk": game_pk,
        "playerMlbId": row["enrichment_player_mlb_id"] if _row_has_column(row, "enrichment_player_mlb_id") else None,
        "playerTeamSide": row["enrichment_player_team_side"] if _row_has_column(row, "enrichment_player_team_side") else None,
        "playerTeamName": row["enrichment_player_team_name"] if _row_has_column(row, "enrichment_player_team_name") else None,
        "lineupConfirmed": bool(row["enrichment_lineup_confirmed"]) if _row_has_column(row, "enrichment_lineup_confirmed") else None,
        "confirmedStarter": bool(row["enrichment_confirmed_starter"]) if _row_has_column(row, "enrichment_confirmed_starter") else None,
        "battingOrder": row["enrichment_batting_order"] if _row_has_column(row, "enrichment_batting_order") else None,
        "batSide": row["enrichment_bat_side"] if _row_has_column(row, "enrichment_bat_side") else None,
        "pitchHand": row["enrichment_pitch_hand"] if _row_has_column(row, "enrichment_pitch_hand") else None,
        "position": row["enrichment_position"] if _row_has_column(row, "enrichment_position") else None,
        "statKey": row["enrichment_stat_key"] if _row_has_column(row, "enrichment_stat_key") else None,
        "statValue": row["enrichment_stat_value"] if _row_has_column(row, "enrichment_stat_value") else None,
        "resultStatus": row["enrichment_result_status"] if _row_has_column(row, "enrichment_result_status") else None,
        "contextQuality": row["enrichment_context_quality"] if _row_has_column(row, "enrichment_context_quality") else None,
        "pregameContext": _json_loads(row["enrichment_pregame_context_json"]) if _row_has_column(row, "enrichment_pregame_context_json") else {},
        "gradingContext": _json_loads(row["enrichment_grading_context_json"]) if _row_has_column(row, "enrichment_grading_context_json") else {},
        "notes": _json_loads(row["enrichment_notes_json"]) if _row_has_column(row, "enrichment_notes_json") else [],
        "source": row["enrichment_source"] if _row_has_column(row, "enrichment_source") else None,
        "enrichedAt": row["enrichment_enriched_at"] if _row_has_column(row, "enrichment_enriched_at") else None,
    }


def _bet_history_enrichment_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "historyLegId": row["history_leg_id"],
        "gamePk": row["game_pk"],
        "playerMlbId": row["player_mlb_id"],
        "playerTeamSide": row["player_team_side"],
        "playerTeamName": row["player_team_name"],
        "lineupConfirmed": bool(row["lineup_confirmed"]),
        "confirmedStarter": bool(row["confirmed_starter"]),
        "battingOrder": row["batting_order"],
        "batSide": row["bat_side"],
        "pitchHand": row["pitch_hand"],
        "position": row["position"],
        "statKey": row["stat_key"],
        "statValue": row["stat_value"],
        "resultStatus": row["enriched_result_status"],
        "contextQuality": row["context_quality"],
        "pregameContext": _json_loads(row["pregame_context_json"]) or {},
        "gradingContext": _json_loads(row["grading_context_json"]) or {},
        "notes": _json_loads(row["notes_json"]) or [],
        "source": row["source"],
        "enrichedAt": row["enriched_at"],
    }


def _bet_history_game_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "gamePk": row["game_pk"],
        "officialDate": row["official_date"],
        "gameDate": row["game_date"],
        "matchupKey": row["matchup_key"],
        "awayTeamName": row["away_team_name"],
        "homeTeamName": row["home_team_name"],
        "finalStatus": row["final_status"],
        "venue": _json_loads(row["venue_json"]) or {},
        "probablePitchers": _json_loads(row["probable_pitchers_json"]) or {},
        "pregameContext": _json_loads(row["pregame_context_json"]) or {},
        "gradingContext": _json_loads(row["grading_context_json"]) or {},
        "rawContext": _json_loads(row["raw_context_json"]) or {},
        "source": row["source"],
        "fetchedAt": row["fetched_at"],
    }


def _bet_history_import_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "importId": row["import_id"],
        "importedAt": row["imported_at"],
        "sourcePath": row["source_path"],
        "sourceFormat": row["source_format"],
        "sourceFingerprint": row["source_fingerprint"],
        "fingerprintVersion": row["fingerprint_version"],
        "parserVersion": row["parser_version"],
        "eligibilityVersion": row["eligibility_version"],
        "rawRows": row["raw_row_count"],
        "parsedLegs": row["parsed_leg_count"],
        "needsReview": row["needs_review_count"],
        "report": _json_loads(row["report_json"]) or {},
    }


def _normalized_history_leg(leg: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in leg.items()
        if key not in {"rawJson", "liveAtImport", "live_at_import"}
    }


def _bet_history_review_sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sourceRowNumber": (row.get("normalized") or {}).get("sourceRowNumber"),
        "legIndex": row.get("legIndex"),
        "playerName": row.get("playerName"),
        "teamName": row.get("teamName"),
        "marketName": row.get("marketName"),
        "marketKey": row.get("marketKey"),
        "side": row.get("side"),
        "line": row.get("line"),
        "odds": row.get("odds"),
        "resultStatus": row.get("resultStatus"),
        "trainingEligible": bool(row.get("trainingEligible")),
        "ignoredFields": row.get("ignoredFields") or [],
        "parseConfidence": row.get("parseConfidence"),
        "parseNotes": row.get("parseNotes") or [],
    }


def _bet_history_review_row(row: dict[str, Any]) -> dict[str, Any]:
    reasons = _history_review_reasons(row)
    return {
        "historyLegId": row.get("historyLegId"),
        "importId": row.get("importId"),
        "sourceRowNumber": (row.get("normalized") or {}).get("sourceRowNumber"),
        "ticketId": row.get("ticketId"),
        "betDate": row.get("betDate"),
        "matchup": row.get("matchup"),
        "playerName": row.get("playerName"),
        "teamName": row.get("teamName"),
        "marketName": row.get("marketName"),
        "marketKey": row.get("marketKey"),
        "side": row.get("side"),
        "line": row.get("line"),
        "odds": row.get("odds"),
        "resultStatus": row.get("resultStatus"),
        "actualStat": row.get("actualStat"),
        "trainingEligible": bool(row.get("trainingEligible")),
        "parseConfidenceLabel": row.get("parseConfidenceLabel"),
        "parseConfidence": row.get("parseConfidence"),
        "status": _history_review_status(reasons),
        "reasons": reasons,
        "repairPolicy": _history_repair_policy(reasons),
        "parseNotes": row.get("parseNotes") or [],
    }


def _history_review_reasons(row: dict[str, Any]) -> list[str]:
    notes = set(row.get("parseNotes") or [])
    reasons: list[str] = []
    ordered = [
        "missing_player_or_team",
        "missing_market",
        "ambiguous_strikeouts_market",
        "unknown_market",
        "unsupported_market_for_training",
        "unsupported_composite_market",
        "missing_side",
        "missing_line",
        "missing_result",
        "result_actual_mismatch",
    ]
    for reason in ordered:
        if reason in notes:
            reasons.append(reason)
    for note in sorted(notes):
        if note.startswith("ambiguous_") and note not in reasons:
            reasons.append(note)
    return reasons or sorted(notes)


def _history_review_status(reasons: list[str]) -> str:
    if not reasons:
        return "converted"
    if "missing_result" in reasons or "result_actual_mismatch" in reasons:
        return "needs_settlement_review"
    if "ambiguous_strikeouts_market" in reasons or "missing_market" in reasons or "unknown_market" in reasons:
        return "needs_market_review"
    if "missing_player_or_team" in reasons:
        return "needs_identity_review"
    return "needs_review"


def _history_repair_policy(reasons: list[str]) -> str:
    if not reasons:
        return "No repair needed."
    if "ambiguous_strikeouts_market" in reasons:
        return "Do not guess batter vs pitcher strikeouts. Repair only if the source text or player role makes it explicit."
    if "unknown_market" in reasons or "missing_market" in reasons:
        return "Do not invent a market. Add an alias only when it maps to a supported MLB prop market."
    if "result_actual_mismatch" in reasons:
        return "Do not auto-repair. Confirm whether source settlement or actual-stat math is authoritative."
    if "missing_result" in reasons:
        return "Do not train until result or actual stat can settle the leg."
    if "missing_player_or_team" in reasons:
        return "Do not train until player/team identity is explicit."
    return "Review source text before training."


def _notes_counter(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter: dict[str, int] = {}
    for row in rows:
        for note in row.get("parseNotes") or []:
            if _history_reportable_note(str(note)):
                counter[str(note)] = counter.get(str(note), 0) + 1
    return counter


def _ignored_fields_counter(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter: dict[str, int] = {}
    for row in rows:
        for field in row.get("ignoredFields") or []:
            counter[str(field)] = counter.get(str(field), 0) + 1
    return counter


def _bet_history_backtest_report(rows: list[dict[str, Any]], *, filters: dict[str, Any]) -> dict[str, Any]:
    overall = _backtest_bucket(rows)
    ticket_report = _ticket_backtest_report(rows)
    enrichment = _history_enrichment_backtest_summary(rows)
    signals = _history_signal_report(rows, ticket_report=ticket_report)
    calibration = _history_calibration_report(rows)
    flow = _automated_backtest_flow(
        rows=rows,
        overall=overall,
        ticket_report=ticket_report,
        signals=signals,
        calibration=calibration,
        enrichment=enrichment,
    )
    final_outcome = _automated_backtest_final_outcome(
        overall=overall,
        ticket_report=ticket_report,
        signals=signals,
        calibration=calibration,
        enrichment=enrichment,
    )
    return {
        "filters": filters,
        "flow": flow,
        "finalOutcome": final_outcome,
        "overall": overall,
        "byMarket": _backtest_groups(rows, "marketKey"),
        "bySide": _backtest_groups(rows, "side"),
        "byPlayerMarket": _backtest_computed_groups(rows, _player_market_label, limit=30),
        "byLineBucket": _backtest_computed_groups(rows, _line_bucket_label, limit=30),
        "tickets": ticket_report,
        "enrichment": enrichment,
        "enrichedBuckets": _history_enriched_bucket_report(rows, ticket_report=ticket_report),
        "signals": signals,
        "calibration": calibration,
        "notes": [
            "Hit rate uses training-eligible won/lost rows (pushes and voids excluded from graded hit rate).",
            "Unit ROI / profitPerUnit uses only won/lost rows with decimal odds (flat 1u legacy view).",
            "When stakeAmount present: realizedRoi / totalProfit use actual risked amount (stake-weighted).",
            "Payout - stake is used for P/L when both are present (even without odds).",
            "Voids (common in props: cancelled, insufficient action, etc.): treated as 0 P/L (stake returned) when stake known. They are training-eligible as historical facts but excluded from hit rate and unit ROI.",
            "Rows missing odds/stake still count for hit rate when settlement is known.",
            "Ticket performance groups correlated SGM/multi legs by ticketId. Ticket ROI requires ticketOdds and excludes winning tickets with push/void legs because payout odds were likely adjusted.",
            "Calibration is descriptive history, not machine learning. Use hit-rate adjustments when sample size is sufficient; use value/EV adjustments only when odds quality is sufficient.",
            "Historical enrichment uses stored MLB snapshots only; historic analysis does not call live MLB APIs.",
            "Enriched buckets are deterministic stored-context slices. Postgame boxscore fields remain grading-only and do not become pregame prediction evidence.",
        ],
    }


def _backtest_groups(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return _backtest_computed_groups(rows, lambda row: str(row.get(key) or "unknown"))


def _backtest_computed_groups(
    rows: list[dict[str, Any]],
    label_func,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        label = str(label_func(row) or "unknown")
        grouped.setdefault(label, []).append(row)
    groups = [
        {"label": label, **_backtest_bucket(bucket), "sampleWarning": _sample_warning(bucket)}
        for label, bucket in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
    return groups[:limit] if limit else groups


def _ticket_backtest_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ticket_id = str(row.get("ticketId") or "").strip()
        if not ticket_id:
            continue
        grouped.setdefault(ticket_id, []).append(row)

    ticket_rows = [_ticket_summary(ticket_id, bucket) for ticket_id, bucket in grouped.items()]
    ticket_rows.sort(
        key=lambda item: (
            str(item.get("betDate") or ""),
            str(item.get("ticketId") or ""),
        ),
        reverse=True,
    )
    return {
        "overall": _ticket_bucket(ticket_rows),
        "tickets": len(ticket_rows),
        "sgmTickets": sum(1 for row in ticket_rows if row.get("legs", 0) >= 2),
        "ticketRows": ticket_rows[:50],
        "failureContributors": _ticket_failure_contributors(ticket_rows),
        "notes": [
            "A ticket loses if any graded leg loses.",
            "A ticket wins when at least one graded leg wins and no graded leg loses.",
            "Push/void-only tickets are excluded from ticket hit rate and ROI.",
        ],
    }


def _ticket_summary(ticket_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    result_counts = {
        "won": sum(1 for row in rows if row.get("resultStatus") == "won"),
        "lost": sum(1 for row in rows if row.get("resultStatus") == "lost"),
        "push": sum(1 for row in rows if row.get("resultStatus") == "push"),
        "void": sum(1 for row in rows if row.get("resultStatus") == "void"),
    }
    graded = result_counts["won"] + result_counts["lost"]
    if result_counts["lost"]:
        ticket_result = "lost"
    elif result_counts["won"]:
        ticket_result = "won"
    elif result_counts["void"]:
        ticket_result = "void"
    elif result_counts["push"]:
        ticket_result = "push"
    else:
        ticket_result = "unknown"

    ticket_odds = _ticket_odds(rows)
    has_push_or_void = bool(result_counts["push"] or result_counts["void"])
    roi_blocker = None
    unit_profit = None
    if ticket_result in {"won", "lost"}:
        if ticket_odds is None:
            roi_blocker = "missing_ticket_odds"
        elif ticket_result == "won" and has_push_or_void:
            roi_blocker = "adjusted_odds_unknown_after_push_or_void"
        elif ticket_result == "won":
            unit_profit = round(ticket_odds - 1.0, 4)
        else:
            unit_profit = -1.0
    else:
        roi_blocker = "not_graded_ticket"

    return {
        "ticketId": ticket_id,
        "betDate": _first_present(row.get("betDate") for row in rows),
        "matchup": _first_present(row.get("matchup") for row in rows),
        "fixtureSlug": _first_present(row.get("fixtureSlug") for row in rows),
        "legs": len(rows),
        "gradedLegs": graded,
        **result_counts,
        "resultStatus": ticket_result,
        "hitRateEligible": 1 if ticket_result in {"won", "lost"} else 0,
        "ticketOdds": ticket_odds,
        "roiEligible": unit_profit is not None,
        "roiBlocker": roi_blocker,
        "profitPerUnit": unit_profit,
        "hasPushOrVoid": has_push_or_void,
        "winningLegs": [_ticket_leg_label(row) for row in rows if row.get("resultStatus") == "won"],
        "losingLegs": [_ticket_leg_label(row) for row in rows if row.get("resultStatus") == "lost"],
        "voidOrPushLegs": [
            _ticket_leg_label(row)
            for row in rows
            if row.get("resultStatus") in {"push", "void"}
        ],
    }


def _ticket_bucket(ticket_rows: list[dict[str, Any]]) -> dict[str, Any]:
    result_counts = {
        "won": sum(1 for row in ticket_rows if row.get("resultStatus") == "won"),
        "lost": sum(1 for row in ticket_rows if row.get("resultStatus") == "lost"),
        "push": sum(1 for row in ticket_rows if row.get("resultStatus") == "push"),
        "void": sum(1 for row in ticket_rows if row.get("resultStatus") == "void"),
        "unknown": sum(1 for row in ticket_rows if row.get("resultStatus") == "unknown"),
    }
    graded = result_counts["won"] + result_counts["lost"]
    roi_rows = [row for row in ticket_rows if row.get("roiEligible")]
    unit_profit = sum(float(row.get("profitPerUnit") or 0.0) for row in roi_rows)
    ticket_odds = [_float_or_none(row.get("ticketOdds")) for row in ticket_rows if row.get("ticketOdds")]
    return {
        "tickets": len(ticket_rows),
        "sgmTickets": sum(1 for row in ticket_rows if row.get("legs", 0) >= 2),
        "gradedTickets": graded,
        "hitRateEligible": graded,
        **result_counts,
        "hitRate": _round_rate(result_counts["won"], graded),
        "roiEligible": len(roi_rows),
        "missingTicketOdds": sum(
            1 for row in ticket_rows
            if row.get("resultStatus") in {"won", "lost"} and row.get("ticketOdds") is None
        ),
        "roiExcludedAdjustedOdds": sum(
            1 for row in ticket_rows
            if row.get("roiBlocker") == "adjusted_odds_unknown_after_push_or_void"
        ),
        "profitPerUnit": round(unit_profit, 4) if roi_rows else None,
        "roi": round(unit_profit / len(roi_rows), 4) if roi_rows else None,
        "averageTicketOdds": _round_average(ticket_odds),
        "sampleWarning": _sample_warning(ticket_rows, graded_key="hitRateEligible"),
    }


def _ticket_failure_contributors(ticket_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    losing_legs: list[dict[str, Any]] = []
    for ticket in ticket_rows:
        if ticket.get("resultStatus") != "lost":
            continue
        for leg in ticket.get("losingLegs") or []:
            losing_legs.append({**leg, "ticketId": ticket.get("ticketId")})
    return {
        "byMarket": _contributor_groups(losing_legs, lambda leg: str(leg.get("marketKey") or "unknown")),
        "byPlayerMarket": _contributor_groups(
            losing_legs,
            lambda leg: f"{leg.get('subject') or 'unknown'} | {leg.get('marketKey') or 'unknown'}",
        ),
        "bySide": _contributor_groups(losing_legs, lambda leg: str(leg.get("side") or "unknown")),
        "byLineBucket": _contributor_groups(
            losing_legs,
            lambda leg: (
                f"{leg.get('marketKey') or 'unknown'} | {leg.get('side') or '?'} | "
                f"{leg.get('line') if leg.get('line') is not None else '?'}"
            ),
        ),
    }


def _contributor_groups(items: list[dict[str, Any]], label_func, *, limit: int = 20) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(str(label_func(item) or "unknown"), []).append(item)
    return [
        {
            "label": label,
            "losingLegs": len(bucket),
            "ticketIds": sorted({str(item.get("ticketId")) for item in bucket if item.get("ticketId")})[:10],
        }
        for label, bucket in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]
    ]


def _history_signal_report(rows: list[dict[str, Any]], *, ticket_report: dict[str, Any]) -> dict[str, Any]:
    by_market = _signal_groups(rows, lambda row: str(row.get("marketKey") or "unknown"))
    by_player_market = _signal_groups(rows, _player_market_label, limit=50)
    by_side = _signal_groups(rows, lambda row: str(row.get("side") or "unknown"))
    by_line_bucket = _signal_groups(rows, _line_bucket_label, limit=50)
    by_market_line = _signal_groups(rows, _market_line_label, limit=50)
    under_rows = [row for row in rows if str(row.get("side") or "").lower() == "under"]
    return {
        "byMarket": by_market,
        "byPlayerMarket": by_player_market,
        "bySide": by_side,
        "byLineBucket": by_line_bucket,
        "byMarketLine": by_market_line,
        "underOnly": {
            "overall": _backtest_bucket(under_rows),
            "byMarket": _signal_groups(under_rows, lambda row: str(row.get("marketKey") or "unknown"), limit=50),
            "byMarketLine": _signal_groups(under_rows, _market_line_label, limit=50),
        },
        "ticketFailureContributors": ticket_report.get("failureContributors") or {},
        "warnings": _signal_warnings(by_market, by_player_market, by_line_bucket),
    }


def _signal_groups(
    rows: list[dict[str, Any]],
    label_func,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    groups = _backtest_computed_groups(rows, label_func, limit=limit)
    for group in groups:
        group["signal"] = _signal_label(group)
    return groups


def _history_calibration_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "marketSideLine": _calibration_groups(rows, _line_bucket_label, limit=40),
        "marketSide": _calibration_groups(rows, _market_side_label, limit=30),
        "playerMarket": _calibration_groups(rows, _player_market_label, limit=40),
        "notes": [
            "Calibration compares historical hit rate against historical average break-even rate from decimal odds.",
            "recommendedAdjustment is capped to avoid overfitting: -0.15 to +0.08.",
            "Buckets below 10 graded rows are marked low_sample and should not drive automated penalties.",
        ],
    }


def _history_enrichment_backtest_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    enrichments = [
        row.get("historicalEnrichment")
        for row in rows
        if row.get("historicalEnrichment")
    ]
    snapshot_games = {
        enrichment.get("gamePk")
        for enrichment in enrichments
        if enrichment.get("gamePk") is not None
    }
    enriched_results = [
        enrichment.get("resultStatus")
        for enrichment in enrichments
        if enrichment.get("resultStatus") in {"won", "lost", "push", "void"}
    ]
    mismatches = 0
    for row in rows:
        enrichment = row.get("historicalEnrichment") or {}
        enriched_result = enrichment.get("resultStatus")
        if (
            enriched_result in {"won", "lost", "push", "void"}
            and row.get("resultStatus") in {"won", "lost", "push", "void"}
            and enriched_result != row.get("resultStatus")
        ):
            mismatches += 1
    legs = len(rows)
    enriched_count = len(enrichments)
    if not legs:
        status = "empty"
    elif enriched_count == 0:
        status = "not_enriched"
    elif enriched_count < legs:
        status = "partial"
    elif mismatches:
        status = "enriched_with_result_mismatches"
    else:
        status = "enriched"
    return {
        "source": "local_sqlite_frozen_mlb_snapshots",
        "status": status,
        "legs": legs,
        "enrichedLegs": enriched_count,
        "coverageRate": _round_rate(enriched_count, legs),
        "snapshotGames": len(snapshot_games),
        "gradedBySnapshot": len(enriched_results),
        "resultMismatches": mismatches,
        "notes": [
            "Historic analysis reads stored enrichment snapshots only and does not call live MLB APIs.",
            "Imported resultStatus remains canonical; enrichedResultStatus is a frozen verification/feature field.",
        ],
    }


def _history_enriched_bucket_report(
    rows: list[dict[str, Any]],
    *,
    ticket_report: dict[str, Any],
) -> dict[str, Any]:
    enriched_rows = [row for row in rows if row.get("historicalEnrichment")]
    ticket_leg_counts = Counter(
        str(row.get("ticketId") or "").strip()
        for row in rows
        if str(row.get("ticketId") or "").strip()
    )
    return {
        "source": "stored_historical_mlb_enrichment",
        "coverage": _round_rate(len(enriched_rows), len(rows)),
        "enrichedLegs": len(enriched_rows),
        "byLineupSpot": _signal_groups(enriched_rows, _lineup_spot_label, limit=30),
        "byStarterRole": _signal_groups(enriched_rows, _starter_role_label, limit=20),
        "byPitchHand": _signal_groups(enriched_rows, _pitch_hand_label, limit=20),
        "byVenue": _signal_groups(enriched_rows, _venue_label, limit=30),
        "byLongshotOdds": _signal_groups(rows, _longshot_odds_label, limit=20),
        "byLegCount": _signal_groups(rows, lambda row: _leg_count_label(row, ticket_leg_counts), limit=20),
        "underOnly": {
            "byLineupSpot": _signal_groups(
                [row for row in enriched_rows if str(row.get("side") or "").lower() == "under"],
                _lineup_spot_label,
                limit=30,
            ),
            "byVenue": _signal_groups(
                [row for row in enriched_rows if str(row.get("side") or "").lower() == "under"],
                _venue_label,
                limit=30,
            ),
            "byLongshotOdds": _signal_groups(
                [row for row in rows if str(row.get("side") or "").lower() == "under"],
                _longshot_odds_label,
                limit=20,
            ),
        },
        "notes": [
            "Lineup, starter, pitch-hand, and venue buckets require historic enrich coverage.",
            "Longshot and leg-count buckets use imported ticket metadata and work before enrichment.",
            "Use ticket-level buckets as the primary strategy read for high-odds SGM history.",
        ],
    }


def _automated_backtest_flow(
    *,
    rows: list[dict[str, Any]],
    overall: dict[str, Any],
    ticket_report: dict[str, Any],
    signals: dict[str, Any],
    calibration: dict[str, Any],
    enrichment: dict[str, Any],
) -> list[dict[str, Any]]:
    ticket_overall = ticket_report.get("overall") or {}
    signal_markets = signals.get("byMarket") or []
    calibration_rows = calibration.get("marketSideLine") or []
    usable_calibration = sum(1 for row in calibration_rows if row.get("status") == "usable")
    return [
        {
            "step": "load_history",
            "status": "done" if rows else "empty",
            "summary": (
                f"{overall.get('legs') or 0} training-eligible legs loaded from SQLite"
                if rows else "No training-eligible rows found"
            ),
        },
        {
            "step": "historical_enrichment",
            "status": enrichment.get("status") or "unknown",
            "summary": (
                f"{enrichment.get('enrichedLegs') or 0}/{enrichment.get('legs') or 0} legs linked to "
                f"{enrichment.get('snapshotGames') or 0} frozen MLB game snapshots"
            ),
        },
        {
            "step": "leg_performance",
            "status": "done" if overall.get("gradedLegs") else "empty",
            "summary": (
                f"{overall.get('gradedLegs') or 0} graded legs, hit rate {_rate_label(overall.get('hitRate'))}"
            ),
        },
        {
            "step": "ticket_performance",
            "status": "done" if ticket_overall.get("tickets") else "empty",
            "summary": (
                f"{ticket_overall.get('tickets') or 0} tickets, "
                f"{ticket_overall.get('gradedTickets') or 0} graded, "
                f"hit rate {_rate_label(ticket_overall.get('hitRate'))}, "
                f"ROI {_rate_label(ticket_overall.get('roi'))}"
            ),
        },
        {
            "step": "signals",
            "status": "done" if signal_markets else "empty",
            "summary": f"{len(signal_markets)} market signal buckets generated",
        },
        {
            "step": "calibration",
            "status": "done" if calibration_rows else "empty",
            "summary": (
                f"{len(calibration_rows)} calibration buckets, {usable_calibration} usable with odds/sample"
            ),
        },
        {
            "step": "final_outcome",
            "status": "done",
            "summary": "Generated one combined outcome from legs, tickets, signals, and calibration",
        },
    ]


def _automated_backtest_final_outcome(
    *,
    overall: dict[str, Any],
    ticket_report: dict[str, Any],
    signals: dict[str, Any],
    calibration: dict[str, Any],
    enrichment: dict[str, Any],
) -> dict[str, Any]:
    ticket_overall = ticket_report.get("overall") or {}
    by_market = signals.get("byMarket") or []
    calibration_rows = calibration.get("marketSideLine") or []
    strongest = _rank_signal_markets(by_market, strongest=True)
    weakest = _rank_signal_markets(by_market, strongest=False)
    history_quality = _history_quality_outcome(overall, ticket_overall)
    calibration_status = _calibration_quality_outcome(overall, calibration_rows)
    model_readiness = _model_readiness_outcome(
        overall=overall,
        ticket_overall=ticket_overall,
        calibration_status=calibration_status,
        enrichment=enrichment,
    )
    warnings = _final_outcome_warnings(
        overall=overall,
        ticket_overall=ticket_overall,
        calibration_status=calibration_status,
    )
    next_action = _final_next_action(
        history_quality=history_quality,
        calibration_status=calibration_status,
        model_readiness=model_readiness,
    )
    return {
        "verdict": _final_verdict(history_quality, ticket_overall, model_readiness),
        "historyQuality": history_quality,
        "legSample": {
            "gradedLegs": overall.get("gradedLegs") or 0,
            "hitRate": overall.get("hitRate"),
            "roi": overall.get("roi"),
            "oddsLegs": overall.get("oddsLegs") or 0,
            "missingOdds": overall.get("roiExcludedMissingOdds") or 0,
        },
        "ticketSample": {
            "tickets": ticket_overall.get("tickets") or 0,
            "gradedTickets": ticket_overall.get("gradedTickets") or 0,
            "hitRate": ticket_overall.get("hitRate"),
            "roi": ticket_overall.get("roi"),
            "roiEligible": ticket_overall.get("roiEligible") or 0,
            "status": _ticket_sample_status(ticket_overall),
        },
        "strongestMarkets": strongest,
        "weakestMarkets": weakest,
        "calibrationStatus": calibration_status,
        "modelReadiness": model_readiness,
        "warnings": warnings,
        "nextAction": next_action,
    }


def _history_quality_outcome(overall: dict[str, Any], ticket_overall: dict[str, Any]) -> dict[str, Any]:
    graded = int(overall.get("gradedLegs") or 0)
    graded_tickets = int(ticket_overall.get("gradedTickets") or 0)
    odds_legs = int(overall.get("oddsLegs") or 0)
    odds_ratio = odds_legs / graded if graded else 0.0
    if graded <= 0:
        return {"status": "empty", "label": "No usable history", "reason": "No graded imported legs."}
    if graded < 30:
        return {
            "status": "building_sample",
            "label": "Building sample",
            "reason": f"{graded} graded legs is useful for review but thin for durable conclusions.",
        }
    if odds_legs <= 0:
        return {
            "status": "usable_missing_leg_odds",
        "label": "Usable odds-light historic sample",
            "reason": "Leg and ticket outcomes are usable; per-leg value calibration is limited because settled single-leg odds are unavailable.",
        }
    if odds_ratio < 0.25:
        return {
            "status": "usable_odds_light",
        "label": "Usable odds-light historic sample",
            "reason": "Leg results, ticket results, and ticket odds are usable; single-leg odds are sparse, so calibration should lean on hit rate and ticket ROI.",
        }
    if graded_tickets < 10:
        return {
            "status": "usable_leg_sample",
            "label": "Usable leg sample",
            "reason": "Leg data has odds, but ticket-level SGM sample is still thin.",
        }
    return {
        "status": "usable",
        "label": "Usable historic analysis sample",
        "reason": "Leg, ticket, and odds data are present enough for a stronger historic analysis loop.",
    }


def _ticket_sample_status(ticket_overall: dict[str, Any]) -> str:
    graded = int(ticket_overall.get("gradedTickets") or 0)
    if graded <= 0:
        return "no_ticket_sample"
    if graded < 10:
        return "low_ticket_sample"
    if graded < 30:
        return "usable_ticket_sample"
    return "strong_ticket_sample"


def _calibration_quality_outcome(overall: dict[str, Any], calibration_rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = sum(1 for row in calibration_rows if row.get("status") == "usable")
    low_sample = sum(1 for row in calibration_rows if row.get("status") == "low_sample")
    missing_odds = sum(1 for row in calibration_rows if row.get("status") == "missing_odds")
    if not calibration_rows:
        return {
            "status": "empty",
            "label": "No calibration data",
            "reason": "No historical buckets were generated.",
            "usableBuckets": 0,
        }
    if int(overall.get("oddsLegs") or 0) <= 0:
        return {
            "status": "limited_missing_per_leg_odds",
            "label": "Limited: odds-light",
            "reason": "Hit rates and ticket ROI are available; per-leg break-even/value comparison is unavailable without single-leg odds.",
            "usableBuckets": usable,
            "lowSampleBuckets": low_sample,
            "missingOddsBuckets": missing_odds,
        }
    if usable <= 0:
        return {
            "status": "limited_low_sample",
            "label": "Limited: low sample",
            "reason": "Odds exist, but buckets are not large enough for reliable adjustment.",
            "usableBuckets": usable,
            "lowSampleBuckets": low_sample,
            "missingOddsBuckets": missing_odds,
        }
    return {
        "status": "usable",
        "label": "Usable calibration buckets",
        "reason": f"{usable} buckets have enough sample and odds for capped historical adjustment.",
        "usableBuckets": usable,
        "lowSampleBuckets": low_sample,
        "missingOddsBuckets": missing_odds,
    }


def _model_readiness_outcome(
    *,
    overall: dict[str, Any],
    ticket_overall: dict[str, Any],
    calibration_status: dict[str, Any],
    enrichment: dict[str, Any],
) -> dict[str, Any]:
    graded = int(overall.get("gradedLegs") or 0)
    graded_tickets = int(ticket_overall.get("gradedTickets") or 0)
    ticket_roi = _float_or_none(ticket_overall.get("roi"))
    enrichment_coverage = _float_or_none(enrichment.get("coverageRate")) or 0.0
    gates = {
        "gradedLegs": graded,
        "gradedTickets": graded_tickets,
        "enrichmentCoverage": enrichment_coverage,
        "calibrationStatus": calibration_status.get("status") or "unknown",
        "ticketRoi": ticket_roi,
    }
    if graded <= 0:
        return {
            "status": "not_ready",
            "label": "Not ready",
            "reason": "No graded imported history.",
            "gates": gates,
        }
    if graded < 250 or graded_tickets < 40:
        return {
            "status": "backtest_ready_ml_dataset_forming",
            "label": "Historic-analysis-ready, ML dataset forming",
            "reason": (
                "Historic analysis is usable, but ML still needs a larger leg and ticket sample. "
                "Per-leg odds are optional and only improve value calibration."
            ),
            "gates": gates,
        }
    if enrichment_coverage < 0.50:
        return {
            "status": "backtest_ready_ml_dataset_forming",
            "label": "Historic-analysis-ready, ML dataset forming",
            "reason": (
                "Leg and ticket samples are useful, but historical MLB enrichment coverage "
                f"is {_rate_label(enrichment_coverage)}; build the frozen context layer before ML."
            ),
            "gates": gates,
        }
    if calibration_status.get("status") != "usable":
        return {
            "status": "ml_baseline_possible_not_validated",
            "label": "ML baseline possible, not validated",
            "reason": (
                "Samples and enrichment are sufficient for a first baseline, but calibration is still limited. "
                "Use it for offline experiments only."
            ),
            "gates": gates,
        }
    if ticket_roi is not None and ticket_roi < 0:
        return {
            "status": "ml_baseline_possible_not_validated",
            "label": "ML baseline possible, not validated",
            "reason": (
                "The dataset can support a baseline model, but current ticket ROI is negative. "
                "Any model must prove improvement on held-out history before influencing builds."
            ),
            "gates": gates,
        }
    if graded < 1000 or graded_tickets < 100 or enrichment_coverage < 0.75:
        return {
            "status": "ml_feature_ready",
            "label": "ML feature-ready",
            "reason": (
                "The enriched dataset is strong enough to create ML features and run offline validation, "
                "but it still needs more volume before trusted deployment."
            ),
            "gates": gates,
        }
    return {
        "status": "ml_ready_to_train",
        "label": "ML-ready to train",
        "reason": (
            "Sample size, ticket history, calibration, and historical enrichment are sufficient "
            "to train and holdout-test a first model. This is not a validated betting edge until the model backtest passes."
        ),
        "gates": gates,
    }


def _rank_signal_markets(by_market: list[dict[str, Any]], *, strongest: bool) -> list[dict[str, Any]]:
    rows = [
        row for row in by_market
        if row.get("hitRate") is not None and int(row.get("gradedLegs") or 0) > 0
    ]
    rows.sort(
        key=lambda row: (
            float(row.get("hitRate") or 0.0),
            int(row.get("gradedLegs") or 0),
            str(row.get("label") or ""),
        ),
        reverse=strongest,
    )
    return [
        {
            "market": row.get("label"),
            "gradedLegs": row.get("gradedLegs") or 0,
            "hitRate": row.get("hitRate"),
            "signal": row.get("signal"),
            "sampleWarning": row.get("sampleWarning"),
        }
        for row in rows[:5]
    ]


def _final_outcome_warnings(
    *,
    overall: dict[str, Any],
    ticket_overall: dict[str, Any],
    calibration_status: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    graded = int(overall.get("gradedLegs") or 0)
    odds_legs = int(overall.get("oddsLegs") or 0)
    missing_odds = int(overall.get("roiExcludedMissingOdds") or 0)
    odds_ratio = odds_legs / graded if graded else 0.0
    if missing_odds and odds_ratio < 0.5:
        warnings.append("Per-leg odds are sparse; ticket ROI remains usable, but leg EV/ROI should use odds-light hit-rate calibration.")
    if int(ticket_overall.get("gradedTickets") or 0) < 30:
        warnings.append("Ticket-level SGM sample is still developing; treat ticket ROI as directional.")
    if calibration_status.get("status") != "usable":
        warnings.append(calibration_status.get("reason") or "Calibration is limited.")
    return warnings


def _final_next_action(
    *,
    history_quality: dict[str, Any],
    calibration_status: dict[str, Any],
    model_readiness: dict[str, Any],
) -> str:
    if history_quality.get("status") == "empty":
        return "Import settled bet historic, then rerun z."
    if model_readiness.get("status") == "backtest_ready_ml_dataset_forming":
        return "Keep importing settled SGM history and run historic enrich --missing-only so ML has frozen pregame context."
    if model_readiness.get("status") == "ml_baseline_possible_not_validated":
        return "Create an offline baseline only; require held-out model backtesting before using model output in builds."
    if calibration_status.get("status") == "limited_missing_per_leg_odds":
        return "Keep importing settled SGM history. Single-leg odds are optional; ticket odds and outcomes can support odds-light ML once the sample is larger."
    if model_readiness.get("status") == "ml_feature_ready":
        return "Build the ML feature export and run offline validation against this historic-analysis baseline."
    if model_readiness.get("status") == "ml_ready_to_train":
        return "Train a first model, then backtest it on held-out history before wiring it into builds."
    return "Review focused views with z tickets, z signals, or z calibration."


def _final_verdict(
    history_quality: dict[str, Any],
    ticket_overall: dict[str, Any],
    model_readiness: dict[str, Any],
) -> str:
    ticket_roi = ticket_overall.get("roi")
    ticket_hit = ticket_overall.get("hitRate")
    if history_quality.get("status") == "empty":
        return "No historic analysis verdict yet; import historic data first."
    performance = "negative ticket performance"
    if ticket_roi is not None and float(ticket_roi) > 0:
        performance = "positive ticket ROI"
    elif ticket_hit is not None and float(ticket_hit) >= 0.5:
        performance = "acceptable ticket hit rate but ROI still needs review"
    return f"{model_readiness.get('label')}: current sample shows {performance}."


def _rate_label(value: Any) -> str:
    numeric = _float_or_none(value)
    if numeric is None:
        return "n/a"
    return f"{numeric * 100:.1f}%"


def _empty_candidate_history_signal(
    *,
    player_name: str | None,
    market_key: str | None,
    side: str | None,
    line: float | None,
    status: str,
    source: str = "local_sqlite_bet_history_fallback",
) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "playerName": player_name,
        "marketKey": market_key,
        "side": side,
        "line": line,
        "buckets": {},
        "ticketFailure": {},
        "applied": {
            "status": status,
            "bucket": None,
            "probabilityAdjustment": 0.0,
            "scoreAdjustment": 0.0,
            "reason": "Historical signal could not be computed for this candidate.",
        },
        "notes": [status],
    }


def _candidate_history_bucket(
    label: str,
    rows: list[dict[str, Any]],
    *,
    min_usable: int,
) -> dict[str, Any]:
    summary = _backtest_bucket(rows)
    graded = int(summary.get("gradedLegs") or 0)
    hit_rate = _float_or_none(summary.get("hitRate"))
    break_even = _float_or_none(summary.get("averageBreakEvenRate"))
    historical_edge = (
        round(hit_rate - break_even, 4)
        if hit_rate is not None and break_even is not None
        else None
    )
    hit_rate_delta = round(hit_rate - 0.50, 4) if hit_rate is not None else None
    if graded <= 0:
        status = "no_sample"
    elif graded < min_usable:
        status = "low_sample"
    elif break_even is None:
        status = "usable_hit_rate_only"
    else:
        status = "usable_value_calibration"
    return {
        "bucket": label,
        "minUsableSample": min_usable,
        "status": status,
        "gradedLegs": graded,
        "won": summary.get("won") or 0,
        "lost": summary.get("lost") or 0,
        "push": summary.get("push") or 0,
        "void": summary.get("void") or 0,
        "hitRate": summary.get("hitRate"),
        "averageBreakEvenRate": summary.get("averageBreakEvenRate"),
        "historicalEdge": historical_edge,
        "hitRateDeltaFrom50": hit_rate_delta,
        "oddsLegs": summary.get("oddsLegs") or 0,
        "missingOdds": summary.get("roiExcludedMissingOdds") or 0,
        "sampleWarning": _sample_warning(rows),
    }


def _candidate_history_ticket_failure(
    market_side_rows: list[dict[str, Any]],
    player_market_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    market_signal = _ticket_failure_from_rows(market_side_rows)
    player_signal = _ticket_failure_from_rows(player_market_rows)
    applied = player_signal if int(player_signal.get("gradedTicketLegs") or 0) >= 10 else market_signal
    return {
        "marketSide": market_signal,
        "playerMarket": player_signal,
        "applied": applied,
    }


def _ticket_failure_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ticket_rows = [row for row in rows if row.get("ticketId")]
    graded = [row for row in ticket_rows if row.get("resultStatus") in {"won", "lost"}]
    lost = [row for row in graded if row.get("resultStatus") == "lost"]
    loss_rate = _round_rate(len(lost), len(graded))
    if not graded:
        status = "no_ticket_sample"
    elif len(graded) < 10:
        status = "low_ticket_sample"
    elif loss_rate is not None and loss_rate >= 0.55:
        status = "elevated_ticket_failure"
    else:
        status = "usable_ticket_signal"
    return {
        "status": status,
        "ticketLegs": len(ticket_rows),
        "gradedTicketLegs": len(graded),
        "losingTicketLegs": len(lost),
        "ticketLegLossRate": loss_rate,
    }


def _candidate_history_applied_signal(
    buckets: dict[str, dict[str, Any]],
    *,
    ticket_failure: dict[str, Any],
) -> dict[str, Any]:
    priority = ("playerMarketLine", "playerMarket", "marketSideLine", "marketSide")
    usable = [
        buckets[name] for name in priority
        if (buckets.get(name) or {}).get("status") in {"usable_value_calibration", "usable_hit_rate_only"}
    ]
    if usable:
        selected = usable[0]
        probability_adjustment = _candidate_history_probability_adjustment(selected)
        score_adjustment = round(max(-6.0, min(3.0, probability_adjustment * 35.0)), 2)
        ticket_applied = (ticket_failure.get("applied") or {})
        if ticket_applied.get("status") == "elevated_ticket_failure":
            score_adjustment = round(max(-6.0, score_adjustment - 1.5), 2)
        return {
            "status": _candidate_history_applied_status(probability_adjustment, ticket_applied),
            "bucket": selected.get("bucket"),
            "gradedLegs": selected.get("gradedLegs"),
            "hitRate": selected.get("hitRate"),
            "averageBreakEvenRate": selected.get("averageBreakEvenRate"),
            "historicalEdge": selected.get("historicalEdge"),
            "probabilityAdjustment": probability_adjustment,
            "scoreAdjustment": score_adjustment,
            "ticketFailureStatus": ticket_applied.get("status"),
            "reason": _candidate_history_applied_reason(selected, probability_adjustment, ticket_applied),
        }

    largest = max((buckets.get(name) or {} for name in priority), key=lambda row: int(row.get("gradedLegs") or 0))
    return {
        "status": "sample_building",
        "bucket": largest.get("bucket"),
        "gradedLegs": largest.get("gradedLegs") or 0,
        "hitRate": largest.get("hitRate"),
        "averageBreakEvenRate": largest.get("averageBreakEvenRate"),
        "historicalEdge": largest.get("historicalEdge"),
        "probabilityAdjustment": 0.0,
        "scoreAdjustment": 0.0,
        "ticketFailureStatus": (ticket_failure.get("applied") or {}).get("status"),
        "reason": "Historical sample is visible but below the gate for score/probability adjustment.",
    }


def _candidate_history_probability_adjustment(bucket: dict[str, Any]) -> float:
    historical_edge = _float_or_none(bucket.get("historicalEdge"))
    hit_delta = _float_or_none(bucket.get("hitRateDeltaFrom50"))
    if historical_edge is not None:
        return round(max(-0.15, min(0.08, historical_edge)), 4)
    if hit_delta is not None:
        # No odds: use hit rate only as a weaker directional adjustment.
        return round(max(-0.08, min(0.05, hit_delta * 0.5)), 4)
    return 0.0


def _candidate_history_applied_status(
    probability_adjustment: float,
    ticket_applied: dict[str, Any],
) -> str:
    if ticket_applied.get("status") == "elevated_ticket_failure" and probability_adjustment <= 0:
        return "negative_with_ticket_failure_risk"
    if probability_adjustment >= 0.025:
        return "positive_history_signal"
    if probability_adjustment <= -0.025:
        return "negative_history_signal"
    return "neutral_history_signal"


def _candidate_history_applied_reason(
    bucket: dict[str, Any],
    probability_adjustment: float,
    ticket_applied: dict[str, Any],
) -> str:
    basis = (
        f"{bucket.get('bucket')} sample: {bucket.get('gradedLegs') or 0} graded, "
        f"hit rate {bucket.get('hitRate') if bucket.get('hitRate') is not None else 'n/a'}"
    )
    if bucket.get("averageBreakEvenRate") is not None:
        basis += f", break-even {bucket.get('averageBreakEvenRate')}, edge {bucket.get('historicalEdge')}"
    else:
        basis += ", no per-leg odds so hit-rate-only adjustment is weaker"
    if ticket_applied.get("status") == "elevated_ticket_failure":
        basis += f"; ticket failure loss rate {ticket_applied.get('ticketLegLossRate')}"
    if probability_adjustment == 0:
        basis += "; no probability adjustment applied."
    return basis


def _candidate_history_signal_notes(
    buckets: dict[str, dict[str, Any]],
    applied: dict[str, Any],
    *,
    enrichment: dict[str, Any] | None = None,
) -> list[str]:
    notes: list[str] = []
    if applied.get("status") == "sample_building":
        notes.append("historical_sample_below_adjustment_gate")
    if not any((bucket.get("oddsLegs") or 0) for bucket in buckets.values()):
        notes.append("historical_per_leg_odds_missing")
    if applied.get("scoreAdjustment"):
        notes.append("historical_signal_score_adjusted")
    enrichment = enrichment or {}
    if enrichment.get("enrichedLegs"):
        notes.append("historical_enrichment_available")
    return notes


def _candidate_history_enrichment_summary(
    *,
    market_side_rows: list[dict[str, Any]],
    player_market_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    subject_rows = player_market_rows or market_side_rows
    enrichments = [
        row.get("historicalEnrichment")
        for row in subject_rows
        if row.get("historicalEnrichment")
    ]
    legs = len(subject_rows)
    return {
        "source": "local_sqlite_frozen_mlb_snapshots",
        "status": "available" if enrichments else "not_enriched",
        "legs": legs,
        "enrichedLegs": len(enrichments),
        "coverageRate": _round_rate(len(enrichments), legs),
        "snapshotGames": len({item.get("gamePk") for item in enrichments if item.get("gamePk") is not None}),
        "gradedBySnapshot": sum(1 for item in enrichments if item.get("resultStatus") in {"won", "lost", "push", "void"}),
    }


def _same_line(left: Any, right: Any) -> bool:
    left_value = _float_or_none(left)
    right_value = _float_or_none(right)
    return left_value is not None and right_value is not None and abs(left_value - right_value) <= 0.0001


def _same_history_subject(left: Any, right: Any) -> bool:
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def _calibration_groups(
    rows: list[dict[str, Any]],
    label_func,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(label_func(row) or "unknown"), []).append(row)
    reports: list[dict[str, Any]] = []
    for label, bucket in grouped.items():
        summary = _backtest_bucket(bucket)
        actual = _float_or_none(summary.get("hitRate"))
        break_even = _float_or_none(summary.get("averageBreakEvenRate"))
        graded = int(summary.get("gradedLegs") or 0)
        edge = round(actual - break_even, 4) if actual is not None and break_even is not None else None
        status, adjustment = _calibration_status_and_adjustment(
            graded=graded,
            historical_edge=edge,
            average_break_even=break_even,
        )
        reports.append(
            {
                "label": label,
                "gradedLegs": graded,
                "hitRate": summary.get("hitRate"),
                "averageBreakEvenRate": summary.get("averageBreakEvenRate"),
                "historicalEdge": edge,
                "recommendedAdjustment": adjustment,
                "status": status,
                "sampleWarning": _sample_warning(bucket),
            }
        )
    reports.sort(
        key=lambda item: (
            item.get("status") != "usable",
            -int(item.get("gradedLegs") or 0),
            str(item.get("label") or ""),
        )
    )
    return reports[:limit]


def _backtest_bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result_counts = {
        "won": sum(1 for row in rows if row.get("resultStatus") == "won"),
        "lost": sum(1 for row in rows if row.get("resultStatus") == "lost"),
        "push": sum(1 for row in rows if row.get("resultStatus") == "push"),
        "void": sum(1 for row in rows if row.get("resultStatus") == "void"),
    }
    graded = result_counts["won"] + result_counts["lost"]

    # Legacy unit-based (flat 1 unit per leg). Preserved for compatibility.
    odds_rows_all = [
        row for row in rows
        if row.get("odds") is not None and row.get("resultStatus") in {"won", "lost", "push"}
    ]
    roi_rows = [
        row for row in rows
        if row.get("odds") is not None and row.get("resultStatus") in {"won", "lost"}
    ]
    unit_profit = 0.0
    for row in roi_rows:
        result = row.get("resultStatus")
        odds = _float_or_none(row.get("odds")) or 0.0
        if result == "won":
            unit_profit += odds - 1.0
        elif result == "lost":
            unit_profit -= 1.0

    # Stake-aware + realized P/L (addresses flat-1u assumption and ignored stakeAmount).
    # - When stakeAmount + odds present: use risked amount * (odds-1) / -risked.
    # - Push or void: stake returned (0 profit). Voids are common in props (e.g. player
    #   didn't play enough, or market was cancelled) - we treat them as "no decision".
    # - When stakeAmount + payoutAmount present: use actual cash movement (payout - stake).
    #   This works even for imports without odds (e.g. richer CSV/JSON or future text scrapes).
    total_profit = 0.0
    total_staked = 0.0
    stake_weighted = 0
    realized_pnl = 0
    for row in rows:
        result = row.get("resultStatus")
        stake = _float_or_none(row.get("stakeAmount"))
        odds = _float_or_none(row.get("odds"))
        payout = _float_or_none(row.get("payoutAmount"))

        if result not in {"won", "lost", "push", "void"}:
            continue

        leg_profit = None
        risked = stake if (stake is not None and stake > 0) else None

        if risked is not None and odds is not None:
            if result == "won":
                leg_profit = risked * (odds - 1.0)
            elif result == "lost":
                leg_profit = -risked
            else:  # push or void: stake returned
                leg_profit = 0.0
            stake_weighted += 1

        if leg_profit is None and stake is not None and payout is not None:
            leg_profit = (payout or 0.0) - (stake or 0.0)
            realized_pnl += 1
            if risked is None and stake > 0:
                risked = stake
        elif leg_profit is None and result in {"push", "void"} and risked is not None:
            # Explicit: push/void with known stake = 0 P/L (stake back)
            leg_profit = 0.0
            realized_pnl += 1

        if leg_profit is not None:
            total_profit += leg_profit
            if risked is not None and risked > 0:
                total_staked += risked

    realized_roi = round(total_profit / total_staked, 4) if total_staked > 0 else None

    return {
        "legs": len(rows),
        "gradedLegs": graded,
        "hitRateEligible": graded,
        **result_counts,
        "hitRate": _round_rate(result_counts["won"], graded),
        "oddsLegs": len(odds_rows_all),
        "roiEligible": len(roi_rows),
        "roiExcludedMissingOdds": sum(1 for row in rows if row.get("odds") is None),
        "roiExcludedPush": sum(1 for row in rows if row.get("resultStatus") == "push"),
        "roiExcludedVoid": sum(1 for row in rows if row.get("resultStatus") == "void"),
        "roiExcludedPushVoid": sum(1 for row in rows if row.get("resultStatus") in {"push", "void"}),  # legacy combined
        "missingOdds": len(rows) - len(odds_rows_all),
        "profitPerUnit": round(unit_profit, 4) if roi_rows else None,
        "roi": round(unit_profit / len(roi_rows), 4) if roi_rows else None,
        "averageOdds": _round_average([_float_or_none(row.get("odds")) for row in odds_rows_all]),
        "averageBreakEvenRate": _round_average([
            1 / float(row["odds"])
            for row in odds_rows_all
            if row.get("odds")
        ]),
        # New stake-aware / realized metrics (0 / None when source data lacks stake/payout).
        "stakeAwareEligible": stake_weighted,
        "realizedPnlRows": realized_pnl,
        "totalStaked": round(total_staked, 4) if total_staked > 0 else None,
        "totalProfit": round(total_profit, 4) if (stake_weighted + realized_pnl) > 0 else None,
        "realizedRoi": realized_roi,
    }


def _round_rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _round_average(values: list[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def _clean_market_filter(value: str | None) -> str | None:
    clean = _clean_text_filter(value)
    if not clean:
        return None
    normalized = normalize_mlb_prop_market_key(
        clean,
        default_strikeouts_to_batter=True,
    )
    if normalized:
        return normalized
    return clean.lower().replace(" ", "_")


def _clean_text_filter(value: str | None) -> str | None:
    clean = str(value or "").strip()
    return clean or None


def _clean_backtest_view(value: str | None) -> str:
    clean = str(value or "dashboard").strip().lower()
    return clean if clean in {"dashboard", "legs", "tickets", "signals", "calibration"} else "dashboard"


def _history_subject(row: dict[str, Any]) -> str:
    return str(row.get("playerName") or row.get("teamName") or "unknown")


def _player_market_label(row: dict[str, Any]) -> str:
    return (
        f"{_history_subject(row)} | {row.get('marketKey') or 'unknown'} | "
        f"{row.get('side') or '?'}"
    )


def _market_side_label(row: dict[str, Any]) -> str:
    return f"{row.get('marketKey') or 'unknown'} | {row.get('side') or '?'}"


def _line_bucket_label(row: dict[str, Any]) -> str:
    return (
        f"{row.get('marketKey') or 'unknown'} | {row.get('side') or '?'} | "
        f"line {_format_number(row.get('line'))}"
    )


def _market_line_label(row: dict[str, Any]) -> str:
    return f"{row.get('marketKey') or 'unknown'} | line {_format_number(row.get('line'))}"


def _lineup_spot_label(row: dict[str, Any]) -> str:
    enrichment = row.get("historicalEnrichment") or {}
    order = _int_or_none(enrichment.get("battingOrder"))
    if order is None:
        return "lineup spot unknown"
    if order <= 3:
        bucket = "top third"
    elif order <= 6:
        bucket = "middle third"
    else:
        bucket = "bottom third"
    return f"{bucket} | batting {order}"


def _starter_role_label(row: dict[str, Any]) -> str:
    enrichment = row.get("historicalEnrichment") or {}
    if enrichment.get("confirmedStarter") is True:
        return "confirmed starter"
    if enrichment.get("confirmedStarter") is False:
        return "not confirmed starter"
    return "starter status unknown"


def _pitch_hand_label(row: dict[str, Any]) -> str:
    enrichment = row.get("historicalEnrichment") or {}
    pitch_hand = str(enrichment.get("pitchHand") or "").strip().upper()
    if pitch_hand in {"L", "LEFT"}:
        return "pitch hand L"
    if pitch_hand in {"R", "RIGHT"}:
        return "pitch hand R"
    return "pitch hand unknown"


def _venue_label(row: dict[str, Any]) -> str:
    enrichment = row.get("historicalEnrichment") or {}
    pregame = enrichment.get("pregameContext") or {}
    venue = pregame.get("venue") or {}
    name = str(venue.get("name") or "").strip()
    return name or "venue unknown"


def _longshot_odds_label(row: dict[str, Any]) -> str:
    odds = _float_or_none(_ticket_odds([row]))
    if odds is None:
        return "ticket odds unknown"
    if odds >= 100000:
        return "ticket odds 100000+"
    if odds >= 10000:
        return "ticket odds 10000-99999"
    if odds >= 1000:
        return "ticket odds 1000-9999"
    if odds >= 100:
        return "ticket odds 100-999"
    if odds >= 10:
        return "ticket odds 10-99"
    return "ticket odds under 10"


def _leg_count_label(row: dict[str, Any], ticket_leg_counts: dict[str, int]) -> str:
    ticket_id = str(row.get("ticketId") or "").strip()
    count = ticket_leg_counts.get(ticket_id) if ticket_id else None
    if count is None:
        return "leg count unknown"
    if count >= 20:
        return "20+ legs"
    if count >= 10:
        return "10-19 legs"
    if count >= 6:
        return "6-9 legs"
    if count >= 2:
        return "2-5 legs"
    return "single leg"


def _format_number(value: Any) -> str:
    numeric = _float_or_none(value)
    if numeric is None:
        return "?"
    if numeric.is_integer():
        return str(int(numeric))
    return str(numeric).rstrip("0").rstrip(".")


def _first_present(values) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _ticket_odds(rows: list[dict[str, Any]]) -> float | None:
    for row in rows:
        normalized = row.get("normalized") or {}
        raw = row.get("raw") or {}
        for source in (
            normalized.get("ticketOdds"),
            raw.get("ticket_odds"),
            raw.get("ticketOdds"),
            raw.get("sgm_multiplier"),
            raw.get("sgmMultiplier"),
        ):
            odds = _float_or_none(source)
            if odds is not None:
                return odds
    return None


def _ticket_leg_label(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject": _history_subject(row),
        "marketKey": row.get("marketKey"),
        "side": row.get("side"),
        "line": row.get("line"),
        "resultStatus": row.get("resultStatus"),
    }


def _sample_warning(rows: list[dict[str, Any]], *, graded_key: str | None = None) -> str | None:
    if graded_key:
        graded = sum(int(row.get(graded_key) or 0) for row in rows)
    else:
        graded = sum(1 for row in rows if row.get("resultStatus") in {"won", "lost"})
    if graded <= 0:
        return "no_graded_sample"
    if graded < 10:
        return "low_sample"
    if graded < 30:
        return "medium_sample"
    return None


def _signal_label(group: dict[str, Any]) -> str:
    graded = int(group.get("gradedLegs") or 0)
    hit_rate = _float_or_none(group.get("hitRate"))
    break_even = _float_or_none(group.get("averageBreakEvenRate"))
    if graded < 10:
        return "low_sample"
    if hit_rate is None:
        return "unknown"
    if break_even is not None:
        edge = hit_rate - break_even
        if edge >= 0.08:
            return "positive_history"
        if edge <= -0.08:
            return "negative_history"
        return "neutral_history"
    if hit_rate >= 0.6:
        return "positive_hit_rate"
    if hit_rate <= 0.45:
        return "negative_hit_rate"
    return "neutral_hit_rate"


def _signal_warnings(
    by_market: list[dict[str, Any]],
    by_player_market: list[dict[str, Any]],
    by_line_bucket: list[dict[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    if any(row.get("sampleWarning") == "low_sample" for row in by_market):
        warnings.append("Some market signals are low-sample; do not use them as hard rules.")
    if any(row.get("sampleWarning") == "low_sample" for row in by_player_market):
        warnings.append("Many player-market signals will be sparse until more history is imported.")
    if any(row.get("averageBreakEvenRate") is None for row in by_line_bucket):
        warnings.append("Some line buckets lack odds, so calibration cannot compare them to break-even.")
    return warnings


def _calibration_status_and_adjustment(
    *,
    graded: int,
    historical_edge: float | None,
    average_break_even: float | None,
) -> tuple[str, float | None]:
    if graded < 10:
        return "low_sample", None
    if average_break_even is None or historical_edge is None:
        return "missing_odds", None
    adjustment = max(min(historical_edge, 0.08), -0.15)
    if abs(adjustment) < 0.02:
        return "neutral", 0.0
    return "usable", round(adjustment, 4)


LEGACY_LIVE_NOTE = "live_or_in_progress_at_import"
HISTORY_REVIEW_BLOCKER_NOTES = {
    "missing_player_or_team",
    "missing_market",
    "missing_side",
    "missing_line",
    "missing_result",
    "ambiguous_strikeouts_market",
    "unknown_market",
    "unsupported_market_for_training",
    "result_actual_mismatch",
}
HISTORY_TRAINING_RESULTS = {"won", "lost", "push", "void"}


def _normalize_legacy_live_history_flags(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT *
        FROM bet_history_legs
        WHERE parse_notes_json LIKE ?
           OR normalized_json LIKE ?
        """,
        (f"%{LEGACY_LIVE_NOTE}%", "%liveAtImport%"),
    ).fetchall()
    for row in rows:
        notes = [
            str(note)
            for note in (_json_loads(row["parse_notes_json"]) or [])
            if str(note) != LEGACY_LIVE_NOTE
        ]
        normalized = _json_loads(row["normalized_json"]) or {}
        normalized.pop("liveAtImport", None)
        normalized.pop("live_at_import", None)
        needs_review = _legacy_history_needs_review(row, notes)
        training_eligible = _legacy_history_training_eligible(row, needs_review)
        conn.execute(
            """
            UPDATE bet_history_legs
            SET parse_notes_json = ?,
                normalized_json = ?,
                needs_review = ?,
                training_eligible = ?
            WHERE history_leg_id = ?
            """,
            (
                _json_dumps(sorted(set(notes))),
                _json_dumps(normalized),
                1 if needs_review else 0,
                1 if training_eligible else 0,
                row["history_leg_id"],
            ),
        )


def _legacy_history_needs_review(row: sqlite3.Row, notes: list[str]) -> bool:
    confidence = _float_or_none(row["parse_confidence"]) or 0.0
    return confidence < 0.70 or bool(set(notes) & HISTORY_REVIEW_BLOCKER_NOTES)


def _legacy_history_training_eligible(row: sqlite3.Row, needs_review: bool) -> bool:
    if needs_review:
        return False
    if row["market_key"] not in SUPPORTED_MLB_PROP_MARKETS:
        return False
    if row["side"] not in {"over", "under"}:
        return False
    if row["line"] is None:
        return False
    if not row["bet_date"]:
        return False
    if not (row["player_name"] or row["team_name"]):
        return False
    return row["result_status"] in HISTORY_TRAINING_RESULTS


def _find_duplicate_bet_history_import(
    conn: sqlite3.Connection,
    *,
    source_fingerprint: str,
    legs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if source_fingerprint:
        row = conn.execute(
            """
            SELECT import_id, imported_at
            FROM bet_history_imports
            WHERE source_fingerprint = ?
            ORDER BY imported_at DESC
            LIMIT 1
            """,
            (source_fingerprint,),
        ).fetchone()
        if row:
            return {"importId": row["import_id"], "importedAt": row["imported_at"]}

    expected = history_fingerprint(legs)
    grouped: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, str | None] = {}
    existing_rows = conn.execute(
        """
        SELECT l.import_id, i.imported_at, l.normalized_json
        FROM bet_history_legs l
        JOIN bet_history_imports i ON i.import_id = l.import_id
        LEFT JOIN bet_history_raw r ON r.raw_id = l.raw_id
        ORDER BY l.import_id, COALESCE(r.source_row_number, 0) ASC, l.leg_index ASC
        """
    ).fetchall()
    for row in existing_rows:
        import_id = row["import_id"]
        grouped.setdefault(import_id, []).append(_json_loads(row["normalized_json"]) or {})
        metadata[import_id] = row["imported_at"]
    for import_id, existing_legs in grouped.items():
        if history_fingerprint(existing_legs) == expected:
            return {"importId": import_id, "importedAt": metadata.get(import_id)}
    return None


def _refresh_duplicate_bet_history_metadata(
    conn: sqlite3.Connection,
    *,
    import_id: str,
    legs: list[dict[str, Any]],
) -> int:
    refreshed = 0
    for leg in legs:
        row = conn.execute(
            """
            SELECT l.*
            FROM bet_history_legs l
            LEFT JOIN bet_history_raw r ON r.raw_id = l.raw_id
            WHERE l.import_id = ?
              AND r.source_row_number = ?
              AND l.leg_index = ?
            """,
            (
                import_id,
                int(leg.get("sourceRowNumber") or 0),
                int(leg.get("legIndex") or 1),
            ),
        ).fetchone()
        if not row:
            row = _unique_bet_history_leg_by_index(
                conn,
                import_id=import_id,
                leg_index=int(leg.get("legIndex") or 1),
            )
        if not row:
            continue
        values = _refreshable_bet_history_values(leg)
        next_normalized = _normalized_history_leg(leg)
        next_raw = leg.get("rawJson") or {}
        if not _bet_history_refresh_needed(row, values, next_normalized, next_raw):
            continue
        conn.execute(
            """
            UPDATE bet_history_legs
            SET ticket_id = ?,
                bet_date = ?,
                settled_date = ?,
                sport = ?,
                league = ?,
                player_name = ?,
                team_name = ?,
                opponent_name = ?,
                fixture_slug = ?,
                matchup = ?,
                market_key = ?,
                market_name = ?,
                side = ?,
                line = ?,
                odds = ?,
                stake_amount = ?,
                payout_amount = ?,
                result_status = ?,
                actual_stat = ?,
                parse_confidence = ?,
                parse_confidence_label = ?,
                needs_review = ?,
                training_eligible = ?,
                parser_version = ?,
                eligibility_version = ?,
                parse_notes_json = ?,
                ignored_fields_json = ?,
                normalized_json = ?,
                raw_json = ?
            WHERE history_leg_id = ?
            """,
            (
                values["ticket_id"],
                values["bet_date"],
                values["settled_date"],
                values["sport"],
                values["league"],
                values["player_name"],
                values["team_name"],
                values["opponent_name"],
                values["fixture_slug"],
                values["matchup"],
                values["market_key"],
                values["market_name"],
                values["side"],
                values["line"],
                values["odds"],
                values["stake_amount"],
                values["payout_amount"],
                values["result_status"],
                values["actual_stat"],
                values["parse_confidence"],
                values["parse_confidence_label"],
                values["needs_review"],
                values["training_eligible"],
                values["parser_version"],
                values["eligibility_version"],
                values["parse_notes_json"],
                values["ignored_fields_json"],
                _json_dumps(next_normalized),
                _json_dumps(next_raw),
                row["history_leg_id"],
            ),
        )
        refreshed += 1
    return refreshed


def _unique_bet_history_leg_by_index(
    conn: sqlite3.Connection,
    *,
    import_id: str,
    leg_index: int,
) -> sqlite3.Row | None:
    rows = conn.execute(
        """
        SELECT *
        FROM bet_history_legs
        WHERE import_id = ? AND leg_index = ?
        LIMIT 2
        """,
        (import_id, leg_index),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def _refreshable_bet_history_values(leg: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_id": leg.get("ticketId"),
        "bet_date": leg.get("betDate"),
        "settled_date": leg.get("settledDate"),
        "sport": leg.get("sport") or "mlb",
        "league": leg.get("league"),
        "player_name": leg.get("playerName"),
        "team_name": leg.get("teamName"),
        "opponent_name": leg.get("opponentName"),
        "fixture_slug": leg.get("fixtureSlug"),
        "matchup": leg.get("matchup"),
        "market_key": leg.get("marketKey"),
        "market_name": leg.get("marketName"),
        "side": leg.get("side"),
        "line": _float_or_none(leg.get("line")),
        "odds": _float_or_none(leg.get("odds")),
        "stake_amount": _float_or_none(leg.get("stakeAmount")),
        "payout_amount": _float_or_none(leg.get("payoutAmount")),
        "result_status": leg.get("resultStatus"),
        "actual_stat": _float_or_none(leg.get("actualStat")),
        "parse_confidence": _float_or_none(leg.get("parseConfidence")) or 0.0,
        "parse_confidence_label": leg.get("parseConfidenceLabel") or "low",
        "needs_review": 1 if leg.get("needsReview") else 0,
        "training_eligible": 1 if leg.get("trainingEligible") else 0,
        "parser_version": leg.get("parserVersion"),
        "eligibility_version": leg.get("eligibilityVersion"),
        "parse_notes_json": _json_dumps(leg.get("parseNotes") or []),
        "ignored_fields_json": _json_dumps(leg.get("ignoredFields") or []),
    }


def _bet_history_refresh_needed(
    row: sqlite3.Row,
    values: dict[str, Any],
    normalized: dict[str, Any],
    raw: dict[str, Any],
) -> bool:
    for key, next_value in values.items():
        current = row[key]
        if isinstance(next_value, float):
            if _float_or_none(current) != next_value:
                return True
        elif key in {"needs_review", "training_eligible"}:
            if int(current or 0) != int(next_value or 0):
                return True
        elif current != next_value:
            return True
    if (_json_loads(row["normalized_json"]) or {}) != normalized:
        return True
    if (_json_loads(row["raw_json"]) or {}) != raw:
        return True
    return False


def _history_reportable_note(note: str) -> bool:
    return (
        note.startswith("missing_")
        or note.startswith("ambiguous_")
        or note in {
            "unknown_market",
            "unsupported_composite_market",
            "unsupported_market_for_training",
        }
    )


def _decision_metadata(
    response: dict[str, Any],
    request_body: dict[str, Any],
) -> dict[str, Any]:
    return {
        "mode": request_body.get("mode"),
        "targetOddsMin": request_body.get("targetOddsMin") or request_body.get("target_odds_min"),
        "targetOddsMax": request_body.get("targetOddsMax") or request_body.get("target_odds_max"),
        "minLegs": request_body.get("minLegs") or request_body.get("min_legs"),
        "maxLegs": request_body.get("maxLegs") or request_body.get("max_legs"),
        "validationMode": (response.get("validation") or {}).get("validationMode"),
        "oddsPolicy": (response.get("validation") or {}).get("oddsPolicy"),
        "selectionCount": response.get("selectionCount"),
        "source": response.get("source"),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remote_history_pull_ttl() -> float:
    try:
        return max(0.0, float(os.getenv("AZP_SUPABASE_HISTORY_PULL_TTL_SECONDS", "60")))
    except ValueError:
        return 60.0


def _fail_on_supabase_history_error() -> bool:
    return str(os.getenv("AZP_FAIL_ON_SUPABASE_HISTORY_ERROR") or "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def _json_loads(value: str) -> Any:
    return json.loads(value) if value else None


def _row_has_column(row: sqlite3.Row, column: str) -> bool:
    return column in row.keys()


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_limit(limit: int) -> int:
    return max(1, min(int(limit), 500))


def _clean_history_limit(limit: int) -> int:
    return max(1, min(int(limit), 50000))


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
