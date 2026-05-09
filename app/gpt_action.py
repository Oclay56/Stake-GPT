from __future__ import annotations

import hmac
import os
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Header, HTTPException

from .analyzer import MARKET_PROFILES
from .mlb_bridge import clear_mlb_bridge_cache, enrich_props_with_mlb_data
from .mlb_props import build_stable_props_payload, slug_key
from .parlay import build_parlay_candidates
from .slate import DEFAULT_TIMEZONE, build_mlb_player_props_slate


CORE_GPT_MARKETS = "hits,runs,rbi,total-bases,home-runs,strikeouts,earned-runs"
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
    season: int | None = None,
    history_limit: int = 5,
    recommendation_limit: int = 10,
    odds_min: float | None = None,
    odds_max: float | None = None,
) -> dict[str, Any]:
    clean_side = _clean_side(side)
    clean_markets = _clean_market_csv(markets) or _clean_market_csv(CORE_GPT_MARKETS)
    target_date = slate_date or _today(timezone_name)
    clear_mlb_bridge_cache()

    slate = await build_mlb_player_props_slate(
        client=stake_client,
        slate_date=target_date,
        timezone_name=timezone_name,
        limit=limit,
        line_mode="all",
        include_markets=clean_markets,
        exclude_markets=None,
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
    recommendations = _build_recommendations(enriched, clean_side)
    recommendations = recommendations[: _clean_int(recommendation_limit, 1, 25)]
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
        },
        "matchedFixtureCount": matchup_payload["fixtureCount"],
        "availablePropCount": matchup_payload["propCount"],
        "matchedPropCount": enriched.get("matchedPropCount", 0),
        "unmatchedPropCount": enriched.get("unmatchedPropCount", 0),
        "recommendationCount": len(recommendations),
        "recommendations": [
            {**pick, "rank": index + 1}
            for index, pick in enumerate(recommendations)
        ],
        "parlay": parlay,
        "notes": _response_notes(matchup_payload, recommendations, parlay),
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
            "description": "Recent game count to pull from MLB Stats API.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
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


def _build_recommendations(
    enriched_payload: dict[str, Any],
    side: str,
) -> list[dict[str, Any]]:
    picks = []
    for prop in enriched_payload.get("props") or []:
        if ((prop.get("player") or {}).get("matchStatus") != "matched_exact_name_team"):
            continue
        if not _pitcher_prop_is_probable_pitcher(prop):
            continue
        sides = ("over", "under") if side == "any" else (side,)
        for pick_side in sides:
            pick = _recommendation_for_side(prop, pick_side)
            if pick:
                picks.append(pick)
    return sorted(
        picks,
        key=lambda pick: (
            -int(pick["score"]),
            _confidence_sort_key(pick.get("confidence")),
            -float(pick["odds"]),
            str((pick.get("player") or {}).get("name") or ""),
        ),
    )


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
    reasons, risk_flags = _reasons_and_risks(
        side=side,
        line=line,
        edge=edge,
        odds=odds_value,
        profile=profile,
        games_used=_int_or_none(context.get("gamesUsed")),
        season_per_game=season_per_game,
    )
    score = _score_pick(edge, threshold, reasons, risk_flags)
    confidence = "high" if not risk_flags else "medium"
    player = prop.get("player") or {}
    team = prop.get("team") or {}
    market = prop.get("market") or {}
    lean = "over" if side == "over" else "under_or_avoid_over"
    return {
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
        "mlbGame": prop.get("mlbGame"),
        "mlbMatch": prop.get("mlbMatch"),
        "riskFlags": risk_flags,
        "reasons": reasons,
        "whyIncluded": _why_included(side, reasons),
        "whyNotStronger": _why_not_stronger(risk_flags),
    }


def _reasons_and_risks(
    side: str,
    line: float,
    edge: float,
    odds: float,
    profile: dict[str, Any],
    games_used: int | None,
    season_per_game: float | None,
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
) -> list[str]:
    notes = []
    if matchup_payload["propCount"] == 0:
        notes.append("No Stake props matched the requested matchup text.")
    if not recommendations and matchup_payload["propCount"] > 0:
        notes.append("Stake had props for the matchup, but none cleared the current recommendation threshold.")
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


def _today(timezone_name: str) -> date:
    if not timezone_name:
        return date.today()
    return datetime.now(ZoneInfo(timezone_name)).date()


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
