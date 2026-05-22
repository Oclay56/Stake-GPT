from __future__ import annotations

import hmac
import os
import re
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Header, HTTPException

from .decision_profiles import (
    build_decision_profile,
    decision_profile_summary,
    evidence_check,
    evidence_windows,
    market_heatmap,
    season_evidence,
    trend_labels,
)
from .mlb_bridge import (
    clear_mlb_bridge_cache,
    enrich_props_with_mlb_data,
    stat_mapping_for_market,
)
from .mlb_props import build_stable_props_payload, slug_key
from .slate import DEFAULT_TIMEZONE, build_mlb_matchups, build_mlb_player_props_slate
from .slip_builder import build_slip_candidate_response


DEFAULT_MIN_PLAYABLE_ODDS = 1.10
DEFAULT_BOARD_LIMIT = 25


def build_gpt_action_openapi_schema(server_url: str) -> dict[str, Any]:
    clean_server = str(server_url or "").rstrip("/") or "http://127.0.0.1:8000"
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "AZP Suite GPT Data API",
            "version": "0.2.0",
            "description": (
                "Structured data API for a Custom GPT. The GPT owns final betting "
                "decisions; this backend only provides Stake availability, MLB "
                "context, validation, and decision logging."
            ),
        },
        "servers": [{"url": clean_server}],
        "paths": {
            "/gpt/health": {
                "get": _operation(
                    "getAzpHealth",
                    "Check backend health",
                    "Returns whether the GPT data backend is available.",
                )
            },
            "/mlb/matchups": {
                "get": _operation(
                    "getMlbMatchups",
                    "Get Stake-backed MLB matchups",
                    "Returns current MLB fixtures available from Stake's odds schedule.",
                    parameters=[_date_param(), _limit_param()],
                )
            },
            "/mlb/schedule": {
                "get": _operation(
                    "getMlbSchedule",
                    "Get official MLB schedule",
                    (
                        "Returns official MLB games for a date from MLB Stats API. "
                        "Use this for game discovery, not bet availability."
                    ),
                    parameters=[_date_param()],
                )
            },
            "/mlb/schedule/stake-map": {
                "get": _operation(
                    "mapMlbScheduleToStake",
                    "Map official MLB schedule to Stake fixtures",
                    (
                        "Returns official MLB games with matching Stake fixtures "
                        "when Stake offers the matchup."
                    ),
                    parameters=[_date_param(), _limit_param()],
                )
            },
            "/mlb/matchup/{matchup}/markets": {
                "get": _operation(
                    "getAvailableMarkets",
                    "Get available Stake markets for a matchup",
                    "Discovers market names currently present in the Stake prop feed.",
                    parameters=[_matchup_path_param(), _date_param(), _limit_param()],
                )
            },
            "/mlb/matchup/{matchup}/props": {
                "get": _operation(
                    "getMatchupPropBoard",
                    "Get Stake props for a matchup",
                    "Returns line-specific Stake selections for one matchup. Use focused board-summary, prop-page, and comparison-board calls for broad scans.",
                    parameters=[
                        _matchup_path_param(),
                        _date_param(),
                        _limit_param(),
                        _market_query_param(),
                        _side_query_param(),
                        _line_mode_param(),
                    ],
                )
            },
            "/mlb/matchup/{matchup}/board-summary": {
                "get": _operation(
                    "getBoardSummary",
                    "Get compact board summary",
                    "Returns counts, market coverage, context coverage, and warnings without dumping the full prop feed.",
                    parameters=[
                        _matchup_path_param(),
                        _date_param(),
                        _limit_param(),
                        _market_query_param(),
                        _side_query_param(),
                        _line_mode_param(),
                    ],
                )
            },
            "/mlb/matchup/{matchup}/prop-page": {
                "get": _operation(
                    "getPropPage",
                    "Get a filtered page of Stake props",
                    "Returns compact paginated rows for GPT navigation. This is not a recommendation endpoint.",
                    parameters=[
                        _matchup_path_param(),
                        _date_param(),
                        _limit_param(),
                        _market_query_param(),
                        _side_query_param(),
                        _line_mode_param(),
                        _page_param(),
                        _page_size_param(),
                        _primary_only_param(),
                        _playable_only_param(),
                        _context_quality_param(),
                    ],
                )
            },
            "/mlb/matchup/{matchup}/comparison-board": {
                "get": _operation(
                    "getComparisonBoard",
                    "Get compact prop comparison rows",
                    "Returns filtered Stake props with compact MLB helper metrics for comparison. The GPT still owns the final decision.",
                    parameters=[
                        _matchup_path_param(),
                        _date_param(),
                        _limit_param(),
                        _market_query_param(),
                        _side_query_param(),
                        _line_mode_param(),
                        _page_param(),
                        _page_size_param(maximum=50),
                        _primary_only_param(),
                        _playable_only_param(),
                        _context_quality_param(),
                        _season_param(),
                        _history_limit_param(),
                    ],
                )
            },
            "/mlb/build-slip-candidates": {
                "post": _operation(
                    "buildSlipCandidates",
                    "Build constrained slip candidates",
                    "Assembles candidate slips from Stake-backed comparison rows. This is support data, not final GPT recommendation authority.",
                    request_body=_slip_candidate_request_body(),
                )
            },
            "/mlb/stake-ui/sgm-board": {
                "post": _operation(
                    "getStakeUiSgmBoard",
                    "Get Stake UI Same Game Multi board",
                    (
                        "Creates a local-helper job that reads the exact Stake UI "
                        "Same Game Multi board through the user's Chrome/VPN session. "
                        "Use this as the source of truth before final SGM picks."
                    ),
                    request_body=_stake_ui_sgm_request_body(),
                )
            },
            "/mlb/stake-ui/mlb-games": {
                "post": _operation(
                    "getStakeUiMlbGames",
                    "Get Stake UI MLB games",
                    (
                        "Creates a local-helper job that reads the visible Stake MLB "
                        "game index through the user's Chrome/VPN session. Use this "
                        "before multi-game SGM work so fixture slugs come from the UI."
                    ),
                    request_body=_stake_ui_mlb_games_request_body(),
                )
            },
            "/mlb/stake-ui/state": {
                "post": _operation(
                    "readStakeUiState",
                    "Read Stake UI helper state",
                    (
                        "Optional diagnostic action. Use only when the SGM board/build "
                        "flow fails, looks stale, or the user asks what happened. It "
                        "reports the current Stake page, fixture, SGM visibility, "
                        "login/region/Cloudflare state, and sidebar state."
                    ),
                    request_body=_stake_ui_state_request_body(),
                )
            },
            "/mlb/stake-ui/clear-sgm-selections": {
                "post": _operation(
                    "clearStakeUiSgmSelections",
                    "Clear pending SGM selections",
                    (
                        "Optional recovery action. Use only after a failed or partial "
                        "SGM build when selected SGM rows need to be cleared before a "
                        "retry. It does not clear the visible sidebar slip and never "
                        "places a bet."
                    ),
                    request_body=_stake_ui_clear_sgm_selections_request_body(),
                )
            },
            "/mlb/stake-ui/remove-sidebar-group": {
                "post": _operation(
                    "removeStakeUiSidebarGroup",
                    "Remove Stake UI sidebar group",
                    (
                        "Optional recovery action. Removes one already-added right-sidebar "
                        "SGM/game group by exact fixture or matchup. Use only when the user "
                        "asks to delete one game from the visible review slip. It never "
                        "clears the whole slip, enters stake amount, or clicks Place Bet."
                    ),
                    request_body=_stake_ui_remove_sidebar_group_request_body(),
                )
            },
            "/mlb/stake-ui/clear-sidebar": {
                "post": _operation(
                    "clearStakeUiSidebar",
                    "Clear Stake UI sidebar",
                    (
                        "Optional recovery action. Clears the whole visible right-sidebar "
                        "review slip by clicking Stake's Clear Bet/Clear Bets control. "
                        "Use only when the user asks to wipe the visible slip. It never "
                        "enters stake amount or clicks Place Bet."
                    ),
                    request_body=_stake_ui_clear_sidebar_request_body(),
                )
            },
            "/mlb/stake-ui/review-slip": {
                "post": _operation(
                    "buildStakeUiReviewSlip",
                    "Build Stake UI review slip",
                    (
                        "Creates a local-helper job that clicks exact validated SGM "
                        "legs into the user's Stake slip for review only. This action "
                        "must never enter stake amount or click Place Bet."
                    ),
                    request_body=_stake_ui_review_slip_request_body(),
                )
            },
            "/mlb/stake-ui/review-slip-batch": {
                "post": _operation(
                    "buildStakeUiReviewSlipBatch",
                    "Build batch Stake UI review slip",
                    (
                        "Creates one local-helper batch job that navigates one Stake "
                        "page through multiple fixture SGM boards and adds each exact "
                        "validated group into the same visible slip for review only. "
                        "This action must never enter stake amount or click Place Bet."
                    ),
                    request_body=_stake_ui_review_slip_batch_request_body(),
                )
            },
            "/mlb/matchup/{matchup}/probable-pitchers": {
                "get": _operation(
                    "getProbablePitchers",
                    "Get MLB probable pitchers for a matchup",
                    "Returns probable pitcher context from MLB Stats API where available.",
                    parameters=[_matchup_path_param(), _date_param()],
                )
            },
            "/mlb/matchup/{matchup}/market-map": {
                "get": _operation(
                    "getMarketMap",
                    "Get market mapping for a matchup",
                    "Returns discovered Stake market names mapped to backend stat keys where supported.",
                    parameters=[_matchup_path_param(), _date_param(), _limit_param()],
                )
            },
            "/mlb/player/{playerId}/context": {
                "get": _operation(
                    "getPlayerMlbContext",
                    "Get MLB context for a player",
                    "Returns player season stats and recent windows for the requested market.",
                    parameters=[
                        _player_id_path_param(),
                        _market_query_param(required=False),
                        _date_param(),
                        _season_param(),
                        _history_limit_param(),
                    ],
                )
            },
            "/mlb/player/{playerId}/recent": {
                "get": _operation(
                    "getPlayerRecentLogs",
                    "Get recent MLB game logs for a player",
                    "Returns recent MLB game logs for a market's stat group.",
                    parameters=[
                        _player_id_path_param(),
                        _market_query_param(required=False),
                        _season_param(),
                        _history_limit_param(),
                    ],
                )
            },
            "/mlb/player/{playerId}/season": {
                "get": _operation(
                    "getPlayerSeasonStats",
                    "Get MLB season stats for a player",
                    "Returns MLB season stats for a market's stat group.",
                    parameters=[
                        _player_id_path_param(),
                        _market_query_param(required=False),
                        _season_param(),
                    ],
                )
            },
            "/mlb/prop/{propId}/context": {
                "get": _operation(
                    "getSpecificPropContext",
                    "Get MLB context for one Stake prop",
                    "Looks up a Stake selection from the current board and enriches it with MLB context.",
                    parameters=[
                        _prop_id_path_param(),
                        _matchup_query_param(),
                        _date_param(),
                        _limit_param(),
                        _market_query_param(required=False),
                        _side_query_param(),
                        _season_param(),
                        _history_limit_param(),
                    ],
                )
            },
            "/mlb/prop-context-batch": {
                "post": _operation(
                    "getPropContextBatch",
                    "Get MLB context for selected Stake props",
                    "Returns exact-side MLB context for up to 20 selected Stake prop IDs in one call.",
                    request_body=_prop_context_batch_request_body(),
                )
            },
            "/mlb/validate-selections": {
                "post": _operation(
                    "validateSelections",
                    "Validate GPT selections against current Stake availability",
                    "Confirms exact prop id, side, line, odds, status, and playable state before the GPT answers.",
                    request_body=_selection_request_body(),
                )
            },
            "/mlb/save-gpt-decision": {
                "post": _operation(
                    "saveGptDecision",
                    "Save a GPT-authored decision",
                    "Stores what the GPT chose after validation. This is not an AZP recommendation.",
                    request_body=_selection_request_body(include_prompt=True),
                )
            },
        },
    }
    if os.getenv("AZP_GPT_API_KEY"):
        schema["components"] = {
            "securitySchemes": {
                "AzpApiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-AZP-API-Key",
                }
            }
        }
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                operation["security"] = [{"AzpApiKey": []}]
    return schema


