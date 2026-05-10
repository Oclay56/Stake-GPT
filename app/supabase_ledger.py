from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .recommendations import summarize_recommendation_rows


class SupabaseLedgerError(RuntimeError):
    pass


def supabase_ledger_enabled() -> bool:
    backend = os.getenv("AZP_RECOMMENDATION_LEDGER_BACKEND", "auto").strip().lower()
    if backend in {"0", "false", "none", "off", "sqlite"}:
        return False
    if backend in {"supabase", "dual"}:
        return bool(_supabase_url() and _supabase_service_key())
    return bool(_supabase_url() and _supabase_service_key())


async def sync_recommendation_result_to_supabase(
    response: dict[str, Any],
    request_id: str,
    request_params: dict[str, Any],
) -> dict[str, Any]:
    url = _supabase_url()
    key = _supabase_service_key()
    if not url or not key:
        return {"enabled": False, "synced": False, "reason": "missing_supabase_env"}

    payload = _payloads(response, request_id, request_params)
    async with httpx.AsyncClient(timeout=10) as client:
        await _post_rows(client, url, key, "recommendation_requests", [payload["request"]])
        if payload["legs"]:
            await _post_rows(client, url, key, "recommendation_legs", payload["legs"])
    return {
        "enabled": True,
        "synced": True,
        "requestId": request_id,
        "legsSynced": len(payload["legs"]),
    }


