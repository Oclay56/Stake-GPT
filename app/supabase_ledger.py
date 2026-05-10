from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx


class SupabaseLedgerError(RuntimeError):
    pass


def supabase_ledger_enabled() -> bool:
    return bool(_supabase_url() and _supabase_service_key())


async def sync_gpt_decision_to_supabase(
    response: dict[str, Any],
    decision_id: str,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    request_rows, leg_rows = _gpt_decision_payloads(
        response=response,
        decision_id=decision_id,
        request_body=request_body,
    )
    request_result = await _post_rows(
        "gpt_decision_requests",
        request_rows,
        on_conflict="decision_id",
    )
    leg_result = await _post_rows(
        "gpt_decision_legs",
        leg_rows,
        on_conflict="leg_id",
    )
    return {
        "synced": True,
        "requests": request_result,
        "legs": leg_result,
    }


async def sync_market_mappings_to_supabase(
    mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    rows = [
        {
            "sport": mapping.get("sport") or "mlb",
            "stake_display_name": mapping.get("stakeDisplayName"),
            "internal_market_key": mapping.get("internalMarketKey"),
            "stat_key": mapping.get("statKey"),
            "group_name": mapping.get("group"),
            "last_seen_at": _utc_now(),
            "active": bool(mapping.get("active", True)),
            "examples": mapping.get("examples") or [],
        }
        for mapping in mappings
    ]
    result = await _post_rows(
        "market_mappings",
        rows,
        on_conflict="sport,stake_display_name,internal_market_key",
    )
    return {"synced": True, "marketMappings": result}


async def _post_rows(
    table: str,
    rows: list[dict[str, Any]],
    on_conflict: str,
) -> dict[str, Any]:
    if not rows:
        return {"table": table, "rowCount": 0}

    url = _supabase_url()
    service_key = _supabase_service_key()
    if not url or not service_key:
        raise SupabaseLedgerError("Supabase URL and service role key are required.")

    endpoint = f"{url.rstrip('/')}/rest/v1/{table}"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            endpoint,
            params={"on_conflict": on_conflict},
            headers=headers,
            json=rows,
        )
    if response.status_code >= 400:
        raise SupabaseLedgerError(
            f"Supabase {table} sync failed: {response.status_code} {response.text}"
        )
    return {"table": table, "rowCount": len(rows)}


def _gpt_decision_payloads(
    response: dict[str, Any],
    decision_id: str,
    request_body: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    captured_at = response.get("generatedAt") or _utc_now()
    request_rows = [
        {
            "decision_id": decision_id,
            "captured_at": captured_at,
            "source": "custom_gpt",
            "matchup": response.get("matchup") or request_body.get("matchup"),
            "slate_date": response.get("date") or request_body.get("date"),
            "prompt": request_body.get("prompt"),
            "request_json": request_body,
            "response_json": response,
            "validation_json": response.get("validation") or {},
        }
    ]
    leg_rows = [
        _gpt_decision_leg_payload(
            decision_id=decision_id,
            captured_at=captured_at,
            slate_date=response.get("date") or request_body.get("date"),
            matchup=response.get("matchup") or request_body.get("matchup"),
            rank=rank,
            selection=selection,
        )
        for rank, selection in enumerate(response.get("selections") or [], start=1)
    ]
    return request_rows, leg_rows


def _gpt_decision_leg_payload(
    decision_id: str,
    captured_at: str,
    slate_date: str | None,
    matchup: str | None,
    rank: int,
    selection: dict[str, Any],
) -> dict[str, Any]:
    player = selection.get("player") or {}
    team = selection.get("team") or {}
    market = selection.get("market") or {}
    availability = selection.get("availability") or {}
    return {
        "leg_id": f"{decision_id}:{rank}",
        "decision_id": decision_id,
        "rank": rank,
        "captured_at": captured_at,
        "slate_date": slate_date,
        "matchup": matchup,
        "selection_id": selection.get("selectionId"),
        "prop_id": selection.get("propId"),
        "fixture_slug": selection.get("fixtureSlug"),
        "player_name": player.get("name"),
        "team_name": team.get("name"),
        "market_key": market.get("key"),
        "market_name": market.get("name"),
        "side": selection.get("side"),
        "line": _float_or_none(selection.get("line")),
        "odds": _float_or_none(selection.get("odds")),
        "playable": bool(selection.get("playable")),
        "status": availability.get("status") or selection.get("status"),
        "selection_json": selection,
    }


def _supabase_url() -> str | None:
    return os.getenv("SUPABASE_URL") or None


def _supabase_service_key() -> str | None:
    return (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
        or None
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