def require_gpt_api_key_value(provided_key: str | None) -> None:
    configured_key = os.getenv("AZP_GPT_API_KEY")
    if not configured_key:
        return None
    if provided_key and hmac.compare_digest(provided_key, configured_key):
        return None
    raise HTTPException(status_code=401, detail="Invalid AZP GPT API key.")


def require_gpt_api_key(
    x_azp_api_key: str | None = Header(default=None, alias="X-AZP-API-Key"),
) -> None:
    return require_gpt_api_key_value(x_azp_api_key)


async def build_matchups(
    stake_client: Any,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
) -> dict[str, Any]:
    return await build_mlb_matchups(
        client=stake_client,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
    )


async def build_available_markets(
    stake_client: Any,
    matchup: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
) -> dict[str, Any]:
    board = await build_matchup_prop_board(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=None,
        side="any",
    )
    return {
        "decisionOwner": "custom_gpt",
        "matchup": board["matchup"],
        "date": board["date"],
        "timezone": board["timezone"],
        "matchedFixtureCount": board["matchedFixtureCount"],
        "marketCount": len(board["markets"]),
        "markets": board["markets"],
    }


async def build_matchup_prop_board(
    stake_client: Any,
    matchup: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
    markets: str | None = None,
    side: str = "any",
    line_mode: str = "primary",
) -> dict[str, Any]:
    props_payload = await _build_matchup_props_payload(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
        line_mode=line_mode,
    )
    clean_side = _clean_side(side)
    selections = _side_level_selections(props_payload.get("props") or [], clean_side)
    market_map = _market_map_from_props(props_payload.get("props") or [])

    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_odds_api",
        "matchup": matchup,
        "date": props_payload.get("date"),
        "timezone": props_payload.get("timezone"),
        "filters": {
            "markets": sorted(_clean_market_csv(markets)),
            "side": clean_side,
            "lineMode": line_mode,
            "minPlayableOdds": _minimum_playable_odds(),
        },
        "matchedFixtureCount": props_payload.get("fixtureCount", 0),
        "propCount": props_payload.get("propCount", 0),
        "availableSelectionCount": len(selections),
        "markets": _market_summary(props_payload.get("props") or []),
        "marketMap": market_map,
        "props": [_board_prop(prop) for prop in props_payload.get("props") or []],
        "selections": selections,
        "notes": _board_notes(props_payload, selections),
        "generatedAt": _utc_now(),
    }


async def build_board_summary(
    stake_client: Any,
    matchup: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
    markets: str | None = None,
    side: str = "any",
    line_mode: str = "primary",
) -> dict[str, Any]:
    props_payload = await _build_matchup_props_payload(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
        line_mode=line_mode,
    )
    clean_side = _clean_side(side)
    selections = _side_level_selections(props_payload.get("props") or [], clean_side)
    filtered = _filter_selections(
        selections,
        primary_only=False,
        playable_only=False,
        context_quality="any",
    )

    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_odds_api",
        "purpose": "board_navigation_summary",
        "matchup": matchup,
        "date": props_payload.get("date"),
        "timezone": props_payload.get("timezone"),
        "filters": {
            "markets": sorted(_clean_market_csv(markets)),
            "side": clean_side,
            "lineMode": line_mode,
            "minPlayableOdds": _minimum_playable_odds(),
        },
        "matchedFixtureCount": props_payload.get("fixtureCount", 0),
        "totalPropsScanned": props_payload.get("propCount", 0),
        "totalSelectionsScanned": len(filtered),
        "playableSelections": sum(1 for row in filtered if row.get("playable")),
        "markets": _summary_markets(filtered),
        "sides": _count_by(filtered, "side"),
        "lineSources": _count_by(filtered, "lineSource"),
        "contextCoverage": _context_coverage(filtered),
        "marketHeatmap": market_heatmap([_compact_selection_row(row) for row in filtered]),
        "warningCounts": _warning_counts(filtered),
        "warnings": _summary_warnings(props_payload, filtered),
        "nextSteps": [
            "Use getPropPage for focused market/side pages.",
            "Use getComparisonBoard for compact MLB helper metrics on filtered candidates.",
            "Use getPropContextBatch for finalists, then validateSelections before answering.",
        ],
        "generatedAt": _utc_now(),
    }


async def build_prop_page(
    stake_client: Any,
    matchup: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
    markets: str | None = None,
    side: str = "any",
    line_mode: str = "primary",
    page: int = 1,
    page_size: int = 30,
    primary_only: bool = False,
    playable_only: bool = True,
    context_quality: str = "any",
) -> dict[str, Any]:
    props_payload = await _build_matchup_props_payload(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
        line_mode=line_mode,
    )
    selections = _filtered_selection_rows(
        props_payload.get("props") or [],
        side=side,
        primary_only=primary_only,
        playable_only=playable_only,
        context_quality=context_quality,
    )
    pagination = _paginate(selections, page=page, page_size=page_size)
    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_odds_api",
        "purpose": "board_navigation_page",
        "matchup": matchup,
        "date": props_payload.get("date"),
        "timezone": props_payload.get("timezone"),
        "filters": _navigation_filters(
            markets=markets,
            side=side,
            line_mode=line_mode,
            primary_only=primary_only,
            playable_only=playable_only,
            context_quality=context_quality,
        ),
        **pagination["meta"],
        "rows": [_compact_selection_row(row) for row in pagination["items"]],
        "generatedAt": _utc_now(),
    }


async def build_comparison_board(
    stake_client: Any,
    mlb_engine: Any,
    matchup: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
    markets: str | None = None,
    side: str = "any",
    line_mode: str = "primary",
    page: int = 1,
    page_size: int = 30,
    primary_only: bool = False,
    playable_only: bool = True,
    context_quality: str = "supported",
    season: int | None = None,
    history_limit: int = 15,
) -> dict[str, Any]:
    props_payload = await _build_matchup_props_payload(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
        line_mode=line_mode,
    )
    selections = _filtered_selection_rows(
        props_payload.get("props") or [],
        side=side,
        primary_only=primary_only,
        playable_only=playable_only,
        context_quality=context_quality,
    )
    selected_prop_ids = {row.get("propId") for row in selections}
    selected_props = [
        prop
        for prop in props_payload.get("props") or []
        if prop.get("propId") in selected_prop_ids
    ]
    enriched = await enrich_props_with_mlb_data(
        {**props_payload, "props": selected_props, "propCount": len(selected_props)},
        mlb_engine,
        season=season,
        history_limit=min(_clean_int(history_limit, 1, 15), 15),
    )
    props_by_id = {prop.get("propId"): prop for prop in enriched.get("props") or []}
    rows = sorted(
        [
            _comparison_selection_row(row, props_by_id.get(row.get("propId")) or {})
            for row in selections
        ],
        key=_comparison_sort_key,
        reverse=True,
    )
    pagination = _paginate(rows, page=page, page_size=min(page_size, 50))
    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_odds_api+mlb_stats_api",
        "purpose": "compact_comparison_board",
        "matchup": matchup,
        "date": props_payload.get("date"),
        "timezone": props_payload.get("timezone"),
        "filters": _navigation_filters(
            markets=markets,
            side=side,
            line_mode=line_mode,
            primary_only=primary_only,
            playable_only=playable_only,
            context_quality=context_quality,
        ),
        **pagination["meta"],
        "decisionProfileSummary": decision_profile_summary(rows),
        "marketHeatmap": market_heatmap(rows),
        "rows": pagination["items"],
        "notes": [
            "Rows are compact helper metrics, not AZP picks.",
            "Use getPropContextBatch for full finalist context before validateSelections.",
        ],
        "generatedAt": _utc_now(),
    }


