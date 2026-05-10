from __future__ import annotations

from pathlib import Path

from app.supabase_ledger import _gpt_decision_payloads, supabase_ledger_enabled


def test_supabase_ledger_enabled_requires_url_and_service_key(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert supabase_ledger_enabled() is False

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    assert supabase_ledger_enabled() is True


def test_gpt_decision_payloads_match_supabase_tables():
    requests, legs = _gpt_decision_payloads(
        response={
            "generatedAt": "2026-05-10T00:00:00Z",
            "matchup": "Blue Jays vs Angels",
            "date": "2026-05-08",
            "validation": {"valid": True},
            "selections": [
                {
                    "selectionId": "prop-1:under",
                    "propId": "prop-1",
                    "fixtureSlug": "blue-jays-angels",
                    "player": {"name": "George Springer"},
                    "team": {"name": "Toronto Blue Jays"},
                    "market": {"key": "hits", "name": "hits"},
                    "side": "under",
                    "line": 0.5,
                    "odds": 2.9,
                    "playable": True,
                    "availability": {"status": "active"},
                }
            ],
        },
        decision_id="decision-1",
        request_body={"prompt": "Pick under hits."},
    )

    assert requests[0]["decision_id"] == "decision-1"
    assert requests[0]["source"] == "custom_gpt"
    assert legs[0]["leg_id"] == "decision-1:1"
    assert legs[0]["selection_id"] == "prop-1:under"
    assert legs[0]["market_key"] == "hits"
    assert legs[0]["playable"] is True


def test_supabase_schema_can_upgrade_existing_decision_tables():
    sql = Path("supabase/gpt_action.sql").read_text(encoding="utf-8").lower()

    assert "alter table public.gpt_decision_requests" in sql
    assert "add column if not exists request_json" in sql
    assert "add column if not exists response_json" in sql
    assert "add column if not exists validation_json" in sql
