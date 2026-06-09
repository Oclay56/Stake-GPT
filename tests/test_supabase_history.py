from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.bet_history import parse_history_rows
from app.storage import GptActionStore
from app.supabase_history import (
    sync_sqlite_history_to_supabase,
    sync_supabase_history_to_sqlite,
    supabase_history_enabled,
)


class FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: Any = None, text: str = "ok") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self) -> Any:
        return self._payload


class FakeSupabaseClient:
    def __init__(self, table_payloads: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.table_payloads = table_payloads or {}
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeSupabaseClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, endpoint: str, **kwargs: Any) -> FakeResponse:
        self.posts.append({"endpoint": endpoint, **kwargs})
        return FakeResponse(status_code=201)

    def get(self, endpoint: str, **kwargs: Any) -> FakeResponse:
        table_name = endpoint.rstrip("/").split("/")[-1]
        self.gets.append({"endpoint": endpoint, **kwargs})
        return FakeResponse(payload=self.table_payloads.get(table_name, []))

    def delete(self, endpoint: str, **kwargs: Any) -> FakeResponse:
        self.deletes.append({"endpoint": endpoint, **kwargs})
        return FakeResponse(status_code=204)


def test_supabase_history_enabled_is_explicit_for_test_db_paths(monkeypatch):
    monkeypatch.delenv("AZP_BET_HISTORY_STORE", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")

    assert supabase_history_enabled(explicit_db_path=False) is True
    assert supabase_history_enabled(explicit_db_path=True) is False

    monkeypatch.setenv("AZP_BET_HISTORY_STORE", "supabase")
    assert supabase_history_enabled(explicit_db_path=True) is True

    monkeypatch.setenv("AZP_BET_HISTORY_STORE", "sqlite")
    assert supabase_history_enabled(explicit_db_path=False) is False


def test_sqlite_history_push_converts_json_and_booleans(tmp_path, monkeypatch):
    monkeypatch.delenv("AZP_BET_HISTORY_STORE", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    db_path = tmp_path / "history.sqlite"
    store = GptActionStore(db_path)
    parsed = parse_history_rows(
        [
            {
                "date": "2026-06-02",
                "player": "George Springer",
                "market": "Hits",
                "side": "Under",
                "line": 0.5,
                "odds": 1.91,
                "result": "Won",
                "actual_stat": 0,
            }
        ],
        source_format="json",
        source_path="inline",
    )
    store.save_bet_history_import(parsed)
    fake = FakeSupabaseClient()

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setattr("app.supabase_history.httpx.Client", lambda **_: fake)

    result = sync_sqlite_history_to_supabase(db_path, table_names=("bet_history_legs",))

    assert result["rowsPushed"] == 1
    pushed = fake.posts[0]["json"][0]
    assert pushed["needs_review"] is False
    assert pushed["training_eligible"] is True
    assert isinstance(pushed["parse_notes_json"], list)
    assert isinstance(pushed["normalized_json"], dict)
    assert pushed["normalized_json"]["playerName"] == "George Springer"


def test_supabase_history_pull_merges_rows_into_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("AZP_BET_HISTORY_STORE", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    db_path = tmp_path / "history.sqlite"
    GptActionStore(db_path)
    fake = FakeSupabaseClient(
        {
            "bet_history_imports": [
                {
                    "import_id": "import-1",
                    "imported_at": "2026-06-08T00:00:00Z",
                    "source_path": "supabase",
                    "source_format": "json",
                    "source_fingerprint": "fingerprint-1",
                    "fingerprint_version": "v1",
                    "parser_version": "parser",
                    "eligibility_version": "eligibility",
                    "raw_row_count": 1,
                    "parsed_leg_count": 1,
                    "needs_review_count": 0,
                    "report_json": {"parsedLegs": 1},
                }
            ],
            "bet_history_raw": [
                {
                    "raw_id": "raw-1",
                    "import_id": "import-1",
                    "source_row_number": 1,
                    "source_format": "json",
                    "raw_text": "{}",
                    "raw_json": {"player": "George Springer"},
                    "parse_status": "parsed",
                    "parse_notes_json": [],
                }
            ],
            "bet_history_legs": [
                {
                    "history_leg_id": "leg-1",
                    "import_id": "import-1",
                    "raw_id": "raw-1",
                    "ticket_id": "ticket-1",
                    "leg_index": 1,
                    "bet_date": "2026-06-02",
                    "settled_date": None,
                    "sport": "mlb",
                    "league": "MLB",
                    "player_name": "George Springer",
                    "team_name": "Toronto Blue Jays",
                    "opponent_name": "New York Yankees",
                    "fixture_slug": None,
                    "matchup": "New York Yankees - Toronto Blue Jays",
                    "market_key": "hits",
                    "market_name": "Hits",
                    "side": "under",
                    "line": 0.5,
                    "odds": 1.91,
                    "stake_amount": None,
                    "payout_amount": None,
                    "result_status": "won",
                    "actual_stat": 0,
                    "parse_confidence": 1.0,
                    "parse_confidence_label": "high",
                    "needs_review": False,
                    "training_eligible": True,
                    "parser_version": "parser",
                    "eligibility_version": "eligibility",
                    "parse_notes_json": [],
                    "ignored_fields_json": [],
                    "normalized_json": {"playerName": "George Springer"},
                    "raw_json": {"player": "George Springer"},
                    "created_at": "2026-06-08T00:00:00Z",
                }
            ],
        }
    )

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setattr("app.supabase_history.httpx.Client", lambda **_: fake)

    result = sync_supabase_history_to_sqlite(
        db_path,
        table_names=("bet_history_imports", "bet_history_raw", "bet_history_legs"),
    )

    assert result["rowsPulled"] == 3
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bet_history_legs WHERE history_leg_id = 'leg-1'").fetchone()
    assert row["player_name"] == "George Springer"
    assert row["needs_review"] == 0
    assert row["training_eligible"] == 1


def test_supabase_schema_contains_historic_tables():
    sql = Path("supabase/gpt_action.sql").read_text(encoding="utf-8").lower()

    assert "create table if not exists public.bet_history_imports" in sql
    assert "create table if not exists public.bet_history_game_snapshots" in sql
    assert "create table if not exists public.bet_history_leg_enrichments" in sql