async def build_slip_candidates(
    stake_client: Any,
    mlb_engine: Any,
    matchup: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
    markets: str | None = None,
    side: str = "any",
    season: int | None = None,
    history_limit: int = 15,
    target_odds_min: float = 2.0,
    target_odds_max: float | None = None,
    min_legs: int = 2,
    max_legs: int = 8,
    mode: str = "balanced",
    quality_floor: float = 55.0,
    allow_no_pick: bool = True,
) -> dict[str, Any]:
    comparison = await build_comparison_board(
        stake_client=stake_client,
        mlb_engine=mlb_engine,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
        side=side,
        line_mode="primary",
        page=1,
        page_size=50,
        primary_only=False,
        playable_only=True,
        context_quality="supported",
        season=season or (slate_date.year if slate_date else None),
        history_limit=history_limit,
    )
    response = build_slip_candidate_response(
        comparison.get("rows") or [],
        target_odds_min=max(1.0, _float_or_none(target_odds_min) or 2.0),
        target_odds_max=_float_or_none(target_odds_max),
        min_legs=_clean_int(min_legs, 1, 25),
        max_legs=_clean_int(max_legs, 1, 25),
        preferred_markets=sorted(_clean_market_csv(markets)),
        preferred_side=_clean_side(side),
        mode=mode,
        quality_floor=max(0.0, min(_float_or_none(quality_floor) or 55.0, 100.0)),
        allow_no_pick=bool(allow_no_pick),
    )
    return {
        **response,
        "matchup": matchup,
        "date": comparison.get("date"),
        "timezone": comparison.get("timezone"),
        "comparisonFilters": comparison.get("filters"),
        "generatedAt": _utc_now(),
    }


async def build_prop_context_batch(
    stake_client: Any,
    mlb_engine: Any,
    matchup: str,
    selections: list[dict[str, Any]],
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
    markets: str | None = None,
    season: int | None = None,
    history_limit: int = 15,
) -> dict[str, Any]:
    props_payload = await _build_matchup_props_payload(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
        line_mode="primary",
    )
    all_selections = _side_level_selections(props_payload.get("props") or [], "any")
    requested = selections[:20]
    found: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    missing = []

    for index, request in enumerate(requested, start=1):
        identifier = request.get("selectionId") or request.get("propId")
        requested_side = _clean_side(request.get("side") or "any")
        selection = _find_selection(all_selections, identifier, side=requested_side)
        if not selection:
            missing.append({"index": index, "requested": request, "status": "missing_selection"})
            continue
        prop = _find_prop(props_payload.get("props") or [], selection.get("propId"))
        if not prop:
            missing.append({"index": index, "requested": request, "status": "missing_prop"})
            continue
        found.append((selection, prop, requested_side))

    selected_prop_ids = {prop.get("propId") for _, prop, _ in found}
    selected_props = [
        prop
        for prop in props_payload.get("props") or []
        if prop.get("propId") in selected_prop_ids
    ]
    enriched = await enrich_props_with_mlb_data(
        {**props_payload, "props": selected_props, "propCount": len(selected_props)},
        mlb_engine,
        season=season,
        history_limit=min(_clean_int(history_limit, 1, 15), 15),
    )
    props_by_id = {prop.get("propId"): prop for prop in enriched.get("props") or []}
    contexts = [
        await _prop_context_response(
            selection=selection,
            prop=props_by_id.get(prop.get("propId")) or prop,
            mlb_engine=mlb_engine,
            season=season,
            history_limit=history_limit,
            requested_side=requested_side,
        )
        for selection, prop, requested_side in found
    ]
    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_odds_api+mlb_stats_api",
        "purpose": "finalist_context_batch",
        "matchup": matchup,
        "date": props_payload.get("date"),
        "timezone": props_payload.get("timezone"),
        "requestedCount": len(selections),
        "processedCount": len(requested),
        "contextCount": len(contexts),
        "truncated": len(selections) > len(requested),
        "missing": missing,
        "contexts": contexts,
        "generatedAt": _utc_now(),
    }


async def build_player_mlb_context(
    stake_client: Any,
    mlb_engine: Any,
    matchup: str,
    prop_id: str,
    side: str = "any",
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
    markets: str | None = None,
    season: int | None = None,
    history_limit: int = 15,
) -> dict[str, Any]:
    props_payload = await _build_matchup_props_payload(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
        line_mode="primary",
    )
    requested_side = _clean_side(side)
    selection = _find_selection(
        _side_level_selections(props_payload.get("props") or [], "any"),
        prop_id,
        side=requested_side,
    )
    if not selection:
        raise HTTPException(status_code=404, detail="Stake prop selection was not found.")

    prop = _find_prop(props_payload.get("props") or [], selection["propId"])
    if not prop:
        raise HTTPException(status_code=404, detail="Stake prop was not found.")

    enriched = await enrich_props_with_mlb_data(
        {**props_payload, "props": [prop], "propCount": 1},
        mlb_engine,
        season=season,
        history_limit=min(_clean_int(history_limit, 1, 15), 15),
    )
    enriched_prop = (enriched.get("props") or [prop])[0]
    return await _prop_context_response(
        selection=selection,
        prop=enriched_prop,
        mlb_engine=mlb_engine,
        season=season,
        history_limit=history_limit,
        requested_side=requested_side,
    )


async def build_player_context_by_id(
    mlb_engine: Any,
    player_id: int,
    market: str | None = None,
    season: int | None = None,
    history_limit: int = 15,
) -> dict[str, Any]:
    mapping = stat_mapping_for_market(market or "hits")
    stat_group = str(mapping["group"])
    profile = await mlb_engine.get_player_profile(
        player_id,
        season=season,
        group=stat_group,
    )
    recent = await _recent_windows(
        mlb_engine=mlb_engine,
        player_id=player_id,
        stat_group=stat_group,
        season=season,
        max_limit=history_limit,
    )
    return {
        "decisionOwner": "custom_gpt",
        "player": (profile.get("player") or {}),
        "season": profile,
        "recent": recent,
        "statContext": mapping,
        "generatedAt": _utc_now(),
    }


async def build_player_recent_logs(
    mlb_engine: Any,
    player_id: int,
    market: str | None = None,
    season: int | None = None,
    history_limit: int = 15,
) -> dict[str, Any]:
    mapping = stat_mapping_for_market(market or "hits")
    return await mlb_engine.get_player_recent_history(
        player_id,
        group=str(mapping["group"]),
        season=season,
        limit=history_limit,
    )


async def build_player_season_stats(
    mlb_engine: Any,
    player_id: int,
    market: str | None = None,
    season: int | None = None,
) -> dict[str, Any]:
    mapping = stat_mapping_for_market(market or "hits")
    return await mlb_engine.get_player_profile(
        player_id,
        season=season,
        group=str(mapping["group"]),
    )


