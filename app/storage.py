from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data") / "gpt_action.sqlite"


class GptActionStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        configured_path = db_path or os.getenv("AZP_DB_PATH") or DEFAULT_DB_PATH
        self.db_path = Path(configured_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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

    def create_slip_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO slip_jobs (
                    job_id, created_at, updated_at, status, source, bridge_id,
                    claimed_at, completed_at, matchup, slate_date, slip_type,
                    mode, prompt, target_json, selections_json, request_json,
                    result_json, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    now,
                    now,
                    "pending",
                    job.get("source") or "custom_gpt",
                    None,
                    None,
                    None,
                    job.get("matchup"),
                    job.get("date"),
                    job.get("slipType") or "review_slip",
                    job.get("mode"),
                    job.get("prompt"),
                    _json_dumps(job.get("target") or {}),
                    _json_dumps(job.get("selections") or []),
                    _json_dumps(job.get("request") or job),
                    _json_dumps({}),
                    None,
                ),
            )
            conn.commit()
        created = self.get_slip_job(job_id)
        if created is None:
            raise RuntimeError("Slip job insert failed.")
        return created

    def get_slip_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM slip_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _slip_job_row(row) if row else None

    def claim_next_slip_job(self, bridge_id: str) -> dict[str, Any] | None:
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM slip_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            job_id = row["job_id"]
            conn.execute(
                """
                UPDATE slip_jobs
                SET status = 'claimed',
                    bridge_id = ?,
                    claimed_at = ?,
                    updated_at = ?,
                    message = ?
                WHERE job_id = ?
                """,
                (
                    bridge_id,
                    now,
                    now,
                    f"Claimed by {bridge_id}",
                    job_id,
                ),
            )
            conn.commit()
        return self.get_slip_job(job_id)

    def update_slip_job_status(
        self,
        job_id: str,
        status: str,
        bridge_id: str | None = None,
        message: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        completed_at = now if status in {"built", "blocked", "failed", "cancelled"} else None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE slip_jobs
                SET status = ?,
                    bridge_id = COALESCE(?, bridge_id),
                    updated_at = ?,
                    completed_at = COALESCE(?, completed_at),
                    message = COALESCE(?, message),
                    result_json = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    bridge_id,
                    now,
                    completed_at,
                    message,
                    _json_dumps(result or {}),
                    job_id,
                ),
            )
            conn.commit()
        updated = self.get_slip_job(job_id)
        if updated is None:
            raise KeyError(f"Slip job not found: {job_id}")
        return updated

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

                CREATE TABLE IF NOT EXISTS slip_jobs (
                    job_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    bridge_id TEXT,
                    claimed_at TEXT,
                    completed_at TEXT,
                    matchup TEXT,
                    slate_date TEXT,
                    slip_type TEXT,
                    mode TEXT,
                    prompt TEXT,
                    target_json TEXT NOT NULL DEFAULT '{}',
                    selections_json TEXT NOT NULL DEFAULT '[]',
                    request_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    message TEXT
                );

                CREATE INDEX IF NOT EXISTS slip_jobs_status_created_idx
                    ON slip_jobs (status, created_at);
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
            _ensure_column(conn, "slip_jobs", "bridge_id", "TEXT")
            _ensure_column(conn, "slip_jobs", "claimed_at", "TEXT")
            _ensure_column(conn, "slip_jobs", "completed_at", "TEXT")
            _ensure_column(conn, "slip_jobs", "result_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(conn, "slip_jobs", "message", "TEXT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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


def _slip_job_row(row: sqlite3.Row) -> dict[str, Any]:
    selections = _json_loads(row["selections_json"]) or []
    return {
        "jobId": row["job_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "status": row["status"],
        "source": row["source"],
        "bridgeId": row["bridge_id"],
        "claimedAt": row["claimed_at"],
        "completedAt": row["completed_at"],
        "matchup": row["matchup"],
        "date": row["slate_date"],
        "slipType": row["slip_type"],
        "mode": row["mode"],
        "prompt": row["prompt"],
        "target": _json_loads(row["target_json"]) or {},
        "selections": selections,
        "legCount": len(selections),
        "request": _json_loads(row["request_json"]) or {},
        "result": _json_loads(row["result_json"]) or {},
        "message": row["message"],
        "safety": {
            "wagerAmountSupported": False,
            "betSubmissionSupported": False,
            "requiresManualReview": True,
        },
    }


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


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def _json_loads(value: str) -> Any:
    return json.loads(value) if value else None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_limit(limit: int) -> int:
    return max(1, min(int(limit), 500))


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
