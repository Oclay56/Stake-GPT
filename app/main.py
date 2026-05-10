from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .dashboard import build_mlb_dashboard
from .gpt_action import (
    build_gpt_decision_result,
    build_gpt_action_openapi_schema,
    build_matchup_picks,
    build_matchup_prop_board,
    build_player_mlb_context,
    require_gpt_api_key,
    validate_gpt_selections,
)
from .line_movement import get_line_movement_history
from .mlb_data import (
    MLBAPIError,
    MLBDataEngine,
    MLBStatsClient,
    build_mlb_http_client,
)
from .mlb_bridge import build_match_audit, enrich_props_with_mlb_data
from .mlb_props import build_stable_props_payload
from .recommendations import (
    settle_recommendation_legs,
    summarize_recommendation_performance,
)
from .slate import (
    DEFAULT_TIMEZONE,
    build_market_slate,
    build_mlb_primary_line_check,
    build_mlb_player_props_slate,
    build_slate,
    render_market_slate_html,
    render_player_props_html,
)
from .stake_client import StakeAPIError, StakeClient, build_http_client
from .storage import SnapshotStore
from .supabase_ledger import (
    fetch_recommendation_performance_from_supabase,
    supabase_ledger_enabled,
    sync_gpt_decision_to_supabase,
    sync_recommendation_result_to_supabase,
    sync_recommendation_settlements_to_supabase,
)


app = FastAPI(
    title="Stake Odds API Wrapper",
    version="0.1.0",
    description="Local read-only wrapper around Stake odds data endpoints.",
)

UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "desktop-concept"
if UI_DIR.exists():
    app.mount("/app", StaticFiles(directory=UI_DIR, html=True), name="azp-app")


async def get_stake_client() -> AsyncIterator[StakeClient]:
    api_key = os.getenv("STAKE_API_KEY") or None
    async with build_http_client() as http_client:
        yield StakeClient(http_client=http_client, api_key=api_key)


async def get_mlb_engine() -> AsyncIterator[MLBDataEngine]:
    async with build_mlb_http_client() as http_client:
        yield MLBDataEngine(MLBStatsClient(http_client))


def get_snapshot_store() -> SnapshotStore:
    return SnapshotStore()


@app.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse(url="/app/")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/gpt/openapi.json", include_in_schema=False)
async def gpt_openapi_schema(request: Request) -> Any:
    return build_gpt_action_openapi_schema(str(request.base_url).rstrip("/"))


@app.get("/gpt/health")
async def gpt_health(_: None = Depends(require_gpt_api_key)) -> dict[str, str]:
    return {"status": "ok", "service": "azp-gpt-action"}


@app.get("/gpt/privacy", include_in_schema=False)
async def gpt_privacy_policy() -> dict[str, Any]:
    return {
        "name": "AZP Suite GPT Action Privacy Policy",
        "summary": (
            "This local read-only action retrieves MLB odds/player prop data from "
            "the configured AZP backend and returns it to your private Custom GPT."
        ),
        "dataUse": [
            "The action does not collect account passwords, Stake login sessions, or payment data.",
            "The action does not place bets or modify any account.",
            "Requests may include matchup names, dates, markets, and filtering preferences.",
            "If hosted or tunneled, requests pass through that host or tunnel provider.",
        ],
    }