async def build_probable_pitchers(
    mlb_engine: Any,
    matchup: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    target_date = slate_date or datetime.now(ZoneInfo(timezone_name)).date()
    schedule = await mlb_engine.get_schedule(target_date.isoformat())
    tokens = _matchup_tokens(matchup)
    game = next(
        (
            game
            for game in schedule.get("games") or []
            if _mlb_game_matches_tokens(game, tokens)
        ),
        None,
    )
    probable_pitchers = await _probable_pitchers_with_context(
        game,
        mlb_engine=mlb_engine,
        season=target_date.year,
    )
    return {
        "decisionOwner": "custom_gpt",
        "matchup": matchup,
        "date": target_date.isoformat(),
        "game": game,
        "probablePitchers": probable_pitchers,
        "generatedAt": _utc_now(),
    }


async def build_market_map(
    stake_client: Any,
    matchup: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
) -> dict[str, Any]:
    board = await build_matchup_prop_board(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
    )
    return {
        "decisionOwner": "custom_gpt",
        "matchup": matchup,
        "date": board["date"],
        "marketMap": board["marketMap"],
        "generatedAt": _utc_now(),
    }


async def validate_gpt_selections(
    stake_client: Any,
    matchup: str,
    selections: list[dict[str, Any]],
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
    markets: str | None = None,
    validation_mode: str = "strict",
    odds_policy: str | None = None,
    odds_tolerance: float | None = None,
) -> dict[str, Any]:
    board = await build_matchup_prop_board(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
        side="any",
    )
    current_selections = board.get("selections") or []
    clean_mode = _clean_validation_mode(validation_mode)
    clean_policy = _clean_odds_policy(odds_policy, clean_mode)
    clean_tolerance = _clean_odds_tolerance(odds_tolerance, clean_mode)
    results = [
        _validate_selection(
            selection,
            current_selections,
            index,
            validation_mode=clean_mode,
            odds_policy=clean_policy,
            odds_tolerance=clean_tolerance,
        )
        for index, selection in enumerate(selections, start=1)
    ]
    return {
        "decisionOwner": "custom_gpt",
        "matchup": matchup,
        "date": board.get("date"),
        "timezone": board.get("timezone"),
        "valid": all(result["valid"] for result in results),
        "validationMode": clean_mode,
        "oddsPolicy": clean_policy,
        "oddsTolerance": clean_tolerance,
        "executionReady": clean_mode == "execution_ready"
        and all(result.get("executionReady") for result in results),
        "results": results,
        "notes": _validation_notes(results),
        "validatedAt": _utc_now(),
    }


async def build_gpt_decision_result(
    stake_client: Any,
    matchup: str,
    selections: list[dict[str, Any]],
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = DEFAULT_BOARD_LIMIT,
    markets: str | None = None,
    prompt: str | None = None,
    reasoning: list[str] | None = None,
    risk_flags: list[str] | None = None,
    validation_mode: str = "strict",
    odds_policy: str | None = None,
    odds_tolerance: float | None = None,
) -> dict[str, Any]:
    validation = await validate_gpt_selections(
        stake_client=stake_client,
        matchup=matchup,
        selections=selections,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
        validation_mode=validation_mode,
        odds_policy=odds_policy,
        odds_tolerance=odds_tolerance,
    )
    accepted = [
        _accepted_selection_from_validation(result)
        for result in validation["results"]
        if result.get("valid") and result.get("current")
    ]
    return {
        "decisionOwner": "custom_gpt",
        "source": "chatgpt_decision",
        "matchup": matchup,
        "date": validation.get("date"),
        "timezone": validation.get("timezone"),
        "prompt": prompt,
        "validation": validation,
        "selectionCount": len(accepted),
        "selections": accepted,
        "reasoning": reasoning or [],
        "riskFlags": risk_flags or [],
        "generatedAt": _utc_now(),
    }


async def _build_matchup_props_payload(
    stake_client: Any,
    matchup: str,
    slate_date: date | None,
    timezone_name: str,
    limit: int,
    markets: str | None,
    line_mode: str,
) -> dict[str, Any]:
    if _clear_mlb_cache_per_gpt_request():
        clear_mlb_bridge_cache()
    tokens = _matchup_tokens(matchup)
    slate = await build_mlb_player_props_slate(
        client=stake_client,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        line_mode=_clean_line_mode(line_mode),
        include_markets=_clean_market_csv(markets),
        fixture_filter=lambda fixture: _fixture_matches_tokens(fixture, tokens),
    )
    payload = build_stable_props_payload(slate)
    return _filter_payload_to_matchup(payload, matchup)


def _side_level_selections(props: list[dict[str, Any]], side: str) -> list[dict[str, Any]]:
    selections = []
    for prop in props:
        for selection_side in ("over", "under"):
            if side != "any" and side != selection_side:
                continue
            odds = _float_or_none((prop.get("odds") or {}).get(selection_side))
            if odds is None:
                continue
            selections.append(_selection_from_prop(prop, selection_side, odds))
    return selections


def _selection_from_prop(prop: dict[str, Any], side: str, odds: float) -> dict[str, Any]:
    availability = _availability_flags(prop, side=side)
    line = _float_or_none(prop.get("line"))
    player = prop.get("player") or {}
    market = prop.get("market") or {}
    return {
        "selectionId": f"{prop.get('propId')}:{side}",
        "propId": prop.get("propId"),
        "fixtureSlug": prop.get("fixtureSlug"),
        "game": prop.get("game"),
        "startTime": prop.get("startTime"),
        "status": prop.get("status"),
        "player": player,
        "team": prop.get("team"),
        "market": market,
        "side": side,
        "line": line,
        "lineSource": prop.get("lineSource") or "unknown",
        "isPrimaryLine": bool(prop.get("isPrimaryLine")),
        "odds": odds,
        "playable": availability["playable"],
        "availability": availability,
        "selection": _selection_text(player.get("name"), side, line, market.get("name")),
    }


def _availability_flags(prop: dict[str, Any], side: str | None = None) -> dict[str, Any]:
    odds = prop.get("odds") or {}
    offered_odds = _float_or_none(odds.get(side)) if side else None
    fixture_status = str(prop.get("status") or "").lower()
    side_offered = side is None or offered_odds is not None
    min_playable_odds = _minimum_playable_odds()
    playable = (
        fixture_status in {"active", "not_started", "not started", "scheduled", ""}
        and side_offered
        and (offered_odds is None or offered_odds >= min_playable_odds)
        and prop.get("line") is not None
    )
    flags = []
    if offered_odds is not None and offered_odds < min_playable_odds:
        flags.append("unplayable_current_odds")
    if prop.get("line") is None:
        flags.append("missing_line")
    if not side_offered:
        flags.append("side_not_offered")
    line_source = str(prop.get("lineSource") or "unknown")
    is_primary_line = bool(prop.get("isPrimaryLine"))
    if line_source == "alternate" or not is_primary_line:
        flags.append("alternate_or_unconfirmed_primary_line")

    return {
        "source": "stake_odds_api",
        "status": prop.get("status"),
        "playable": playable,
        "visibleOnStakeUi": None,
        "uiVerification": "not_available",
        "executionConfirmed": False,
        "playableConfidence": "feed_primary" if is_primary_line else "feed_only",
        "lineSource": line_source,
        "isPrimaryLine": is_primary_line,
        "sideOffered": side_offered,
        "linePresent": prop.get("line") is not None,
        "minPlayableOdds": min_playable_odds,
        "flags": flags,
        "checkedAt": _utc_now(),
    }


def _board_prop(prop: dict[str, Any]) -> dict[str, Any]:
    return {
        "propId": prop.get("propId"),
        "fixtureSlug": prop.get("fixtureSlug"),
        "game": prop.get("game"),
        "startTime": prop.get("startTime"),
        "status": prop.get("status"),
        "player": prop.get("player"),
        "team": prop.get("team"),
        "market": prop.get("market"),
        "line": _float_or_none(prop.get("line")),
        "lineSource": prop.get("lineSource") or "unknown",
        "isPrimaryLine": bool(prop.get("isPrimaryLine")),
        "odds": {
            "over": _float_or_none((prop.get("odds") or {}).get("over")),
            "under": _float_or_none((prop.get("odds") or {}).get("under")),
        },
        "availability": _availability_flags(prop),
    }


async def _prop_context_response(
    selection: dict[str, Any],
    prop: dict[str, Any],
    mlb_engine: Any,
    season: int | None,
    history_limit: int,
    requested_side: str,
) -> dict[str, Any]:
    stat_context = prop.get("statContext") or stat_mapping_for_market(
        ((prop.get("market") or {}).get("key") or "")
    )
    player = prop.get("player") or {}
    player_id = player.get("mlbId")
    recent = {}
    if player_id is not None:
        recent = await _recent_windows(
            mlb_engine=mlb_engine,
            player_id=int(player_id),
            stat_group=str(stat_context.get("group") or "hitting"),
            season=season,
            max_limit=history_limit,
        )

    metrics = _selection_metrics(selection, prop, stat_context)
    flags = _comparison_flags(selection, prop, stat_context)
    profile = build_decision_profile(
        selection=selection,
        stat_context=stat_context,
        metrics=metrics,
        flags=flags,
        mlb_match=prop.get("mlbMatch"),
    )
    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_odds_api+mlb_stats_api",
        "selection": selection,
        "requestedSide": requested_side,
        "player": player,
        "team": prop.get("team"),
        "market": prop.get("market"),
        "line": prop.get("line"),
        "odds": selection.get("odds"),
        "side": selection.get("side"),
        "availability": selection.get("availability"),
        "mlbMatch": prop.get("mlbMatch"),
        "matchupGame": prop.get("mlbGame"),
        "statContext": stat_context,
        "season": _season_context(prop.get("mlbProfile")),
        "recent": recent,
        "metrics": metrics,
        "decisionProfile": profile,
        "flags": flags,
        "notes": _player_context_notes(prop),
        "generatedAt": _utc_now(),
    }


def _accepted_selection_from_validation(result: dict[str, Any]) -> dict[str, Any]:
    current = dict(result.get("current") or {})
    requested = result.get("requested") or {}
    if requested.get("decisionProfile"):
        current["decisionProfile"] = requested.get("decisionProfile")
    if requested.get("riskFlags"):
        current["riskFlags"] = requested.get("riskFlags")
    current["validationResult"] = {
        key: result.get(key)
        for key in (
            "status",
            "validationMode",
            "oddsPolicy",
            "oddsTolerance",
            "verificationSource",
            "uiVerification",
            "sideMatch",
            "lineMatch",
            "oddsMatch",
            "playableMatch",
            "identityMatch",
            "checkedAt",
        )
        if key in result
    }
    return current


async def _recent_windows(
    mlb_engine: Any,
    player_id: int,
    stat_group: str,
    season: int | None,
    max_limit: int,
) -> dict[str, Any]:
    windows = {}
    for window in (5, 10, 15):
        if window > max(_clean_int(max_limit, 1, 15), 5) and window != 5:
            continue
        history = await mlb_engine.get_player_recent_history(
            player_id,
            group=stat_group,
            season=season,
            limit=window,
        )
        windows[str(window)] = history
    return {"windows": windows}


def _validate_selection(
    requested: dict[str, Any],
    current_selections: list[dict[str, Any]],
    index: int,
    validation_mode: str,
    odds_policy: str,
    odds_tolerance: float,
) -> dict[str, Any]:
    selection_id = requested.get("selectionId")
    prop_id = requested.get("propId")
    side = _clean_side(requested.get("side") or "any")
    current = _find_selection(current_selections, selection_id or prop_id, side=side)
    if current is None and not (selection_id or prop_id):
        current = _find_selection_by_requested_fields(current_selections, requested, side=side)
    checked_at = _utc_now()
    base = {
        "index": index,
        "requested": requested,
        "current": current,
        "validationMode": validation_mode,
        "oddsPolicy": odds_policy,
        "oddsTolerance": odds_tolerance,
        "verificationSource": "stake_feed",
        "uiVerification": "not_available",
        "checkedAt": checked_at,
        "executionReady": False,
    }
    if current is None:
        return {
            **base,
            "valid": False,
            "status": "missing_selection",
            "rejectReasons": ["missing_selection"],
        }
    side_match = side == "any" or current.get("side") == side
    line_match = _numbers_match(requested.get("line"), current.get("line"))
    odds_match = _odds_acceptable(
        requested.get("odds"),
        current.get("odds"),
        policy=odds_policy,
        tolerance=odds_tolerance,
    )
    playable_match = bool(current.get("playable"))
    identity_match = _requested_identity_matches(requested, current)
    checks = {
        "sideMatch": side_match,
        "lineMatch": line_match,
        "oddsMatch": odds_match,
        "playableMatch": playable_match,
        "identityMatch": identity_match,
        "propIdMatch": not prop_id or str(prop_id) == str(current.get("propId") or ""),
        "selectionIdMatch": not selection_id
        or str(selection_id) == str(current.get("selectionId") or ""),
    }
    base = {**base, **checks}
    if not identity_match:
        return {
            **base,
            "valid": False,
            "status": "identity_mismatch",
            "rejectReasons": ["identity_mismatch"],
        }
    if not side_match:
        return {
            **base,
            "valid": False,
            "status": "side_mismatch",
            "rejectReasons": ["side_mismatch"],
        }
    if not line_match:
        return {
            **base,
            "valid": False,
            "status": "line_mismatch",
            "rejectReasons": ["line_mismatch"],
        }
    if not odds_match:
        return {
            **base,
            "valid": False,
            "status": "odds_mismatch",
            "rejectReasons": ["odds_mismatch"],
        }
    if not playable_match:
        return {
            **base,
            "valid": False,
            "status": "unplayable",
            "rejectReasons": ["unplayable"],
        }
    if validation_mode == "execution_ready":
        return {
            **base,
            "valid": False,
            "status": "quote_required",
            "quoteRequired": True,
            "rejectReasons": ["stake_ui_quote_required"],
            "message": "Stake feed matched, but no final bet-slip quote was confirmed.",
        }
    return {**base, "valid": True, "status": "valid", "rejectReasons": []}


