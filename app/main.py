from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query, Request

from .local_archive import (
    archive_gpt_decision,
    archive_market_mappings,
    archive_status,
)
from .local_ui_bridge import (
    STAKE_SGM_JOB_TYPE,
    LocalUiBridgeDisabled,
    LocalUiBridgeError,
    LocalUiBridgeTimeout,
    SupabaseLocalUiJobStore,
)
from .gpt_action import (
    build_available_markets,
    build_board_summary,
    build_comparison_board,
    build_gpt_action_openapi_schema,
    build_gpt_decision_result,
    build_market_map,
    build_matchup_prop_board,
    build_matchups,
    build_player_context_by_id,
    build_player_mlb_context,
    build_player_recent_logs,
    build_player_season_stats,
    build_prop_context_batch,
    build_prop_page,
    build_probable_pitchers,
    require_gpt_api_key,
    build_slip_candidates,
    validate_gpt_selections,
)
from .mlb_data import MLBAPIError, MLBDataEngine, MLBStatsClient, build_mlb_http_client
from .mlb_schedule import build_mlb_schedule_stake_map, build_mlb_schedule_view
from .mlb_props import slug_key
from .slate import DEFAULT_TIMEZONE
from .stake_client import StakeAPIError, StakeClient, build_http_client
from .storage import GptActionStore
from .supabase_ledger import (
    supabase_ledger_enabled,
    sync_gpt_decision_to_supabase,
    sync_market_mappings_to_supabase,
)


app = FastAPI(
    title="AZP GPT Data API",
    version="0.2.0",
    description=(
        "Thin data layer for Custom GPT Actions. GPT decides; this backend "
        "normalizes Stake odds, MLB Stats API context, validation, and logging."
    ),
)


async def get_stake_client() -> AsyncIterator[StakeClient]:
    api_key = os.getenv("STAKE_API_KEY") or None
    async with build_http_client() as http_client:
        yield StakeClient(http_client=http_client, api_key=api_key)


async def get_mlb_engine() -> AsyncIterator[MLBDataEngine]:
    async with build_mlb_http_client() as http_client:
        yield MLBDataEngine(MLBStatsClient(http_client))


def get_gpt_store() -> GptActionStore:
    return GptActionStore()


def get_local_ui_job_store() -> SupabaseLocalUiJobStore:
    return SupabaseLocalUiJobStore()


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "azp-gpt-data-api", "status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/gpt/health")
async def gpt_health(_: None = Depends(require_gpt_api_key)) -> dict[str, str]:
    return {"status": "ok", "service": "azp-gpt-data-api"}


@app.get("/archive/status")
async def local_archive_status(
    _: None = Depends(require_gpt_api_key),
) -> dict[str, Any]:
    return archive_status()


@app.get("/gpt/privacy", include_in_schema=False)
async def gpt_privacy_policy() -> dict[str, Any]:
    return {
        "name": "AZP Suite GPT Action Privacy Policy",
        "summary": (
            "The action retrieves Stake odds data and MLB Stats API data for your "
            "private Custom GPT. It does not log in to Stake, place bets, or handle "
            "payment credentials."
        ),
        "dataUse": [
            "Requests may include matchup names, player ids, prop ids, dates, markets, and GPT-selected props.",
            "Validated GPT decisions may be stored in Supabase or local SQLite if configured.",
            "No Stake account login, wallet, password, or bet-placement action is supported.",
        ],
    }


@app.get("/gpt/openapi.json", include_in_schema=False)
async def gpt_openapi_schema(request: Request) -> Any:
    return build_gpt_action_openapi_schema(str(request.base_url).rstrip("/"))


