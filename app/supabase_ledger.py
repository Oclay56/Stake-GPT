from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx


class SupabaseLedgerError(RuntimeError):
    pass


def supabase_ledger_enabled() -> bool:
    return bool(_supabase_url() and _supabase_service_key())


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