def _find_selection(
    selections: list[dict[str, Any]],
    identifier: Any,
    side: str | None = None,
) -> dict[str, Any] | None:
    identifier_text = str(identifier or "")
    for selection in selections:
        if side and side != "any" and selection.get("side") != side:
            continue
        if identifier_text in {
            str(selection.get("selectionId") or ""),
            str(selection.get("propId") or ""),
        }:
            return selection
    return None


def _find_selection_by_requested_fields(
    selections: list[dict[str, Any]],
    requested: dict[str, Any],
    side: str | None = None,
) -> dict[str, Any] | None:
    for selection in selections:
        if side and side != "any" and selection.get("side") != side:
            continue
        if not _numbers_match(requested.get("line"), selection.get("line")):
            continue
        if requested.get("odds") is not None and not _numbers_match(
            requested.get("odds"),
            selection.get("odds"),
            tolerance=0.01,
        ):
            continue
        if _requested_identity_matches(requested, selection):
            return selection
    return None


def _requested_identity_matches(
    requested: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    checks = [
        ("player", "name"),
        ("team", "name"),
        ("market", "key"),
        ("market", "name"),
        ("fixtureSlug", None),
    ]
    for field, nested in checks:
        requested_value = _requested_identity_value(requested, field, nested)
        if not requested_value:
            continue
        current_value = _requested_identity_value(current, field, nested)
        if slug_key(requested_value) != slug_key(current_value):
            return False
    return True


def _requested_identity_value(row: dict[str, Any], field: str, nested: str | None) -> Any:
    value = row.get(field)
    if isinstance(value, dict):
        if nested and value.get(nested) is not None:
            return value.get(nested)
        return value.get("key") or value.get("name")
    return value


def _find_prop(props: list[dict[str, Any]], prop_id: Any) -> dict[str, Any] | None:
    prop_id_text = str(prop_id or "")
    for prop in props:
        if str(prop.get("propId") or "") == prop_id_text:
            return prop
    return None


def _market_summary(props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markets: dict[str, dict[str, Any]] = {}
    for prop in props:
        market = prop.get("market") or {}
        key = str(market.get("key") or "")
        if not key:
            continue
        row = markets.setdefault(
            key,
            {
                "key": key,
                "name": market.get("name"),
                "propCount": 0,
                "selectionCount": 0,
            },
        )
        row["propCount"] += 1
        row["selectionCount"] += len(_side_level_selections([prop], "any"))
    return sorted(markets.values(), key=lambda row: row["key"])


def _market_map_from_props(props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for prop in props:
        market = prop.get("market") or {}
        mapping = stat_mapping_for_market(str(market.get("key") or ""))
        key = (str(market.get("name") or ""), str(market.get("key") or ""))
        examples = rows.setdefault(
            key,
            {
                "sport": "mlb",
                "stakeDisplayName": market.get("name"),
                "internalMarketKey": market.get("key"),
                "statKey": mapping.get("statKey"),
                "group": mapping.get("group"),
                "supported": mapping.get("supported"),
                "contextQuality": mapping.get("contextQuality"),
                "active": True,
                "examples": [],
            },
        )["examples"]
        if len(examples) < 3:
            examples.append(
                {
                    "player": (prop.get("player") or {}).get("name"),
                    "line": prop.get("line"),
                    "odds": prop.get("odds"),
                }
            )
    return sorted(rows.values(), key=lambda row: str(row.get("internalMarketKey") or ""))


def _filtered_selection_rows(
    props: list[dict[str, Any]],
    side: str,
    primary_only: bool,
    playable_only: bool,
    context_quality: str,
) -> list[dict[str, Any]]:
    return _filter_selections(
        _side_level_selections(props, _clean_side(side)),
        primary_only=primary_only,
        playable_only=playable_only,
        context_quality=context_quality,
    )


def _filter_selections(
    selections: list[dict[str, Any]],
    primary_only: bool,
    playable_only: bool,
    context_quality: str,
) -> list[dict[str, Any]]:
    clean_quality = _clean_context_quality(context_quality)
    rows = []
    for selection in selections:
        if primary_only and not selection.get("isPrimaryLine"):
            continue
        if playable_only and not selection.get("playable"):
            continue
        quality = _selection_context_quality(selection)
        if clean_quality == "supported" and quality == "unsupported":
            continue
        if clean_quality in {"strong", "partial", "unsupported"} and quality != clean_quality:
            continue
        rows.append(selection)
    return sorted(
        rows,
        key=lambda row: (
            str((row.get("market") or {}).get("key") or ""),
            str((row.get("player") or {}).get("name") or ""),
            str(row.get("side") or ""),
            float(row.get("line") or 0),
        ),
    )


def _compact_selection_row(selection: dict[str, Any]) -> dict[str, Any]:
    mapping = stat_mapping_for_market(((selection.get("market") or {}).get("key") or ""))
    return {
        "selectionId": selection.get("selectionId"),
        "propId": selection.get("propId"),
        "player": selection.get("player"),
        "team": selection.get("team"),
        "fixtureSlug": selection.get("fixtureSlug"),
        "game": selection.get("game"),
        "market": selection.get("market"),
        "side": selection.get("side"),
        "line": selection.get("line"),
        "odds": selection.get("odds"),
        "lineSource": selection.get("lineSource"),
        "isPrimaryLine": selection.get("isPrimaryLine"),
        "playable": selection.get("playable"),
        "playableConfidence": (selection.get("availability") or {}).get("playableConfidence"),
        "availabilityFlags": (selection.get("availability") or {}).get("flags") or [],
        "statContext": {
            "marketKey": mapping.get("marketKey"),
            "statKey": mapping.get("statKey"),
            "group": mapping.get("group"),
            "supported": mapping.get("supported"),
            "contextQuality": mapping.get("contextQuality"),
        },
    }


def _comparison_selection_row(
    selection: dict[str, Any],
    prop: dict[str, Any],
) -> dict[str, Any]:
    prop = prop or {}
    stat_context = prop.get("statContext") or stat_mapping_for_market(
        ((selection.get("market") or {}).get("key") or "")
    )
    metrics = _selection_metrics(selection, prop, stat_context)
    flags = _comparison_flags(selection, prop, stat_context)
    profile = build_decision_profile(
        selection=selection,
        stat_context=stat_context,
        metrics=metrics,
        flags=flags,
        mlb_match=prop.get("mlbMatch"),
    )
    return {
        **_compact_selection_row(selection),
        "mlbMatch": prop.get("mlbMatch"),
        "matchupGame": _compact_game(prop.get("mlbGame")),
        "metrics": metrics,
        "flags": flags,
        "decisionProfile": profile,
        "helperStrength": metrics.get("agreementScore"),
    }


def _comparison_sort_key(row: dict[str, Any]) -> tuple[float, float, float]:
    metrics = row.get("metrics") or {}
    return (
        _float_or_none(metrics.get("agreementScore")) or 0.0,
        _float_or_none(metrics.get("recentSideMargin")) or -999.0,
        _float_or_none(row.get("odds")) or 0.0,
    )


def _selection_metrics(
    selection: dict[str, Any],
    prop: dict[str, Any],
    stat_context: dict[str, Any],
) -> dict[str, Any]:
    stat_key = stat_context.get("statKey")
    line = _float_or_none(selection.get("line"))
    side = str(selection.get("side") or "")
    recent = prop.get("recentHistory") or {}
    recent_average = _recent_average(recent, stat_key)
    season_average = _season_average(prop.get("mlbProfile"), stat_key)
    hit_rates = _recent_hit_rates(recent, stat_key, line)
    recent_margin = _side_margin(recent_average, line, side)
    season_margin = _side_margin(season_average, line, side)
    agreement = _agreement_score(recent_margin, season_margin, hit_rates, side)
    windows = evidence_windows(recent, stat_key, line, side)
    season = season_evidence(prop.get("mlbProfile"), stat_key, line, side)
    guard = evidence_check(windows, season, side)
    labels = trend_labels(windows, season, side)
    if guard.get("last5OverreactionRisk"):
        labels = sorted(set(labels + ["last5_overreaction_risk"]))
    if guard.get("last5OnlySupport"):
        labels = sorted(set(labels + ["last5_only_support"]))
    if guard.get("missingBroaderEvidence"):
        labels = sorted(set(labels + ["broader_evidence_missing"]))
    return {
        "statKey": stat_key,
        "recentGamesUsed": recent.get("gamesUsed"),
        "recentAverage": recent_average,
        "seasonAverage": season_average,
        "recentHitRateOver": hit_rates.get("over"),
        "recentHitRateUnder": hit_rates.get("under"),
        "recentSideMargin": recent_margin,
        "seasonSideMargin": season_margin,
        "agreementScore": agreement,
        "windows": windows,
        "season": season,
        "evidenceCheck": guard,
        "trendLabels": labels,
    }


def _comparison_flags(
    selection: dict[str, Any],
    prop: dict[str, Any],
    stat_context: dict[str, Any],
) -> list[str]:
    flags = list((selection.get("availability") or {}).get("flags") or [])
    if not stat_context.get("supported"):
        flags.append("context_unsupported")
    elif stat_context.get("contextQuality") == "partial":
        flags.append("context_partial")
    match = prop.get("mlbMatch") or {}
    if match.get("status") == "unmatched":
        flags.append("mlb_player_unmatched")
    if match.get("status") == "name_match_team_unconfirmed":
        flags.append("team_unconfirmed")
    return sorted(set(flags))


def _compact_game(game: dict[str, Any] | None) -> dict[str, Any] | None:
    if not game:
        return None
    return {
        "gamePk": game.get("gamePk"),
        "gameDate": game.get("gameDate"),
        "status": game.get("status"),
        "awayTeam": (game.get("awayTeam") or {}).get("name"),
        "homeTeam": (game.get("homeTeam") or {}).get("name"),
    }


def _recent_average(recent: dict[str, Any], stat_key: Any) -> float | None:
    if not stat_key:
        return None
    per_game = recent.get("perGame") or {}
    direct = _float_or_none(per_game.get(str(stat_key)))
    if direct is not None:
        return direct
    totals = recent.get("totals") or {}
    total = _float_or_none(totals.get(str(stat_key)))
    games_used = _float_or_none(recent.get("gamesUsed"))
    if total is None or not games_used:
        return None
    return round(total / games_used, 4)


def _season_average(profile: dict[str, Any] | None, stat_key: Any) -> float | None:
    if not stat_key:
        return None
    stats = (((profile or {}).get("player") or {}).get("stats") or {})
    total = _float_or_none(stats.get(str(stat_key)))
    denominator = (
        _float_or_none(stats.get("gamesPlayed"))
        or _float_or_none(stats.get("gamesPitched"))
        or _float_or_none(stats.get("gamesStarted"))
    )
    if total is None or not denominator:
        return None
    return round(total / denominator, 4)


def _recent_hit_rates(
    recent: dict[str, Any],
    stat_key: Any,
    line: float | None,
) -> dict[str, float | None]:
    if not stat_key or line is None:
        return {"over": None, "under": None}
    values = [
        _float_or_none((game.get("stats") or {}).get(str(stat_key)))
        for game in recent.get("games") or []
    ]
    values = [value for value in values if value is not None]
    if not values:
        return {"over": None, "under": None}
    over = sum(1 for value in values if value > line) / len(values)
    under = sum(1 for value in values if value < line) / len(values)
    return {"over": round(over, 4), "under": round(under, 4)}


def _side_margin(value: float | None, line: float | None, side: str) -> float | None:
    if value is None or line is None:
        return None
    margin = value - line if side == "over" else line - value
    return round(margin, 4)


def _agreement_score(
    recent_margin: float | None,
    season_margin: float | None,
    hit_rates: dict[str, float | None],
    side: str,
) -> float | None:
    signals = []
    if recent_margin is not None:
        signals.append(50 + max(-25, min(25, recent_margin * 20)))
    if season_margin is not None:
        signals.append(50 + max(-25, min(25, season_margin * 15)))
    side_rate = hit_rates.get(side)
    if side_rate is not None:
        signals.append(side_rate * 100)
    if not signals:
        return None
    return round(sum(signals) / len(signals), 2)


def _paginate(
    items: list[dict[str, Any]],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    clean_page = _clean_int(page, 1, 10_000)
    clean_page_size = _clean_int(page_size, 1, 100)
    start = (clean_page - 1) * clean_page_size
    end = start + clean_page_size
    return {
        "items": items[start:end],
        "meta": {
            "page": clean_page,
            "pageSize": clean_page_size,
            "totalItems": len(items),
            "totalPages": ((len(items) - 1) // clean_page_size) + 1 if items else 0,
            "hasNextPage": end < len(items),
        },
    }


def _navigation_filters(
    markets: str | None,
    side: str,
    line_mode: str,
    primary_only: bool,
    playable_only: bool,
    context_quality: str,
) -> dict[str, Any]:
    return {
        "markets": sorted(_clean_market_csv(markets)),
        "side": _clean_side(side),
        "lineMode": _clean_line_mode(line_mode),
        "primaryOnly": bool(primary_only),
        "playableOnly": bool(playable_only),
        "contextQuality": _clean_context_quality(context_quality),
        "minPlayableOdds": _minimum_playable_odds(),
    }


def _summary_markets(selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for selection in selections:
        market = selection.get("market") or {}
        key = str(market.get("key") or "")
        row = rows.setdefault(
            key,
            {
                "key": key,
                "name": market.get("name"),
                "selectionCount": 0,
                "playableSelectionCount": 0,
                "sideCounts": {"over": 0, "under": 0},
                "contextQuality": _selection_context_quality(selection),
            },
        )
        row["selectionCount"] += 1
        if selection.get("playable"):
            row["playableSelectionCount"] += 1
        if selection.get("side") in row["sideCounts"]:
            row["sideCounts"][selection["side"]] += 1
    return sorted(rows.values(), key=lambda row: str(row.get("key") or ""))


def _context_coverage(selections: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"strong": 0, "partial": 0, "unsupported": 0}
    for selection in selections:
        quality = _selection_context_quality(selection)
        counts[quality] = counts.get(quality, 0) + 1
    counts["supported"] = counts.get("strong", 0) + counts.get("partial", 0)
    return counts


def _warning_counts(selections: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for selection in selections:
        for flag in (selection.get("availability") or {}).get("flags") or []:
            counts[flag] = counts.get(flag, 0) + 1
        quality = _selection_context_quality(selection)
        if quality == "partial":
            counts["context_partial"] = counts.get("context_partial", 0) + 1
        if quality == "unsupported":
            counts["context_unsupported"] = counts.get("context_unsupported", 0) + 1
    return counts


def _summary_warnings(
    props_payload: dict[str, Any],
    selections: list[dict[str, Any]],
) -> list[str]:
    warnings = []
    if props_payload.get("propCount", 0) == 0:
        warnings.append("No Stake props matched the matchup/filter.")
    warning_counts = _warning_counts(selections)
    if warning_counts.get("alternate_or_unconfirmed_primary_line"):
        warnings.append("Some rows are alternate or unconfirmed primary lines.")
    if warning_counts.get("context_unsupported"):
        warnings.append("Some markets have no direct MLB stat context.")
    if warning_counts.get("unplayable_current_odds"):
        warnings.append("Some rows were removed from playable consideration because odds are below the minimum threshold.")
    return warnings


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _selection_context_quality(selection: dict[str, Any]) -> str:
    market_key = ((selection.get("market") or {}).get("key") or "")
    quality = str(stat_mapping_for_market(market_key).get("contextQuality") or "unsupported")
    return quality if quality in {"strong", "partial", "unsupported"} else "unsupported"


def _filter_payload_to_matchup(
    props_payload: dict[str, Any],
    matchup: str,
) -> dict[str, Any]:
    tokens = _matchup_tokens(matchup)
    props = [
        prop
        for prop in props_payload.get("props") or []
        if _prop_matches_tokens(prop, tokens)
    ]
    fixture_slugs = {prop.get("fixtureSlug") for prop in props if prop.get("fixtureSlug")}
    payload = dict(props_payload)
    payload["props"] = props
    payload["propCount"] = len(props)
    payload["fixtureCount"] = len(fixture_slugs)
    return payload


def _prop_matches_tokens(prop: dict[str, Any], tokens: set[str]) -> bool:
    if not tokens:
        return True
    haystack = " ".join(
        str(value or "")
        for value in [
            prop.get("fixtureSlug"),
            prop.get("game"),
            (prop.get("team") or {}).get("name"),
        ]
    )
    return tokens.issubset(_matchup_tokens(haystack))


def _fixture_matches_tokens(fixture: dict[str, Any], tokens: set[str]) -> bool:
    if not tokens:
        return True
    haystack = " ".join(
        str(fixture.get(key) or "")
        for key in ("slug", "name")
    )
    return tokens.issubset(_matchup_tokens(haystack))


def _mlb_game_matches_tokens(game: dict[str, Any], tokens: set[str]) -> bool:
    if not tokens:
        return True
    away = game.get("awayTeam") or {}
    home = game.get("homeTeam") or {}
    haystack = " ".join(
        str(value or "")
        for value in [
            away.get("name"),
            away.get("key"),
            home.get("name"),
            home.get("key"),
        ]
    )
    return tokens.issubset(_matchup_tokens(haystack))


def _probable_pitchers_from_game(game: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not game:
        return []
    pitchers = []
    for side_key, label in (("awayTeam", "away"), ("homeTeam", "home")):
        team = game.get(side_key) or {}
        pitcher = team.get("probablePitcher")
        if pitcher:
            pitchers.append({"side": label, "team": team.get("name"), "pitcher": pitcher})
    return pitchers


async def _probable_pitchers_with_context(
    game: dict[str, Any] | None,
    mlb_engine: Any,
    season: int,
) -> list[dict[str, Any]]:
    rows = _probable_pitchers_from_game(game)
    for row in rows:
        pitcher = row.get("pitcher") or {}
        pitcher_id = pitcher.get("mlbId")
        if pitcher_id is None:
            row["roleSanity"] = _pitcher_role_sanity(None, None)
            continue
        try:
            profile = await mlb_engine.get_player_profile(
                int(pitcher_id),
                season=season,
                group="pitching",
            )
            recent = await mlb_engine.get_player_recent_history(
                int(pitcher_id),
                group="pitching",
                season=season,
                limit=5,
            )
        except Exception as exc:
            row["roleSanity"] = {
                "volumePropRisk": "unknown",
                "flags": ["pitcher_role_context_unavailable"],
                "message": str(exc),
            }
            continue
        row["season"] = _season_context(profile)
        row["recent"] = recent
        row["roleSanity"] = _pitcher_role_sanity(profile, recent)
    return rows


def _pitcher_role_sanity(
    profile: dict[str, Any] | None,
    recent: dict[str, Any] | None,
) -> dict[str, Any]:
    stats = (((profile or {}).get("player") or {}).get("stats") or {})
    games_started = _int_or_none(stats.get("gamesStarted"))
    games_played = (
        _int_or_none(stats.get("gamesPlayed"))
        or _int_or_none(stats.get("gamesPitched"))
        or games_started
    )
    flags = []
    start_share = None
    if games_started is None:
        flags.append("probable_pitcher_start_count_unknown")
    if games_played and games_started is not None:
        start_share = games_started / games_played if games_played else None
        if games_started == 0 or start_share < 0.5:
            flags.append("probable_pitcher_low_start_share")

    recent_games_used = _int_or_none((recent or {}).get("gamesUsed"))
    if recent_games_used == 0:
        flags.append("probable_pitcher_no_recent_logs")

    if "probable_pitcher_low_start_share" in flags:
        risk = "high"
    elif flags:
        risk = "medium"
    else:
        risk = "low"

    return {
        "volumePropRisk": risk,
        "flags": flags,
        "gamesStarted": games_started,
        "gamesPlayed": games_played,
        "startShare": start_share,
    }


def _matchup_tokens(value: str) -> set[str]:
    text = str(value or "").lower()
    text = re.sub(r"\b(vs|at|and|the|mlb)\b", " ", text)
    return {
        token
        for token in re.split(r"[^a-z0-9]+", text)
        if token and token not in {"", "vs", "at"}
    }


def _board_notes(
    props_payload: dict[str, Any],
    selections: list[dict[str, Any]],
) -> list[str]:
    notes = [
        "Custom GPT owns final pick logic; backend output is data, not a recommendation."
    ]
    if props_payload.get("propCount", 0) == 0:
        notes.append("No Stake player props matched the requested matchup and filters.")
    if not selections and props_payload.get("propCount", 0) > 0:
        notes.append("Stake props matched, but no playable selections matched the side filter.")
    return notes


def _player_context_notes(prop: dict[str, Any]) -> list[str]:
    notes = []
    match = prop.get("mlbMatch") or {}
    stat_context = prop.get("statContext") or {}
    if match.get("status") == "unmatched":
        notes.append("MLB player match was not confirmed.")
    if not stat_context.get("supported", True):
        notes.append("Market has no direct MLB stat mapping yet.")
    elif stat_context.get("contextQuality") == "partial":
        notes.append("Market has partial MLB context support; use extra caution.")
    return notes


def _season_context(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not profile:
        return None
    player = profile.get("player") or {}
    return {
        "player": player,
        "season": profile.get("season"),
        "group": profile.get("group"),
        "stats": player.get("stats") or {},
    }


def _validation_notes(results: list[dict[str, Any]]) -> list[str]:
    invalid = [result for result in results if not result.get("valid")]
    if not invalid:
        return ["All selections still match the current Stake-backed board."]
    return [
        "One or more selections no longer match the current Stake-backed board. Do not recommend invalid legs."
    ]


def _selection_text(player: Any, side: str, line: Any, market: Any) -> str:
    return f"{player} {side} {line} {market}".strip()


def _numbers_match(a: Any, b: Any, tolerance: float = 0.0001) -> bool:
    left = _float_or_none(a)
    right = _float_or_none(b)
    if left is None or right is None:
        return left is right
    return abs(left - right) <= tolerance


def _odds_acceptable(
    requested: Any,
    current: Any,
    policy: str,
    tolerance: float,
) -> bool:
    requested_odds = _float_or_none(requested)
    current_odds = _float_or_none(current)
    if requested_odds is None or current_odds is None:
        return requested_odds is current_odds
    if policy == "within_tolerance":
        return abs(requested_odds - current_odds) <= tolerance
    if policy == "accept_better":
        return current_odds + 0.0001 >= requested_odds
    return abs(requested_odds - current_odds) <= 0.0001


def _clean_market_csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {
        slug_key(part)
        for part in str(value).split(",")
        if slug_key(part)
    }


def _clean_side(value: Any) -> str:
    side = str(value or "any").strip().lower()
    return side if side in {"any", "over", "under"} else "any"


def _clean_validation_mode(value: Any) -> str:
    mode = str(value or "strict").strip().lower().replace("-", "_")
    return mode if mode in {"recommendation", "strict", "execution_ready"} else "strict"


def _clean_odds_policy(value: Any, validation_mode: str) -> str:
    policy = str(value or "").strip().lower().replace("-", "_")
    if policy in {"exact", "accept_better", "within_tolerance"}:
        return policy
    if validation_mode == "recommendation":
        return "within_tolerance"
    return "exact"


def _clean_odds_tolerance(value: Any, validation_mode: str) -> float:
    default = 0.01 if validation_mode == "recommendation" else 0.0001
    tolerance = _float_or_none(value)
    if tolerance is None:
        return default
    return max(0.0, min(tolerance, 1.0))


def _clean_line_mode(value: Any) -> str:
    return "all" if str(value or "").strip().lower() == "all" else "primary"


def _clean_context_quality(value: Any) -> str:
    quality = str(value or "any").strip().lower().replace("-", "_")
    if quality in {"any", "supported", "strong", "partial", "unsupported"}:
        return quality
    return "any"


def _minimum_playable_odds() -> float:
    return max(1.0, _float_or_none(os.getenv("AZP_MIN_PLAYABLE_ODDS")) or DEFAULT_MIN_PLAYABLE_ODDS)


def _clear_mlb_cache_per_gpt_request() -> bool:
    return os.getenv("AZP_CLEAR_MLB_CACHE_PER_GPT_REQUEST", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(number, maximum))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _operation(
    operation_id: str,
    summary: str,
    description: str,
    parameters: list[dict[str, Any]] | None = None,
    request_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operation = {
        "operationId": operation_id,
        "summary": summary,
        "description": description,
        "responses": {
            "200": {
                "description": "Successful response",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        }
                    }
                },
            }
        },
    }
    if parameters:
        operation["parameters"] = parameters
    if request_body:
        operation["requestBody"] = request_body
    return operation


def _matchup_path_param() -> dict[str, Any]:
    return {
        "name": "matchup",
        "in": "path",
        "required": True,
        "description": "Matchup text, for example Blue Jays vs Angels.",
        "schema": {"type": "string"},
    }


def _matchup_query_param() -> dict[str, Any]:
    return {
        "name": "matchup",
        "in": "query",
        "required": True,
        "schema": {"type": "string"},
    }


def _prop_id_path_param() -> dict[str, Any]:
    return {
        "name": "propId",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
    }


def _player_id_path_param() -> dict[str, Any]:
    return {
        "name": "playerId",
        "in": "path",
        "required": True,
        "schema": {"type": "integer"},
    }


def _date_param() -> dict[str, Any]:
    return {
        "name": "date",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "format": "date"},
    }


def _limit_param() -> dict[str, Any]:
    return {
        "name": "limit",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
    }


def _market_query_param(required: bool = False) -> dict[str, Any]:
    return {
        "name": "market",
        "in": "query",
        "required": required,
        "description": "Comma-separated market keys or display names.",
        "schema": {"type": "string"},
    }


def _side_query_param() -> dict[str, Any]:
    return {
        "name": "side",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["any", "over", "under"]},
    }


def _line_mode_param() -> dict[str, Any]:
    return {
        "name": "lineMode",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["primary", "all"]},
    }


def _season_param() -> dict[str, Any]:
    return {
        "name": "season",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1876, "maximum": 2100},
    }


def _history_limit_param() -> dict[str, Any]:
    return {
        "name": "historyLimit",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 15},
    }


def _page_param() -> dict[str, Any]:
    return {
        "name": "page",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1},
    }


def _page_size_param(maximum: int = 100) -> dict[str, Any]:
    return {
        "name": "pageSize",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": maximum},
    }


def _primary_only_param() -> dict[str, Any]:
    return {
        "name": "primaryOnly",
        "in": "query",
        "required": False,
        "schema": {"type": "boolean"},
    }


def _playable_only_param() -> dict[str, Any]:
    return {
        "name": "playableOnly",
        "in": "query",
        "required": False,
        "schema": {"type": "boolean"},
    }


def _context_quality_param() -> dict[str, Any]:
    return {
        "name": "contextQuality",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": ["any", "supported", "strong", "partial", "unsupported"],
        },
    }


