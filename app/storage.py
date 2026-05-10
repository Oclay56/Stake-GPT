from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data") / "azp.sqlite"


class SnapshotStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS prop_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    snapshot_phase TEXT NOT NULL DEFAULT 'manual',
                    snapshot_label TEXT,
                    slate_date TEXT,
                    timezone TEXT,
                    prop_id TEXT NOT NULL,
                    fixture_slug TEXT,
                    game TEXT,
                    mlb_game_pk INTEGER,
                    player_name TEXT,
                    player_key TEXT,
                    player_mlb_id INTEGER,
                    team_name TEXT,
                    team_key TEXT,
                    team_mlb_id INTEGER,
                    market_name TEXT,
                    market_key TEXT,
                    line REAL,
                    over_odds REAL,
                    under_odds REAL,
                    match_status TEXT,
                    stat_key TEXT,
                    season_value REAL,
                    recent_total REAL,
                    recent_per_game REAL,
                    games_used INTEGER,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_prop_snapshots_date
                    ON prop_snapshots (slate_date, captured_at);
                CREATE INDEX IF NOT EXISTS idx_prop_snapshots_prop_id
                    ON prop_snapshots (prop_id, captured_at);
                CREATE INDEX IF NOT EXISTS idx_prop_snapshots_player
                    ON prop_snapshots (player_name, player_mlb_id);

                CREATE TABLE IF NOT EXISTS player_stat_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    snapshot_phase TEXT NOT NULL DEFAULT 'manual',
                    snapshot_label TEXT,
                    player_mlb_id INTEGER NOT NULL,
                    player_name TEXT,
                    season INTEGER,
                    group_name TEXT,
                    stats_json TEXT NOT NULL,
                    recent_history_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_player_stat_snapshots_player
                    ON player_stat_snapshots (player_mlb_id, season, group_name, captured_at);

                CREATE TABLE IF NOT EXISTS analysis_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    snapshot_phase TEXT NOT NULL DEFAULT 'manual',
                    snapshot_label TEXT,
                    slate_date TEXT,
                    prop_id TEXT NOT NULL,
                    player_name TEXT,
                    team_name TEXT,
                    market_key TEXT,
                    line REAL,
                    bucket TEXT,
                    lean TEXT,
                    edge REAL,
                    score INTEGER,
                    confidence TEXT,
                    market_profile TEXT,
                    market_threshold REAL,
                    risk_flags_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_analysis_snapshots_date
                    ON analysis_snapshots (slate_date, snapshot_phase, captured_at);
                CREATE INDEX IF NOT EXISTS idx_analysis_snapshots_prop_id
                    ON analysis_snapshots (prop_id, captured_at);

                CREATE TABLE IF NOT EXISTS recommendation_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    captured_at TEXT NOT NULL,
                    source TEXT,
                    matchup TEXT,
                    slate_date TEXT,
                    timezone TEXT,
                    diversity_mode TEXT,
                    filters_json TEXT NOT NULL,
                    request_params_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL,
                    concentration_tags_json TEXT NOT NULL,
                    matched_fixture_count INTEGER,
                    available_prop_count INTEGER,
                    matched_prop_count INTEGER,
                    unmatched_prop_count INTEGER,
                    recommendation_count INTEGER,
                    parlay_json TEXT NOT NULL,
                    notes_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recommendation_requests_date
                    ON recommendation_requests (slate_date, captured_at);
                CREATE INDEX IF NOT EXISTS idx_recommendation_requests_diversity
                    ON recommendation_requests (diversity_mode, captured_at);

                CREATE TABLE IF NOT EXISTS recommendation_legs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    slate_date TEXT,
                    matchup TEXT,
                    rank INTEGER,
                    prop_id TEXT,
                    fixture_slug TEXT,
                    game TEXT,
                    mlb_game_pk INTEGER,
                    player_name TEXT,
                    player_key TEXT,
                    player_mlb_id INTEGER,
                    team_name TEXT,
                    team_key TEXT,
                    team_mlb_id INTEGER,
                    market_key TEXT,
                    stat_key TEXT,
                    line REAL,
                    side TEXT,
                    lean TEXT,
                    odds REAL,
                    over_odds REAL,
                    under_odds REAL,
                    edge REAL,
                    score INTEGER,
                    confidence TEXT,
                    selection TEXT,
                    diversity_mode TEXT,
                    risk_flags_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    contextual_tags_json TEXT NOT NULL,
                    deferred_layers_json TEXT NOT NULL,
                    concentration_tags_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recommendation_legs_date
                    ON recommendation_legs (slate_date, captured_at);
                CREATE INDEX IF NOT EXISTS idx_recommendation_legs_prop
                    ON recommendation_legs (prop_id, captured_at);
                CREATE INDEX IF NOT EXISTS idx_recommendation_legs_market
                    ON recommendation_legs (market_key, side, captured_at);

                CREATE TABLE IF NOT EXISTS recommendation_settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    leg_rank INTEGER NOT NULL,
                    prop_id TEXT,
                    slate_date TEXT,
                    market_key TEXT,
                    side TEXT,
                    actual_value REAL,
                    actual_result TEXT,
                    over_outcome TEXT,
                    decision_outcome TEXT,
                    reasons_json TEXT NOT NULL,
                    settled_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    UNIQUE (request_id, leg_rank)
                );

                CREATE INDEX IF NOT EXISTS idx_recommendation_settlements_date
                    ON recommendation_settlements (slate_date, settled_at);
                CREATE INDEX IF NOT EXISTS idx_recommendation_settlements_prop
                    ON recommendation_settlements (prop_id, settled_at);

                CREATE TABLE IF NOT EXISTS gpt_decision_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL UNIQUE,
                    captured_at TEXT NOT NULL,
                    source TEXT,
                    matchup TEXT,
                    slate_date TEXT,
                    timezone TEXT,
                    prompt TEXT,
                    request_body_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    notes_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_gpt_decision_requests_date
                    ON gpt_decision_requests (slate_date, captured_at);

                CREATE TABLE IF NOT EXISTS gpt_decision_legs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    source TEXT,
                    slate_date TEXT,
                    matchup TEXT,
                    rank INTEGER,
                    prop_id TEXT,
                    selection_id TEXT,
                    fixture_slug TEXT,
                    game TEXT,
                    player_name TEXT,
                    player_key TEXT,
                    player_mlb_id INTEGER,
                    team_name TEXT,
                    team_key TEXT,
                    team_mlb_id INTEGER,
                    market_key TEXT,
                    line REAL,
                    side TEXT,
                    odds REAL,
                    over_odds REAL,
                    under_odds REAL,
                    selection TEXT,
                    valid INTEGER,
                    validation_issues_json TEXT NOT NULL,
                    rationale TEXT,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_gpt_decision_legs_date
                    ON gpt_decision_legs (slate_date, captured_at);
                CREATE INDEX IF NOT EXISTS idx_gpt_decision_legs_decision
                    ON gpt_decision_legs (decision_id, rank);
                """
            )
            _ensure_column(conn, "prop_snapshots", "snapshot_phase", "TEXT NOT NULL DEFAULT 'manual'")
            _ensure_column(conn, "prop_snapshots", "snapshot_label", "TEXT")
            _ensure_column(conn, "prop_snapshots", "mlb_game_pk", "INTEGER")
            _ensure_column(conn, "player_stat_snapshots", "snapshot_phase", "TEXT NOT NULL DEFAULT 'manual'")
            _ensure_column(conn, "player_stat_snapshots", "snapshot_label", "TEXT")

    def save_gpt_decision_result(
        self,
        response: dict[str, Any],
        request_body: dict[str, Any] | None = None,
        captured_at: datetime | None = None,
    ) -> dict[str, Any]:
        self.init_db()
        captured_text = _captured_at(captured_at)
        decision_id = str(response.get("decisionId") or uuid.uuid4())
        selections = response.get("selections") or []

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gpt_decision_requests (
                    decision_id, captured_at, source, matchup, slate_date, timezone,
                    prompt, request_body_json, validation_json, notes_json, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    captured_text,
                    response.get("source"),
                    response.get("matchup"),
                    response.get("date"),
                    response.get("timezone"),
                    response.get("prompt"),
                    _json_dumps(request_body or {}),
                    _json_dumps(response.get("validation") or {}),
                    _json_dumps(response.get("notes") or []),
                    _json_dumps({**response, "decisionId": decision_id}),
                ),
            )
            for index, selection in enumerate(selections, start=1):
                conn.execute(
                    """
                    INSERT INTO gpt_decision_legs (
                        decision_id, captured_at, source, slate_date, matchup, rank,
                        prop_id, selection_id, fixture_slug, game,
                        player_name, player_key, player_mlb_id,
                        team_name, team_key, team_mlb_id,
                        market_key, line, side, odds, over_odds, under_odds,
                        selection, valid, validation_issues_json, rationale, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _gpt_decision_leg_values(
                        decision_id=decision_id,
                        captured_at=captured_text,
                        response=response,
                        rank=index,
                        selection=selection,
                    ),
                )

        return {
            "decisionId": decision_id,
            "gptDecisionRequestsInserted": 1,
            "gptDecisionLegsInserted": len(selections),
        }

    def list_gpt_decision_legs(
        self,
        date_text: str | None = None,
        decision_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self.init_db()
        where = []
        params: list[Any] = []
        if date_text:
            where.append("slate_date = ?")
            params.append(date_text)
        if decision_id:
            where.append("decision_id = ?")
            params.append(decision_id)

        sql = "SELECT * FROM gpt_decision_legs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY captured_at DESC, decision_id DESC, rank ASC LIMIT ?"
        params.append(_clean_limit(limit))

        with self._connect() as conn:
            return [_gpt_decision_leg_row(row) for row in conn.execute(sql, params)]

    def save_recommendation_result(
        self,
        response: dict[str, Any],
        captured_at: datetime | None = None,
        request_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.init_db()
        captured_text = _captured_at(captured_at)
        request_id = str(response.get("requestId") or uuid.uuid4())
        filters = response.get("filters") or {}
        diagnostics = response.get("recommendationDiagnostics") or {}
        concentration_tags = diagnostics.get("concentrationTags") or []
        recommendations = response.get("recommendations") or []

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recommendation_requests (
                    request_id, captured_at, source, matchup, slate_date, timezone,
                    diversity_mode, filters_json, request_params_json,
                    diagnostics_json, concentration_tags_json,
                    matched_fixture_count, available_prop_count, matched_prop_count,
                    unmatched_prop_count, recommendation_count, parlay_json,
                    notes_json, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    captured_text,
                    response.get("source"),
                    response.get("matchup"),
                    response.get("date"),
                    response.get("timezone"),
                    filters.get("diversityMode"),
                    _json_dumps(filters),
                    _json_dumps(request_params or {}),
                    _json_dumps(diagnostics),
                    _json_dumps(concentration_tags),
                    _int_or_none(response.get("matchedFixtureCount")),
                    _int_or_none(response.get("availablePropCount")),
                    _int_or_none(response.get("matchedPropCount")),
                    _int_or_none(response.get("unmatchedPropCount")),
                    _int_or_none(response.get("recommendationCount")),
                    _json_dumps(response.get("parlay") or {}),
                    _json_dumps(response.get("notes") or []),
                    _json_dumps({**response, "requestId": request_id}),
                ),
            )
            for index, recommendation in enumerate(recommendations, start=1):
                conn.execute(
                    """
                    INSERT INTO recommendation_legs (
                        request_id, captured_at, slate_date, matchup, rank,
                        prop_id, fixture_slug, game, mlb_game_pk,
                        player_name, player_key, player_mlb_id,
                        team_name, team_key, team_mlb_id,
                        market_key, stat_key, line, side, lean, odds,
                        over_odds, under_odds, edge, score, confidence,
                        selection, diversity_mode, risk_flags_json, reasons_json,
                        contextual_tags_json, deferred_layers_json,
                        concentration_tags_json, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _recommendation_leg_values(
                        request_id=request_id,
                        captured_at=captured_text,
                        response=response,
                        rank=index,
                        recommendation=recommendation,
                        concentration_tags=concentration_tags,
                    ),
                )

        return {
            "requestId": request_id,
            "recommendationRequestsInserted": 1,
            "recommendationLegsInserted": len(recommendations),
        }

    def list_recommendation_requests(
        self,
        date_text: str | None = None,
        request_id: str | None = None,
        diversity_mode: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db()
        where = []
        params: list[Any] = []
        if date_text:
            where.append("slate_date = ?")
            params.append(date_text)
        if request_id:
            where.append("request_id = ?")
            params.append(request_id)
        if diversity_mode:
            where.append("diversity_mode = ?")
            params.append(diversity_mode)

        sql = "SELECT * FROM recommendation_requests"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY captured_at DESC, id DESC LIMIT ?"
        params.append(_clean_limit(limit))

        with self._connect() as conn:
            return [_recommendation_request_row(row) for row in conn.execute(sql, params)]

    def list_recommendation_legs(
        self,
        date_text: str | None = None,
        market: str | None = None,
        side: str | None = None,
        request_id: str | None = None,
        diversity_mode: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self.init_db()
        where = []
        params: list[Any] = []
        if date_text:
            where.append("slate_date = ?")
            params.append(date_text)
        if market:
            where.append("market_key = ?")
            params.append(market)
        if side:
            where.append("side = ?")
            params.append(side)
        if request_id:
            where.append("request_id = ?")
            params.append(request_id)
        if diversity_mode:
            where.append("diversity_mode = ?")
            params.append(diversity_mode)

        sql = "SELECT * FROM recommendation_legs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY captured_at DESC, request_id DESC, rank ASC LIMIT ?"
        params.append(_clean_limit(limit))

        with self._connect() as conn:
            return [_recommendation_leg_row(row) for row in conn.execute(sql, params)]

    def save_recommendation_settlements(
        self,
        rows: list[dict[str, Any]],
        settled_at: datetime | None = None,
    ) -> dict[str, int]:
        self.init_db()
        settled_text = _captured_at(settled_at)
        with self._connect() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO recommendation_settlements (
                        request_id, leg_rank, prop_id, slate_date, market_key,
                        side, actual_value, actual_result, over_outcome,
                        decision_outcome, reasons_json, settled_at, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(request_id, leg_rank) DO UPDATE SET
                        prop_id = excluded.prop_id,
                        slate_date = excluded.slate_date,
                        market_key = excluded.market_key,
                        side = excluded.side,
                        actual_value = excluded.actual_value,
                        actual_result = excluded.actual_result,
                        over_outcome = excluded.over_outcome,
                        decision_outcome = excluded.decision_outcome,
                        reasons_json = excluded.reasons_json,
                        settled_at = excluded.settled_at,
                        raw_json = excluded.raw_json
                    """,
                    (
                        row.get("requestId"),
                        _int_or_none(row.get("rank")) or 0,
                        row.get("propId"),
                        row.get("date"),
                        row.get("marketKey"),
                        row.get("side"),
                        _float_or_none(row.get("actualValue")),
                        row.get("actualResult"),
                        row.get("overOutcome"),
                        row.get("decisionOutcome"),
                        _json_dumps(row.get("reasons") or []),
                        settled_text,
                        _json_dumps(row),
                    ),
                )
        return {"recommendationSettlementsSaved": len(rows)}

    def list_recommendation_settlements(
        self,
        date_text: str | None = None,
        request_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self.init_db()
        where = []
        params: list[Any] = []
        if date_text:
            where.append("slate_date = ?")
            params.append(date_text)
        if request_id:
            where.append("request_id = ?")
            params.append(request_id)

        sql = "SELECT * FROM recommendation_settlements"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY settled_at DESC, request_id DESC, leg_rank ASC LIMIT ?"
        params.append(_clean_limit(limit))

        with self._connect() as conn:
            return [
                _recommendation_settlement_row(row)
                for row in conn.execute(sql, params)
            ]

    def save_analysis_result(
        self,
        analysis: dict[str, Any],
        captured_at: datetime | None = None,
        snapshot_label: str | None = None,
    ) -> dict[str, int]:
        self.init_db()
        captured_text = _captured_at(captured_at)
        snapshot_phase = _clean_snapshot_phase(analysis.get("snapshotPhase"))
        snapshot_label = _clean_snapshot_label(snapshot_label)
        rows = _analysis_rows_from_result(analysis)

        with self._connect() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO analysis_snapshots (
                        captured_at, snapshot_phase, snapshot_label,
                        slate_date, prop_id, player_name, team_name,
                        market_key, line, bucket, lean, edge, score,
                        confidence, market_profile, market_threshold,
                        risk_flags_json, reasons_json, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _analysis_snapshot_values(
                        analysis,
                        row,
                        captured_text,
                        snapshot_phase,
                        snapshot_label,
                    ),
                )

        return {"analysisSnapshotsInserted": len(rows)}

    def save_enriched_props(
        self,
        payload: dict[str, Any],
        captured_at: datetime | None = None,
        snapshot_phase: str = "manual",
        snapshot_label: str | None = None,
    ) -> dict[str, int]:
        self.init_db()
        captured_text = _captured_at(captured_at)
        snapshot_phase = _clean_snapshot_phase(snapshot_phase)
        snapshot_label = _clean_snapshot_label(snapshot_label)
        prop_count = 0
        stat_count = 0
        seen_stat_keys: set[tuple[int, int | None, str | None]] = set()

        props = _dedupe_props(payload.get("props") or [])

        with self._connect() as conn:
            for prop in props:
                conn.execute(
                    """
                    INSERT INTO prop_snapshots (
                        captured_at, snapshot_phase, snapshot_label,
                        slate_date, timezone, prop_id, fixture_slug, game,
                        mlb_game_pk,
                        player_name, player_key, player_mlb_id,
                        team_name, team_key, team_mlb_id,
                        market_name, market_key, line, over_odds, under_odds,
                        match_status, stat_key, season_value, recent_total,
                        recent_per_game, games_used, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _prop_snapshot_values(
                        payload,
                        prop,
                        captured_text,
                        snapshot_phase,
                        snapshot_label,
                    ),
                )
                prop_count += 1

                stat_key = _player_stat_key(prop)
                if stat_key is None or stat_key in seen_stat_keys:
                    continue
                values = _player_stat_values(
                    prop,
                    captured_text,
                    snapshot_phase,
                    snapshot_label,
                )
                if values is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO player_stat_snapshots (
                        captured_at, snapshot_phase, snapshot_label,
                        player_mlb_id, player_name, season, group_name,
                        stats_json, recent_history_json, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                seen_stat_keys.add(stat_key)
                stat_count += 1

        return {
            "propSnapshotsInserted": prop_count,
            "playerStatSnapshotsInserted": stat_count,
        }

    def list_latest_analysis_snapshots(
        self,
        date_text: str | None = None,
        market: str | None = None,
        snapshot_phase: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db()
        where = []
        params: list[Any] = []

        if date_text:
            where.append("slate_date = ?")
            params.append(date_text)
        if market:
            where.append("market_key = ?")
            params.append(market)
        if snapshot_phase:
            where.append("snapshot_phase = ?")
            params.append(_clean_snapshot_phase(snapshot_phase))

        source_sql = "SELECT * FROM analysis_snapshots"
        if where:
            source_sql += " WHERE " + " AND ".join(where)

        sql = f"""
            SELECT * FROM (
                SELECT source.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY prop_id
                           ORDER BY captured_at DESC, id DESC
                       ) AS row_number
                FROM ({source_sql}) AS source
            )
            WHERE row_number = 1
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
        """
        params.append(_clean_limit(limit))

        with self._connect() as conn:
            return [_analysis_row(row) for row in conn.execute(sql, params)]

    def list_prop_snapshots(
        self,
        date_text: str | None = None,
        player: str | None = None,
        market: str | None = None,
        snapshot_phase: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db()
        where = []
        params: list[Any] = []

        if date_text:
            where.append("slate_date = ?")
            params.append(date_text)
        if player:
            where.append("player_name LIKE ?")
            params.append(f"%{player}%")
        if market:
            where.append("market_key = ?")
            params.append(market)
        if snapshot_phase:
            where.append("snapshot_phase = ?")
            params.append(_clean_snapshot_phase(snapshot_phase))

        sql = "SELECT * FROM prop_snapshots"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY captured_at DESC, id DESC LIMIT ?"
        params.append(_clean_limit(limit))

        with self._connect() as conn:
            return [_prop_row(row) for row in conn.execute(sql, params)]

    def list_latest_prop_snapshots(
        self,
        date_text: str | None = None,
        player: str | None = None,
        market: str | None = None,
        snapshot_phase: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db()
        where = []
        params: list[Any] = []

        if date_text:
            where.append("slate_date = ?")
            params.append(date_text)
        if player:
            where.append("player_name LIKE ?")
            params.append(f"%{player}%")
        if market:
            where.append("market_key = ?")
            params.append(market)
        if snapshot_phase:
            where.append("snapshot_phase = ?")
            params.append(_clean_snapshot_phase(snapshot_phase))

        source_sql = "SELECT * FROM prop_snapshots"
        if where:
            source_sql += " WHERE " + " AND ".join(where)

        sql = f"""
            SELECT * FROM (
                SELECT source.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY prop_id
                           ORDER BY captured_at DESC, id DESC
                       ) AS row_number
                FROM ({source_sql}) AS source
            )
            WHERE row_number = 1
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
        """
        params.append(_clean_limit(limit))

        with self._connect() as conn:
            return [_prop_row(row) for row in conn.execute(sql, params)]

    def list_player_stat_snapshots(
        self,
        player_mlb_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db()
        params: list[Any] = []
        sql = "SELECT * FROM player_stat_snapshots"
        if player_mlb_id is not None:
            sql += " WHERE player_mlb_id = ?"
            params.append(player_mlb_id)
        sql += " ORDER BY captured_at DESC, id DESC LIMIT ?"
        params.append(_clean_limit(limit))

        with self._connect() as conn:
            return [_player_stat_row(row) for row in conn.execute(sql, params)]

    def get_prop_movement(
        self,
        prop_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.init_db()
        with self._connect() as conn:
            rows = [
                _prop_row(row)
                for row in conn.execute(
                    """
                    SELECT * FROM prop_snapshots
                    WHERE prop_id = ?
                    ORDER BY captured_at ASC, id ASC
                    LIMIT ?
                    """,
                    (prop_id, _clean_limit(limit)),
                )
            ]

        return {
            "propId": prop_id,
            "snapshotCount": len(rows),
            "snapshots": rows,
            "changes": _movement_changes(rows),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def default_db_path() -> Path:
    configured = os.getenv("AZP_DB_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_DB_PATH


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str,
) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _dedupe_props(props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for index, prop in enumerate(props):
        prop_id = str(prop.get("propId") or f"missing-prop-id-{index}")
        existing = deduped.get(prop_id)
        deduped[prop_id] = (
            prop if existing is None else _preferred_duplicate_prop(existing, prop)
        )
    return list(deduped.values())


def _preferred_duplicate_prop(
    current: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    current_line = _float_or_none(current.get("line"))
    candidate_line = _float_or_none(candidate.get("line"))
    if current_line == candidate_line:
        return candidate

    current_rank = _primary_line_rank(current)
    candidate_rank = _primary_line_rank(candidate)
    return candidate if candidate_rank < current_rank else current


def _primary_line_rank(prop: dict[str, Any]) -> tuple[float, float]:
    odds = prop.get("odds") or {}
    over = _float_or_none(odds.get("over"))
    under = _float_or_none(odds.get("under"))
    line = _float_or_none(prop.get("line"))
    if over is None or under is None or over <= 1 or under <= 1:
        return (999.0, abs(line or 999.0))
    return (round(abs(over - under), 4), abs(line or 0.0))


def _prop_snapshot_values(
    payload: dict[str, Any],
    prop: dict[str, Any],
    captured_at: str,
    snapshot_phase: str,
    snapshot_label: str | None,
) -> tuple[Any, ...]:
    player = prop.get("player") or {}
    team = prop.get("team") or {}
    market = prop.get("market") or {}
    odds = prop.get("odds") or {}
    context = prop.get("statContext") or {}
    mlb_game = prop.get("mlbGame") or {}

    return (
        captured_at,
        snapshot_phase,
        snapshot_label,
        payload.get("date"),
        payload.get("timezone"),
        prop.get("propId"),
        prop.get("fixtureSlug"),
        prop.get("game"),
        _int_or_none(mlb_game.get("gamePk")),
        player.get("name"),
        player.get("key"),
        _int_or_none(player.get("mlbId")),
        team.get("name"),
        team.get("key"),
        _int_or_none(team.get("mlbId")),
        market.get("name"),
        market.get("key"),
        _float_or_none(prop.get("line")),
        _float_or_none(odds.get("over")),
        _float_or_none(odds.get("under")),
        player.get("matchStatus"),
        context.get("statKey"),
        _float_or_none(context.get("seasonValue")),
        _float_or_none(context.get("recentTotal")),
        _float_or_none(context.get("recentPerGame")),
        _int_or_none(context.get("gamesUsed")),
        _json_dumps(prop),
    )


def _player_stat_values(
    prop: dict[str, Any],
    captured_at: str,
    snapshot_phase: str,
    snapshot_label: str | None,
) -> tuple[Any, ...] | None:
    profile = prop.get("mlbProfile") or {}
    recent = prop.get("recentHistory") or {}
    player = profile.get("player") or {}
    player_id = _int_or_none(player.get("mlbId") or (prop.get("player") or {}).get("mlbId"))
    if player_id is None:
        return None

    stats = player.get("stats") or {}
    season = _int_or_none(profile.get("season") or recent.get("season"))
    group = profile.get("group") or recent.get("group")
    raw = {"mlbProfile": profile, "recentHistory": recent}
    return (
        captured_at,
        snapshot_phase,
        snapshot_label,
        player_id,
        player.get("name") or (prop.get("player") or {}).get("name"),
        season,
        group,
        _json_dumps(stats),
        _json_dumps(recent),
        _json_dumps(raw),
    )


def _player_stat_key(prop: dict[str, Any]) -> tuple[int, int | None, str | None] | None:
    profile = prop.get("mlbProfile") or {}
    recent = prop.get("recentHistory") or {}
    player = profile.get("player") or {}
    player_id = _int_or_none(player.get("mlbId") or (prop.get("player") or {}).get("mlbId"))
    if player_id is None:
        return None
    season = _int_or_none(profile.get("season") or recent.get("season"))
    group = profile.get("group") or recent.get("group")
    return (player_id, season, group)


def _analysis_rows_from_result(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    buckets = analysis.get("buckets") or {}
    rows = []
    for bucket in ("watchlist", "neutral", "avoid"):
        for row in buckets.get(bucket) or []:
            row = dict(row)
            row["bucket"] = row.get("bucket") or bucket
            rows.append(row)
    return rows


def _analysis_snapshot_values(
    analysis: dict[str, Any],
    row: dict[str, Any],
    captured_at: str,
    snapshot_phase: str,
    snapshot_label: str | None,
) -> tuple[Any, ...]:
    return (
        captured_at,
        snapshot_phase,
        snapshot_label,
        analysis.get("date"),
        row.get("propId"),
        row.get("playerName"),
        row.get("teamName"),
        row.get("marketKey"),
        _float_or_none(row.get("line")),
        row.get("bucket"),
        row.get("lean"),
        _float_or_none(row.get("edge")),
        _int_or_none(row.get("score")),
        row.get("confidence"),
        row.get("marketProfile"),
        _float_or_none(row.get("marketThreshold")),
        _json_dumps(row.get("riskFlags") or []),
        _json_dumps(row.get("reasons") or []),
        _json_dumps(row),
    )


def _recommendation_leg_values(
    request_id: str,
    captured_at: str,
    response: dict[str, Any],
    rank: int,
    recommendation: dict[str, Any],
    concentration_tags: list[str],
) -> tuple[Any, ...]:
    player = recommendation.get("player") or {}
    team = recommendation.get("team") or {}
    market = recommendation.get("market") or {}
    contextual = recommendation.get("contextualEdge") or {}
    mlb_game = recommendation.get("mlbGame") or {}
    return (
        request_id,
        captured_at,
        response.get("date"),
        response.get("matchup"),
        _int_or_none(recommendation.get("rank")) or rank,
        recommendation.get("propId"),
        recommendation.get("fixtureSlug"),
        recommendation.get("game"),
        _int_or_none(mlb_game.get("gamePk")),
        recommendation.get("playerName") or player.get("name"),
        player.get("key"),
        _int_or_none(player.get("mlbId")),
        recommendation.get("teamName") or team.get("name"),
        team.get("key"),
        _int_or_none(team.get("mlbId")),
        recommendation.get("marketKey") or market.get("key"),
        recommendation.get("statKey"),
        _float_or_none(recommendation.get("line")),
        recommendation.get("side"),
        recommendation.get("lean"),
        _float_or_none(recommendation.get("odds")),
        _float_or_none(recommendation.get("overOdds")),
        _float_or_none(recommendation.get("underOdds")),
        _float_or_none(recommendation.get("edge")),
        _int_or_none(recommendation.get("score")),
        recommendation.get("confidence"),
        recommendation.get("selection"),
        (response.get("filters") or {}).get("diversityMode"),
        _json_dumps(recommendation.get("riskFlags") or []),
        _json_dumps(recommendation.get("reasons") or []),
        _json_dumps(contextual.get("tags") or []),
        _json_dumps(contextual.get("deferredLayers") or []),
        _json_dumps(concentration_tags),
        _json_dumps(recommendation),
    )


def _gpt_decision_leg_values(
    decision_id: str,
    captured_at: str,
    response: dict[str, Any],
    rank: int,
    selection: dict[str, Any],
) -> tuple[Any, ...]:
    player = selection.get("player") or {}
    team = selection.get("team") or {}
    market = selection.get("market") or {}
    return (
        decision_id,
        captured_at,
        response.get("source"),
        response.get("date"),
        response.get("matchup"),
        _int_or_none(selection.get("rank")) or rank,
        selection.get("propId"),
        selection.get("selectionId"),
        selection.get("fixtureSlug"),
        selection.get("game"),
        player.get("name"),
        player.get("key"),
        _int_or_none(player.get("mlbId")),
        team.get("name"),
        team.get("key"),
        _int_or_none(team.get("mlbId")),
        market.get("key"),
        _float_or_none(selection.get("line")),
        selection.get("side"),
        _float_or_none(selection.get("odds")),
        _float_or_none(selection.get("overOdds")),
        _float_or_none(selection.get("underOdds")),
        selection.get("selection"),
        1 if selection.get("valid") else 0,
        _json_dumps(selection.get("validationIssues") or []),
        selection.get("rationale"),
        _json_dumps(selection),
    )


def _prop_row(row: sqlite3.Row) -> dict[str, Any]:
    raw = _json_loads(row["raw_json"])
    recent_history = raw.get("recentHistory") or {}
    mlb_profile = raw.get("mlbProfile") or {}
    profile_player = mlb_profile.get("player") or {}
    return {
        "id": row["id"],
        "capturedAt": row["captured_at"],
        "snapshotPhase": row["snapshot_phase"],
        "snapshotLabel": row["snapshot_label"],
        "date": row["slate_date"],
        "timezone": row["timezone"],
        "propId": row["prop_id"],
        "fixtureSlug": row["fixture_slug"],
        "game": row["game"],
        "mlbGamePk": row["mlb_game_pk"],
        "playerName": row["player_name"],
        "playerKey": row["player_key"],
        "playerMlbId": row["player_mlb_id"],
        "teamName": row["team_name"],
        "teamKey": row["team_key"],
        "teamMlbId": row["team_mlb_id"],
        "marketName": row["market_name"],
        "marketKey": row["market_key"],
        "line": row["line"],
        "overOdds": row["over_odds"],
        "underOdds": row["under_odds"],
        "matchStatus": row["match_status"],
        "statKey": row["stat_key"],
        "seasonValue": row["season_value"],
        "recentTotal": row["recent_total"],
        "recentPerGame": row["recent_per_game"],
        "gamesUsed": row["games_used"],
        "recentHistory": recent_history,
        "recentGames": recent_history.get("games") or [],
        "seasonStats": profile_player.get("stats") or {},
    }


def _analysis_row(row: sqlite3.Row) -> dict[str, Any]:
    raw = _json_loads(row["raw_json"])
    return {
        "id": row["id"],
        "capturedAt": row["captured_at"],
        "snapshotPhase": row["snapshot_phase"],
        "snapshotLabel": row["snapshot_label"],
        "date": row["slate_date"],
        "propId": row["prop_id"],
        "fixtureSlug": raw.get("fixtureSlug") or _fixture_slug_from_prop_id(row["prop_id"]),
        "game": raw.get("game"),
        "playerName": row["player_name"],
        "teamName": row["team_name"],
        "marketKey": row["market_key"],
        "line": row["line"],
        "bucket": row["bucket"],
        "lean": row["lean"],
        "edge": row["edge"],
        "score": row["score"],
        "confidence": row["confidence"],
        "marketProfile": row["market_profile"],
        "marketThreshold": row["market_threshold"],
        "overOdds": raw.get("overOdds"),
        "underOdds": raw.get("underOdds"),
        "statKey": raw.get("statKey") or raw.get("marketKey"),
        "recentPerGame": raw.get("recentPerGame"),
        "seasonValue": raw.get("seasonValue"),
        "seasonPerGame": raw.get("seasonPerGame"),
        "seasonEdge": raw.get("seasonEdge"),
        "gamesUsed": raw.get("gamesUsed"),
        "recentGames": raw.get("recentGames") or [],
        "seasonStats": raw.get("seasonStats") or {},
        "riskFlags": _json_loads(row["risk_flags_json"]),
        "reasons": _json_loads(row["reasons_json"]),
    }


def _recommendation_request_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "requestId": row["request_id"],
        "capturedAt": row["captured_at"],
        "source": row["source"],
        "matchup": row["matchup"],
        "date": row["slate_date"],
        "timezone": row["timezone"],
        "diversityMode": row["diversity_mode"],
        "filters": _json_loads(row["filters_json"]),
        "requestParams": _json_loads(row["request_params_json"]),
        "recommendationDiagnostics": _json_loads(row["diagnostics_json"]),
        "concentrationTags": _json_loads(row["concentration_tags_json"]),
        "matchedFixtureCount": row["matched_fixture_count"],
        "availablePropCount": row["available_prop_count"],
        "matchedPropCount": row["matched_prop_count"],
        "unmatchedPropCount": row["unmatched_prop_count"],
        "recommendationCount": row["recommendation_count"],
        "parlay": _json_loads(row["parlay_json"]),
        "notes": _json_loads(row["notes_json"]),
        "raw": _json_loads(row["raw_json"]),
    }


def _recommendation_leg_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "requestId": row["request_id"],
        "capturedAt": row["captured_at"],
        "date": row["slate_date"],
        "matchup": row["matchup"],
        "rank": row["rank"],
        "propId": row["prop_id"],
        "fixtureSlug": row["fixture_slug"],
        "game": row["game"],
        "mlbGamePk": row["mlb_game_pk"],
        "playerName": row["player_name"],
        "playerKey": row["player_key"],
        "playerMlbId": row["player_mlb_id"],
        "teamName": row["team_name"],
        "teamKey": row["team_key"],
        "teamMlbId": row["team_mlb_id"],
        "marketKey": row["market_key"],
        "statKey": row["stat_key"],
        "line": row["line"],
        "side": row["side"],
        "lean": row["lean"],
        "odds": row["odds"],
        "overOdds": row["over_odds"],
        "underOdds": row["under_odds"],
        "edge": row["edge"],
        "score": row["score"],
        "confidence": row["confidence"],
        "selection": row["selection"],
        "diversityMode": row["diversity_mode"],
        "riskFlags": _json_loads(row["risk_flags_json"]),
        "reasons": _json_loads(row["reasons_json"]),
        "contextualTags": _json_loads(row["contextual_tags_json"]),
        "deferredLayers": _json_loads(row["deferred_layers_json"]),
        "concentrationTags": _json_loads(row["concentration_tags_json"]),
        "raw": _json_loads(row["raw_json"]),
    }


def _recommendation_settlement_row(row: sqlite3.Row) -> dict[str, Any]:
    raw = _json_loads(row["raw_json"])
    return {
        "id": row["id"],
        "requestId": row["request_id"],
        "rank": row["leg_rank"],
        "propId": row["prop_id"],
        "date": row["slate_date"],
        "marketKey": row["market_key"],
        "side": row["side"],
        "actualValue": row["actual_value"],
        "actualResult": row["actual_result"],
        "overOutcome": row["over_outcome"],
        "decisionOutcome": row["decision_outcome"],
        "reasons": _json_loads(row["reasons_json"]),
        "settledAt": row["settled_at"],
        "raw": raw,
    }


def _gpt_decision_leg_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "decisionId": row["decision_id"],
        "capturedAt": row["captured_at"],
        "source": row["source"],
        "date": row["slate_date"],
        "matchup": row["matchup"],
        "rank": row["rank"],
        "propId": row["prop_id"],
        "selectionId": row["selection_id"],
        "fixtureSlug": row["fixture_slug"],
        "game": row["game"],
        "playerName": row["player_name"],
        "playerKey": row["player_key"],
        "playerMlbId": row["player_mlb_id"],
        "teamName": row["team_name"],
        "teamKey": row["team_key"],
        "teamMlbId": row["team_mlb_id"],
        "marketKey": row["market_key"],
        "line": row["line"],
        "side": row["side"],
        "odds": row["odds"],
        "overOdds": row["over_odds"],
        "underOdds": row["under_odds"],
        "selection": row["selection"],
        "valid": bool(row["valid"]),
        "validationIssues": _json_loads(row["validation_issues_json"]),
        "rationale": row["rationale"],
        "raw": _json_loads(row["raw_json"]),
    }


def _player_stat_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "capturedAt": row["captured_at"],
        "snapshotPhase": row["snapshot_phase"],
        "snapshotLabel": row["snapshot_label"],
        "playerMlbId": row["player_mlb_id"],
        "playerName": row["player_name"],
        "season": row["season"],
        "groupName": row["group_name"],
        "stats": _json_loads(row["stats_json"]),
        "recentHistory": _json_loads(row["recent_history_json"]),
    }


def _movement_changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes = []
    for previous, current in zip(rows, rows[1:]):
        changes.append(
            {
                "fromCapturedAt": previous["capturedAt"],
                "toCapturedAt": current["capturedAt"],
                "line": _delta(current["line"], previous["line"]),
                "overOdds": _delta(current["overOdds"], previous["overOdds"]),
                "underOdds": _delta(current["underOdds"], previous["underOdds"]),
            }
        )
    return changes


def _captured_at(value: datetime | None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _clean_snapshot_phase(value: str | None) -> str:
    cleaned = str(value or "manual").strip().lower()
    return cleaned or "manual"


def _clean_snapshot_label(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _delta(current: Any, previous: Any) -> float | None:
    current_value = _float_or_none(current)
    previous_value = _float_or_none(previous)
    if current_value is None or previous_value is None:
        return None
    return round(current_value - previous_value, 4)


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str) -> Any:
    return json.loads(value)


def _fixture_slug_from_prop_id(prop_id: Any) -> str | None:
    text = str(prop_id or "").strip()
    if not text:
        return None
    if ":" in text:
        return text.split(":", 1)[0]
    return None


def _clean_limit(limit: int) -> int:
    return max(1, min(int(limit), 500))
