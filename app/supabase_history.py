from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


class SupabaseHistoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoryTable:
    name: str
    primary_key: str
    json_columns: frozenset[str] = frozenset()
    bool_columns: frozenset[str] = frozenset()


HISTORY_TABLES: tuple[HistoryTable, ...] = (
    HistoryTable(
        "bet_history_imports",
        "import_id",
        json_columns=frozenset({"report_json"}),
    ),
    HistoryTable(
        "bet_history_raw",
        "raw_id",
        json_columns=frozenset({"raw_json", "parse_notes_json"}),
    ),
    HistoryTable(
        "bet_history_legs",
        "history_leg_id",
        json_columns=frozenset({"parse_notes_json", "ignored_fields_json", "normalized_json", "raw_json"}),
        bool_columns=frozenset({"needs_review", "training_eligible"}),
    ),
    HistoryTable(
        "bet_history_game_snapshots",
        "game_pk",
        json_columns=frozenset(
            {
                "venue_json",
                "probable_pitchers_json",
                "pregame_context_json",
                "grading_context_json",
                "raw_context_json",
            }
        ),
    ),
    HistoryTable(
        "bet_history_leg_enrichments",
        "history_leg_id",
        json_columns=frozenset({"pregame_context_json", "grading_context_json", "notes_json"}),
        bool_columns=frozenset({"lineup_confirmed", "confirmed_starter"}),
    ),
)


HISTORY_TABLE_BY_NAME = {table.name: table for table in HISTORY_TABLES}
DEFAULT_PAGE_SIZE = 1000
DEFAULT_PUSH_CHUNK_SIZE = 500


def supabase_history_enabled(*, explicit_db_path: bool = False) -> bool:
    mode = _history_store_mode()
    if mode in {"sqlite", "local", "off", "disabled"}:
        return False
    if explicit_db_path and mode != "supabase":
        return False
    return bool(_supabase_url() and _supabase_service_key())