def _prop_context_batch_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "matchup": {"type": "string"},
                        "date": {"type": "string", "format": "date"},
                        "market": {"type": "string"},
                        "historyLimit": {"type": "integer", "minimum": 1, "maximum": 15},
                        "selections": {
                            "type": "array",
                            "maxItems": 20,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "selectionId": {"type": "string"},
                                    "propId": {"type": "string"},
                                    "side": {"type": "string", "enum": ["over", "under"]},
                                },
                                "additionalProperties": True,
                            },
                        },
                    },
                    "required": ["matchup", "selections"],
                    "additionalProperties": True,
                }
            }
        },
    }


def _slip_candidate_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "matchup": {"type": "string"},
                        "date": {"type": "string", "format": "date"},
                        "markets": {"type": "string"},
                        "side": {"type": "string", "enum": ["any", "over", "under"]},
                        "targetOddsMin": {"type": "number", "minimum": 1},
                        "targetOddsMax": {"type": "number", "minimum": 1},
                        "minLegs": {"type": "integer", "minimum": 1, "maximum": 25},
                        "maxLegs": {"type": "integer", "minimum": 1, "maximum": 25},
                        "mode": {
                            "type": "string",
                            "enum": [
                                "balanced",
                                "best_available",
                                "safe_volume",
                                "compact_power",
                                "mega_under",
                                "strict_diversity",
                                "longshot",
                            ],
                        },
                        "qualityFloor": {"type": "number", "minimum": 0, "maximum": 100},
                        "allowNoPick": {"type": "boolean"},
                        "historyLimit": {"type": "integer", "minimum": 1, "maximum": 15},
                    },
                    "required": ["matchup"],
                    "additionalProperties": True,
                }
            }
        },
    }


