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
    STAKE_CLEAR_SIDEBAR_JOB_TYPE,
    STAKE_MLB_GAMES_JOB_TYPE,
    STAKE_REMOVE_SIDEBAR_GROUP_JOB_TYPE,
    STAKE_SGM_CLEAR_SELECTIONS_JOB_TYPE,
    STAKE_SGM_BUILD_SLIP_JOB_TYPE,
    STAKE_SGM_BUILD_SLIP_BATCH_JOB_TYPE,
    STAKE_SGM_JOB_TYPE,
    STAKE_UI_STATE_JOB_TYPE,
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
from .stake_sgm_browser import make_sgm_selection_row_id, sgm_market_filter_matches
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


@app.post("/mlb/stake-ui/mlb-games")
async def mlb_stake_ui_mlb_games(
    payload: dict[str, Any] = Body(default_factory=dict),
    _: None = Depends(require_gpt_api_key),
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

    timeout_seconds = _clean_int_from_body(
        payload,
        "timeoutSeconds",
        45,
        minimum=1,
        maximum=90,
    )
    limit = _clean_int_from_body(payload, "limit", 50, minimum=1, maximum=100)
    request = {
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_mlb_game_index",
        "limit": limit,
    }
    job: dict[str, Any] | None = None
    try:
        job = await job_store.create_job(
            job_type=STAKE_MLB_GAMES_JOB_TYPE,
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
                "jobId": (job or {}).get("jobId"),
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

    result = completed.get("result") or {}
    games = list(result.get("games") or [])[:limit]
    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_ui_mlb_games_via_local_helper",
        "purpose": "stake_ui_mlb_game_index",
        "bridge": {
            "jobId": completed.get("jobId"),
            "status": completed.get("status"),
            "workerId": completed.get("workerId"),
            "createdAt": completed.get("createdAt"),
            "completedAt": completed.get("completedAt"),
            "updatedAt": completed.get("updatedAt"),
        },
        "uiGames": {
            "source": result.get("source"),
            "capturedAt": result.get("capturedAt"),
            "url": result.get("url"),
            "returnedGames": len(games),
            "warnings": result.get("warnings") or [],
            "games": games,
        },
    }


@app.post("/mlb/stake-ui/state")
async def mlb_stake_ui_state(
    payload: dict[str, Any] = Body(default_factory=dict),
    _: None = Depends(require_gpt_api_key),
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

    timeout_seconds = _clean_int_from_body(
        payload,
        "timeoutSeconds",
        20,
        minimum=1,
        maximum=60,
    )
    fixture_slug = str(payload.get("fixtureSlug") or "").strip()
    request = {
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_diagnostics",
        "fixtureSlug": fixture_slug or None,
    }
    job: dict[str, Any] | None = None
    try:
        job = await job_store.create_job(
            job_type=STAKE_UI_STATE_JOB_TYPE,
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
                "jobId": (job or {}).get("jobId"),
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
        "source": "stake_ui_state_via_local_helper",
        "purpose": "stake_ui_diagnostics",
        "bridge": _local_ui_bridge_summary(completed),
        "state": completed.get("result") or {},
    }


@app.post("/mlb/stake-ui/clear-sgm-selections")
async def mlb_stake_ui_clear_sgm_selections(
    payload: dict[str, Any] = Body(default_factory=dict),
    _: None = Depends(require_gpt_api_key),
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

    timeout_seconds = _clean_int_from_body(
        payload,
        "timeoutSeconds",
        20,
        minimum=1,
        maximum=60,
    )
    fixture_slug = str(payload.get("fixtureSlug") or "").strip()
    request = {
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_sgm_recovery_clear_selection",
        "fixtureSlug": fixture_slug or None,
    }
    job: dict[str, Any] | None = None
    try:
        job = await job_store.create_job(
            job_type=STAKE_SGM_CLEAR_SELECTIONS_JOB_TYPE,
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
                "jobId": (job or {}).get("jobId"),
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
        "source": "stake_ui_sgm_clear_selections_via_local_helper",
        "purpose": "stake_ui_recovery",
        "bridge": _local_ui_bridge_summary(completed),
        "result": completed.get("result") or {},
    }


@app.post("/mlb/stake-ui/remove-sidebar-group")
async def mlb_stake_ui_remove_sidebar_group(
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

    review_only = _bool_from_body(payload, "reviewOnly", "review_only", True)
    if not review_only:
        raise HTTPException(
            status_code=422,
            detail="reviewOnly must be true. AZP will not place bets or enter stake amounts.",
        )

    matchup = str(payload.get("matchup") or "").strip()
    fixture_slug = str(payload.get("fixtureSlug") or "").strip()
    if not fixture_slug and not matchup:
        raise HTTPException(status_code=422, detail="fixtureSlug or matchup is required")

    slate_date = _date_from_body(payload)
    timeout_seconds = _clean_int_from_body(
        payload,
        "timeoutSeconds",
        20,
        minimum=1,
        maximum=60,
    )
    schedule_limit = _clean_int_from_body(
        payload,
        "scheduleLimit",
        25,
        minimum=1,
        maximum=100,
    )
    if not fixture_slug:
        fixture_slug = await _resolve_stake_fixture_slug(
            client=client,
            matchup=matchup,
            slate_date=slate_date,
            limit=schedule_limit,
        )

    request = {
        "matchup": matchup or None,
        "fixtureSlug": fixture_slug,
        "date": slate_date.isoformat() if slate_date else None,
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_remove_sidebar_group",
        "reviewOnly": True,
        "forbiddenActions": ["enter_stake_amount", "click_place_bet"],
    }
    job: dict[str, Any] | None = None
    try:
        job = await job_store.create_job(
            job_type=STAKE_REMOVE_SIDEBAR_GROUP_JOB_TYPE,
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
                "matchup": matchup or None,
                "jobId": (job or {}).get("jobId"),
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

    result = completed.get("result") or {}
    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_ui_remove_sidebar_group_via_local_helper",
        "purpose": "stake_ui_review_slip_sidebar_removal",
        "matchup": matchup or result.get("matchup"),
        "date": slate_date.isoformat() if slate_date else None,
        "fixtureSlug": fixture_slug,
        "bridge": _local_ui_bridge_summary(completed),
        "result": _compact_remove_sidebar_group_result(result),
    }


@app.post("/mlb/stake-ui/clear-sidebar")
async def mlb_stake_ui_clear_sidebar(
    payload: dict[str, Any] = Body(default_factory=dict),
    _: None = Depends(require_gpt_api_key),
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

    review_only = _bool_from_body(payload, "reviewOnly", "review_only", True)
    if not review_only:
        raise HTTPException(
            status_code=422,
            detail="reviewOnly must be true. AZP will not place bets or enter stake amounts.",
        )

    timeout_seconds = _clean_int_from_body(
        payload,
        "timeoutSeconds",
        20,
        minimum=1,
        maximum=60,
    )
    request = {
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_clear_sidebar",
        "reviewOnly": True,
        "forbiddenActions": ["enter_stake_amount", "click_place_bet"],
    }
    job: dict[str, Any] | None = None
    try:
        job = await job_store.create_job(
            job_type=STAKE_CLEAR_SIDEBAR_JOB_TYPE,
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
                "jobId": (job or {}).get("jobId"),
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

    result = completed.get("result") or {}
    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_ui_clear_sidebar_via_local_helper",
        "purpose": "stake_ui_review_slip_sidebar_clear",
        "bridge": _local_ui_bridge_summary(completed),
        "result": _compact_clear_sidebar_result(result),
    }


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
        45,
        minimum=1,
        maximum=90,
    )
    max_cache_age_seconds = _clean_int_from_body(
        payload,
        "maxCacheAgeSeconds",
        180,
        minimum=0,
        maximum=600,
    )
    fixture_slug = str(payload.get("fixtureSlug") or "").strip()
    if not fixture_slug:
        fixture_slug = await _resolve_stake_fixture_slug(
            client=client,
            matchup=matchup,
            slate_date=slate_date,
            limit=limit,
        )

    cached = await job_store.find_recent_completed_job(
        job_type=STAKE_SGM_JOB_TYPE,
        fixture_slug=fixture_slug,
        max_age_seconds=max_cache_age_seconds,
    )
    if cached and cached.get("result"):
        return _stake_ui_sgm_board_response(
            matchup=matchup,
            slate_date=slate_date,
            fixture_slug=fixture_slug,
            completed=cached,
            limit=limit,
            side=side,
            market=market,
            scope=scope,
            playable_only=playable_only,
            cache_hit=True,
        )

    request = {
        "matchup": matchup,
        "fixtureSlug": fixture_slug,
        "date": slate_date.isoformat() if slate_date else None,
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_sgm_truth_board",
    }
    job: dict[str, Any] | None = None
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
            detail=_local_ui_timeout_detail(
                message=str(exc),
                job=job,
                fixture_slug=fixture_slug,
                matchup=matchup,
            ),
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

    return _stake_ui_sgm_board_response(
        matchup=matchup,
        slate_date=slate_date,
        fixture_slug=fixture_slug,
        completed=completed,
        limit=limit,
        side=side,
        market=market,
        scope=scope,
        playable_only=playable_only,
        cache_hit=False,
    )


@app.post("/mlb/stake-ui/review-slip")
async def mlb_stake_ui_review_slip(
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

    review_only = _bool_from_body(payload, "reviewOnly", "review_only", True)
    if not review_only:
        raise HTTPException(
            status_code=422,
            detail="reviewOnly must be true. AZP will not place bets or enter stake amounts.",
        )

    matchup = _required_body_text(payload, "matchup")
    slate_date = _date_from_body(payload)
    timeout_seconds = _clean_int_from_body(
        payload,
        "timeoutSeconds",
        30,
        minimum=1,
        maximum=60,
    )
    schedule_limit = _clean_int_from_body(
        payload,
        "scheduleLimit",
        25,
        minimum=1,
        maximum=100,
    )
    fixture_slug = str(payload.get("fixtureSlug") or "").strip()
    if not fixture_slug:
        fixture_slug = await _resolve_stake_fixture_slug(
            client=client,
            matchup=matchup,
            slate_date=slate_date,
            limit=schedule_limit,
        )

    selections = _review_slip_selections_from_body(payload)
    fallback_selections = _review_slip_selections_from_body(
        {
            "selections": (
                payload.get("fallbackSelections")
                or payload.get("fallback_selections")
                or payload.get("replacementSelections")
                or payload.get("replacement_selections")
                or payload.get("backupSelections")
                or payload.get("backup_selections")
            ),
            "rowIds": (
                payload.get("fallbackRowIds")
                or payload.get("fallback_row_ids")
                or payload.get("replacementRowIds")
                or payload.get("replacement_row_ids")
                or payload.get("backupRowIds")
                or payload.get("backup_row_ids")
            ),
        },
        required=False,
    )
    required_legs = _clean_optional_int(
        payload,
        "requiredLegs",
        "required_legs",
        "targetLegs",
        "target_legs",
        default=len(selections),
        minimum=1,
        maximum=20,
    )
    request = {
        "matchup": matchup,
        "fixtureSlug": fixture_slug,
        "date": slate_date.isoformat() if slate_date else None,
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_sgm_review_slip",
        "reviewOnly": True,
        "forbiddenActions": ["enter_stake_amount", "click_place_bet"],
        "selections": selections,
        "fallbackSelections": fallback_selections,
        "requiredLegs": required_legs,
        "localExecutionTimeoutSeconds": _local_helper_execution_timeout_seconds(
            timeout_seconds
        ),
    }

    job: dict[str, Any] | None = None
    try:
        job = await job_store.create_job(
            job_type=STAKE_SGM_BUILD_SLIP_JOB_TYPE,
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
            detail=_local_ui_timeout_detail(
                message=str(exc),
                job=job,
                fixture_slug=fixture_slug,
                matchup=matchup,
            ),
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

    result = completed.get("result") or {}
    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_ui_sgm_review_slip_via_local_helper",
        "purpose": "stake_ui_review_only_slip_builder",
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
        "result": _compact_review_slip_result(result),
    }


@app.post("/mlb/stake-ui/review-slip-batch")
async def mlb_stake_ui_review_slip_batch(
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

    review_only = _bool_from_body(payload, "reviewOnly", "review_only", True)
    if not review_only:
        raise HTTPException(
            status_code=422,
            detail="reviewOnly must be true. AZP will not place bets or enter stake amounts.",
        )

    slate_date = _date_from_body(payload)
    timeout_seconds = _clean_int_from_body(
        payload,
        "timeoutSeconds",
        60,
        minimum=1,
        maximum=180,
    )
    schedule_limit = _clean_int_from_body(
        payload,
        "scheduleLimit",
        50,
        minimum=1,
        maximum=100,
    )
    groups = await _review_slip_groups_from_body(
        payload,
        client=client,
        slate_date=slate_date,
        schedule_limit=schedule_limit,
    )
    continue_on_group_failure = _bool_from_body(
        payload,
        "continueOnGroupFailure",
        "continue_on_group_failure",
        False,
    )
    min_groups_required = _clean_optional_int(
        payload,
        "minGroupsRequired",
        "min_groups_required",
        default=1,
        minimum=1,
        maximum=15,
    )
    request = {
        "date": slate_date.isoformat() if slate_date else None,
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_sgm_review_slip_batch",
        "reviewOnly": True,
        "forbiddenActions": ["enter_stake_amount", "click_place_bet"],
        "groups": groups,
        "continueOnGroupFailure": continue_on_group_failure,
        "minGroupsRequired": min_groups_required,
        "localExecutionTimeoutSeconds": _local_helper_execution_timeout_seconds(
            timeout_seconds
        ),
    }

    job: dict[str, Any] | None = None
    try:
        job = await job_store.create_job(
            job_type=STAKE_SGM_BUILD_SLIP_BATCH_JOB_TYPE,
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
            detail=_local_ui_timeout_detail(message=str(exc), job=job),
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

    result = completed.get("result") or {}
    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_ui_sgm_review_slip_batch_via_local_helper",
        "purpose": "stake_ui_review_only_batch_slip_builder",
        "date": slate_date.isoformat() if slate_date else None,
        "bridge": {
            "jobId": completed.get("jobId"),
            "status": completed.get("status"),
            "workerId": completed.get("workerId"),
            "createdAt": completed.get("createdAt"),
            "completedAt": completed.get("completedAt"),
        },
        "result": _compact_batch_review_slip_result(result),
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


def _local_ui_timeout_detail(
    *,
    message: str,
    job: dict[str, Any] | None,
    fixture_slug: str | None = None,
    matchup: str | None = None,
) -> dict[str, Any]:
    return {
        "source": "local_ui_bridge",
        "status": "timeout",
        "phase": "local_helper_wait",
        "message": message,
        "jobId": (job or {}).get("jobId"),
        "clickedLegs": 0,
        "lastAction": "waiting_for_local_helper_result",
        "lastKnownFixtureSlug": fixture_slug,
        "matchup": matchup,
        "diagnostics": {
            "helperReturned": False,
            "enteredStakeAmount": False,
            "clickedPlaceBet": False,
        },
    }


def _local_helper_execution_timeout_seconds(timeout_seconds: int) -> int:
    if timeout_seconds <= 2:
        return 1
    if timeout_seconds <= 10:
        return max(1, timeout_seconds - 1)
    return max(1, timeout_seconds - 5)


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


def _review_slip_selections_from_body(
    payload: dict[str, Any],
    *,
    required: bool = True,
) -> list[dict[str, Any]]:
    raw_selection_value = payload.get("selections")
    if raw_selection_value is None:
        raw_selections: list[Any] = []
    elif isinstance(raw_selection_value, list):
        raw_selections = list(raw_selection_value)
    else:
        raise HTTPException(status_code=422, detail="selections must be a list")

    raw_row_ids = payload.get("rowIds") or payload.get("row_ids")
    if raw_row_ids is not None and not isinstance(raw_row_ids, list):
        raise HTTPException(status_code=422, detail="rowIds must be a list")
    for row_id in raw_row_ids or []:
        if str(row_id or "").strip():
            raw_selections.append({"rowId": str(row_id).strip()})

    if not isinstance(raw_selections, list) or not raw_selections:
        if not required:
            return []
        raise HTTPException(
            status_code=422,
            detail="selections or rowIds must be a non-empty list of exact Stake UI-backed legs",
        )
    if len(raw_selections) > 20:
        raise HTTPException(status_code=422, detail="selections cannot contain more than 20 legs")

    required_fields = ("market", "side", "line", "odds")
    cleaned: list[dict[str, Any]] = []
    for index, raw_selection in enumerate(raw_selections, start=1):
        if not isinstance(raw_selection, dict):
            raise HTTPException(status_code=422, detail=f"selection {index} must be an object")

        row_id = _clean_nullable_text(
            raw_selection.get("rowId")
            or raw_selection.get("row_id")
            or (
                raw_selection.get("selectionId")
                if str(raw_selection.get("selectionId") or "").startswith("sgm_")
                else None
            )
        )
        if row_id:
            cleaned_selection: dict[str, Any] = {"rowId": row_id}
            for output_key, input_key in (
                ("player", "player"),
                ("team", "team"),
                ("market", "market"),
                ("side", "side"),
                ("scope", "scope"),
            ):
                text_value = _clean_nullable_text(raw_selection.get(input_key))
                if text_value:
                    cleaned_selection[output_key] = text_value
            for numeric_key in ("line", "odds"):
                if raw_selection.get(numeric_key) is not None:
                    cleaned_selection[numeric_key] = raw_selection.get(numeric_key)
            cleaned.append(cleaned_selection)
            continue

        missing = [
            field
            for field in required_fields
            if raw_selection.get(field) is None or str(raw_selection.get(field)).strip() == ""
        ]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"selection {index} is missing required field(s): {', '.join(missing)}",
            )

        side = str(raw_selection.get("side")).strip().lower()
        if side not in {"over", "under"}:
            raise HTTPException(
                status_code=422,
                detail=f"selection {index} side must be over or under",
            )

        try:
            line = float(raw_selection.get("line"))
            odds = float(raw_selection.get("odds"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"selection {index} line and odds must be numeric",
            ) from exc

        cleaned.append(
            {
                "player": _clean_nullable_text(raw_selection.get("player")),
                "team": _clean_nullable_text(raw_selection.get("team")),
                "market": str(raw_selection.get("market")).strip(),
                "side": side,
                "line": line,
                "odds": odds,
                "scope": _clean_nullable_text(raw_selection.get("scope")),
                "propId": _clean_nullable_text(raw_selection.get("propId")),
                "lineId": _clean_nullable_text(raw_selection.get("lineId")),
                "marketId": _clean_nullable_text(raw_selection.get("marketId")),
                "selectionId": _clean_nullable_text(raw_selection.get("selectionId")),
            }
        )
    return cleaned


async def _review_slip_groups_from_body(
    payload: dict[str, Any],
    *,
    client: StakeClient,
    slate_date: date | None,
    schedule_limit: int,
) -> list[dict[str, Any]]:
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise HTTPException(
            status_code=422,
            detail="groups must be a non-empty list of fixture selection groups",
        )
    if len(raw_groups) > 15:
        raise HTTPException(status_code=422, detail="groups cannot contain more than 15 games")

    groups: list[dict[str, Any]] = []
    for index, raw_group in enumerate(raw_groups, start=1):
        if not isinstance(raw_group, dict):
            raise HTTPException(status_code=422, detail=f"group {index} must be an object")

        matchup = str(raw_group.get("matchup") or "").strip()
        fixture_slug = str(raw_group.get("fixtureSlug") or "").strip()
        if not fixture_slug:
            if not matchup:
                raise HTTPException(
                    status_code=422,
                    detail=f"group {index} must include fixtureSlug or matchup",
                )
            fixture_slug = await _resolve_stake_fixture_slug(
                client=client,
                matchup=matchup,
                slate_date=slate_date,
                limit=schedule_limit,
            )

        selections = _review_slip_selections_from_body(
            {
                "selections": raw_group.get("selections"),
                "rowIds": raw_group.get("rowIds") or raw_group.get("row_ids"),
            }
        )
        fallback_selections = _review_slip_selections_from_body(
            {
                "selections": (
                    raw_group.get("fallbackSelections")
                    or raw_group.get("fallback_selections")
                    or raw_group.get("replacementSelections")
                    or raw_group.get("replacement_selections")
                    or raw_group.get("backupSelections")
                    or raw_group.get("backup_selections")
                ),
                "rowIds": (
                    raw_group.get("fallbackRowIds")
                    or raw_group.get("fallback_row_ids")
                    or raw_group.get("replacementRowIds")
                    or raw_group.get("replacement_row_ids")
                    or raw_group.get("backupRowIds")
                    or raw_group.get("backup_row_ids")
                ),
            },
            required=False,
        )
        required_legs = _clean_optional_int(
            raw_group,
            "requiredLegs",
            "required_legs",
            "targetLegs",
            "target_legs",
            default=len(selections),
            minimum=1,
            maximum=20,
        )
        groups.append(
            {
                "matchup": matchup or None,
                "fixtureSlug": fixture_slug,
                "selections": selections,
                "fallbackSelections": fallback_selections,
                "requiredLegs": required_legs,
            }
        )
    return groups


def _clean_optional_int(
    payload: dict[str, Any],
    *keys: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    for key in keys:
        if payload.get(key) is None:
            continue
        try:
            return max(minimum, min(maximum, int(payload.get(key))))
        except (TypeError, ValueError):
            continue
    return max(minimum, min(maximum, int(default)))


def _clean_nullable_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _compact_review_slip_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": result.get("source"),
        "fixtureSlug": result.get("fixtureSlug"),
        "capturedAt": result.get("capturedAt"),
        "status": result.get("status"),
        "reviewOnly": bool(result.get("reviewOnly", True)),
        "clickedLegs": int(result.get("clickedLegs") or 0),
        "selectedRows": result.get("selectedRows") or [],
        "missingSelections": result.get("missingSelections") or [],
        "clickResults": result.get("clickResults") or [],
        "addBetResult": result.get("addBetResult") or {},
        "transactionPlan": result.get("transactionPlan") or {},
        "warnings": result.get("warnings") or [],
        "safety": {
            "enteredStakeAmount": False,
            "clickedPlaceBet": False,
            **(result.get("safety") or {}),
        },
    }


def _compact_remove_sidebar_group_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": result.get("source"),
        "capturedAt": result.get("capturedAt"),
        "status": result.get("status"),
        "fixtureSlug": result.get("fixtureSlug"),
        "matchup": result.get("matchup"),
        "teams": result.get("teams") or [],
        "removeResult": result.get("removeResult") or {},
        "slip": result.get("slip") or {},
        "safety": {
            "enteredStakeAmount": False,
            "clickedPlaceBet": False,
            "removedSidebarGroupOnly": True,
            **(result.get("safety") or {}),
        },
    }


def _compact_clear_sidebar_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": result.get("source"),
        "capturedAt": result.get("capturedAt"),
        "status": result.get("status"),
        "clearResult": result.get("clearResult") or {},
        "slip": result.get("slip") or {},
        "safety": {
            "enteredStakeAmount": False,
            "clickedPlaceBet": False,
            "clearedEntireSidebar": bool(result.get("status") == "cleared"),
            **(result.get("safety") or {}),
        },
    }


def _local_ui_bridge_summary(completed: dict[str, Any]) -> dict[str, Any]:
    return {
        "jobId": completed.get("jobId"),
        "status": completed.get("status"),
        "workerId": completed.get("workerId"),
        "createdAt": completed.get("createdAt"),
        "completedAt": completed.get("completedAt"),
        "updatedAt": completed.get("updatedAt"),
    }


def _compact_batch_review_slip_result(result: dict[str, Any]) -> dict[str, Any]:
    groups = []
    for group in result.get("groups") or []:
        if not isinstance(group, dict):
            continue
        groups.append(
            {
                "source": group.get("source"),
                "matchup": group.get("matchup"),
                "fixtureSlug": group.get("fixtureSlug"),
                "capturedAt": group.get("capturedAt"),
                "status": group.get("status"),
                "reviewOnly": bool(group.get("reviewOnly", True)),
                "clickedLegs": int(group.get("clickedLegs") or 0),
                "selectedRows": group.get("selectedRows") or [],
                "missingSelections": group.get("missingSelections") or [],
                "clickResults": group.get("clickResults") or [],
                "addBetResult": group.get("addBetResult") or {},
                "transactionPlan": group.get("transactionPlan") or {},
                "warnings": group.get("warnings") or [],
            }
        )

    return {
        "source": result.get("source"),
        "capturedAt": result.get("capturedAt"),
        "status": result.get("status"),
        "reviewOnly": bool(result.get("reviewOnly", True)),
        "fixtureCount": int(result.get("fixtureCount") or 0),
        "processedGroups": int(result.get("processedGroups") or 0),
        "clickedGroups": int(result.get("clickedGroups") or 0),
        "clickedLegs": int(result.get("clickedLegs") or 0),
        "stopReason": result.get("stopReason"),
        "groups": groups,
        "safety": {
            "enteredStakeAmount": False,
            "clickedPlaceBet": False,
            **(result.get("safety") or {}),
        },
    }


def _stake_ui_sgm_board_response(
    *,
    matchup: str,
    slate_date: date | None,
    fixture_slug: str,
    completed: dict[str, Any],
    limit: int,
    side: str,
    market: str,
    scope: str,
    playable_only: bool,
    cache_hit: bool,
) -> dict[str, Any]:
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
            "updatedAt": completed.get("updatedAt"),
            "cacheHit": cache_hit,
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
        "marketDiagnostics": board.get("marketDiagnostics") or {},
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
        if market_key and not sgm_market_filter_matches(row, market):
            continue
        row_scope = str(row.get("scope") or "").lower()
        if scope_key and scope_key != row_scope:
            continue

        for row_side in wanted_sides:
            odds = row.get(row_side)
            if odds is None:
                continue
            row_id = make_sgm_selection_row_id(
                str(board.get("fixtureSlug") or ""),
                row,
                row_side,
            )
            compact_rows.append(
                {
                    "rowId": row_id,
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
                    "playabilityMode": row.get("playabilityMode"),
                    "playabilityWarnings": row.get("playabilityWarnings") or [],
                    "nonPlayableReasons": row.get("nonPlayableReasons") or [],
                    "identifierWarnings": row.get("identifierWarnings") or [],
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
