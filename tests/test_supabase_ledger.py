from __future__ import annotations

from pathlib import Path

from app.supabase_ledger import supabase_ledger_enabled


def test_supabase_ledger_enabled_requires_url_and_service_key(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert supabase_ledger_enabled() is False

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    assert supabase_ledger_enabled() is True


def test_supabase_schema_keeps_market_mapping_and_history_tables():
    sql = Path("supabase/gpt_action.sql").read_text(encoding="utf-8").lower()

    assert "create table if not exists public.market_mappings" in sql
    assert "create table if not exists public.gpt_decision_requests" not in sql
    assert "create table if not exists public.gpt_decision_legs" not in sql
    assert "create table if not exists public.bet_history_imports" in sql
    assert "create table if not exists public.bet_history_raw" in sql
    assert "create table if not exists public.bet_history_legs" in sql
    assert "source_fingerprint" in sql
    assert "fingerprint_version" in sql
    assert "parser_version" in sql
    assert "eligibility_version" in sql
    assert "bet_history_imports_fingerprint_idx" in sql
    assert "bet_history_legs_ticket_idx" in sql
    assert "bet_history_legs_player_idx" in sql
