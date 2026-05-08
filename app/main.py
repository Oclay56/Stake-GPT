from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .line_movement import get_line_movement_history
from .mlb_props import build_stable_props_payload
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


app = FastAPI(
    title="Stake Odds API Wrapper",
    version="0.1.0",
    description="Local read-only wrapper around Stake odds data endpoints.",
)


async def get_stake_client() -> AsyncIterator[StakeClient]:
    api_key = os.getenv("STAKE_API_KEY") or None
    async with build_http_client() as http_client:
        yield StakeClient(http_client=http_client, api_key=api_key)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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


async def _call_stake(method: Any, *args: Any) -> Any:
    try:
        return await method(*args)
    except StakeAPIError as exc:
        status_code = exc.status_code if 400 <= exc.status_code <= 599 else 502
        raise HTTPException(status_code=status_code, detail=exc.message) from exc
