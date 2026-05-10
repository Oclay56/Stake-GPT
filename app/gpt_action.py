from __future__ import annotations

import hmac
import os
import re
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Header, HTTPException

from .mlb_bridge import (
    clear_mlb_bridge_cache,
    enrich_props_with_mlb_data,
    stat_mapping_for_market,
)
from .mlb_props import build_stable_props_payload, slug_key
from .slate import DEFAULT_TIMEZONE, build_mlb_matchups, build_mlb_player_props_slate


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
                    "Returns line-specific Stake selections for one matchup. This does not rank or choose picks.",
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
                        _season_param(),
                        _history_limit_param(),
                    ],
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


async def build_player_mlb_context(
    stake_client: Any,
    mlb_engine: Any,
    matchup: str,
    prop_id: str,
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
    selection = _find_selection(
        _side_level_selections(props_payload.get("props") or [], "any"),
        prop_id,
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
    return {
        "decisionOwner": "custom_gpt",
        "matchup": matchup,
        "date": target_date.isoformat(),
        "game": game,
        "probablePitchers": _probable_pitchers_from_game(game),
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
    results = [
        _validate_selection(selection, current_selections, index)
        for index, selection in enumerate(selections, start=1)
    ]
    return {
        "decisionOwner": "custom_gpt",
        "matchup": matchup,
        "date": board.get("date"),
        "timezone": board.get("timezone"),
        "valid": all(result["valid"] for result in results),
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
) -> dict[str, Any]:
    validation = await validate_gpt_selections(
        stake_client=stake_client,
        matchup=matchup,
        selections=selections,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
    )
    accepted = [
        result["current"]
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

    return {
        "source": "stake_odds_api",
        "status": prop.get("status"),
        "playable": playable,
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

    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_odds_api+mlb_stats_api",
        "selection": selection,
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
        "notes": _player_context_notes(prop),
        "generatedAt": _utc_now(),
    }


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
) -> dict[str, Any]:
    selection_id = requested.get("selectionId")
    prop_id = requested.get("propId")
    side = _clean_side(requested.get("side") or "any")
    current = _find_selection(current_selections, selection_id or prop_id, side=side)
    base = {"index": index, "requested": requested, "current": current}
    if current is None:
        return {**base, "valid": False, "status": "missing_selection"}
    if side != "any" and current.get("side") != side:
        return {**base, "valid": False, "status": "side_mismatch"}
    if not _numbers_match(requested.get("line"), current.get("line")):
        return {**base, "valid": False, "status": "line_mismatch"}
    if not _numbers_match(requested.get("odds"), current.get("odds"), tolerance=0.01):
        return {**base, "valid": False, "status": "odds_mismatch"}
    if not current.get("playable"):
        return {**base, "valid": False, "status": "unplayable"}
    return {**base, "valid": True, "status": "valid"}


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
    if match.get("status") == "unmatched":
        notes.append("MLB player match was not confirmed.")
    if not (prop.get("statContext") or {}).get("supported", True):
        notes.append("Market has no direct MLB stat mapping yet.")
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


def _clean_line_mode(value: Any) -> str:
    return "all" if str(value or "").strip().lower() == "all" else "primary"


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


def _selection_request_body(include_prompt: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "matchup": {"type": "string"},
        "date": {"type": "string", "format": "date"},
        "market": {"type": "string"},
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