@app.get("/gpt/mlb/matchup-picks")
async def gpt_mlb_matchup_picks(
    matchup: str = Query(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    markets: str | None = Query(None),
    side: str = Query("any", pattern="^(any|over|under)$"),
    legs: int = Query(2, ge=1, le=6),
    mode: str = Query("sgp", pattern="^(sgp|standard)$"),
    diversity_mode: str = Query(
        "balanced",
        alias="diversityMode",
        pattern="^(balanced|best_available|strict_diversity|longshot)$",
    ),
    season: int | None = Query(None, ge=1876, le=2100),
    history_limit: int = Query(10, alias="historyLimit", ge=1, le=15),
    recommendation_limit: int = Query(10, alias="recommendationLimit", ge=1, le=25),
    odds_min: float | None = Query(None, alias="oddsMin", ge=1),
    odds_max: float | None = Query(None, alias="oddsMax", ge=1),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
    store: SnapshotStore = Depends(get_snapshot_store),
) -> Any:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    response = await _call_data_sources(
        build_matchup_picks,
        client,
        engine,
        matchup,
        slate_date,
        timezone_name,
        limit,
        markets,
        side,
        legs,
        mode,
        diversity_mode,
        season,
        history_limit,
        recommendation_limit,
        odds_min,
        odds_max,
    )
    if _save_gpt_recommendations_enabled():
        request_params = {
            "matchup": matchup,
            "date": slate_date.isoformat() if slate_date else None,
            "limit": limit,
            "markets": markets,
            "side": side,
            "legs": legs,
            "mode": mode,
            "diversityMode": diversity_mode,
            "season": season,
            "historyLimit": history_limit,
            "recommendationLimit": recommendation_limit,
            "oddsMin": odds_min,
            "oddsMax": odds_max,
        }
        saved = store.save_recommendation_result(
            response,
            request_params=request_params,
        )
        response["recommendationLedger"] = {
            "saved": True,
            "requestId": saved["requestId"],
            "legsSaved": saved["recommendationLegsInserted"],
            "supabaseSynced": False,
        }
        if supabase_ledger_enabled():
            try:
                supabase_result = await sync_recommendation_result_to_supabase(
                    response,
                    request_id=saved["requestId"],
                    request_params=request_params,
                )
                response["recommendationLedger"]["supabaseSynced"] = bool(
                    supabase_result.get("synced")
                )
            except Exception:
                if os.getenv("AZP_FAIL_ON_SUPABASE_LEDGER_ERROR", "").lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }:
                    raise
                response["recommendationLedger"]["supabaseSynced"] = False
                response["recommendationLedger"]["supabaseWarning"] = (
                    "Supabase ledger sync failed; local ledger save still completed."
                )
    return response


