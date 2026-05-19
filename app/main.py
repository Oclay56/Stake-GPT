from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query, Request

from .local_archive import (
    archive_gpt_decision,
    archive_market_mappings,
    archive_status,
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
from .slate import DEFAULT_TIMEZONE
from .slip_jobs import (
    build_mlb_schedule_stake_map,
    build_mlb_schedule_view,
    normalize_slip_job_request,
)
from .stake_client import StakeAPIError, StakeClient, build_http_client
from .storage import GptActionStore
from .supabase_ledger import (
    supabase_ledger_enabled,
    sync_gpt_decision_to_supabase,
    sync_market_mappings_to_supabase,
    sync_slip_job_status_to_supabase,
    sync_slip_job_to_supabase,
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


@app.post("/slip-jobs")
async def create_slip_job(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    store: GptActionStore = Depends(get_gpt_store),
) -> Any:
    try:
        normalized = normalize_slip_job_request(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = store.create_slip_job(normalized)
    response = {
        **job,
        "jobStore": {
            "localSaved": True,
            "supabaseSynced": False,
        },
    }
    if supabase_ledger_enabled():
        try:
            supabase_result = await sync_slip_job_to_supabase(job)
            response["jobStore"]["supabaseSynced"] = bool(supabase_result.get("synced"))
        except Exception as exc:
            response["jobStore"]["supabaseSynced"] = False
            response["jobStore"]["supabaseWarning"] = str(exc)
    return response


@app.get("/slip-jobs/next")
async def next_slip_job(
    bridge_id: str = Query(..., alias="bridgeId", min_length=2),
    _: None = Depends(require_gpt_api_key),
    store: GptActionStore = Depends(get_gpt_store),
) -> Any:
    job = store.claim_next_slip_job(bridge_id)
    return {
        "job": job,
        "pollAfterSeconds": int(os.getenv("AZP_BRIDGE_POLL_SECONDS", "10")),
    }


@app.get("/slip-jobs/{jobId}")
async def get_slip_job_status(
    job_id: str = Path(..., alias="jobId", min_length=2),
    _: None = Depends(require_gpt_api_key),
    store: GptActionStore = Depends(get_gpt_store),
) -> Any:
    job = store.get_slip_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Slip job not found.")
    return job


@app.post("/slip-jobs/{jobId}/status")
async def update_slip_job_status(
    payload: dict[str, Any] = Body(...),
    job_id: str = Path(..., alias="jobId", min_length=2),
    _: None = Depends(require_gpt_api_key),
    store: GptActionStore = Depends(get_gpt_store),
) -> Any:
    status = _required_body_text(payload, "status")
    try:
        job = store.update_slip_job_status(
            job_id=job_id,
            status=status,
            bridge_id=payload.get("bridgeId") or payload.get("bridge_id"),
            message=payload.get("message"),
            result=payload.get("result") if isinstance(payload.get("result"), dict) else {},
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if supabase_ledger_enabled():
        try:
            supabase_result = await sync_slip_job_status_to_supabase(job)
            job["jobStore"] = {"supabaseSynced": bool(supabase_result.get("synced"))}
        except Exception as exc:
            job["jobStore"] = {"supabaseSynced": False, "supabaseWarning": str(exc)}
    return job


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
