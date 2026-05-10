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
                    prompt, request_json, response_json, validation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            for rank, selection in enumerate(selections, start=1):
                conn.execute(
                    """
                    INSERT INTO gpt_decision_legs (
                        leg_id, decision_id, rank, captured_at, slate_date, matchup,
                        selection_id, prop_id, fixture_slug, player_name, team_name,
                        market_key, market_name, side, line, odds, playable, status,
                        selection_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    validation_json TEXT NOT NULL
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
                """
            )

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