@app.get("/gpt/mlb/matchup-prop-board")
async def gpt_mlb_matchup_prop_board(
    matchup: str = Query(..., min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    markets: str | None = Query(None),
    side: str = Query("any", pattern="^(any|over|under)$"),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    return await _call_data_sources(
        build_matchup_prop_board,
        client,
        matchup,
        slate_date,
        timezone_name,
        limit,
        markets,
        side,
    )


@app.get("/gpt/mlb/player-context")
async def gpt_mlb_player_context(
    matchup: str = Query(..., min_length=2),
    prop_id: str = Query(..., alias="propId", min_length=2),
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    markets: str | None = Query(None),
    season: int | None = Query(None, ge=1876, le=2100),
    history_limit: int = Query(15, alias="historyLimit", ge=1, le=15),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    return await _call_data_sources(
        build_player_mlb_context,
        client,
        engine,
        matchup,
        prop_id,
        slate_date,
        timezone_name,
        limit,
        markets,
        season,
        history_limit,
    )


@app.post("/gpt/mlb/validate-selections")
async def gpt_mlb_validate_selections(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    matchup = _required_body_text(payload, "matchup")
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    return await _call_data_sources(
        validate_gpt_selections,
        client,
        matchup,
        _body_selections(payload),
        _body_date(payload),
        timezone_name,
        _body_limit(payload),
        payload.get("markets"),
    )


@app.post("/gpt/mlb/gpt-decisions")
async def gpt_mlb_save_gpt_decision(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_gpt_api_key),
    client: StakeClient = Depends(get_stake_client),
    store: SnapshotStore = Depends(get_snapshot_store),
) -> Any:
    matchup = _required_body_text(payload, "matchup")
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    response = await _call_data_sources(
        build_gpt_decision_result,
        client,
        matchup,
        _body_selections(payload),
        _body_date(payload),
        timezone_name,
        _body_limit(payload),
        payload.get("markets"),
        payload.get("prompt"),
        payload.get("notes") if isinstance(payload.get("notes"), list) else None,
    )
    if _save_gpt_decisions_enabled():
        saved = store.save_gpt_decision_result(response, request_body=payload)
        response["gptDecisionLedger"] = {
            "saved": True,
            "decisionId": saved["decisionId"],
            "legsSaved": saved["gptDecisionLegsInserted"],
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
            except Exception:
                if os.getenv("AZP_FAIL_ON_SUPABASE_LEDGER_ERROR", "").lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }:
                    raise
                response["gptDecisionLedger"]["supabaseWarning"] = (
                    "Supabase GPT decision sync failed; local ledger save still completed."
                )
    return response


@app.get("/gpt/mlb/performance-summary")
async def gpt_mlb_performance_summary(
    slate_date: date | None = Query(None, alias="date"),
    market: str | None = Query(None),
    side: str | None = Query(None, pattern="^(over|under)$"),
    request_id: str | None = Query(None, alias="requestId"),
    diversity_mode: str | None = Query(
        None,
        alias="diversityMode",
        pattern="^(balanced|best_available|strict_diversity|longshot)$",
    ),
    limit: int = Query(500, ge=1, le=500),
    _: None = Depends(require_gpt_api_key),
    store: SnapshotStore = Depends(get_snapshot_store),
) -> Any:
    date_text = slate_date.isoformat() if slate_date else None
    if supabase_ledger_enabled():
        try:
            return await fetch_recommendation_performance_from_supabase(
                date_text=date_text,
                market=market,
                side=side,
                request_id=request_id,
                diversity_mode=diversity_mode,
                limit=limit,
            )
        except Exception:
            if os.getenv("AZP_FAIL_ON_SUPABASE_LEDGER_ERROR", "").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }:
                raise

    summary = summarize_recommendation_performance(
        store,
        date_text=date_text,
        market=market,
        side=side,
        request_id=request_id,
        diversity_mode=diversity_mode,
        limit=limit,
    )
    return {
        "source": "local_sqlite",
        **summary,
        "supabaseWarning": (
            "Supabase is not configured or could not be reached; returned local ledger only."
        )
        if supabase_ledger_enabled()
        else None,
    }


@app.get("/gpt/mlb/settle-recommendations")
async def gpt_mlb_settle_recommendations(
    slate_date: date | None = Query(None, alias="date"),
    market: str | None = Query(None),
    side: str | None = Query(None, pattern="^(over|under)$"),
    request_id: str | None = Query(None, alias="requestId"),
    diversity_mode: str | None = Query(
        None,
        alias="diversityMode",
        pattern="^(balanced|best_available|strict_diversity|longshot)$",
    ),
    season: int | None = Query(None, ge=1876, le=2100),
    limit: int = Query(500, ge=1, le=500),
    history_limit: int = Query(30, alias="historyLimit", ge=1, le=100),
    _: None = Depends(require_gpt_api_key),
    engine: MLBDataEngine = Depends(get_mlb_engine),
    store: SnapshotStore = Depends(get_snapshot_store),
) -> Any:
    date_text = slate_date.isoformat() if slate_date else None
    try:
        result = await settle_recommendation_legs(
            store,
            engine,
            date_text=date_text,
            market=market,
            side=side,
            request_id=request_id,
            diversity_mode=diversity_mode,
            season=season,
            limit=limit,
            history_limit=history_limit,
        )
    except MLBAPIError as exc:
        status_code = exc.status_code if 400 <= exc.status_code <= 599 else 502
        raise HTTPException(status_code=status_code, detail=exc.message) from exc

    result = {
        "source": "local_sqlite_plus_mlb_stats",
        **result,
        "settlementLedger": {
            "saved": True,
            "supabaseSynced": False,
        },
    }
    if supabase_ledger_enabled():
        try:
            supabase_result = await sync_recommendation_settlements_to_supabase(
                result.get("rows") or []
            )
            result["settlementLedger"]["supabaseSynced"] = bool(
                supabase_result.get("synced")
            )
            result["settlementLedger"]["settlementsSynced"] = supabase_result.get(
                "settlementsSynced",
                0,
            )
        except Exception:
            if os.getenv("AZP_FAIL_ON_SUPABASE_LEDGER_ERROR", "").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }:
                raise
            result["settlementLedger"]["supabaseWarning"] = (
                "Supabase settlement sync failed; local settlement save still completed."
            )
    return result


@app.get("/dashboard/mlb")
async def mlb_dashboard(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(5, ge=1, le=25),
    snapshot_phase: str | None = Query(None, alias="snapshotPhase"),
    profile: str = Query("custom"),
    store: SnapshotStore = Depends(get_snapshot_store),
) -> Any:
    date_text = slate_date.isoformat() if slate_date else None
    return build_mlb_dashboard(
        store=store,
        date_text=date_text,
        limit=limit,
        snapshot_phase=snapshot_phase,
        profile=profile,
    )


@app.get("/dashboard/mlb/view", include_in_schema=False)
async def mlb_dashboard_view() -> RedirectResponse:
    return RedirectResponse(url="/app/")


@app.get("/sports")
async def sports(client: StakeClient = Depends(get_stake_client)) -> Any:
    return await _call_stake(client.get_sports)


@app.get("/sports/{sport}/categories")
async def sport_categories(
    sport: str,
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _call_stake(client.get_sport_categories, sport)


@app.get("/schedule/{sport}")
async def sport_schedule(
    sport: str,
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _call_stake(client.get_sport_schedule, sport)


@app.get("/fixtures/{fixture_slug}")
async def fixture(
    fixture_slug: str,
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _call_stake(client.get_fixture, fixture_slug)


@app.get("/odds/{fixture_slug}")
async def odds(
    fixture_slug: str,
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _call_stake(client.get_odds, fixture_slug)


@app.get("/slate/{sport}")
async def slate(
    sport: str,
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    return await _call_stake(build_slate, client, sport, slate_date, timezone_name, limit)


@app.get("/slate/{sport}/markets")
async def slate_markets(
    sport: str,
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    return await _call_stake(
        build_market_slate,
        client,
        sport,
        slate_date,
        timezone_name,
        limit,
    )


@app.get("/slate/{sport}/view", response_class=HTMLResponse)
async def slate_view(
    sport: str,
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    client: StakeClient = Depends(get_stake_client),
) -> HTMLResponse:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    market_slate = await _call_stake(
        build_market_slate,
        client,
        sport,
        slate_date,
        timezone_name,
        limit,
    )
    return HTMLResponse(render_market_slate_html(market_slate))


@app.get("/slate/mlb/player-props")
async def mlb_player_props(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    line_mode: str = Query("primary", alias="lineMode", pattern="^(primary|all)$"),
    markets: str | None = Query(None),
    exclude_markets: str | None = Query(None, alias="excludeMarkets"),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _mlb_player_props_response(
        client=client,
        slate_date=slate_date,
        limit=limit,
        line_mode=line_mode,
        markets=markets,
        exclude_markets=exclude_markets,
    )


@app.get("/mlb/player-props")
async def mlb_player_props_alias(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    line_mode: str = Query("primary", alias="lineMode", pattern="^(primary|all)$"),
    markets: str | None = Query(None),
    exclude_markets: str | None = Query(None, alias="excludeMarkets"),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    return await _mlb_player_props_response(
        client=client,
        slate_date=slate_date,
        limit=limit,
        line_mode=line_mode,
        markets=markets,
        exclude_markets=exclude_markets,
    )


@app.get("/mlb/props")
async def mlb_props(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    markets: str | None = Query(None),
    exclude_markets: str | None = Query(None, alias="excludeMarkets"),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    player_props_slate = await _mlb_player_props_response(
        client=client,
        slate_date=slate_date,
        limit=limit,
        line_mode="primary",
        markets=markets,
        exclude_markets=exclude_markets,
    )
    return build_stable_props_payload(player_props_slate)


@app.get("/mlb/props/enriched")
async def mlb_props_enriched(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    markets: str | None = Query(None),
    exclude_markets: str | None = Query(None, alias="excludeMarkets"),
    season: int | None = Query(None, ge=1876, le=2100),
    group_mode: str = Query("auto", alias="group", pattern="^(auto|hitting|pitching)$"),
    history_limit: int = Query(5, alias="historyLimit", ge=1, le=25),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    player_props_slate = await _mlb_player_props_response(
        client=client,
        slate_date=slate_date,
        limit=limit,
        line_mode="primary",
        markets=markets,
        exclude_markets=exclude_markets,
    )
    props_payload = build_stable_props_payload(player_props_slate)
    return await _call_mlb(
        enrich_props_with_mlb_data,
        props_payload,
        engine,
        season,
        group_mode,
        history_limit,
    )


@app.get("/mlb/props/match-audit")
async def mlb_props_match_audit(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    markets: str | None = Query(None),
    exclude_markets: str | None = Query(None, alias="excludeMarkets"),
    season: int | None = Query(None, ge=1876, le=2100),
    group_mode: str = Query("auto", alias="group", pattern="^(auto|hitting|pitching)$"),
    history_limit: int = Query(5, alias="historyLimit", ge=1, le=25),
    client: StakeClient = Depends(get_stake_client),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    enriched = await mlb_props_enriched(
        slate_date=slate_date,
        limit=limit,
        markets=markets,
        exclude_markets=exclude_markets,
        season=season,
        group_mode=group_mode,
        history_limit=history_limit,
        client=client,
        engine=engine,
    )
    return build_match_audit(enriched)


@app.get("/mlb/line-movement")
async def mlb_line_movement() -> Any:
    return get_line_movement_history()


@app.get("/mlb/primary-line-check")
async def mlb_primary_line_check(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    markets: str | None = Query(None),
    exclude_markets: str | None = Query(None, alias="excludeMarkets"),
    client: StakeClient = Depends(get_stake_client),
) -> Any:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    return await _call_stake(
        build_mlb_primary_line_check,
        client,
        slate_date,
        timezone_name,
        limit,
        _parse_market_filter(markets),
        _parse_market_filter(exclude_markets),
    )


@app.get("/mlb-data/teams")
async def mlb_data_teams(
    season: int | None = Query(None, ge=1876, le=2100),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_mlb(engine.get_teams, season)


@app.get("/mlb-data/schedule")
async def mlb_data_schedule(
    game_date: date = Query(..., alias="date"),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_mlb(engine.get_schedule, game_date.isoformat())


@app.get("/mlb-data/teams/{team_id}/roster")
async def mlb_data_team_roster(
    team_id: int,
    season: int | None = Query(None, ge=1876, le=2100),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_mlb(engine.get_team_roster, team_id, season)


@app.get("/mlb-data/players/search")
async def mlb_data_player_search(
    query: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=100),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_mlb(engine.search_players, query, limit)


@app.get("/mlb-data/players/{player_id}")
async def mlb_data_player(
    player_id: int,
    season: int | None = Query(None, ge=1876, le=2100),
    group: str = Query("hitting", pattern="^(hitting|pitching)$"),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_mlb(engine.get_player_profile, player_id, season, group)


@app.get("/mlb-data/players/{player_id}/history")
async def mlb_data_player_history(
    player_id: int,
    season: int | None = Query(None, ge=1876, le=2100),
    group: str = Query("hitting", pattern="^(hitting|pitching)$"),
    limit: int = Query(10, ge=1, le=100),
    engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
    return await _call_mlb(
        engine.get_player_recent_history,
        player_id,
        group,
        season,
        limit,
    )


@app.get("/slate/mlb/player-props/view", response_class=HTMLResponse)
async def mlb_player_props_view(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    line_mode: str = Query("primary", alias="lineMode", pattern="^(primary|all)$"),
    markets: str | None = Query(None),
    exclude_markets: str | None = Query(None, alias="excludeMarkets"),
    refresh_seconds: int = Query(30, alias="refreshSeconds", ge=5, le=300),
    client: StakeClient = Depends(get_stake_client),
) -> HTMLResponse:
    return await _mlb_player_props_view_response(
        client=client,
        slate_date=slate_date,
        limit=limit,
        line_mode=line_mode,
        markets=markets,
        exclude_markets=exclude_markets,
        refresh_seconds=refresh_seconds,
        data_path="/slate/mlb/player-props",
    )


@app.get("/mlb/player-props/view", response_class=HTMLResponse)
async def mlb_player_props_view_alias(
    slate_date: date | None = Query(None, alias="date"),
    limit: int = Query(25, ge=1, le=100),
    line_mode: str = Query("primary", alias="lineMode", pattern="^(primary|all)$"),
    markets: str | None = Query(None),
    exclude_markets: str | None = Query(None, alias="excludeMarkets"),
    refresh_seconds: int = Query(30, alias="refreshSeconds", ge=5, le=300),
    client: StakeClient = Depends(get_stake_client),
) -> HTMLResponse:
    return await _mlb_player_props_view_response(
        client=client,
        slate_date=slate_date,
        limit=limit,
        line_mode=line_mode,
        markets=markets,
        exclude_markets=exclude_markets,
        refresh_seconds=refresh_seconds,
        data_path="/mlb/player-props",
    )


async def _mlb_player_props_response(
    client: StakeClient,
    slate_date: date | None,
    limit: int,
    line_mode: str,
    markets: str | None,
    exclude_markets: str | None,
) -> Any:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    return await _call_stake(
        build_mlb_player_props_slate,
        client,
        slate_date,
        timezone_name,
        limit,
        line_mode,
        _parse_market_filter(markets),
        _parse_market_filter(exclude_markets),
    )


async def _mlb_player_props_view_response(
    client: StakeClient,
    slate_date: date | None,
    limit: int,
    line_mode: str,
    markets: str | None,
    exclude_markets: str | None,
    refresh_seconds: int,
    data_path: str,
) -> HTMLResponse:
    player_props_slate = await _mlb_player_props_response(
        client=client,
        slate_date=slate_date,
        limit=limit,
        line_mode=line_mode,
        markets=markets,
        exclude_markets=exclude_markets,
    )
    data_params: dict[str, str | int] = {}
    if slate_date:
        data_params["date"] = slate_date.isoformat()
    data_params["limit"] = limit
    data_params["lineMode"] = line_mode
    if markets:
        data_params["markets"] = markets
    if exclude_markets:
        data_params["excludeMarkets"] = exclude_markets
    data_url = f"{data_path}?{urlencode(data_params)}"
    return HTMLResponse(
        render_player_props_html(
            player_props_slate,
            data_url=data_url,
            refresh_seconds=refresh_seconds,
        )
    )


def _parse_market_filter(value: str | None) -> set[str]:
    if not value:
        return set()

    return {part.strip() for part in value.split(",") if part.strip()}


def _save_gpt_recommendations_enabled() -> bool:
    value = os.getenv("AZP_SAVE_GPT_RECOMMENDATIONS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _save_gpt_decisions_enabled() -> bool:
    value = os.getenv("AZP_SAVE_GPT_DECISIONS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _required_body_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value.strip()) < 2:
        raise HTTPException(status_code=422, detail=f"Missing required field: {key}")
    return value.strip()


def _body_selections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    selections = payload.get("selections")
    if not isinstance(selections, list) or not selections:
        raise HTTPException(status_code=422, detail="Missing required field: selections")
    clean = [selection for selection in selections if isinstance(selection, dict)]
    if not clean:
        raise HTTPException(status_code=422, detail="Selections must be objects.")
    return clean


def _body_date(payload: dict[str, Any]) -> date | None:
    value = payload.get("date")
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD.") from exc


def _body_limit(payload: dict[str, Any]) -> int:
    try:
        value = int(payload.get("limit", 25))
    except (TypeError, ValueError):
        value = 25
    return max(1, min(value, 100))


async def _call_stake(method: Any, *args: Any) -> Any:
    try:
        return await method(*args)
    except StakeAPIError as exc:
        status_code = exc.status_code if 400 <= exc.status_code <= 599 else 502
        raise HTTPException(status_code=status_code, detail=exc.message) from exc


async def _call_mlb(method: Any, *args: Any) -> Any:
    try:
        return await method(*args)
    except MLBAPIError as exc:
        status_code = exc.status_code if 400 <= exc.status_code <= 599 else 502
        raise HTTPException(status_code=status_code, detail=exc.message) from exc


async def _call_data_sources(method: Any, *args: Any) -> Any:
    try:
        return await method(*args)
    except StakeAPIError as exc:
        status_code = exc.status_code if 400 <= exc.status_code <= 599 else 502
        raise HTTPException(status_code=status_code, detail=exc.message) from exc
    except MLBAPIError as exc:
        status_code = exc.status_code if 400 <= exc.status_code <= 599 else 502
        raise HTTPException(status_code=status_code, detail=exc.message) from exc