def _stake_ui_sgm_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "matchup": {
                            "type": "string",
                            "description": "Matchup text, for example Braves vs Marlins.",
                        },
                        "fixtureSlug": {
                            "type": "string",
                            "description": "Stake fixture slug. Preferred when known.",
                        },
                        "date": {"type": "string", "format": "date"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "description": "Maximum compact selection rows to return.",
                        },
                        "side": {
                            "type": "string",
                            "enum": ["any", "over", "under"],
                            "description": "Optional side filter. Use under for under-only slip research.",
                        },
                        "market": {
                            "type": "string",
                            "description": "Optional market name filter, for example hits, runs, total bases, or strikeouts.",
                        },
                        "scope": {
                            "type": "string",
                            "description": "Optional scope filter, for example player, team_props, or match_props.",
                        },
                        "playableOnly": {
                            "type": "boolean",
                            "description": "When true, returns only rows Stake marks playable in the UI-backed SGM data.",
                        },
                        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 90},
                        "maxCacheAgeSeconds": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 600,
                            "description": (
                                "Reuse a recent completed local UI board for this fixture when available. "
                                "Use 0 to force a fresh local read."
                            ),
                        },
                    },
                    "required": ["matchup"],
                    "additionalProperties": True,
                }
            }
        },
    }


def _stake_ui_mlb_games_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "description": "Maximum visible Stake MLB games to return.",
                        },
                        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 90},
                    },
                    "additionalProperties": True,
                }
            }
        },
    }