@app.get("/mlb/matchups")
async def mlb_matchups(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _call_data_sources(
        build_matchups,
        client,
        slate_date,
        _timezone_name(),
        limit,
    )


@app.get("/mlb/schedule")
async def mlb_schedule(
    slate_date: date | None = Query(None, alias="date"),
    _: None = Depends(require_gpt_api_key),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_data_sources(
        build_mlb_schedule_view,
        engine,
        slate_date,
        _timezone_name(),
    )


@app.get("/mlb/schedule/stake-map")
async def mlb_schedule_stake_map(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(100, ge=1, le=100),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_data_sources(
        build_mlb_schedule_stake_map,
        engine,
        client,
        slate_date,
        _timezone_name(),
        limit,
    )


@app.get("/mlb/matchup/{matchup}/markets")
async def mlb_matchup_markets(
    matchup: str = Path(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _call_data_sources(
        build_available_markets,
        client,
        matchup,
        slate_date,
        _timezone_name(),
        limit,
    )


@app.get("/mlb/matchup/{matchup}/props")
async def mlb_matchup_props(
    matchup: str = Path(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    market: str | None = Query(None),
    side: str = Query("any", pattern="^(any|over|under)$"),
    line_mode: str = Query("primary", alias="lineMode", pattern="^(primary|all)$"),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _call_data_sources(
        build_matchup_prop_board,
        client,
        matchup,
        slate_date,
        _timezone_name(),
        limit,
        market,
        side,
        line_mode,
    )


@app.get("/mlb/matchup/{matchup}/board-summary")
async def mlb_matchup_board_summary(
    matchup: str = Path(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    market: str | None = Query(None),
    side: str = Query("any", pattern="^(any|over|under)$"),
    line_mode: str = Query("primary", alias="lineMode", pattern="^(primary|all)$"),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _call_data_sources(
        build_board_summary,
        client,
        matchup,
        slate_date,
        _timezone_name(),
        limit,
        market,
        side,
        line_mode,
    )


@app.get("/mlb/matchup/{matchup}/prop-page")
async def mlb_matchup_prop_page(
    matchup: str = Path(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    market: str | None = Query(None),
    side: str = Query("any", pattern="^(any|over|under)$"),
    line_mode: str = Query("primary", alias="lineMode", pattern="^(primary|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, alias="pageSize", ge=1, le=100),
    primary_only: bool = Query(False, alias="primaryOnly"),
    playable_only: bool = Query(True, alias="playableOnly"),
    context_quality: str = Query("any", alias="contextQuality"),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _call_data_sources(
        build_prop_page,
        client,
        matchup,
        slate_date,
        _timezone_name(),
        limit,
        market,
        side,
        line_mode,
        page,
        page_size,
        primary_only,
        playable_only,
        context_quality,
    )


@app.get("/mlb/matchup/{matchup}/comparison-board")
async def mlb_matchup_comparison_board(
    matchup: str = Path(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    market: str | None = Query(None),
    side: str = Query("any", pattern="^(any|over|under)$"),
    line_mode: str = Query("primary", alias="lineMode", pattern="^(primary|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, alias="pageSize", ge=1, le=50),
    primary_only: bool = Query(False, alias="primaryOnly"),
    playable_only: bool = Query(True, alias="playableOnly"),
    context_quality: str = Query("supported", alias="contextQuality"),
    season: int | None = Query(None, ge=1876, le=2100),
    history_limit: int = Query(15, alias="historyLimit", ge=1, le=15),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_data_sources(
        build_comparison_board,
        client,
        engine,
        matchup,
        slate_date,
        _timezone_name(),
        limit,
        market,
        side,
        line_mode,
        page,
        page_size,
        primary_only,
        playable_only,
        context_quality,
        season or (slate_date.year if slate_date else None),
        history_limit,
    )


@app.post("/mlb/build-slip-candidates")
async def mlb_build_slip_candidates(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    matchup = _required_body_text(payload, "matchup")
    slate_date = _date_from_body(payload)
    return await _call_data_sources(
        build_slip_candidates,
        client,
        engine,
        matchup,
        slate_date,
        _timezone_name(),
        int(payload.get("limit") or 25),
        payload.get("markets") or payload.get("market"),
        payload.get("side") or "any",
        payload.get("season") or (slate_date.year if slate_date else None),
        int(payload.get("historyLimit") or payload.get("history_limit") or 15),
        payload.get("targetOddsMin") or payload.get("target_odds_min") or 2.0,
        payload.get("targetOddsMax") or payload.get("target_odds_max"),
        int(payload.get("minLegs") or payload.get("min_legs") or 2),
        int(payload.get("maxLegs") or payload.get("max_legs") or 8),
        payload.get("mode") or "balanced",
        payload.get("qualityFloor") or payload.get("quality_floor") or 55.0,
        _bool_from_body(payload, "allowNoPick", "allow_no_pick", True),
    )


@app.post("/mlb/stake-ui/sgm-board")
async def mlb_stake_ui_sgm_board(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    job_store: SupabaseLocalUiJobStore = Depends(get_local_ui_job_store),
) -> Any:
    if not job_store.enabled():
        raise HTTPException(
            status_code=503,
            detail={
                "source": "local_ui_bridge",
                "message": (
                    "Supabase local UI bridge is not configured. Set SUPABASE_URL "
                    "and SUPABASE_SERVICE_ROLE_KEY on Render and the local helper."
                ),
            },
        )

    matchup = _required_body_text(payload, "matchup")
    slate_date = _date_from_body(payload)
    limit = _clean_int_from_body(payload, "limit", 25, minimum=1, maximum=100)
    side = _optional_body_text(payload, "side").lower() or "any"
    if side not in {"any", "over", "under"}:
        raise HTTPException(status_code=422, detail="side must be any, over, or under")
    market = _optional_body_text(payload, "market")
    scope = _optional_body_text(payload, "scope").lower()
    playable_only = _bool_from_body(payload, "playableOnly", "playable_only", True)
    timeout_seconds = _clean_int_from_body(
        payload,
        "timeoutSeconds",
        25,
        minimum=1,
        maximum=45,
    )
    fixture_slug = str(payload.get("fixtureSlug") or "").strip()
    if not fixture_slug:
        fixture_slug = await _resolve_stake_fixture_slug(
            client=client,
            matchup=matchup,
            slate_date=slate_date,
            limit=limit,
        )

    request = {
        "matchup": matchup,
        "fixtureSlug": fixture_slug,
        "date": slate_date.isoformat() if slate_date else None,
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_sgm_truth_board",
    }
    try:
        job = await job_store.create_job(
            job_type=STAKE_SGM_JOB_TYPE,
            request=request,
            timeout_seconds=timeout_seconds,
        )
        completed = await job_store.wait_for_completed_result(
            job["jobId"],
            timeout_seconds=timeout_seconds,
        )
    except LocalUiBridgeDisabled as exc:
        raise HTTPException(
            status_code=503,
            detail={"source": "local_ui_bridge", "message": str(exc)},
        ) from exc
    except LocalUiBridgeTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail={
                "source": "local_ui_bridge",
                "message": str(exc),
                "fixtureSlug": fixture_slug,
                "matchup": matchup,
            },
        ) from exc
    except LocalUiBridgeError as exc:
        raise HTTPException(
            status_code=502,
            detail={"source": "local_ui_bridge", "message": str(exc)},
        ) from exc

    if completed.get("status") != "completed":
        raise HTTPException(
            status_code=502,
            detail={
                "source": "local_ui_bridge",
                "message": completed.get("error") or "Local helper job did not complete.",
                "status": completed.get("status"),
                "jobId": completed.get("jobId"),
            },
        )

    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_ui_sgm_via_local_helper",
        "purpose": "stake_ui_truth_board",
        "matchup": matchup,
        "date": slate_date.isoformat() if slate_date else None,
        "fixtureSlug": fixture_slug,
        "bridge": {
            "jobId": completed.get("jobId"),
            "status": completed.get("status"),
            "workerId": completed.get("workerId"),
            "createdAt": completed.get("createdAt"),
            "completedAt": completed.get("completedAt"),
        },
        "uiBoard": _compact_stake_ui_sgm_board(
            completed.get("result") or {},
            limit=limit,
            side=side,
            market=market,
            scope=scope,
            playable_only=playable_only,
        ),
    }


@app.get("/mlb/matchup/{matchup}/probable-pitchers")
async def mlb_matchup_probable_pitchers(
    matchup: str = Path(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    _: None = Depends(require_gpt_api_key),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_data_sources(
        build_probable_pitchers,
        engine,
        matchup,
        slate_date,
        _timezone_name(),
    )


@app.get("/mlb/matchup/{matchup}/market-map")
async def mlb_matchup_market_map(
    matchup: str = Path(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    store: GptActionStore = Depends(get_gpt_store),
) -> Any:
    response = await _call_data_sources(
        build_market_map,
        client,
        matchup,
        slate_date,
        _timezone_name(),
        limit,
    )
    saved = store.save_market_mappings(response.get("marketMap") or [])
    response["marketMappingStore"] = {
        "localSaved": saved["marketMappingsSaved"],
        "localArchive": archive_market_mappings(response),
    }
    if supabase_ledger_enabled() and response.get("marketMap"):
        try:
            supabase_result = await sync_market_mappings_to_supabase(
                response["marketMap"]
            )
            response["marketMappingStore"]["supabaseSynced"] = bool(
                supabase_result.get("synced")
            )
        except Exception as exc:
            response["marketMappingStore"]["supabaseSynced"] = False
            response["marketMappingStore"]["supabaseWarning"] = str(exc)
    return response


@app.get("/mlb/player/{player_id}/context")
async def mlb_player_context(
    player_id: int = Path(..., ge=1),
    market: str | None = Query(None),
    slate_date: date | None = Query(None, alias="date"),
    season: int | None = Query(None, ge=1876, le=2100),
    history_limit: int = Query(15, alias="historyLimit", ge=1, le=15),
    _: None = Depends(require_gpt_api_key),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    resolved_season = season or (slate_date.year if slate_date else None)
    return await _call_data_sources(
        build_player_context_by_id,
        engine,
        player_id,
        market,
        resolved_season,
        history_limit,
    )


@app.get("/mlb/player/{player_id}/recent")
async def mlb_player_recent(
    player_id: int = Path(..., ge=1),
    market: str | None = Query(None),
    season: int | None = Query(None, ge=1876, le=2100),
    history_limit: int = Query(15, alias="historyLimit", ge=1, le=15),
    _: None = Depends(require_gpt_api_key),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_data_sources(
        build_player_recent_logs,
        engine,
        player_id,
        market,
        season,
        history_limit,
    )


@app.get("/mlb/player/{player_id}/season")
async def mlb_player_season(
    player_id: int = Path(..., ge=1),
    market: str | None = Query(None),
    season: int | None = Query(None, ge=1876, le=2100),
    _: None = Depends(require_gpt_api_key),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_data_sources(
        build_player_season_stats,
        engine,
        player_id,
        market,
        season,
    )


@app.get("/mlb/prop/{prop_id}/context")
async def mlb_prop_context(
    prop_id: str = Path(..., min_length=2),
    matchup: str = Query(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    market: str | None = Query(None),
    side: str = Query("any", pattern="^(any|over|under)$"),
    season: int | None = Query(None, ge=1876, le=2100),
    history_limit: int = Query(15, alias="historyLimit", ge=1, le=15),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_data_sources(
        build_player_mlb_context,
        client,
        engine,
        matchup,
        prop_id,
        side,
        slate_date,
        _timezone_name(),
        limit,
        market,
        season or (slate_date.year if slate_date else None),
        history_limit,
    )


@app.post("/mlb/prop-context-batch")
async def mlb_prop_context_batch(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    matchup = _required_body_text(payload, "matchup")
    slate_date = _date_from_body(payload)
    return await _call_data_sources(
        build_prop_context_batch,
        client,
        engine,
        matchup,
        list(payload.get("selections") or payload.get("props") or []),
        slate_date,
        _timezone_name(),
        int(payload.get("limit") or 25),
        payload.get("market") or payload.get("markets"),
        payload.get("season") or (slate_date.year if slate_date else None),
        int(payload.get("historyLimit") or payload.get("history_limit") or 15),
    )


@app.post("/mlb/validate-selections")
async def mlb_validate_selections(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    matchup = _required_body_text(payload, "matchup")
    slate_date = _date_from_body(payload)
    return await _call_data_sources(
        validate_gpt_selections,
        client,
        matchup,
        list(payload.get("selections") or []),
        slate_date,
        _timezone_name(),
        int(payload.get("limit") or 25),
        payload.get("market") or payload.get("markets"),
        payload.get("validationMode") or payload.get("validation_mode") or "strict",
        payload.get("oddsPolicy") or payload.get("odds_policy"),
        payload.get("oddsTolerance") or payload.get("odds_tolerance"),
    )


@app.post("/mlb/save-gpt-decision")
async def mlb_save_gpt_decision(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    store: GptActionStore = Depends(get_gpt_store),
) -> Any:
    matchup = _required_body_text(payload, "matchup")
    slate_date = _date_from_body(payload)
    response = await _call_data_sources(
        build_gpt_decision_result,
        client,
        matchup,
        list(payload.get("selections") or []),
        slate_date,
        _timezone_name(),
        int(payload.get("limit") or 25),
        payload.get("market") or payload.get("markets"),
        payload.get("prompt"),
        list(payload.get("reasoning") or []),
        list(payload.get("riskFlags") or []),
        payload.get("validationMode") or payload.get("validation_mode") or "strict",
        payload.get("oddsPolicy") or payload.get("odds_policy"),
        payload.get("oddsTolerance") or payload.get("odds_tolerance"),
    )
    saved = store.save_gpt_decision_result(response, request_body=payload)
    response["gptDecisionLedger"] = {
        "saved": True,
        "decisionId": saved["decisionId"],
        "legsSaved": saved["gptDecisionLegsInserted"],
        "localArchive": archive_gpt_decision(
            response,
            request_body=payload,
            decision_id=saved["decisionId"],
        ),
        "supabaseSynced": False,
    }
    if supabase_ledger_enabled():
        try:
            supabase_result = await sync_gpt_decision_to_supabase(
                response,
                decision_id=saved["decisionId"],
                request_body=payload,
            )
            response["gptDecisionLedger"]["supabaseSynced"] = bool(
                supabase_result.get("synced")
            )
        except Exception as exc:
            response["gptDecisionLedger"]["supabaseSynced"] = False
            response["gptDecisionLedger"]["supabaseWarning"] = str(exc)
    return response


# Temporary compatibility aliases for the current Custom GPT action schema.
@app.get("/gpt/mlb/matchup-prop-board", include_in_schema=False)
async def legacy_gpt_mlb_matchup_prop_board(
    matchup: str = Query(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    markets: str | None = Query(None),
    side: str = Query("any", pattern="^(any|over|under)$"),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await mlb_matchup_props(
        matchup=matchup,
        slate_date=slate_date,
        limit=limit,
        market=markets,
        side=side,
        line_mode="primary",
        _=_,
        client=client,
    )


@app.get("/gpt/mlb/player-context", include_in_schema=False)
async def legacy_gpt_mlb_player_context(
    matchup: str = Query(..., min_length=2),
    prop_id: str = Query(..., alias="propId", min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    markets: str | None = Query(None),
    side: str = Query("any", pattern="^(any|over|under)$"),
    season: int | None = Query(None, ge=1876, le=2100),
    history_limit: int = Query(15, alias="historyLimit", ge=1, le=15),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await mlb_prop_context(
        prop_id=prop_id,
        matchup=matchup,
        slate_date=slate_date,
        limit=limit,
        market=markets,
        side=side,
        season=season,
        history_limit=history_limit,
        _=_,
        client=client,
        engine=engine,
    )


@app.post("/gpt/mlb/validate-selections", include_in_schema=False)
async def legacy_gpt_mlb_validate_selections(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await mlb_validate_selections(payload=payload, _=_, client=client)


@app.post("/gpt/mlb/gpt-decisions", include_in_schema=False)
async def legacy_gpt_mlb_save_gpt_decision(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    store: GptActionStore = Depends(get_gpt_store),
) -> Any:
    return await mlb_save_gpt_decision(payload=payload, _=_, client=client, store=store)


async def _call_data_sources(callback: Any, *args: Any) -> Any:
    try:
        return await callback(*args)
    except StakeAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code if exc.status_code < 500 else 502,
            detail={"source": "stake", "message": exc.message},
        ) from exc
    except MLBAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code if exc.status_code < 500 else 502,
            detail={"source": "mlb", "message": exc.message},
        ) from exc


def _timezone_name() -> str:
    return os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)


def _required_body_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail=f"Missing required body field: {key}")
    return value


def _optional_body_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _date_from_body(payload: dict[str, Any]) -> date | None:
    raw_date = payload.get("date")
    if not raw_date:
        return None
    try:
        return date.fromisoformat(str(raw_date))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc


def _bool_from_body(
    payload: dict[str, Any],
    camel_key: str,
    snake_key: str,
    default: bool,
) -> bool:
    raw_value = payload.get(camel_key, payload.get(snake_key, default))
    if isinstance(raw_value, bool):
        return raw_value
    if raw_value is None:
        return default
    if isinstance(raw_value, (int, float)):
        return raw_value != 0

    text_value = str(raw_value).strip().lower()
    if text_value in {"1", "true", "yes", "y", "on"}:
        return True
    if text_value in {"0", "false", "no", "n", "off"}:
        return False

    raise HTTPException(
        status_code=422,
        detail=f"{camel_key} must be a boolean value",
    )


def _clean_int_from_body(
    payload: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = payload.get(key, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{key} must be an integer") from exc
    return max(minimum, min(value, maximum))


def _compact_stake_ui_sgm_board(
    board: dict[str, Any],
    *,
    limit: int,
    side: str,
    market: str,
    scope: str,
    playable_only: bool,
) -> dict[str, Any]:
    rows = _stake_ui_selection_rows(
        board,
        limit=limit,
        side=side,
        market=market,
        scope=scope,
        playable_only=playable_only,
    )
    return {
        "source": board.get("source"),
        "fixtureSlug": board.get("fixtureSlug"),
        "capturedAt": board.get("capturedAt"),
        "fixture": board.get("fixture") or {},
        "teams": board.get("teams") or [],
        "counts": board.get("counts") or {},
        "warnings": board.get("warnings") or [],
        "filters": {
            "side": side,
            "market": market or None,
            "scope": scope or None,
            "playableOnly": playable_only,
            "limit": limit,
        },
        "returnedRows": len(rows),
        "rows": rows,
    }


def _stake_ui_selection_rows(
    board: dict[str, Any],
    *,
    limit: int,
    side: str,
    market: str,
    scope: str,
    playable_only: bool,
) -> list[dict[str, Any]]:
    source_rows = list(board.get("playerProps") or []) + list(board.get("teamMarkets") or [])
    wanted_sides = ("over", "under") if side == "any" else (side,)
    market_key = market.lower().strip()
    scope_key = scope.lower().strip()
    compact_rows: list[dict[str, Any]] = []

    for row in source_rows:
        if playable_only and not row.get("playable"):
            continue
        row_market = str(row.get("market") or "")
        if market_key and market_key not in row_market.lower():
            continue
        row_scope = str(row.get("scope") or "").lower()
        if scope_key and scope_key != row_scope:
            continue

        for row_side in wanted_sides:
            odds = row.get(row_side)
            if odds is None:
                continue
            compact_rows.append(
                {
                    "selectionId": f"{row.get('lineId') or ''}:{row_side}",
                    "propId": row.get("lineId"),
                    "player": row.get("player"),
                    "team": row.get("team"),
                    "position": row.get("position"),
                    "scope": row.get("scope"),
                    "market": row.get("market"),
                    "side": row_side,
                    "line": row.get("line"),
                    "odds": odds,
                    "playable": bool(row.get("playable")),
                    "suspended": bool(row.get("suspended")),
                    "customBet": bool(row.get("customBet")),
                    "liveCustomBetAvailable": bool(row.get("liveCustomBetAvailable")),
                    "playerId": row.get("playerId"),
                    "marketId": row.get("marketId"),
                    "lineId": row.get("lineId"),
                    "swishStatId": row.get("swishStatId"),
                }
            )
            if len(compact_rows) >= limit:
                return compact_rows

    return compact_rows


async def _resolve_stake_fixture_slug(
    *,
    client: StakeClient,
    matchup: str,
    slate_date: date | None,
    limit: int,
) -> str:
    schedule = await build_matchups(
        client,
        slate_date,
        _timezone_name(),
        limit,
    )
    matchup_key = _matchup_key(matchup)
    for row in schedule.get("matchups") or []:
        row_keys = {
            _matchup_key(row.get("name") or ""),
            _matchup_key(" ".join(row.get("teams") or [])),
        }
        row_key_joined = "|".join(sorted(slug_key(team) for team in row.get("teams") or []))
        if matchup_key in row_keys or _same_team_set(matchup, row.get("teams") or []):
            return str(row.get("fixtureSlug") or "")
        if matchup_key and matchup_key == row_key_joined:
            return str(row.get("fixtureSlug") or "")

    raise HTTPException(
        status_code=404,
        detail={
            "source": "stake",
            "message": "Could not resolve matchup to a Stake fixture slug.",
            "matchup": matchup,
            "date": schedule.get("date"),
        },
    )


def _same_team_set(matchup: str, teams: list[Any]) -> bool:
    requested_tokens = {
        slug_key(part)
        for part in re.split(r"\s+(?:vs|at|@|-)\s+", matchup, flags=re.IGNORECASE)
        if slug_key(part)
    }
    team_tokens = {slug_key(team) for team in teams if slug_key(team)}
    if not requested_tokens or not team_tokens:
        return False
    return all(
        any(requested == team or requested in team or team in requested for team in team_tokens)
        for requested in requested_tokens
    )


def _matchup_key(value: str) -> str:
    parts = [
        slug_key(part)
        for part in re.split(r"\s+(?:vs|at|@|-)\s+", str(value), flags=re.IGNORECASE)
        if slug_key(part)
    ]
    return "|".join(sorted(parts))
