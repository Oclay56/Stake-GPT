from __future__ import annotations

import hmac
import os
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Header, HTTPException

from .analyzer import MARKET_PROFILES
from .contextual_edges import apply_contextual_edge_layer
from .mlb_bridge import clear_mlb_bridge_cache, enrich_props_with_mlb_data
from .mlb_props import build_stable_props_payload, slug_key
from .parlay import build_parlay_candidates
from .slate import DEFAULT_TIMEZONE, build_mlb_player_props_slate


CORE_GPT_MARKETS = "hits,runs,rbi,total-bases,home-runs,strikeouts,earned-runs"
DEFAULT_MIN_PLAYABLE_ODDS = 1.10
DEFAULT_MAX_RECOMMENDATIONS_PER_MARKET = 2
DEFAULT_SOFT_DIVERSITY_SCORE_GAP = 8
DIVERSITY_MODES = ("balanced", "best_available", "strict_diversity", "longshot")
PITCHER_ONLY_MARKETS = {
    "strikeouts",
    "pitcher-strikeouts",
    "earned-runs",
    "pitcher-earned-runs",
    "walks-allowed",
    "hits-allowed",
    "outs-recorded",
    "pitcher-outs",
    "first-earned-run",
}


def build_gpt_action_openapi_schema(server_url: str) -> dict[str, Any]:
    clean_server = str(server_url or "").rstrip("/") or "http://127.0.0.1:8000"
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "AZP Suite GPT Action",
            "version": "0.1.0",
            "description": (
                "Read-only AZP action for finding Stake-offered MLB props and "
                "enriching them with MLB Stats API context."
            ),
        },
        "servers": [{"url": clean_server}],
        "paths": {
            "/gpt/health": {
                "get": {
                    "operationId": "getAzpHealth",
                    "summary": "Check AZP backend health",
                    "description": "Returns whether the local AZP GPT action backend is available.",
                    "responses": {
                        "200": {
                            "description": "Backend is available",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "status": {"type": "string"},
                                            "service": {"type": "string"},
                                        },
                                        "required": ["status"],
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/gpt/mlb/matchup-picks": {
                "get": {
                    "operationId": "getMlbMatchupPicks",
                    "summary": "Get Stake-backed MLB matchup picks",
                    "description": (
                        "Returns only Stake-offered MLB player props for the requested matchup, "
                        "with MLB Stats API recent and season context. Use this before giving "
                        "any MLB prop or parlay recommendation."
                    ),
                    "parameters": _matchup_pick_parameters(),
                    "responses": {
                        "200": {
                            "description": "Stake-backed matchup recommendations",
                            "content": {
                                "application/json": {
                                    "schema": _matchup_pick_response_schema()
                                }
                            },
                        }
                    },
                }
            },
            "/gpt/mlb/matchup-prop-board": {
                "get": {
                    "operationId": "getMatchupPropBoard",
                    "summary": "Get the current Stake prop board for one MLB matchup",
                    "description": (
                        "Returns side-level Stake-offered props for a matchup without "
                        "AZP recommendation scoring. Use this when the GPT should inspect "
                        "the board and make its own decision."
                    ),
                    "parameters": _matchup_prop_board_parameters(),
                    "responses": {
                        "200": {
                            "description": "Stake-backed matchup prop board",
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
            },
            "/gpt/mlb/player-context": {
                "get": {
                    "operationId": "getPlayerMlbContext",
                    "summary": "Get MLB context for one Stake prop",
                    "description": (
                        "Returns recent 5/10/15, season context, matchup game, and "
                        "probable pitcher context for a Stake propId from the board."
                    ),
                    "parameters": _player_context_parameters(),
                    "responses": {
                        "200": {
                            "description": "Stake prop with MLB context",
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
            },
            "/gpt/mlb/validate-selections": {
                "post": {
                    "operationId": "validateSelections",
                    "summary": "Validate GPT-selected props against the current Stake board",
                    "description": (
                        "Confirms that GPT-selected propIds, sides, lines, and odds still "
                        "match the current Stake-backed board before the GPT answers."
                    ),
                    "requestBody": _selection_request_body(required_prompt=False),
                    "responses": {
                        "200": {
                            "description": "Selection validation results",
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
            },
            "/gpt/mlb/gpt-decisions": {
                "post": {
                    "operationId": "saveGptDecision",
                    "summary": "Save a GPT-authored MLB prop decision",
                    "description": (
                        "Stores what the GPT chose after inspecting the board and MLB "
                        "context. This is separate from AZP's deterministic recommendations."
                    ),
                    "requestBody": _selection_request_body(required_prompt=False),
                    "responses": {
                        "200": {
                            "description": "Saved GPT decision",
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
            },
            "/gpt/mlb/performance-summary": {
                "get": {
                    "operationId": "getMlbPerformanceSummary",
                    "summary": "Summarize saved AZP recommendation performance",
                    "description": (
                        "Returns settlement-backed performance summaries for saved AZP "
                        "MLB recommendations by market, side, confidence, risk flag, "
                        "contextual tag, and diversity mode."
                    ),
                    "parameters": _performance_summary_parameters(),
                    "responses": {
                        "200": {
                            "description": "Saved recommendation performance summary",
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
            },
            "/gpt/mlb/settle-recommendations": {
                "get": {
                    "operationId": "settleMlbRecommendations",
                    "summary": "Settle saved AZP MLB recommendations",
                    "description": (
                        "Grades saved AZP MLB recommendation legs against MLB Stats API "
                        "results and stores the settlement ledger. Use this after games "
                        "finish before asking for performance summaries."
                    ),
                    "parameters": _settlement_parameters(),
                    "responses": {
                        "200": {
                            "description": "Recommendation settlement result",
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
        for path in schema["paths"].values():
            for operation in path.values():
                operation["security"] = [{"AzpApiKey": []}]
    return schema


def _matchup_pick_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "readOnly": {"type": "boolean"},
            "warning": {"type": "string"},
            "matchup": {"type": "string"},
            "date": {"type": "string"},
            "timezone": {"type": "string"},
            "filters": {"type": "object", "properties": {}, "additionalProperties": True},
            "matchedFixtureCount": {"type": "integer"},
            "availablePropCount": {"type": "integer"},
            "matchedPropCount": {"type": "integer"},
            "unmatchedPropCount": {"type": "integer"},
            "recommendationCount": {"type": "integer"},
            "recommendationDiagnostics": {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
            },
            "parlay": {"type": "object", "properties": {}, "additionalProperties": True},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "source",
            "readOnly",
            "matchup",
            "date",
            "matchedFixtureCount",
            "availablePropCount",
            "recommendations",
        ],
    }


def require_gpt_api_key_value(provided_key: str | None) -> None:
    expected = os.getenv("AZP_GPT_API_KEY")
    if not expected:
        return None
    if not provided_key or not hmac.compare_digest(provided_key, expected):
        raise HTTPException(status_code=401, detail="Invalid AZP GPT API key.")
    return None


def require_gpt_api_key(
    x_azp_api_key: str | None = Header(default=None, alias="X-AZP-API-Key"),
) -> None:
    return require_gpt_api_key_value(x_azp_api_key)


async def build_matchup_picks(
    stake_client: Any,
    mlb_engine: Any,
    matchup: str,
    slate_date: date | None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
    markets: str | None = None,
    side: str = "any",
    legs: int = 2,
    mode: str = "sgp",
    diversity_mode: str = "balanced",
    season: int | None = None,
    history_limit: int = 10,
    recommendation_limit: int = 10,
    odds_min: float | None = None,
    odds_max: float | None = None,
) -> dict[str, Any]:
    clean_side = _clean_side(side)
    explicit_markets = _clean_market_csv(markets)
    clean_markets = explicit_markets or _clean_market_csv(CORE_GPT_MARKETS)
    should_diversify_markets = len(explicit_markets) != 1
    clean_diversity_mode = _clean_diversity_mode(diversity_mode)
    recommendation_limit_value = _clean_int(recommendation_limit, 1, 25)
    target_date = slate_date or _today(timezone_name)
    matchup_tokens = _matchup_tokens(matchup)
    if _clear_mlb_cache_per_gpt_request():
        clear_mlb_bridge_cache()

    slate = await build_mlb_player_props_slate(
        client=stake_client,
        slate_date=target_date,
        timezone_name=timezone_name,
        limit=limit,
        line_mode="all",
        include_markets=clean_markets,
        exclude_markets=None,
        fixture_filter=lambda fixture: _fixture_matches_tokens(fixture, matchup_tokens),
    )
    props_payload = build_stable_props_payload(slate, include_movement=False)
    props_payload = _dedupe_payload_to_visible_lines(props_payload)
    matchup_payload = _filter_payload_to_matchup(props_payload, matchup)
    enriched = await enrich_props_with_mlb_data(
        matchup_payload,
        mlb_engine,
        season=season,
        group_mode="auto",
        history_limit=history_limit,
    )
    recommendations, recommendation_diagnostics = _build_recommendations(
        enriched,
        clean_side,
        enable_market_diversity=should_diversify_markets,
        diversity_mode=clean_diversity_mode,
        recommendation_limit=recommendation_limit_value,
    )
    recommendations = recommendations[:recommendation_limit_value]
    recommendation_diagnostics["recommendationLimit"] = recommendation_limit_value
    recommendation_diagnostics["returnedCount"] = len(recommendations)
    recommendation_diagnostics["marketCounts"] = _market_counts(recommendations)
    parlay = build_parlay_candidates(
        recommendations,
        legs=legs,
        odds_min=odds_min,
        odds_max=odds_max,
        count=3,
        mode=mode,
        allow_risk=True,
        buckets={"watchlist"},
        max_pool=40,
    )
    return {
        "source": "live_stake_odds_plus_mlb_stats",
        "readOnly": True,
        "warning": (
            "These are research outputs, not guaranteed picks. The route only recommends "
            "from player props currently returned by Stake for the requested board."
        ),
        "matchup": matchup,
        "date": enriched.get("date"),
        "timezone": enriched.get("timezone"),
        "filters": {
            "markets": sorted(clean_markets),
            "side": clean_side,
            "legs": _clean_int(legs, 1, 12),
            "mode": _clean_mode(mode),
            "diversityMode": clean_diversity_mode,
        },
        "matchedFixtureCount": matchup_payload["fixtureCount"],
        "availablePropCount": matchup_payload["propCount"],
        "matchedPropCount": enriched.get("matchedPropCount", 0),
        "unmatchedPropCount": enriched.get("unmatchedPropCount", 0),
        "recommendationCount": len(recommendations),
        "recommendationDiagnostics": recommendation_diagnostics,
        "recommendations": [
            {**pick, "rank": index + 1}
            for index, pick in enumerate(recommendations)
        ],
        "parlay": parlay,
        "notes": _response_notes(
            matchup_payload,
            recommendations,
            parlay,
            recommendation_diagnostics,
            _clean_int(legs, 1, 12),
        ),
    }


async def build_matchup_prop_board(
    stake_client: Any,
    matchup: str,
    slate_date: date | None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
    markets: str | None = None,
    side: str = "any",
) -> dict[str, Any]:
    matchup_payload = await _build_matchup_props_payload(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
    )
    clean_side = _clean_side(side)
    selections = _side_level_selections(matchup_payload.get("props") or [], clean_side)
    return {
        "source": "live_stake_prop_board",
        "decisionOwner": "chatgpt",
        "readOnly": True,
        "warning": (
            "This board is data for GPT reasoning only. It is not an AZP pick list, "
            "and any final GPT choice should be validated before answering."
        ),
        "matchup": matchup,
        "date": matchup_payload.get("date"),
        "timezone": matchup_payload.get("timezone"),
        "filters": {
            "markets": sorted(_markets_for_board(markets)),
            "side": clean_side,
        },
        "matchedFixtureCount": matchup_payload.get("fixtureCount", 0),
        "propCount": matchup_payload.get("propCount", 0),
        "selectionCount": len(selections),
        "props": [_board_prop(prop) for prop in matchup_payload.get("props") or []],
        "selections": selections,
        "notes": _board_notes(matchup_payload, selections),
    }


async def build_player_mlb_context(
    stake_client: Any,
    mlb_engine: Any,
    matchup: str,
    prop_id: str,
    slate_date: date | None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
    markets: str | None = None,
    season: int | None = None,
    history_limit: int = 15,
) -> dict[str, Any]:
    matchup_payload = await _build_matchup_props_payload(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
    )
    prop = _find_prop(matchup_payload.get("props") or [], prop_id)
    if prop is None:
        return {
            "source": "stake_prop_plus_mlb_context",
            "readOnly": True,
            "found": False,
            "matchup": matchup,
            "date": matchup_payload.get("date"),
            "propId": prop_id,
            "notes": ["The requested propId is not currently on the Stake board."],
        }

    enriched = await enrich_props_with_mlb_data(
        {**matchup_payload, "props": [prop], "propCount": 1},
        mlb_engine,
        season=season,
        group_mode="auto",
        history_limit=history_limit,
    )
    enriched_prop = (enriched.get("props") or [None])[0]
    if not isinstance(enriched_prop, dict):
        return {
            "source": "stake_prop_plus_mlb_context",
            "readOnly": True,
            "found": False,
            "matchup": matchup,
            "date": matchup_payload.get("date"),
            "propId": prop_id,
            "notes": ["The requested prop could not be enriched with MLB context."],
        }

    return _player_context_response(enriched_prop, matchup, enriched, history_limit)


async def validate_gpt_selections(
    stake_client: Any,
    matchup: str,
    selections: list[dict[str, Any]],
    slate_date: date | None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
    markets: str | None = None,
) -> dict[str, Any]:
    matchup_payload = await _build_matchup_props_payload(
        stake_client=stake_client,
        matchup=matchup,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
        markets=markets,
    )
    props = matchup_payload.get("props") or []
    results = [
        _validate_selection(selection, props, index)
        for index, selection in enumerate(selections, start=1)
    ]
    valid_count = sum(1 for result in results if result["valid"])
    return {
        "source": "live_stake_validation",
        "readOnly": True,
        "matchup": matchup,
        "date": matchup_payload.get("date"),
        "timezone": matchup_payload.get("timezone"),
        "valid": valid_count == len(results) and bool(results),
        "selectionCount": len(results),
        "validCount": valid_count,
        "invalidCount": len(results) - valid_count,
        "results": results,
        "notes": _validation_notes(results),
    }


async def build_gpt_decision_result(
    stake_client: Any,
    matchup: str,
    selections: list[dict[str, Any]],
    slate_date: date | None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
    markets: str | None = None,
    prompt: str | None = None,
    notes: list[str] | None = None,
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
    merged = [
        _decision_selection(selection, validation_result)
        for selection, validation_result in zip(
            selections,
            validation.get("results") or [],
            strict=False,
        )
    ]
    return {
        "source": "chatgpt_decision",
        "decisionOwner": "chatgpt",
        "azpRecommendation": False,
        "readOnly": True,
        "matchup": matchup,
        "date": validation.get("date"),
        "timezone": validation.get("timezone"),
        "prompt": prompt,
        "selectionCount": len(merged),
        "validSelectionCount": validation.get("validCount", 0),
        "invalidSelectionCount": validation.get("invalidCount", 0),
        "validation": validation,
        "selections": merged,
        "notes": notes or [],
    }


def _matchup_pick_parameters() -> list[dict[str, Any]]:
    return [
        {
            "name": "matchup",
            "in": "query",
            "required": True,
            "description": "MLB matchup text, for example 'Blue Jays vs Angels' or 'Yankees @ Red Sox'.",
            "schema": {"type": "string"},
        },
        {
            "name": "date",
            "in": "query",
            "required": False,
            "description": "Slate date in YYYY-MM-DD. Defaults to today in America/New_York.",
            "schema": {"type": "string", "format": "date"},
        },
        {
            "name": "markets",
            "in": "query",
            "required": False,
            "description": "Comma-separated Stake prop markets such as hits,total-bases,strikeouts.",
            "schema": {"type": "string"},
        },
        {
            "name": "side",
            "in": "query",
            "required": False,
            "description": "Pick side to evaluate.",
            "schema": {"type": "string", "enum": ["any", "over", "under"], "default": "any"},
        },
        {
            "name": "legs",
            "in": "query",
            "required": False,
            "description": "Requested leg count for the candidate parlay.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 6, "default": 2},
        },
        {
            "name": "mode",
            "in": "query",
            "required": False,
            "description": "Parlay mode. Use sgp for same-game parlays and standard for one leg per fixture.",
            "schema": {"type": "string", "enum": ["sgp", "standard"], "default": "sgp"},
        },
        {
            "name": "diversityMode",
            "in": "query",
            "required": False,
            "description": (
                "Recommendation selection style. balanced prefers market spread but "
                "allows clearly stronger repeated markets; best_available takes the "
                "top scores; strict_diversity hard-caps repeated markets; longshot "
                "allows concentration and flags it."
            ),
            "schema": {
                "type": "string",
                "enum": list(DIVERSITY_MODES),
                "default": "balanced",
            },
        },
        {
            "name": "season",
            "in": "query",
            "required": False,
            "description": "MLB season year for player stats. Defaults to MLB API behavior.",
            "schema": {"type": "integer", "minimum": 1876, "maximum": 2100},
        },
        {
            "name": "historyLimit",
            "in": "query",
            "required": False,
            "description": "Recent game count to pull from MLB Stats API for blended form context.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 15, "default": 10},
        },
        {
            "name": "recommendationLimit",
            "in": "query",
            "required": False,
            "description": "Maximum number of individual recommendations to return.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "description": "Maximum number of MLB fixtures to inspect from the Stake slate.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        },
        {
            "name": "oddsMin",
            "in": "query",
            "required": False,
            "description": "Minimum raw product odds for the candidate parlay.",
            "schema": {"type": "number", "minimum": 1},
        },
        {
            "name": "oddsMax",
            "in": "query",
            "required": False,
            "description": "Maximum raw product odds for the candidate parlay.",
            "schema": {"type": "number", "minimum": 1},
        },
    ]


def _matchup_prop_board_parameters() -> list[dict[str, Any]]:
    return [
        {
            "name": "matchup",
            "in": "query",
            "required": True,
            "description": "MLB matchup text, for example 'Blue Jays vs Angels'.",
            "schema": {"type": "string"},
        },
        {
            "name": "date",
            "in": "query",
            "required": False,
            "description": "Slate date in YYYY-MM-DD. Defaults to today in America/New_York.",
            "schema": {"type": "string", "format": "date"},
        },
        {
            "name": "markets",
            "in": "query",
            "required": False,
            "description": "Comma-separated markets. Omit for AZP core markets or use all for the full returned board.",
            "schema": {"type": "string"},
        },
        {
            "name": "side",
            "in": "query",
            "required": False,
            "description": "Optional side filter for side-level selections.",
            "schema": {"type": "string", "enum": ["any", "over", "under"], "default": "any"},
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "description": "Maximum number of MLB fixtures to inspect from the Stake slate.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        },
    ]


def _player_context_parameters() -> list[dict[str, Any]]:
    return [
        *_matchup_prop_board_parameters()[:2],
        {
            "name": "propId",
            "in": "query",
            "required": True,
            "description": "The propId returned by getMatchupPropBoard.",
            "schema": {"type": "string"},
        },
        {
            "name": "markets",
            "in": "query",
            "required": False,
            "description": "Optional market filter used when rebuilding the board.",
            "schema": {"type": "string"},
        },
        {
            "name": "season",
            "in": "query",
            "required": False,
            "description": "MLB season year for player stats.",
            "schema": {"type": "integer", "minimum": 1876, "maximum": 2100},
        },
        {
            "name": "historyLimit",
            "in": "query",
            "required": False,
            "description": "Recent game count to pull from MLB Stats API.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 15, "default": 15},
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "description": "Maximum number of MLB fixtures to inspect from the Stake slate.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        },
    ]


def _selection_request_body(required_prompt: bool) -> dict[str, Any]:
    required = ["matchup", "selections"]
    if required_prompt:
        required.append("prompt")
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "matchup": {
                            "type": "string",
                            "description": "MLB matchup text, for example Blue Jays vs Angels.",
                        },
                        "date": {"type": "string", "format": "date"},
                        "markets": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "prompt": {"type": "string"},
                        "notes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "selections": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "propId": {"type": "string"},
                                    "side": {
                                        "type": "string",
                                        "enum": ["over", "under"],
                                    },
                                    "line": {"type": "number"},
                                    "odds": {"type": "number"},
                                    "rationale": {"type": "string"},
                                },
                                "required": ["propId", "side", "line"],
                                "additionalProperties": True,
                            },
                        },
                    },
                    "required": required,
                    "additionalProperties": True,
                }
            }
        },
    }


def _performance_summary_parameters() -> list[dict[str, Any]]:
    return [
        {
            "name": "date",
            "in": "query",
            "required": False,
            "description": "Slate date in YYYY-MM-DD.",
            "schema": {"type": "string", "format": "date"},
        },
        {
            "name": "market",
            "in": "query",
            "required": False,
            "description": "Optional market key such as hits, runs, total-bases, or strikeouts.",
            "schema": {"type": "string"},
        },
        {
            "name": "side",
            "in": "query",
            "required": False,
            "description": "Optional side filter.",
            "schema": {"type": "string", "enum": ["over", "under"]},
        },
        {
            "name": "requestId",
            "in": "query",
            "required": False,
            "description": "Optional saved recommendation request id.",
            "schema": {"type": "string"},
        },
        {
            "name": "diversityMode",
            "in": "query",
            "required": False,
            "description": "Optional diversity mode filter.",
            "schema": {"type": "string", "enum": list(DIVERSITY_MODES)},
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "description": "Maximum saved recommendation legs to summarize.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 500, "default": 500},
        },
    ]


def _settlement_parameters() -> list[dict[str, Any]]:
    return [
        {
            "name": "date",
            "in": "query",
            "required": False,
            "description": "Slate date in YYYY-MM-DD for saved recommendation legs to settle.",
            "schema": {"type": "string", "format": "date"},
        },
        {
            "name": "market",
            "in": "query",
            "required": False,
            "description": "Optional market key such as hits, runs, total-bases, or strikeouts.",
            "schema": {"type": "string"},
        },
        {
            "name": "side",
            "in": "query",
            "required": False,
            "description": "Optional side filter.",
            "schema": {"type": "string", "enum": ["over", "under"]},
        },
        {
            "name": "requestId",
            "in": "query",
            "required": False,
            "description": "Optional saved recommendation request id.",
            "schema": {"type": "string"},
        },
        {
            "name": "diversityMode",
            "in": "query",
            "required": False,
            "description": "Optional diversity mode filter.",
            "schema": {"type": "string", "enum": list(DIVERSITY_MODES)},
        },
        {
            "name": "season",
            "in": "query",
            "required": False,
            "description": "MLB season year used to retrieve player game logs.",
            "schema": {"type": "integer", "minimum": 1876, "maximum": 2100},
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "description": "Maximum saved recommendation legs to settle.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 500, "default": 500},
        },
        {
            "name": "historyLimit",
            "in": "query",
            "required": False,
            "description": "Maximum player game logs to inspect while settling.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
        },
    ]


async def _build_matchup_props_payload(
    stake_client: Any,
    matchup: str,
    slate_date: date | None,
    timezone_name: str,
    limit: int,
    markets: str | None = None,
) -> dict[str, Any]:
    target_date = slate_date or _today(timezone_name)
    matchup_tokens = _matchup_tokens(matchup)
    if _clear_mlb_cache_per_gpt_request():
        clear_mlb_bridge_cache()
    slate = await build_mlb_player_props_slate(
        client=stake_client,
        slate_date=target_date,
        timezone_name=timezone_name,
        limit=limit,
        line_mode="all",
        include_markets=_markets_for_board(markets),
        exclude_markets=None,
        fixture_filter=lambda fixture: _fixture_matches_tokens(fixture, matchup_tokens),
    )
    props_payload = build_stable_props_payload(slate, include_movement=False)
    props_payload = _dedupe_payload_to_visible_lines(props_payload)
    return _filter_payload_to_matchup(props_payload, matchup)


def _markets_for_board(markets: str | None) -> set[str]:
    cleaned = str(markets or "").strip().lower()
    if cleaned in {"all", "*"}:
        return set()
    return _clean_market_csv(markets) or _clean_market_csv(CORE_GPT_MARKETS)


def _side_level_selections(
    props: list[dict[str, Any]],
    side: str,
) -> list[dict[str, Any]]:
    sides = ("over", "under") if side == "any" else (side,)
    selections: list[dict[str, Any]] = []
    for prop in props:
        for selected_side in sides:
            selection = _selection_from_prop(prop, selected_side)
            if selection:
                selections.append(selection)
    return selections


def _selection_from_prop(
    prop: dict[str, Any],
    side: str,
) -> dict[str, Any] | None:
    odds = prop.get("odds") or {}
    odds_value = _float_or_none(odds.get(side))
    if odds_value is None:
        return None
    player = prop.get("player") or {}
    team = prop.get("team") or {}
    market = prop.get("market") or {}
    line = _float_or_none(prop.get("line"))
    prop_id = str(prop.get("propId") or "")
    return {
        "selectionId": f"{prop_id}:{side}",
        "propId": prop_id,
        "fixtureSlug": prop.get("fixtureSlug"),
        "game": prop.get("game"),
        "startTime": prop.get("startTime"),
        "status": prop.get("status"),
        "player": player,
        "team": team,
        "market": market,
        "line": line,
        "side": side,
        "odds": odds_value,
        "overOdds": _float_or_none(odds.get("over")),
        "underOdds": _float_or_none(odds.get("under")),
        "selection": _selection_text(player.get("name"), side, line or 0.0, market.get("name")),
        "availability": _availability_flags(prop, side),
    }


def _availability_flags(prop: dict[str, Any], side: str | None = None) -> dict[str, Any]:
    odds = prop.get("odds") or {}
    over = _float_or_none(odds.get("over"))
    under = _float_or_none(odds.get("under"))
    side_odds = _float_or_none(odds.get(side)) if side else None
    min_odds = _minimum_playable_odds()
    issues = []
    if over is None or under is None:
        issues.append("missing_two_sided_odds")
    if over is not None and over <= 1.0:
        issues.append("invalid_over_odds")
    if under is not None and under <= 1.0:
        issues.append("invalid_under_odds")
    if over is not None and 1.0 < over < min_odds:
        issues.append("over_below_playable_threshold")
    if under is not None and 1.0 < under < min_odds:
        issues.append("under_below_playable_threshold")
    if side and side_odds is None:
        issues.append("missing_selected_side_odds")
    if side and side_odds is not None and side_odds < min_odds:
        issues.append("selected_side_below_playable_threshold")

    two_sided = over is not None and under is not None and over > 1.0 and under > 1.0
    market_playable = two_sided and over >= min_odds and under >= min_odds
    side_playable = side_odds is None or side_odds >= min_odds
    return {
        "stakeReturned": True,
        "twoSided": two_sided,
        "marketPlayable": market_playable,
        "playable": market_playable and side_playable,
        "minPlayableOdds": min_odds,
        "issues": sorted(set(issues)),
    }


def _board_prop(prop: dict[str, Any]) -> dict[str, Any]:
    return {
        "propId": prop.get("propId"),
        "fixtureSlug": prop.get("fixtureSlug"),
        "game": prop.get("game"),
        "startTime": prop.get("startTime"),
        "status": prop.get("status"),
        "player": prop.get("player") or {},
        "team": prop.get("team") or {},
        "market": prop.get("market") or {},
        "line": _float_or_none(prop.get("line")),
        "odds": prop.get("odds") or {},
        "availability": _availability_flags(prop),
    }


def _board_notes(
    matchup_payload: dict[str, Any],
    selections: list[dict[str, Any]],
) -> list[str]:
    notes = []
    if matchup_payload.get("propCount", 0) == 0:
        notes.append("No Stake props matched the requested matchup text.")
    unplayable = sum(
        1
        for selection in selections
        if not (selection.get("availability") or {}).get("playable")
    )
    if unplayable:
        notes.append(
            f"{unplayable} returned selections are flagged as not playable; "
            "do not choose them unless the user confirms they appear in Stake."
        )
    notes.append(
        "This is a board for GPT reasoning. It deliberately does not include AZP scores."
    )
    return notes


def _find_prop(props: list[dict[str, Any]], prop_id: str) -> dict[str, Any] | None:
    for prop in props:
        if str(prop.get("propId") or "") == str(prop_id or ""):
            return prop
    return None


def _player_context_response(
    prop: dict[str, Any],
    matchup: str,
    enriched: dict[str, Any],
    history_limit: int,
) -> dict[str, Any]:
    player = prop.get("player") or {}
    team = prop.get("team") or {}
    market = prop.get("market") or {}
    stat_context = prop.get("statContext") or {}
    stat_key = stat_context.get("statKey")
    history = prop.get("recentHistory") or {}
    games = history.get("games") or []
    season_stats = (((prop.get("mlbProfile") or {}).get("player") or {}).get("stats") or {})
    season_value = stat_context.get("seasonValue")
    return {
        "source": "stake_prop_plus_mlb_context",
        "readOnly": True,
        "found": True,
        "matchup": matchup,
        "date": enriched.get("date"),
        "timezone": enriched.get("timezone"),
        "propId": prop.get("propId"),
        "player": player,
        "team": team,
        "market": market,
        "stakeProp": {
            "fixtureSlug": prop.get("fixtureSlug"),
            "game": prop.get("game"),
            "line": _float_or_none(prop.get("line")),
            "odds": prop.get("odds") or {},
            "availability": _availability_flags(prop),
        },
        "statContext": stat_context,
        "recent": {
            "last5": _recent_summary(games, stat_key, 5),
            "last10": _recent_summary(games, stat_key, 10),
            "last15": _recent_summary(games, stat_key, 15),
            "requestedLimit": _clean_int(history_limit, 1, 100),
        },
        "season": {
            "value": season_value,
            "perGame": _season_per_game(season_stats, season_value),
            "stats": season_stats,
        },
        "matchupGame": prop.get("mlbGame"),
        "mlbMatch": prop.get("mlbMatch"),
        "notes": _player_context_notes(prop),
    }


def _recent_summary(
    games: list[dict[str, Any]],
    stat_key: Any,
    limit: int,
) -> dict[str, Any]:
    if not stat_key:
        return {"gamesUsed": 0, "total": None, "perGame": None, "games": []}
    selected_games = games[:limit]
    values = [
        _float_or_none((game.get("stats") or {}).get(str(stat_key)))
        for game in selected_games
    ]
    clean_values = [value for value in values if value is not None]
    total = round(sum(clean_values), 4) if clean_values else None
    per_game = round(total / len(clean_values), 4) if clean_values else None
    return {
        "gamesUsed": len(clean_values),
        "total": total,
        "perGame": per_game,
        "games": selected_games,
    }


def _player_context_notes(prop: dict[str, Any]) -> list[str]:
    notes = []
    match = prop.get("mlbMatch") or {}
    if match.get("status") != "matched_exact_name_team":
        notes.append("MLB player/team match is not fully confirmed.")
    if not (prop.get("statContext") or {}).get("supported"):
        notes.append("This market does not have a supported MLB stat mapping yet.")
    if not _availability_flags(prop).get("playable"):
        notes.append("Stake odds are currently flagged as not playable.")
    return notes


def _validate_selection(
    selection: dict[str, Any],
    props: list[dict[str, Any]],
    rank: int,
) -> dict[str, Any]:
    prop_id = str(selection.get("propId") or "")
    side = _clean_side(selection.get("side"))
    submitted_line = _float_or_none(selection.get("line"))
    submitted_odds = _float_or_none(selection.get("odds"))
    issues = []
    if not prop_id:
        issues.append("missing_prop_id")
    if side not in {"over", "under"}:
        issues.append("invalid_side")
    prop = _find_prop(props, prop_id)
    current_selection = _selection_from_prop(prop, side) if prop and side in {"over", "under"} else None
    if prop is None:
        issues.append("prop_not_available")
    elif current_selection is None:
        issues.append("side_not_available")
    else:
        current_line = _float_or_none(current_selection.get("line"))
        current_odds = _float_or_none(current_selection.get("odds"))
        if submitted_line is None:
            issues.append("missing_line")
        elif current_line is None or abs(current_line - submitted_line) > 0.000001:
            issues.append("line_mismatch")
        if submitted_odds is not None and current_odds is not None:
            if abs(current_odds - submitted_odds) > 0.000001:
                issues.append("odds_changed")
        if not (current_selection.get("availability") or {}).get("playable"):
            issues.append("unplayable_current_odds")

    return {
        "rank": rank,
        "valid": not issues,
        "issues": sorted(set(issues)),
        "submitted": selection,
        "currentSelection": current_selection,
    }


def _validation_notes(results: list[dict[str, Any]]) -> list[str]:
    if not results:
        return ["No selections were submitted for validation."]
    invalid = [result for result in results if not result.get("valid")]
    if not invalid:
        return ["All submitted selections still match the current Stake board."]
    issue_counts: dict[str, int] = {}
    for result in invalid:
        for issue in result.get("issues") or []:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    return [
        "Some selections no longer match the current Stake board.",
        "Issues: "
        + ", ".join(
            f"{issue}={count}"
            for issue, count in sorted(issue_counts.items())
        ),
    ]


def _decision_selection(
    selection: dict[str, Any],
    validation_result: dict[str, Any],
) -> dict[str, Any]:
    current = validation_result.get("currentSelection") or {}
    submitted = validation_result.get("submitted") or selection
    merged = {
        **current,
        "rank": validation_result.get("rank"),
        "valid": validation_result.get("valid"),
        "validationIssues": validation_result.get("issues") or [],
        "submitted": submitted,
    }
    if not current:
        merged.update(
            {
                "propId": submitted.get("propId"),
                "side": submitted.get("side"),
                "line": _float_or_none(submitted.get("line")),
                "odds": _float_or_none(submitted.get("odds")),
            }
        )
    if submitted.get("rationale"):
        merged["rationale"] = submitted.get("rationale")
    return merged


def _filter_payload_to_matchup(
    props_payload: dict[str, Any],
    matchup: str,
) -> dict[str, Any]:
    tokens = _matchup_tokens(matchup)
    matched_props = [
        prop
        for prop in props_payload.get("props") or []
        if _prop_matches_tokens(prop, tokens)
    ]
    fixture_slugs = {
        str(prop.get("fixtureSlug") or "")
        for prop in matched_props
        if prop.get("fixtureSlug")
    }
    payload = dict(props_payload)
    payload["fixtureCount"] = len(fixture_slugs)
    payload["propCount"] = len(matched_props)
    payload["props"] = matched_props
    return payload


def _dedupe_payload_to_visible_lines(props_payload: dict[str, Any]) -> dict[str, Any]:
    selected: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for prop in props_payload.get("props") or []:
        player = prop.get("player") or {}
        team = prop.get("team") or {}
        market = prop.get("market") or {}
        key = (
            str(prop.get("fixtureSlug") or ""),
            str(player.get("key") or ""),
            str(team.get("key") or ""),
            str(market.get("key") or ""),
        )
        current = selected.get(key)
        if current is None or _visible_line_rank(prop) < _visible_line_rank(current):
            selected[key] = prop

    payload = dict(props_payload)
    payload["props"] = list(selected.values())
    payload["propCount"] = len(payload["props"])
    return payload


def _visible_line_rank(prop: dict[str, Any]) -> tuple[float, float]:
    line = _float_or_none(prop.get("line"))
    odds = prop.get("odds") or {}
    over = _float_or_none(odds.get("over"))
    under = _float_or_none(odds.get("under"))
    if line is None:
        return (9999.0, 9999.0)
    if over is None or under is None or over <= 1.0 or under <= 1.0:
        return (9999.0, abs(line))
    return (abs(line), abs(float(over) - float(under)))


def _market_has_unplayable_odds(
    prop: dict[str, Any],
    min_playable_odds: float,
) -> bool:
    odds = prop.get("odds") or {}
    over = _float_or_none(odds.get("over"))
    under = _float_or_none(odds.get("under"))
    return (
        over is None
        or under is None
        or over <= 1.0
        or under <= 1.0
        or over < min_playable_odds
        or under < min_playable_odds
    )


def _build_recommendations(
    enriched_payload: dict[str, Any],
    side: str,
    enable_market_diversity: bool,
    diversity_mode: str,
    recommendation_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    picks = []
    diagnostics = _recommendation_diagnostics(
        enable_market_diversity=enable_market_diversity,
        diversity_mode=diversity_mode,
    )
    min_playable_odds = _minimum_playable_odds()
    diagnostics["minPlayableOdds"] = min_playable_odds
    for prop in enriched_payload.get("props") or []:
        if ((prop.get("player") or {}).get("matchStatus") != "matched_exact_name_team"):
            continue
        if not _pitcher_prop_is_probable_pitcher(prop):
            continue
        if _market_has_unplayable_odds(prop, min_playable_odds):
            diagnostics["discardedUnavailableMarket"] += 1
            diagnostics["discardedInvalidOdds"] += 1
            continue
        sides = ("over", "under") if side == "any" else (side,)
        for pick_side in sides:
            diagnostics["consideredSides"] += 1
            odds_value = _float_or_none((prop.get("odds") or {}).get(pick_side))
            if odds_value is None:
                diagnostics["discardedMissingOdds"] += 1
                continue
            if odds_value <= 1.0:
                diagnostics["discardedInvalidOdds"] += 1
                continue
            if odds_value < min_playable_odds:
                diagnostics["discardedBelowMinOdds"] += 1
                continue
            pick = _recommendation_for_side(prop, pick_side)
            if pick:
                picks.append(pick)
    sorted_picks = sorted(
        picks,
        key=lambda pick: (
            -int(pick["score"]),
            _confidence_sort_key(pick.get("confidence")),
            -float(pick["odds"]),
            str((pick.get("player") or {}).get("name") or ""),
        ),
    )
    if enable_market_diversity:
        diagnostics["maxRecommendationsPerMarket"] = _max_recommendations_per_market()
        sorted_picks = _apply_recommendation_diversity(
            sorted_picks,
            diagnostics,
            recommendation_limit,
        )
    diagnostics["eligibleBeforeDiversity"] = len(picks)
    diagnostics["marketCounts"] = _market_counts(sorted_picks)
    return sorted_picks, diagnostics


def _recommendation_diagnostics(
    enable_market_diversity: bool,
    diversity_mode: str,
) -> dict[str, Any]:
    return {
        "minPlayableOdds": DEFAULT_MIN_PLAYABLE_ODDS,
        "consideredSides": 0,
        "discardedMissingOdds": 0,
        "discardedInvalidOdds": 0,
        "discardedBelowMinOdds": 0,
        "discardedUnavailableMarket": 0,
        "discardedByMarketDiversity": 0,
        "eligibleBeforeDiversity": 0,
        "marketDiversityApplied": enable_market_diversity,
        "diversityMode": diversity_mode,
        "maxRecommendationsPerMarket": None,
        "softDiversityScoreGap": _soft_diversity_score_gap(),
        "softDiversityPromotions": 0,
        "softDiversityOverrides": 0,
        "softDiversityPromotionDetails": [],
        "softDiversityOverrideDetails": [],
        "concentrationTags": [],
        "marketCounts": {},
    }


def _apply_recommendation_diversity(
    picks: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    recommendation_limit: int,
) -> list[dict[str, Any]]:
    mode = str(diagnostics.get("diversityMode") or "balanced")
    target_count = max(1, min(int(recommendation_limit), len(picks) or 1))
    if mode in {"best_available", "longshot"}:
        diagnostics["concentrationTags"] = _concentration_tags(
            picks[:target_count],
            int(diagnostics["maxRecommendationsPerMarket"]),
        )
        return picks
    if mode == "strict_diversity":
        selected = _apply_strict_market_diversity(picks, diagnostics)
        diagnostics["concentrationTags"] = _concentration_tags(
            selected[:target_count],
            int(diagnostics["maxRecommendationsPerMarket"]),
        )
        return selected
    selected = _apply_soft_market_diversity(picks, diagnostics, target_count)
    diagnostics["concentrationTags"] = _concentration_tags(
        selected[:target_count],
        int(diagnostics["maxRecommendationsPerMarket"]),
    )
    return selected


def _apply_strict_market_diversity(
    picks: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> list[dict[str, Any]]:
    max_per_market = int(diagnostics["maxRecommendationsPerMarket"])
    kept: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    discarded = 0
    for pick in picks:
        market = str(pick.get("marketKey") or "unknown")
        if counts.get(market, 0) >= max_per_market:
            discarded += 1
            continue
        kept.append(pick)
        counts[market] = counts.get(market, 0) + 1

    diagnostics["discardedByMarketDiversity"] = discarded
    return kept


def _apply_soft_market_diversity(
    picks: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    recommendation_limit: int,
) -> list[dict[str, Any]]:
    max_per_market = int(diagnostics["maxRecommendationsPerMarket"])
    score_gap = int(diagnostics["softDiversityScoreGap"])
    selected: list[dict[str, Any]] = []
    remaining = list(picks)
    counts: dict[str, int] = {}

    while remaining and len(selected) < recommendation_limit:
        candidate = remaining[0]
        market = str(candidate.get("marketKey") or "unknown")
        if counts.get(market, 0) >= max_per_market:
            alternative_index = _first_under_market_cap_index(
                remaining,
                counts,
                max_per_market,
            )
            if alternative_index is not None:
                alternative = remaining[alternative_index]
                gap = _pick_score(candidate) - _pick_score(alternative)
                if gap <= score_gap:
                    choice = remaining.pop(alternative_index)
                    diagnostics["softDiversityPromotions"] += 1
                    diagnostics["softDiversityPromotionDetails"].append(
                        _diversity_detail(candidate, choice, gap)
                    )
                    _append_selected_pick(selected, choice, counts)
                    continue
                diagnostics["softDiversityOverrides"] += 1
                diagnostics["softDiversityOverrideDetails"].append(
                    _diversity_detail(candidate, alternative, gap)
                )

        choice = remaining.pop(0)
        _append_selected_pick(selected, choice, counts)

    return selected + remaining


def _append_selected_pick(
    selected: list[dict[str, Any]],
    pick: dict[str, Any],
    counts: dict[str, int],
) -> None:
    selected.append(pick)
    market = str(pick.get("marketKey") or "unknown")
    counts[market] = counts.get(market, 0) + 1


def _first_under_market_cap_index(
    picks: list[dict[str, Any]],
    counts: dict[str, int],
    max_per_market: int,
) -> int | None:
    for index, pick in enumerate(picks):
        market = str(pick.get("marketKey") or "unknown")
        if counts.get(market, 0) < max_per_market:
            return index
    return None


def _diversity_detail(
    repeated_pick: dict[str, Any],
    alternative_pick: dict[str, Any],
    score_gap: int,
) -> dict[str, Any]:
    return {
        "repeatedMarket": repeated_pick.get("marketKey"),
        "repeatedSelection": repeated_pick.get("selection"),
        "repeatedScore": _pick_score(repeated_pick),
        "alternativeMarket": alternative_pick.get("marketKey"),
        "alternativeSelection": alternative_pick.get("selection"),
        "alternativeScore": _pick_score(alternative_pick),
        "scoreGap": score_gap,
    }


def _pick_score(pick: dict[str, Any]) -> int:
    try:
        return int(pick.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def _concentration_tags(
    picks: list[dict[str, Any]],
    max_per_market: int,
) -> list[str]:
    tags: list[str] = []
    market_counts = _market_counts(picks)
    for market, count in market_counts.items():
        if count > max_per_market:
            tags.append(f"market_concentration:{market}")

    side_counts = _side_counts(picks)
    for side, count in side_counts.items():
        if side in {"over", "under"} and count >= 3:
            tags.append(f"same_side_cluster:{side}")
            if side == "under":
                tags.append("low_scoring_script")
            elif side == "over":
                tags.append("high_scoring_script")

    if tags and any(count >= 2 for count in _fixture_counts(picks).values()):
        tags.append("sgp_repricing_sensitive")

    return sorted(set(tags))


def _market_counts(picks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pick in picks:
        market = str(pick.get("marketKey") or "unknown")
        counts[market] = counts.get(market, 0) + 1
    return {market: counts[market] for market in sorted(counts)}


def _side_counts(picks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pick in picks:
        side = str(pick.get("side") or "unknown")
        counts[side] = counts.get(side, 0) + 1
    return {side: counts[side] for side in sorted(counts)}


def _fixture_counts(picks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pick in picks:
        fixture = str(pick.get("fixtureSlug") or "unknown")
        counts[fixture] = counts.get(fixture, 0) + 1
    return {fixture: counts[fixture] for fixture in sorted(counts)}


def _pitcher_prop_is_probable_pitcher(prop: dict[str, Any]) -> bool:
    market_key = str((prop.get("market") or {}).get("key") or "")
    if market_key not in PITCHER_ONLY_MARKETS:
        return True

    probable_keys = _probable_pitcher_keys(prop.get("mlbGame") or {})
    if not probable_keys:
        return True

    player = prop.get("player") or {}
    match = prop.get("mlbMatch") or {}
    matched_player = match.get("matchedPlayer") or {}
    player_keys = {
        slug_key(player.get("key") or player.get("name")),
        slug_key(matched_player.get("key") or matched_player.get("name")),
    }
    return bool(probable_keys.intersection(player_keys))


def _probable_pitcher_keys(mlb_game: dict[str, Any]) -> set[str]:
    keys = set()
    for side in ("awayTeam", "homeTeam"):
        pitcher = ((mlb_game.get(side) or {}).get("probablePitcher") or {})
        key = slug_key(pitcher.get("key") or pitcher.get("name"))
        if key:
            keys.add(key)
    return keys


def _recommendation_for_side(
    prop: dict[str, Any],
    side: str,
) -> dict[str, Any] | None:
    context = prop.get("statContext") or {}
    line = _float_or_none(prop.get("line"))
    recent_per_game = _float_or_none(context.get("recentPerGame"))
    odds = (prop.get("odds") or {}).get(side)
    odds_value = _float_or_none(odds)
    stat_key = context.get("statKey")
    if line is None or recent_per_game is None or odds_value is None or not stat_key:
        return None

    edge = recent_per_game - line if side == "over" else line - recent_per_game
    profile = _market_profile((prop.get("market") or {}).get("key"))
    threshold = max(float(profile["minEdge"]), 0.01)
    if edge < threshold:
        return None

    season_stats = (((prop.get("mlbProfile") or {}).get("player") or {}).get("stats") or {})
    season_per_game = _season_per_game(season_stats, context.get("seasonValue"))
    form_context = _form_context(
        prop=prop,
        stat_key=str(stat_key),
        line=line,
        side=side,
        season_per_game=season_per_game,
    )
    reasons, risk_flags = _reasons_and_risks(
        side=side,
        line=line,
        edge=edge,
        odds=odds_value,
        profile=profile,
        games_used=_int_or_none(context.get("gamesUsed")),
        season_per_game=season_per_game,
        form_context=form_context,
    )
    score = _score_pick(edge, threshold, reasons, risk_flags)
    confidence = "high" if not risk_flags else "medium"
    player = prop.get("player") or {}
    team = prop.get("team") or {}
    market = prop.get("market") or {}
    lean = "over" if side == "over" else "under_or_avoid_over"
    pick = {
        "rank": None,
        "bucket": "watchlist",
        "propId": prop.get("propId"),
        "fixtureSlug": prop.get("fixtureSlug"),
        "game": prop.get("game"),
        "playerName": player.get("name"),
        "teamName": team.get("name"),
        "marketKey": market.get("key"),
        "statKey": stat_key,
        "line": line,
        "lean": lean,
        "side": side,
        "odds": odds_value,
        "overOdds": (prop.get("odds") or {}).get("over"),
        "underOdds": (prop.get("odds") or {}).get("under"),
        "edge": round(edge, 4),
        "score": score,
        "confidence": confidence,
        "selection": _selection_text(player.get("name"), side, line, market.get("name")),
        "player": {
            "name": player.get("name"),
            "key": player.get("key"),
            "mlbId": player.get("mlbId"),
            "matchStatus": player.get("matchStatus"),
        },
        "team": {
            "name": team.get("name"),
            "key": team.get("key"),
            "mlbId": team.get("mlbId"),
        },
        "market": {
            "name": market.get("name"),
            "key": market.get("key"),
        },
        "stakeOdds": prop.get("odds") or {},
        "recent5": {
            "gamesUsed": context.get("gamesUsed"),
            "total": context.get("recentTotal"),
            "perGame": recent_per_game,
            "games": ((prop.get("recentHistory") or {}).get("games") or [])[:5],
        },
        "season": {
            "value": context.get("seasonValue"),
            "perGame": season_per_game,
            "stats": season_stats,
        },
        "formContext": form_context,
        "mlbGame": prop.get("mlbGame"),
        "mlbMatch": prop.get("mlbMatch"),
        "riskFlags": risk_flags,
        "reasons": reasons,
        "whyIncluded": _why_included(side, reasons),
        "whyNotStronger": _why_not_stronger(risk_flags),
    }
    return apply_contextual_edge_layer(pick)


def _form_context(
    prop: dict[str, Any],
    stat_key: str,
    line: float,
    side: str,
    season_per_game: float | None,
) -> dict[str, Any]:
    recent_games = ((prop.get("recentHistory") or {}).get("games") or [])
    recent5 = _recent_window_per_game(recent_games, stat_key, 5)
    recent10 = _recent_window_per_game(recent_games, stat_key, 10)
    recent15 = _recent_window_per_game(recent_games, stat_key, 15)
    blended = _weighted_average(
        [
            (recent5, 0.5),
            (recent10, 0.25),
            (season_per_game, 0.25),
        ]
    )
    blended_edge = None
    if blended is not None:
        blended_edge = line - blended if side == "under" else blended - line
        blended_edge = round(blended_edge, 4)
    return {
        "recent5PerGame": recent5,
        "recent10PerGame": recent10,
        "recent15PerGame": recent15,
        "seasonPerGame": season_per_game,
        "blendedPerGame": blended,
        "blendedEdge": blended_edge,
        "weights": {
            "recent5": 0.5,
            "recent10": 0.25,
            "season": 0.25,
        },
    }


def _recent_window_per_game(
    games: list[dict[str, Any]],
    stat_key: str,
    limit: int,
) -> float | None:
    values = []
    for game in games[:limit]:
        value = _float_or_none((game.get("stats") or {}).get(stat_key))
        if value is not None:
            values.append(value)
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _weighted_average(values: list[tuple[float | None, float]]) -> float | None:
    weighted = [(value, weight) for value, weight in values if value is not None]
    if not weighted:
        return None
    total_weight = sum(weight for _, weight in weighted)
    if total_weight <= 0:
        return None
    return round(sum(float(value) * weight for value, weight in weighted) / total_weight, 4)


def _reasons_and_risks(
    side: str,
    line: float,
    edge: float,
    odds: float,
    profile: dict[str, Any],
    games_used: int | None,
    season_per_game: float | None,
    form_context: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    reasons = [f"recent_per_game_{'above' if side == 'over' else 'below'}_line"]
    risk_flags: list[str] = []
    min_games = int(profile["minGames"])
    threshold = float(profile["minEdge"])

    if profile["sparse"]:
        risk_flags.append("sparse_market")
    if games_used is not None and games_used < min_games:
        risk_flags.append("small_recent_sample")
    if odds >= 4.0:
        risk_flags.append("long_odds")
    if side == "over" and line >= float(profile["highLine"]):
        risk_flags.append("high_line")

    if season_per_game is not None:
        if side == "over" and season_per_game >= line:
            reasons.append("season_baseline_supports_over")
            reasons.append("recent_and_season_agree")
        elif side == "under" and season_per_game <= line:
            reasons.append("season_baseline_supports_under")
            reasons.append("recent_and_season_agree")
        elif abs(season_per_game - line) >= threshold:
            risk_flags.append("season_baseline_conflicts_with_recent")

    if edge >= threshold * 2:
        reasons.append("clear_recent_edge")

    blended_edge = _float_or_none((form_context or {}).get("blendedEdge"))
    if blended_edge is not None:
        if blended_edge >= threshold:
            reasons.append("blended_form_supports_side")
        else:
            risk_flags.append("blended_form_below_threshold")
    recent5 = _float_or_none((form_context or {}).get("recent5PerGame"))
    recent10 = _float_or_none((form_context or {}).get("recent10PerGame"))
    if recent5 is not None and recent10 is not None and abs(recent5 - recent10) >= threshold:
        risk_flags.append("recent_window_volatility")

    return reasons, risk_flags


def _score_pick(
    edge: float,
    threshold: float,
    reasons: list[str],
    risk_flags: list[str],
) -> int:
    score = 72 + min(20, int(round((edge / max(threshold, 0.01)) * 6)))
    if "recent_and_season_agree" in reasons:
        score += 5
    if "blended_form_supports_side" in reasons:
        score += 3
    score -= min(18, len(risk_flags) * 4)
    return max(0, min(100, score))


def _season_per_game(stats: dict[str, Any], season_value: Any) -> float | None:
    value = _float_or_none(season_value)
    if value is None:
        return None
    for key in ("gamesPlayed", "gamesStarted", "gamesPitched", "games"):
        games = _float_or_none(stats.get(key))
        if games and games > 0:
            return round(value / games, 4)
    return None


def _selection_text(player: Any, side: str, line: float, market: Any) -> str:
    return f"{player or 'Unknown Player'} {side} {line:g} {market or 'prop'}"


def _why_included(side: str, reasons: list[str]) -> list[str]:
    text = [f"recent form supports the {side}"]
    if "season_baseline_supports_over" in reasons or "season_baseline_supports_under" in reasons:
        text.append("season baseline supports the same side")
    if "clear_recent_edge" in reasons:
        text.append("edge clears the market threshold by a wide margin")
    return text


def _why_not_stronger(risk_flags: list[str]) -> list[str]:
    labels = {
        "sparse_market": "sparse market with higher natural variance",
        "small_recent_sample": "recent sample is small",
        "long_odds": "long odds carry higher variance",
        "high_line": "line is above the normal market tier",
        "season_baseline_conflicts_with_recent": "season baseline conflicts with recent form",
    }
    return [labels.get(flag, flag.replace("_", " ")) for flag in risk_flags]


def _response_notes(
    matchup_payload: dict[str, Any],
    recommendations: list[dict[str, Any]],
    parlay: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
    requested_legs: int | None = None,
) -> list[str]:
    notes = []
    if matchup_payload["propCount"] == 0:
        notes.append("No Stake props matched the requested matchup text.")
    if not recommendations and matchup_payload["propCount"] > 0:
        notes.append("Stake had props for the matchup, but none cleared the current recommendation threshold.")
    if diagnostics:
        discarded_odds = int(diagnostics.get("discardedInvalidOdds") or 0) + int(
            diagnostics.get("discardedBelowMinOdds") or 0
        )
        if discarded_odds:
            notes.append(
                "Filtered "
                f"{discarded_odds} Stake feed legs below playable odds "
                f"threshold ({float(diagnostics['minPlayableOdds']):.2f}); "
                "not backfilled with weaker picks."
            )
        unavailable_markets = int(diagnostics.get("discardedUnavailableMarket") or 0)
        if unavailable_markets:
            notes.append(
                f"Filtered {unavailable_markets} markets with incomplete or unplayable "
                "two-sided Stake odds; these are treated as unavailable."
            )
        discarded_diversity = int(diagnostics.get("discardedByMarketDiversity") or 0)
        if discarded_diversity:
            notes.append(
                "Strict diversity capped repeated markets at "
                f"{diagnostics.get('maxRecommendationsPerMarket')} per market; "
                f"removed {discarded_diversity} lower-ranked repeated-market legs."
            )
        soft_promotions = int(diagnostics.get("softDiversityPromotions") or 0)
        if soft_promotions:
            notes.append(
                "Soft diversity promoted "
                f"{soft_promotions} close-score alternative market legs instead of "
                "overloading one market."
            )
        soft_overrides = int(diagnostics.get("softDiversityOverrides") or 0)
        if soft_overrides:
            notes.append(
                "Soft diversity allowed "
                f"{soft_overrides} repeated-market legs because their score gap beat "
                f"the alternative threshold ({diagnostics.get('softDiversityScoreGap')})."
            )
        concentration_tags = diagnostics.get("concentrationTags") or []
        if concentration_tags:
            notes.append(
                "Concentration flagged: "
                f"{', '.join(str(tag) for tag in concentration_tags)}. "
                "Stake SGP quote is still needed before treating parlay odds as final."
            )
    if (
        requested_legs is not None
        and len(recommendations) < requested_legs
        and matchup_payload["propCount"] > 0
    ):
        notes.append(
            f"Only {len(recommendations)} playable recommendations cleared filters "
            f"for the requested {requested_legs} legs; not force-filling weaker legs."
        )
    if parlay.get("warnings"):
        notes.extend(str(warning) for warning in parlay["warnings"])
    return notes


def _prop_matches_tokens(prop: dict[str, Any], tokens: set[str]) -> bool:
    if not tokens:
        return True
    game = str(prop.get("game") or "")
    fixture = str(prop.get("fixtureSlug") or "")
    team = str((prop.get("team") or {}).get("name") or "")
    haystack = slug_key(f"{game} {fixture} {team}")
    return all(token in haystack for token in tokens)


def _fixture_matches_tokens(fixture: dict[str, Any], tokens: set[str]) -> bool:
    if not tokens:
        return True
    name = str(fixture.get("name") or "")
    slug = str(fixture.get("slug") or "")
    haystack = slug_key(f"{name} {slug}")
    return all(token in haystack for token in tokens)


def _matchup_tokens(matchup: str) -> set[str]:
    text = str(matchup or "").lower()
    text = re.sub(r"\b(vs|versus|at)\b|[@/&+]", " ", text)
    tokens = {
        slug_key(part)
        for part in re.split(r"[^a-z0-9]+", text)
        if len(part.strip()) >= 3
    }
    aliases = {
        "ari": "diamondbacks",
        "atl": "braves",
        "bal": "orioles",
        "bos": "red-sox",
        "chc": "cubs",
        "cin": "reds",
        "cle": "guardians",
        "col": "rockies",
        "cws": "white-sox",
        "det": "tigers",
        "hou": "astros",
        "kc": "royals",
        "laa": "angels",
        "lad": "dodgers",
        "mia": "marlins",
        "mil": "brewers",
        "min": "twins",
        "nym": "mets",
        "nyy": "yankees",
        "oak": "athletics",
        "phi": "phillies",
        "pit": "pirates",
        "sd": "padres",
        "sea": "mariners",
        "sf": "giants",
        "stl": "cardinals",
        "tb": "rays",
        "tex": "rangers",
        "tor": "blue-jays",
        "wsh": "nationals",
        "jays": "blue-jays",
        "sox": "red-sox",
        "dbacks": "diamondbacks",
    }
    return {aliases.get(token, token) for token in tokens}


def _market_profile(market_key: Any) -> dict[str, Any]:
    normalized = str(market_key or "").strip().lower()
    return MARKET_PROFILES.get(
        normalized,
        {
            "name": "generic",
            "minEdge": 0.35,
            "minGames": 3,
            "sparse": False,
            "highLine": 2.5,
        },
    )


def _clean_market_csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {slug_key(part) for part in value.split(",") if slug_key(part)}


def _clean_side(value: Any) -> str:
    cleaned = str(value or "any").strip().lower()
    return cleaned if cleaned in {"any", "over", "under"} else "any"


def _clean_mode(value: Any) -> str:
    cleaned = str(value or "sgp").strip().lower()
    return cleaned if cleaned in {"sgp", "standard"} else "sgp"


def _clean_diversity_mode(value: Any) -> str:
    cleaned = str(value or "balanced").strip().lower().replace("-", "_")
    return cleaned if cleaned in DIVERSITY_MODES else "balanced"


def _today(timezone_name: str) -> date:
    if not timezone_name:
        return date.today()
    return datetime.now(ZoneInfo(timezone_name)).date()


def _clear_mlb_cache_per_gpt_request() -> bool:
    value = os.getenv("AZP_CLEAR_MLB_CACHE_PER_GPT_REQUEST", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _minimum_playable_odds() -> float:
    value = _float_or_none(os.getenv("AZP_MIN_PLAYABLE_ODDS"))
    if value is None:
        return DEFAULT_MIN_PLAYABLE_ODDS
    return max(1.01, min(value, 100.0))


def _max_recommendations_per_market() -> int:
    value = _int_or_none(os.getenv("AZP_MAX_RECOMMENDATIONS_PER_MARKET"))
    if value is None:
        return DEFAULT_MAX_RECOMMENDATIONS_PER_MARKET
    return max(1, min(value, 25))


def _soft_diversity_score_gap() -> int:
    value = _int_or_none(os.getenv("AZP_SOFT_DIVERSITY_SCORE_GAP"))
    if value is None:
        return DEFAULT_SOFT_DIVERSITY_SCORE_GAP
    return max(0, min(value, 50))


def _clean_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(parsed, maximum))


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


def _confidence_sort_key(value: Any) -> int:
    ranks = {"high": 0, "medium": 1, "low": 2}
    return ranks.get(str(value or "").lower(), 3)