def _stake_ui_state_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "fixtureSlug": {
                            "type": "string",
                            "description": (
                                "Optional Stake fixture slug. If omitted, the helper reads "
                                "whichever Stake tab/page is currently active in the helper browser."
                            ),
                        },
                        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 60},
                    },
                    "additionalProperties": True,
                }
            }
        },
    }


def _stake_ui_clear_sgm_selections_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "fixtureSlug": {
                            "type": "string",
                            "description": (
                                "Optional Stake fixture slug. Provide it when clearing pending "
                                "selections for a specific Same Game Multi page."
                            ),
                        },
                        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 60},
                    },
                    "additionalProperties": True,
                }
            }
        },
    }


def _stake_ui_remove_sidebar_group_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "fixtureSlug": {
                            "type": "string",
                            "description": (
                                "Stake fixture slug for the game group already visible in the "
                                "right sidebar. Preferred when known."
                            ),
                        },
                        "matchup": {
                            "type": "string",
                            "description": (
                                "Fallback matchup text, for example Cardinals vs Pirates. "
                                "Used to resolve the fixture and match the sidebar group."
                            ),
                        },
                        "date": {"type": "string", "format": "date"},
                        "reviewOnly": {
                            "type": "boolean",
                            "const": True,
                            "description": (
                                "Must be true. The helper can remove one visible review-slip "
                                "group, but cannot enter stake amount or place a bet."
                            ),
                        },
                        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 60},
                        "scheduleLimit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["reviewOnly"],
                    "additionalProperties": True,
                }
            }
        },
    }


def _stake_ui_clear_sidebar_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "reviewOnly": {
                            "type": "boolean",
                            "const": True,
                            "description": (
                                "Must be true. The helper can clear the visible review "
                                "slip, but cannot enter stake amount or place a bet."
                            ),
                        },
                        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 60},
                    },
                    "required": ["reviewOnly"],
                    "additionalProperties": True,
                }
            }
        },
    }


def _stake_ui_exact_selection_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "rowId": {
                "type": "string",
                "description": (
                    "Preferred. Exact clickable rowId returned by getStakeUiSgmBoard. "
                    "Use this instead of reconstructing player/market/line/odds."
                ),
            },
            "player": {
                "type": "string",
                "description": "Player name exactly as returned by getStakeUiSgmBoard. Omit only for team or match markets.",
            },
            "team": {"type": "string"},
            "market": {"type": "string"},
            "side": {"type": "string", "enum": ["over", "under"]},
            "line": {"type": "number"},
            "odds": {"type": "number"},
            "scope": {"type": "string"},
            "selectionId": {"type": "string"},
            "propId": {"type": "string"},
            "lineId": {"type": "string"},
            "marketId": {"type": "string"},
        },
        "description": (
            "Use rowId when available. If rowId is unavailable, provide exact market, side, line, "
            "and odds from getStakeUiSgmBoard."
        ),
        "additionalProperties": True,
    }


def _stake_ui_review_slip_request_body() -> dict[str, Any]:
    exact_selection_schema = _stake_ui_exact_selection_schema()
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "matchup": {
                            "type": "string",
                            "description": "Matchup text, for example Braves vs Marlins.",
                        },
                        "fixtureSlug": {
                            "type": "string",
                            "description": "Stake fixture slug. Preferred when known.",
                        },
                        "date": {"type": "string", "format": "date"},
                        "reviewOnly": {
                            "type": "boolean",
                            "const": True,
                            "description": "Must be true. The helper only builds a visible slip for user review.",
                        },
                        "selections": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "items": exact_selection_schema,
                            "description": "Exact UI-backed legs returned from getStakeUiSgmBoard. Prefer rowId-only objects.",
                        },
                        "fallbackSelections": {
                            "type": "array",
                            "maxItems": 20,
                            "items": exact_selection_schema,
                            "description": (
                                "Optional backup UI-backed legs. The helper may use these only when "
                                "a primary row is stale, hidden, or not buildable during preflight."
                            ),
                        },
                        "rowIds": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "items": {"type": "string"},
                            "description": (
                                "Preferred shorthand: rowId values copied exactly from getStakeUiSgmBoard rows."
                            ),
                        },
                        "fallbackRowIds": {
                            "type": "array",
                            "maxItems": 20,
                            "items": {"type": "string"},
                            "description": "Optional backup rowIds copied exactly from getStakeUiSgmBoard rows.",
                        },
                        "requiredLegs": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "description": (
                                "How many legs must be buildable before clicking. Defaults to the "
                                "number of primary selections."
                            ),
                        },
                        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 60},
                        "scheduleLimit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["matchup", "reviewOnly"],
                    "additionalProperties": True,
                }
            }
        },
    }


def _stake_ui_review_slip_batch_request_body() -> dict[str, Any]:
    exact_selection_schema = _stake_ui_exact_selection_schema()
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "format": "date"},
                        "reviewOnly": {
                            "type": "boolean",
                            "const": True,
                            "description": "Must be true. The helper only builds a visible slip for user review.",
                        },
                        "groups": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 15,
                            "description": (
                                "Each item is one game's exact UI-backed SGM legs. "
                                "The local helper processes these groups through one "
                                "Stake page so they land in the same visible slip."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "matchup": {
                                        "type": "string",
                                        "description": "Matchup text, for example Yankees vs Blue Jays.",
                                    },
                                    "fixtureSlug": {
                                        "type": "string",
                                        "description": "Stake fixture slug from getStakeUiMlbGames or getStakeUiSgmBoard.",
                                    },
                                    "selections": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 20,
                                        "items": exact_selection_schema,
                                        "description": "Exact UI-backed legs. Prefer rowId-only objects.",
                                    },
                                    "fallbackSelections": {
                                        "type": "array",
                                        "maxItems": 20,
                                        "items": exact_selection_schema,
                                        "description": (
                                            "Optional backup UI-backed legs for this game. Used only "
                                            "when a primary row is stale, hidden, or not buildable."
                                        ),
                                    },
                                    "rowIds": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 20,
                                        "items": {"type": "string"},
                                        "description": (
                                            "Preferred shorthand: rowId values copied exactly from this game's getStakeUiSgmBoard rows."
                                        ),
                                    },
                                    "fallbackRowIds": {
                                        "type": "array",
                                        "maxItems": 20,
                                        "items": {"type": "string"},
                                        "description": "Optional backup rowIds for this game.",
                                    },
                                    "requiredLegs": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 20,
                                        "description": (
                                            "How many legs from primary plus fallback rows must be "
                                            "confirmed buildable before the helper clicks anything."
                                        ),
                                    },
                                },
                                "additionalProperties": True,
                            },
                        },
                        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 180},
                        "scheduleLimit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["reviewOnly", "groups"],
                    "additionalProperties": True,
                }
            }
        },
    }


def _selection_request_body(include_prompt: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "matchup": {"type": "string"},
        "date": {"type": "string", "format": "date"},
        "market": {"type": "string"},
        "validationMode": {
            "type": "string",
            "enum": ["recommendation", "strict", "execution_ready"],
        },
        "oddsPolicy": {
            "type": "string",
            "enum": ["exact", "accept_better", "within_tolerance"],
        },
        "oddsTolerance": {"type": "number", "minimum": 0, "maximum": 1},
        "selections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "selectionId": {"type": "string"},
                    "propId": {"type": "string"},
                    "side": {"type": "string", "enum": ["over", "under"]},
                    "line": {"type": "number"},
                    "odds": {"type": "number"},
                },
                "required": ["side", "line", "odds"],
            },
        },
    }
    required = ["matchup", "selections"]
    if include_prompt:
        properties["prompt"] = {"type": "string"}
        properties["reasoning"] = {"type": "array", "items": {"type": "string"}}
        properties["riskFlags"] = {"type": "array", "items": {"type": "string"}}
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": True,
                }
            }
        },
    }