async def sync_recommendation_settlements_to_supabase(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    url = _supabase_url()
    key = _supabase_service_key()
    if not url or not key:
        return {"enabled": False, "synced": False, "reason": "missing_supabase_env"}

    payloads = settlement_payloads(rows)
    async with httpx.AsyncClient(timeout=10) as client:
        if payloads:
            await _post_rows(client, url, key, "recommendation_settlements", payloads)
    return {
        "enabled": True,
        "synced": True,
        "settlementsSynced": len(payloads),
    }


async def sync_gpt_decision_to_supabase(
    response: dict[str, Any],
    decision_id: str,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    url = _supabase_url()
    key = _supabase_service_key()
    if not url or not key:
        return {"enabled": False, "synced": False, "reason": "missing_supabase_env"}

    payload = _gpt_decision_payloads(response, decision_id, request_body)
    async with httpx.AsyncClient(timeout=10) as client:
        await _post_rows(client, url, key, "gpt_decision_requests", [payload["request"]])
        if payload["legs"]:
            await _post_rows(client, url, key, "gpt_decision_legs", payload["legs"])
    return {
        "enabled": True,
        "synced": True,
        "decisionId": decision_id,
        "legsSynced": len(payload["legs"]),
    }


async def fetch_recommendation_performance_from_supabase(
    date_text: str | None = None,
    market: str | None = None,
    side: str | None = None,
    request_id: str | None = None,
    diversity_mode: str | None = None,
    limit: int = 500,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    url = _supabase_url()
    key = _supabase_service_key()
    if not url or not key:
        raise SupabaseLedgerError("Supabase ledger env is not configured.")

    params = _performance_query_params(
        date_text=date_text,
        market=market,
        side=side,
        request_id=request_id,
        diversity_mode=diversity_mode,
        limit=limit,
    )
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=10)
    try:
        leg_rows = await _get_rows(
            active_client,
            url,
            key,
            "recommendation_legs",
            params,
        )
        settlement_rows = await _get_rows(
            active_client,
            url,
            key,
            "recommendation_settlements",
            _settlement_query_params(
                date_text=date_text,
                market=market,
                side=side,
                request_id=request_id,
                limit=limit,
            ),
        )
    finally:
        if owns_client:
            await active_client.aclose()

    summary = summarize_recommendation_rows(
        [_remote_leg_row(row) for row in leg_rows],
        [_remote_settlement_row(row) for row in settlement_rows],
        date_text=date_text,
        market=market,
        side=side,
        request_id=request_id,
        diversity_mode=diversity_mode,
    )
    return {
        "source": "supabase",
        **summary,
    }


def settlement_payloads(
    rows: list[dict[str, Any]],
    settled_at: str | None = None,
) -> list[dict[str, Any]]:
    settled_text = settled_at or _utc_now()
    payloads = []
    for row in rows:
        request_id = row.get("requestId")
        rank = _int_or_none(row.get("rank"))
        if not request_id or rank is None:
            continue
        leg_id = f"{request_id}:{rank}"
        payloads.append(
            {
                "settlement_id": leg_id,
                "request_id": request_id,
                "leg_id": leg_id,
                "leg_rank": rank,
                "prop_id": row.get("propId"),
                "slate_date": row.get("date"),
                "market_key": row.get("marketKey"),
                "side": row.get("side"),
                "actual_value": _float_or_none(row.get("actualValue")),
                "actual_result": row.get("actualResult"),
                "over_outcome": row.get("overOutcome"),
                "decision_outcome": row.get("decisionOutcome"),
                "reasons": row.get("reasons") or [],
                "settled_at": settled_text,
                "raw": row,
            }
        )
    return payloads


async def _post_rows(
    client: httpx.AsyncClient,
    supabase_url: str,
    service_key: str,
    table: str,
    rows: list[dict[str, Any]],
) -> None:
    response = await client.post(
        f"{supabase_url.rstrip('/')}/rest/v1/{table}",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": _conflict_key(table)},
        json=rows,
    )
    if response.status_code >= 400:
        raise SupabaseLedgerError(
            f"Supabase ledger sync failed for {table}: {response.status_code}"
        )


async def _get_rows(
    client: httpx.AsyncClient,
    supabase_url: str,
    service_key: str,
    table: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    response = await client.get(
        f"{supabase_url.rstrip('/')}/rest/v1/{table}",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
        },
        params=params,
    )
    if response.status_code >= 400:
        raise SupabaseLedgerError(
            f"Supabase ledger fetch failed for {table}: {response.status_code}"
        )
    data = response.json()
    return data if isinstance(data, list) else []


def _payloads(
    response: dict[str, Any],
    request_id: str,
    request_params: dict[str, Any],
) -> dict[str, Any]:
    captured_at = _utc_now()
    filters = response.get("filters") or {}
    diagnostics = response.get("recommendationDiagnostics") or {}
    concentration_tags = diagnostics.get("concentrationTags") or []
    request = {
        "request_id": request_id,
        "captured_at": captured_at,
        "source": response.get("source"),
        "matchup": response.get("matchup"),
        "slate_date": response.get("date"),
        "timezone": response.get("timezone"),
        "diversity_mode": filters.get("diversityMode"),
        "filters": filters,
        "request_params": request_params,
        "diagnostics": diagnostics,
        "concentration_tags": concentration_tags,
        "matched_fixture_count": _int_or_none(response.get("matchedFixtureCount")),
        "available_prop_count": _int_or_none(response.get("availablePropCount")),
        "matched_prop_count": _int_or_none(response.get("matchedPropCount")),
        "unmatched_prop_count": _int_or_none(response.get("unmatchedPropCount")),
        "recommendation_count": _int_or_none(response.get("recommendationCount")),
        "parlay": response.get("parlay") or {},
        "notes": response.get("notes") or [],
        "raw": {**response, "requestId": request_id},
    }
    legs = [
        _leg_payload(
            recommendation,
            request_id=request_id,
            captured_at=captured_at,
            response=response,
            rank=index,
            concentration_tags=concentration_tags,
        )
        for index, recommendation in enumerate(response.get("recommendations") or [], start=1)
    ]
    return {"request": request, "legs": legs}


def _gpt_decision_payloads(
    response: dict[str, Any],
    decision_id: str,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    captured_at = _utc_now()
    request = {
        "decision_id": decision_id,
        "captured_at": captured_at,
        "source": response.get("source"),
        "matchup": response.get("matchup"),
        "slate_date": response.get("date"),
        "timezone": response.get("timezone"),
        "prompt": response.get("prompt"),
        "request_body": request_body,
        "validation": response.get("validation") or {},
        "notes": response.get("notes") or [],
        "raw": {**response, "decisionId": decision_id},
    }
    legs = [
        _gpt_decision_leg_payload(
            selection,
            decision_id=decision_id,
            captured_at=captured_at,
            response=response,
            rank=index,
        )
        for index, selection in enumerate(response.get("selections") or [], start=1)
    ]
    return {"request": request, "legs": legs}


def _gpt_decision_leg_payload(
    selection: dict[str, Any],
    decision_id: str,
    captured_at: str,
    response: dict[str, Any],
    rank: int,
) -> dict[str, Any]:
    player = selection.get("player") or {}
    team = selection.get("team") or {}
    market = selection.get("market") or {}
    clean_rank = _int_or_none(selection.get("rank")) or rank
    return {
        "decision_leg_id": f"{decision_id}:{clean_rank}",
        "decision_id": decision_id,
        "captured_at": captured_at,
        "source": response.get("source"),
        "slate_date": response.get("date"),
        "matchup": response.get("matchup"),
        "rank": clean_rank,
        "prop_id": selection.get("propId"),
        "selection_id": selection.get("selectionId"),
        "fixture_slug": selection.get("fixtureSlug"),
        "game": selection.get("game"),
        "player_name": player.get("name"),
        "player_key": player.get("key"),
        "player_mlb_id": _int_or_none(player.get("mlbId")),
        "team_name": team.get("name"),
        "team_key": team.get("key"),
        "team_mlb_id": _int_or_none(team.get("mlbId")),
        "market_key": market.get("key"),
        "line": _float_or_none(selection.get("line")),
        "side": selection.get("side"),
        "odds": _float_or_none(selection.get("odds")),
        "over_odds": _float_or_none(selection.get("overOdds")),
        "under_odds": _float_or_none(selection.get("underOdds")),
        "selection": selection.get("selection"),
        "valid": bool(selection.get("valid")),
        "validation_issues": selection.get("validationIssues") or [],
        "rationale": selection.get("rationale"),
        "raw": selection,
    }


def _leg_payload(
    recommendation: dict[str, Any],
    request_id: str,
    captured_at: str,
    response: dict[str, Any],
    rank: int,
    concentration_tags: list[str],
) -> dict[str, Any]:
    player = recommendation.get("player") or {}
    team = recommendation.get("team") or {}
    market = recommendation.get("market") or {}
    contextual = recommendation.get("contextualEdge") or {}
    mlb_game = recommendation.get("mlbGame") or {}
    clean_rank = _int_or_none(recommendation.get("rank")) or rank
    return {
        "leg_id": f"{request_id}:{clean_rank}",
        "request_id": request_id,
        "captured_at": captured_at,
        "slate_date": response.get("date"),
        "matchup": response.get("matchup"),
        "rank": clean_rank,
        "prop_id": recommendation.get("propId"),
        "fixture_slug": recommendation.get("fixtureSlug"),
        "game": recommendation.get("game"),
        "mlb_game_pk": _int_or_none(mlb_game.get("gamePk")),
        "player_name": recommendation.get("playerName") or player.get("name"),
        "player_key": player.get("key"),
        "player_mlb_id": _int_or_none(player.get("mlbId")),
        "team_name": recommendation.get("teamName") or team.get("name"),
        "team_key": team.get("key"),
        "team_mlb_id": _int_or_none(team.get("mlbId")),
        "market_key": recommendation.get("marketKey") or market.get("key"),
        "stat_key": recommendation.get("statKey"),
        "line": _float_or_none(recommendation.get("line")),
        "side": recommendation.get("side"),
        "lean": recommendation.get("lean"),
        "odds": _float_or_none(recommendation.get("odds")),
        "over_odds": _float_or_none(recommendation.get("overOdds")),
        "under_odds": _float_or_none(recommendation.get("underOdds")),
        "edge": _float_or_none(recommendation.get("edge")),
        "score": _int_or_none(recommendation.get("score")),
        "confidence": recommendation.get("confidence"),
        "selection": recommendation.get("selection"),
        "diversity_mode": (response.get("filters") or {}).get("diversityMode"),
        "risk_flags": recommendation.get("riskFlags") or [],
        "reasons": recommendation.get("reasons") or [],
        "contextual_tags": contextual.get("tags") or [],
        "deferred_layers": contextual.get("deferredLayers") or [],
        "concentration_tags": concentration_tags,
        "raw": recommendation,
    }


def _conflict_key(table: str) -> str:
    if table == "gpt_decision_requests":
        return "decision_id"
    if table == "gpt_decision_legs":
        return "decision_leg_id"
    if table == "recommendation_requests":
        return "request_id"
    if table == "recommendation_legs":
        return "leg_id"
    return "settlement_id"


def _performance_query_params(
    date_text: str | None,
    market: str | None,
    side: str | None,
    request_id: str | None,
    diversity_mode: str | None,
    limit: int,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "select": "*",
        "order": "captured_at.desc,request_id.desc,rank.asc",
        "limit": _clean_limit(limit),
    }
    if date_text:
        params["slate_date"] = f"eq.{date_text}"
    if market:
        params["market_key"] = f"eq.{market}"
    if side:
        params["side"] = f"eq.{side}"
    if request_id:
        params["request_id"] = f"eq.{request_id}"
    if diversity_mode:
        params["diversity_mode"] = f"eq.{diversity_mode}"
    return params


def _settlement_query_params(
    date_text: str | None,
    market: str | None,
    side: str | None,
    request_id: str | None,
    limit: int,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "select": "*",
        "order": "settled_at.desc,request_id.desc,leg_rank.asc",
        "limit": _clean_limit(limit),
    }
    if date_text:
        params["slate_date"] = f"eq.{date_text}"
    if market:
        params["market_key"] = f"eq.{market}"
    if side:
        params["side"] = f"eq.{side}"
    if request_id:
        params["request_id"] = f"eq.{request_id}"
    return params


def _remote_leg_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "requestId": row.get("request_id"),
        "capturedAt": row.get("captured_at"),
        "date": row.get("slate_date"),
        "matchup": row.get("matchup"),
        "rank": _int_or_none(row.get("rank")),
        "propId": row.get("prop_id"),
        "fixtureSlug": row.get("fixture_slug"),
        "game": row.get("game"),
        "mlbGamePk": _int_or_none(row.get("mlb_game_pk")),
        "playerName": row.get("player_name"),
        "playerKey": row.get("player_key"),
        "playerMlbId": _int_or_none(row.get("player_mlb_id")),
        "teamName": row.get("team_name"),
        "teamKey": row.get("team_key"),
        "teamMlbId": _int_or_none(row.get("team_mlb_id")),
        "marketKey": row.get("market_key"),
        "statKey": row.get("stat_key"),
        "line": _float_or_none(row.get("line")),
        "side": row.get("side"),
        "lean": row.get("lean"),
        "odds": _float_or_none(row.get("odds")),
        "overOdds": _float_or_none(row.get("over_odds")),
        "underOdds": _float_or_none(row.get("under_odds")),
        "edge": _float_or_none(row.get("edge")),
        "score": _int_or_none(row.get("score")),
        "confidence": row.get("confidence"),
        "selection": row.get("selection"),
        "diversityMode": row.get("diversity_mode"),
        "riskFlags": _json_array(row.get("risk_flags")),
        "reasons": _json_array(row.get("reasons")),
        "contextualTags": _json_array(row.get("contextual_tags")),
        "deferredLayers": _json_array(row.get("deferred_layers")),
        "concentrationTags": _json_array(row.get("concentration_tags")),
        "raw": row.get("raw") or {},
    }


def _remote_settlement_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "requestId": row.get("request_id"),
        "rank": _int_or_none(row.get("leg_rank")),
        "propId": row.get("prop_id"),
        "date": row.get("slate_date"),
        "marketKey": row.get("market_key"),
        "side": row.get("side"),
        "actualValue": _float_or_none(row.get("actual_value")),
        "actualResult": row.get("actual_result"),
        "overOutcome": row.get("over_outcome"),
        "decisionOutcome": row.get("decision_outcome"),
        "reasons": _json_array(row.get("reasons")),
        "settledAt": row.get("settled_at"),
        "raw": row.get("raw") or {},
    }


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _supabase_url() -> str | None:
    return os.getenv("SUPABASE_URL") or os.getenv("AZP_SUPABASE_URL") or None


def _supabase_service_key() -> str | None:
    return (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("AZP_SUPABASE_SERVICE_ROLE_KEY")
        or None
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _clean_limit(limit: int) -> int:
    return max(1, min(int(limit), 500))