def sync_sqlite_history_to_supabase(
    db_path: str | Path,
    *,
    table_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    url = _require_supabase_url()
    service_key = _require_service_key()
    tables = _selected_tables(table_names)
    result = {
        "enabled": True,
        "direction": "sqlite_to_supabase",
        "syncedAt": _utc_now(),
        "tables": {},
        "rowsPushed": 0,
    }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        with httpx.Client(timeout=60) as client:
            for table in tables:
                rows = conn.execute(f"SELECT * FROM {table.name}").fetchall()
                pushed = 0
                for chunk in _chunks(
                    [_sqlite_row_to_supabase_payload(row, table) for row in rows],
                    DEFAULT_PUSH_CHUNK_SIZE,
                ):
                    _post_rows(client, url, service_key, table, chunk)
                    pushed += len(chunk)
                result["tables"][table.name] = {"rowsPushed": pushed}
                result["rowsPushed"] += pushed
    return result


def sync_supabase_history_to_sqlite(
    db_path: str | Path,
    *,
    table_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    url = _require_supabase_url()
    service_key = _require_service_key()
    tables = _selected_tables(table_names)
    result = {
        "enabled": True,
        "direction": "supabase_to_sqlite",
        "syncedAt": _utc_now(),
        "tables": {},
        "rowsPulled": 0,
    }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        with httpx.Client(timeout=60) as client:
            for table in tables:
                rows = _fetch_all_rows(client, url, service_key, table.name)
                merged = 0
                for row in rows:
                    _upsert_sqlite_row(conn, table, row)
                    merged += 1
                conn.commit()
                result["tables"][table.name] = {"rowsPulled": len(rows), "rowsMerged": merged}
                result["rowsPulled"] += len(rows)
    return result


def delete_supabase_history_import(import_id: str) -> dict[str, Any]:
    clean_id = str(import_id or "").strip()
    if not clean_id:
        return {"deleted": False, "reason": "missing_import_id"}
    url = _require_supabase_url()
    service_key = _require_service_key()
    endpoint = f"{url.rstrip('/')}/rest/v1/bet_history_imports"
    with httpx.Client(timeout=30) as client:
        response = client.delete(
            endpoint,
            params={"import_id": f"eq.{clean_id}"},
            headers={**_headers(service_key), "Prefer": "return=minimal"},
        )
    if response.status_code >= 400:
        raise SupabaseHistoryError(
            f"Supabase bet_history_imports delete failed: {response.status_code} {response.text}"
        )
    return {"deleted": True, "importId": clean_id}


def _history_store_mode() -> str:
    return (
        os.getenv("AZP_BET_HISTORY_STORE")
        or os.getenv("BET_HISTORY_STORAGE")
        or "auto"
    ).strip().lower()


def _selected_tables(table_names: list[str] | tuple[str, ...] | None) -> tuple[HistoryTable, ...]:
    if not table_names:
        return HISTORY_TABLES
    selected: list[HistoryTable] = []
    for name in table_names:
        table = HISTORY_TABLE_BY_NAME.get(str(name))
        if not table:
            raise SupabaseHistoryError(f"Unknown bet history table: {name}")
        selected.append(table)
    return tuple(selected)


def _fetch_all_rows(
    client: httpx.Client,
    url: str,
    service_key: str,
    table_name: str,
) -> list[dict[str, Any]]:
    endpoint = f"{url.rstrip('/')}/rest/v1/{table_name}"
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = client.get(
            endpoint,
            params={"select": "*"},
            headers={
                **_headers(service_key),
                "Range-Unit": "items",
                "Range": f"{offset}-{offset + DEFAULT_PAGE_SIZE - 1}",
            },
        )
        if response.status_code >= 400:
            raise SupabaseHistoryError(
                f"Supabase {table_name} fetch failed: {response.status_code} {response.text}"
            )
        page = response.json()
        if not isinstance(page, list):
            raise SupabaseHistoryError(f"Supabase {table_name} fetch returned non-list payload.")
        rows.extend(page)
        if len(page) < DEFAULT_PAGE_SIZE:
            break
        offset += DEFAULT_PAGE_SIZE
    return rows


def _post_rows(
    client: httpx.Client,
    url: str,
    service_key: str,
    table: HistoryTable,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    endpoint = f"{url.rstrip('/')}/rest/v1/{table.name}"
    response = client.post(
        endpoint,
        params={"on_conflict": table.primary_key},
        headers={
            **_headers(service_key),
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=rows,
    )
    if response.status_code >= 400:
        raise SupabaseHistoryError(
            f"Supabase {table.name} sync failed: {response.status_code} {response.text}"
        )


def _sqlite_row_to_supabase_payload(row: sqlite3.Row, table: HistoryTable) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in table.json_columns:
            payload[key] = _json_value(value, column=key)
        elif key in table.bool_columns:
            payload[key] = bool(value)
        else:
            payload[key] = value
    return payload


def _upsert_sqlite_row(conn: sqlite3.Connection, table: HistoryTable, source: dict[str, Any]) -> None:
    sqlite_columns = _sqlite_columns(conn, table.name)
    row: dict[str, Any] = {}
    for column in sqlite_columns:
        if column not in source:
            continue
        row[column] = _supabase_value_to_sqlite(source[column], column, table)
    if table.primary_key not in row:
        raise SupabaseHistoryError(f"Supabase {table.name} row is missing {table.primary_key}.")
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(
        f"{column} = excluded.{column}"
        for column in columns
        if column != table.primary_key
    )
    conflict_action = f"DO UPDATE SET {assignments}" if assignments else "DO NOTHING"
    sql = (
        f"INSERT INTO {table.name} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT({table.primary_key}) {conflict_action}"
    )
    conn.execute(sql, [row[column] for column in columns])


def _sqlite_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [row["name"] for row in rows]


def _supabase_value_to_sqlite(value: Any, column: str, table: HistoryTable) -> Any:
    if column in table.json_columns:
        return json.dumps(
            value if value is not None else _json_default_for_column(column),
            ensure_ascii=True,
            separators=(",", ":"),
            default=str,
        )
    if column in table.bool_columns:
        return 1 if value else 0
    return value


def _json_value(value: Any, *, column: str) -> Any:
    if value is None or value == "":
        return _json_default_for_column(column)
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _json_default_for_column(column: str) -> Any:
    if column.endswith("_notes_json") or column in {"notes_json", "ignored_fields_json"}:
        return []
    return {}


def _chunks(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def _headers(service_key: str) -> dict[str, str]:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }


def _supabase_url() -> str | None:
    return os.getenv("SUPABASE_URL") or None


def _supabase_service_key() -> str | None:
    return (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
        or None
    )


def _require_supabase_url() -> str:
    url = _supabase_url()
    if not url:
        raise SupabaseHistoryError("SUPABASE_URL is required for Supabase bet history storage.")
    return url


def _require_service_key() -> str:
    service_key = _supabase_service_key()
    if not service_key:
        raise SupabaseHistoryError(
            "SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SERVICE_KEY is required for Supabase bet history storage."
        )
    return service_key


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
