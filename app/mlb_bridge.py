from __future__ import annotations

import copy
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .mlb_props import slug_key


BRIDGE_CACHE_TTL_SECONDS = 60.0
_LOOKUP_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}

MARKET_STAT_MAP = {
    "hits": {"group": "hitting", "statKey": "hits", "label": "Hits"},
    "hit": {"group": "hitting", "statKey": "hits", "label": "Hits"},
    "singles": {
        "group": "hitting",
        "statKey": None,
        "statFormula": "singles",
        "label": "Singles",
        "contextQuality": "partial",
    },
    "single": {
        "group": "hitting",
        "statKey": None,
        "statFormula": "singles",
        "label": "Singles",
        "contextQuality": "partial",
    },
    "stolen-bases": {
        "group": "hitting",
        "statKey": "stolenBases",
        "label": "Stolen Bases",
        "contextQuality": "partial",
    },
    "steals": {
        "group": "hitting",
        "statKey": "stolenBases",
        "label": "Stolen Bases",
        "contextQuality": "partial",
    },
    "batter-walks": {
        "group": "hitting",
        "statKey": "baseOnBalls",
        "label": "Batter Walks",
        "contextQuality": "partial",
    },
    "walks-drawn": {
        "group": "hitting",
        "statKey": "baseOnBalls",
        "label": "Batter Walks",
        "contextQuality": "partial",
    },
    "total-bases": {
        "group": "hitting",
        "statKey": "totalBases",
        "label": "Total Bases",
    },
    "hits-runs-rbis": {
        "group": "hitting",
        "statKey": None,
        "statFormula": "hits_runs_rbis",
        "label": "Hits + Runs + RBIs",
        "contextQuality": "partial",
    },
    "hits-runs-rbi": {
        "group": "hitting",
        "statKey": None,
        "statFormula": "hits_runs_rbis",
        "label": "Hits + Runs + RBIs",
        "contextQuality": "partial",
    },
    "hrr": {
        "group": "hitting",
        "statKey": None,
        "statFormula": "hits_runs_rbis",
        "label": "Hits + Runs + RBIs",
        "contextQuality": "partial",
    },
    "home-runs": {"group": "hitting", "statKey": "homeRuns", "label": "Home Runs"},
    "home-run": {"group": "hitting", "statKey": "homeRuns", "label": "Home Runs"},
    "rbi": {"group": "hitting", "statKey": "rbi", "label": "RBI"},
    "runs": {"group": "hitting", "statKey": "runs", "label": "Runs"},
    "strikeouts": {
        "group": "pitching",
        "statKey": "strikeOuts",
        "label": "Strikeouts",
    },
    "pitcher-strikeouts": {
        "group": "pitching",
        "statKey": "strikeOuts",
        "label": "Strikeouts",
    },
    "earned-runs": {
        "group": "pitching",
        "statKey": "earnedRuns",
        "label": "Earned Runs",
    },
    "walks-allowed": {
        "group": "pitching",
        "statKey": "baseOnBalls",
        "label": "Walks Allowed",
    },
    "hits-allowed": {
        "group": "pitching",
        "statKey": "hits",
        "label": "Hits Allowed",
    },
    "outs-recorded": {
        "group": "pitching",
        "statKey": "outs",
        "label": "Outs Recorded",
    },
    "batter-strikeouts": {
        "group": "hitting",
        "statKey": "strikeOuts",
        "label": "Batter Strikeouts",
        "contextQuality": "partial",
    },
    "hitter-strikeouts": {
        "group": "hitting",
        "statKey": "strikeOuts",
        "label": "Batter Strikeouts",
        "contextQuality": "partial",
    },
}

PITCHING_MARKET_KEYS = {
    "strikeouts",
    "pitcher-strikeouts",
    "earned-runs",
    "pitcher-earned-runs",
    "walks-allowed",
    "hits-allowed",
    "outs-recorded",
    "pitcher-outs",
}


def clear_mlb_bridge_cache() -> None:
    _LOOKUP_CACHE.clear()


async def enrich_props_with_mlb_data(
    props_payload: dict[str, Any],
    engine: Any,
    season: int | None = None,
    group_mode: str = "auto",
    history_limit: int = 5,
    search_limit: int = 5,
) -> dict[str, Any]:
    enriched_props = []
    matched_count = 0
    slate_date = str(props_payload.get("date") or "")

    for prop in props_payload.get("props") or []:
        enriched_prop = await _enrich_prop(
            prop,
            engine,
            season=season,
            group_mode=group_mode,
            history_limit=history_limit,
            search_limit=search_limit,
            slate_date=slate_date,
        )
        if enriched_prop["mlbMatch"]["status"] != "unmatched":
            matched_count += 1
        enriched_props.append(enriched_prop)

    payload = copy.deepcopy(props_payload)
    payload.update(
        {
            "enriched": True,
            "season": season,
            "historyLimit": _clean_limit(history_limit),
            "matchedPropCount": matched_count,
            "unmatchedPropCount": len(enriched_props) - matched_count,
            "props": enriched_props,
        }
    )
    return payload


def group_for_market(market_key: str) -> str:
    return str(stat_mapping_for_market(market_key)["group"])


def stat_mapping_for_market(market_key: str) -> dict[str, Any]:
    normalized = slug_key(market_key)
    mapping = MARKET_STAT_MAP.get(normalized)
    if mapping:
        result = {
            "marketKey": normalized,
            "group": mapping["group"],
            "statKey": mapping.get("statKey"),
            "label": mapping["label"],
            "supported": True,
            "contextQuality": mapping.get("contextQuality", "strong"),
        }
        if mapping.get("statFormula"):
            result["statFormula"] = mapping["statFormula"]
        return result

    return {
        "marketKey": normalized,
        "group": "pitching" if normalized in PITCHING_MARKET_KEYS else "hitting",
        "statKey": None,
        "label": str(market_key or normalized),
        "supported": False,
        "contextQuality": "unsupported",
    }


def stat_value_from_stats(mapping_or_key: Any, stats: dict[str, Any] | None) -> float | None:
    stats = stats or {}
    if isinstance(mapping_or_key, dict):
        formula = str(mapping_or_key.get("statFormula") or "")
        if formula == "singles":
            hits = _float_or_none(stats.get("hits"))
            if hits is None:
                return None
            doubles = _float_or_none(stats.get("doubles")) or 0.0
            triples = _float_or_none(stats.get("triples")) or 0.0
            home_runs = _float_or_none(stats.get("homeRuns")) or 0.0
            return max(0.0, round(hits - doubles - triples - home_runs, 4))
        if formula == "hits_runs_rbis":
            hits = _float_or_none(stats.get("hits"))
            runs = _float_or_none(stats.get("runs"))
            rbi = _float_or_none(stats.get("rbi"))
            if hits is None or runs is None or rbi is None:
                return None
            return round(hits + runs + rbi, 4)
        stat_key = mapping_or_key.get("statKey")
    else:
        stat_key = mapping_or_key

    if not stat_key:
        return None
    return _float_or_none(stats.get(str(stat_key)))


def build_match_audit(enriched_payload: dict[str, Any]) -> dict[str, Any]:
    rows = [_audit_row(prop) for prop in enriched_payload.get("props") or []]
    status_counts: dict[str, int] = {}
    issue_counts: dict[str, int] = {}

    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        for issue in row["issues"]:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1

    return {
        "date": enriched_payload.get("date"),
        "propCount": enriched_payload.get("propCount", len(rows)),
        "matchedPropCount": enriched_payload.get("matchedPropCount", 0),
        "unmatchedPropCount": enriched_payload.get("unmatchedPropCount", 0),
        "statusCounts": status_counts,
        "issueCounts": issue_counts,
        "rows": rows,
    }


async def _enrich_prop(
    prop: dict[str, Any],
    engine: Any,
    season: int | None,
    group_mode: str,
    history_limit: int,
    search_limit: int,
    slate_date: str,
) -> dict[str, Any]:
    row = copy.deepcopy(prop)
    player = row.get("player") or {}
    team = row.get("team") or {}
    player_name = str(player.get("name") or "")
    player_key = slug_key(player.get("key") or player_name)
    team_key = slug_key(team.get("key") or team.get("name"))

    search_payload = await _cached_search_players(
        engine,
        player_name,
        _clean_limit(search_limit),
    )
    candidates = search_payload.get("players") or []
    match = _select_match(player_key, team_key, candidates)
    row["mlbMatch"] = match
    row["mlbGame"] = await _fixture_mlb_game(engine, row, slate_date)
    game_context = await _fixture_game_context(engine, row["mlbGame"])
    row["gameContext"] = _compact_game_context(game_context)

    if match["status"] == "unmatched":
        player["mlbId"] = None
        player["matchStatus"] = "unmatched"
        row["player"] = player
        row["mlbProfile"] = None
        row["recentHistory"] = None
        row["lineupContext"] = None
        row["opponentPitcherContext"] = None
        row["opponentTeamContext"] = None
        row["playerSplits"] = None
        return row

    matched_player = match["matchedPlayer"]
    player_id = int(matched_player["mlbId"])
    market_mapping = stat_mapping_for_market((row.get("market") or {}).get("key") or "")
    stat_group = str(market_mapping["group"] if group_mode == "auto" else group_mode)

    row["mlbProfile"] = await _cached_player_profile(
        engine,
        player_id,
        season,
        stat_group,
    )
    match = _upgrade_match_from_profile(match, row["mlbProfile"], team_key, candidates)
    match = await _upgrade_match_from_roster(
        match,
        engine,
        slate_date,
        season,
        player_key,
        team_key,
        candidates,
    )
    row["mlbMatch"] = match
    matched_player = match["matchedPlayer"]

    player["mlbId"] = player_id
    player["matchStatus"] = match["status"]
    row["player"] = player

    matched_team = (matched_player or {}).get("team") or {}
    if team.get("mlbId") is None and _team_key(matched_team) == team_key:
        team["mlbId"] = matched_team.get("mlbId")
        row["team"] = team

    row["recentHistory"] = await _cached_recent_history(
        engine,
        player_id,
        stat_group,
        season,
        _clean_limit(history_limit),
    )
    row["statContext"] = _stat_context(
        row,
        market_mapping,
        row["mlbProfile"],
        row["recentHistory"],
    )
    row["lineupContext"] = _lineup_context(row, game_context, match)
    row["opponentPitcherContext"] = await _opponent_pitcher_context(
        row,
        game_context,
        engine,
        season,
        market_mapping,
    )
    row["opponentTeamContext"] = await _opponent_team_context(
        row,
        game_context,
        engine,
        slate_date,
        season,
        market_mapping,
    )
    row["playerSplits"] = await _player_split_context(
        row,
        engine,
        player_id,
        stat_group,
        season,
    )
    return row


async def _upgrade_match_from_roster(
    match: dict[str, Any],
    engine: Any,
    slate_date: str,
    season: int | None,
    player_key: str,
    team_key: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if match.get("status") != "matched_exact_name" or not slate_date:
        return match

    schedule = await _cached_schedule(engine, slate_date)
    team = _schedule_team(schedule, team_key)
    team_id = team.get("mlbId") if team else None
    if team_id is None:
        return match

    roster = await _cached_team_roster(
        engine,
        int(team_id),
        season if season is not None else _season_from_date(slate_date),
    )
    roster_player = _roster_player(roster, match, player_key)
    if not roster_player:
        return match

    confirmed_player = copy.deepcopy(match["matchedPlayer"])
    confirmed_player["team"] = {
        "mlbId": team.get("mlbId"),
        "name": team.get("name"),
        "key": _team_key(team),
    }
    if roster_player.get("position") and not confirmed_player.get("position"):
        confirmed_player["position"] = roster_player.get("position")

    return _match_payload(
        "matched_exact_name_team",
        1.0,
        candidates,
        confirmed_player,
    )


def _upgrade_match_from_profile(
    match: dict[str, Any],
    profile: dict[str, Any],
    team_key: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if match.get("status") != "matched_exact_name":
        return match

    matched_player = match.get("matchedPlayer")
    if not isinstance(matched_player, dict):
        return match

    profile_player = (profile or {}).get("player") or {}
    profile_team = profile_player.get("team") or {}
    if _team_key(profile_team) != team_key:
        return match

    confirmed_player = copy.deepcopy(matched_player)
    confirmed_player["team"] = copy.deepcopy(profile_team)
    return _match_payload(
        "matched_exact_name_team",
        1.0,
        candidates,
        confirmed_player,
    )


def _select_match(
    player_key: str,
    team_key: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    exact_name = [
        candidate
        for candidate in candidates
        if slug_key(candidate.get("key") or candidate.get("name")) == player_key
    ]
    exact_team = [
        candidate
        for candidate in exact_name
        if _team_key(candidate.get("team") or {}) == team_key
    ]

    if exact_team:
        return _match_payload(
            "matched_exact_name_team",
            1.0,
            candidates,
            exact_team[0],
        )
    if exact_name:
        return _match_payload(
            "matched_exact_name",
            0.85,
            candidates,
            exact_name[0],
        )

    return {
        "status": "unmatched",
        "confidence": 0.0,
        "candidateCount": len(candidates),
        "matchedPlayer": None,
    }


def _match_payload(
    status: str,
    confidence: float,
    candidates: list[dict[str, Any]],
    matched_player: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": status,
        "confidence": confidence,
        "candidateCount": len(candidates),
        "matchedPlayer": matched_player,
    }


def _team_key(team: dict[str, Any]) -> str:
    return slug_key(team.get("key") or team.get("name"))


async def _fixture_mlb_game(
    engine: Any,
    prop: dict[str, Any],
    slate_date: str,
) -> dict[str, Any] | None:
    if not slate_date:
        return None

    schedule = await _cached_schedule(engine, slate_date)
    fixture_keys = _fixture_team_keys(str(prop.get("game") or ""))
    candidates = []
    for game in schedule.get("games") or []:
        game_keys = {
            _team_key(game.get("awayTeam") or {}),
            _team_key(game.get("homeTeam") or {}),
        }
        if fixture_keys:
            if game_keys == fixture_keys:
                candidates.append(game)
            continue

        team_key = _team_key(prop.get("team") or {})
        if team_key and team_key in game_keys:
            candidates.append(game)

    if not candidates:
        return None
    if len(candidates) == 1:
        return _mlb_game_payload(candidates[0])

    closest = _closest_game_by_start_time(candidates, prop.get("startTime"))
    return _mlb_game_payload(closest) if closest else None


def _fixture_team_keys(game_name: str) -> set[str]:
    if " - " not in game_name:
        return set()
    keys = {
        slug_key(part)
        for part in game_name.split(" - ", 1)
        if part.strip()
    }
    return keys if len(keys) == 2 else set()


def _closest_game_by_start_time(
    games: list[dict[str, Any]],
    stake_start_time: Any,
) -> dict[str, Any] | None:
    start = _timestamp_ms(stake_start_time)
    if start is None:
        return None

    dated_games = [
        (abs(game_start - start), game)
        for game in games
        for game_start in [_timestamp_text(game.get("gameDate"))]
        if game_start is not None
    ]
    if not dated_games:
        return None
    dated_games.sort(key=lambda item: item[0])
    return dated_games[0][1]


def _mlb_game_payload(game: dict[str, Any]) -> dict[str, Any]:
    return {
        "gamePk": game.get("gamePk"),
        "gameDate": game.get("gameDate"),
        "status": game.get("status"),
        "awayTeam": _game_team_payload(game.get("awayTeam") or {}),
        "homeTeam": _game_team_payload(game.get("homeTeam") or {}),
    }


def _game_team_payload(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "mlbId": team.get("mlbId"),
        "name": team.get("name"),
        "key": _team_key(team),
        "probablePitcher": team.get("probablePitcher"),
    }


async def _fixture_game_context(
    engine: Any,
    game: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not game or not hasattr(engine, "get_game_context"):
        return None
    game_pk = game.get("gamePk")
    if game_pk is None:
        return None
    try:
        return await _cached_game_context(engine, int(game_pk))
    except Exception as exc:
        return {"status": "unavailable", "warnings": ["game_context_unavailable"], "message": str(exc)}


def _compact_game_context(game_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not game_context:
        return None
    if game_context.get("status") == "unavailable":
        return game_context
    teams = game_context.get("teams") or {}
    return {
        "gamePk": game_context.get("gamePk"),
        "gameDate": game_context.get("gameDate"),
        "officialDate": game_context.get("officialDate"),
        "status": game_context.get("status"),
        "statusRiskFlags": game_context.get("statusRiskFlags") or [],
        "gameInfo": game_context.get("gameInfo") or {},
        "venue": game_context.get("venue"),
        "weather": game_context.get("weather"),
        "lineups": {
            side: {
                "team": ((team_context or {}).get("team") or {}).get("name"),
                "lineupConfirmed": bool((team_context or {}).get("lineupConfirmed")),
                "battingOrder": (team_context or {}).get("battingOrder") or [],
            }
            for side, team_context in teams.items()
        },
    }


def _lineup_context(
    prop: dict[str, Any],
    game_context: dict[str, Any] | None,
    match: dict[str, Any],
) -> dict[str, Any] | None:
    if not game_context or game_context.get("status") == "unavailable":
        return None
    side = _prop_team_side(prop, game_context)
    if not side:
        return {"status": "team_side_unknown", "lineupConfirmed": False}

    team_context = ((game_context.get("teams") or {}).get(side)) or {}
    lineup_confirmed = bool(team_context.get("lineupConfirmed"))
    matched_player = match.get("matchedPlayer") if isinstance(match, dict) else None
    player_id = (matched_player or {}).get("mlbId")
    players_by_id = team_context.get("playersById") or {}
    player = players_by_id.get(str(player_id)) if player_id is not None else None
    opponent_pitcher = _probable_pitcher_for_side(prop, side)
    opponent_hand = None
    if isinstance(opponent_pitcher, dict):
        opponent_hand = opponent_pitcher.get("pitchHand")

    if not lineup_confirmed:
        status = "lineup_unconfirmed"
    elif player and player.get("confirmedStarter"):
        status = "confirmed_starter"
    else:
        status = "not_in_confirmed_lineup"

    return {
        "status": status,
        "teamSide": side,
        "lineupConfirmed": lineup_confirmed,
        "confirmedStarter": bool(player and player.get("confirmedStarter")),
        "battingOrder": (player or {}).get("battingOrder"),
        "defensivePosition": (player or {}).get("position"),
        "batSide": (player or {}).get("batSide"),
        "opponentPitcherHand": opponent_hand,
        "platoonSide": _platoon_side((player or {}).get("batSide"), opponent_hand),
    }


async def _opponent_pitcher_context(
    prop: dict[str, Any],
    game_context: dict[str, Any] | None,
    engine: Any,
    season: int | None,
    market_mapping: dict[str, Any],
) -> dict[str, Any] | None:
    if market_mapping.get("group") != "hitting":
        return None
    side = _prop_team_side(prop, game_context) if game_context else None
    pitcher = _probable_pitcher_for_side(prop, side)
    pitcher_id = (pitcher or {}).get("mlbId")
    if pitcher_id is None:
        return {
            "status": "probable_pitcher_unavailable",
            "pitcher": pitcher,
            "riskFlags": ["probable_pitcher_unavailable"],
        }

    try:
        profile = await _cached_player_profile(engine, int(pitcher_id), season, "pitching")
        recent = await _cached_recent_history(engine, int(pitcher_id), "pitching", season, 5)
    except Exception as exc:
        return {
            "status": "pitcher_context_unavailable",
            "pitcher": pitcher,
            "riskFlags": ["pitcher_context_unavailable"],
            "message": str(exc),
        }

    stats = (((profile or {}).get("player") or {}).get("stats") or {})
    player = (profile or {}).get("player") or {}
    role = _pitcher_role_sanity(profile, recent)
    risk_flags = list(role.get("flags") or [])
    if not stats:
        risk_flags.append("opponent_pitcher_season_stats_missing")
    if (recent or {}).get("gamesUsed", 0) < 3:
        risk_flags.append("opponent_pitcher_recent_sample_thin")
    return {
        "status": "available",
        "pitcher": {
            **(pitcher or {}),
            "pitchHand": player.get("pitchHand"),
        },
        "season": _pitching_stat_summary(stats),
        "recent": _recent_pitching_summary(recent),
        "roleSanity": role,
        "riskFlags": sorted(set(risk_flags)),
    }


async def _opponent_team_context(
    prop: dict[str, Any],
    game_context: dict[str, Any] | None,
    engine: Any,
    slate_date: str,
    season: int | None,
    market_mapping: dict[str, Any],
) -> dict[str, Any] | None:
    if market_mapping.get("group") != "pitching":
        return None
    game = prop.get("mlbGame") or {}
    side = _prop_team_side(prop, game_context) if game_context else _prop_team_side_from_game(prop, game)
    opponent_side = _opponent_side(side)
    opponent_team = _team_for_side(game, opponent_side)
    opponent_team_id = (opponent_team or {}).get("mlbId")
    if opponent_team_id is None:
        return {
            "status": "opponent_team_unavailable",
            "riskFlags": ["opponent_team_unavailable"],
        }

    risk_flags = []
    season_context = None
    try:
        if hasattr(engine, "get_team_profile"):
            season_context = await _cached_team_profile(
                engine,
                int(opponent_team_id),
                season,
                "hitting",
            )
    except Exception:
        risk_flags.append("opponent_team_season_context_unavailable")

    recent_form = None
    try:
        recent_form = await _recent_team_form(
            engine,
            int(opponent_team_id),
            slate_date,
        )
    except Exception:
        risk_flags.append("opponent_team_recent_form_unavailable")

    lineup = None
    if game_context and opponent_side:
        team_context = ((game_context.get("teams") or {}).get(opponent_side)) or {}
        lineup = _lineup_handedness_summary(team_context.get("lineup") or [])
        if not team_context.get("lineupConfirmed"):
            risk_flags.append("opponent_lineup_unconfirmed")

    return {
        "status": "available",
        "team": opponent_team,
        "seasonHitting": _team_hitting_summary((season_context or {}).get("stats") or {}),
        "recentForm": recent_form,
        "projectedLineup": lineup,
        "riskFlags": sorted(set(risk_flags)),
    }


async def _player_split_context(
    prop: dict[str, Any],
    engine: Any,
    player_id: int,
    stat_group: str,
    season: int | None,
) -> dict[str, Any] | None:
    recent = prop.get("recentHistory") or {}
    result = {
        "source": "mlb_stats_api",
        "recentHomeAway": _recent_home_away_splits(recent),
        "seasonSplits": None,
        "riskFlags": [],
    }
    if not hasattr(engine, "get_player_splits"):
        result["riskFlags"].append("season_stat_splits_unavailable")
        return result
    try:
        splits = await _cached_player_splits(
            engine,
            player_id,
            stat_group,
            season,
            "h,a,vr,vl",
        )
        result["seasonSplits"] = _compact_player_splits(splits)
    except Exception:
        result["riskFlags"].append("season_stat_splits_unavailable")
    return result


def _prop_team_side(prop: dict[str, Any], game_context: dict[str, Any] | None) -> str | None:
    if not game_context:
        return None
    team_key = _team_key(prop.get("team") or {})
    for side, team_context in (game_context.get("teams") or {}).items():
        team = (team_context or {}).get("team") or {}
        if team_key and _team_key(team) == team_key:
            return side
    return None


def _prop_team_side_from_game(prop: dict[str, Any], game: dict[str, Any]) -> str | None:
    team_key = _team_key(prop.get("team") or {})
    for side in ("away", "home"):
        team = _team_for_side(game, side)
        if team_key and _team_key(team or {}) == team_key:
            return side
    return None


def _opponent_side(side: str | None) -> str | None:
    if side == "away":
        return "home"
    if side == "home":
        return "away"
    return None


def _team_for_side(game: dict[str, Any], side: str | None) -> dict[str, Any] | None:
    if side == "away":
        return game.get("awayTeam") or {}
    if side == "home":
        return game.get("homeTeam") or {}
    return None


def _probable_pitcher_for_side(prop: dict[str, Any], side: str | None) -> dict[str, Any] | None:
    opponent = _opponent_side(side)
    team = _team_for_side(prop.get("mlbGame") or {}, opponent)
    pitcher = (team or {}).get("probablePitcher")
    return pitcher if isinstance(pitcher, dict) else None


def _platoon_side(bat_side: Any, pitch_hand: Any) -> str | None:
    bat = str(bat_side or "").upper()
    hand = str(pitch_hand or "").upper()
    if not bat or not hand:
        return None
    if bat == "S":
        return "switch"
    if bat == hand:
        return "same_hand"
    return "opposite_hand"


def _pitching_stat_summary(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "gamesPitched": stats.get("gamesPitched") or stats.get("gamesPlayed"),
        "gamesStarted": stats.get("gamesStarted"),
        "inningsPitched": stats.get("inningsPitched"),
        "hitsAllowed": stats.get("hits"),
        "walksAllowed": stats.get("baseOnBalls"),
        "strikeOuts": stats.get("strikeOuts"),
        "homeRunsAllowed": stats.get("homeRuns"),
        "era": stats.get("era"),
        "whip": stats.get("whip"),
    }


def _recent_pitching_summary(recent: dict[str, Any] | None) -> dict[str, Any]:
    recent = recent or {}
    return {
        "gamesUsed": recent.get("gamesUsed"),
        "totals": recent.get("totals") or {},
        "perGame": recent.get("perGame") or {},
    }


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
    if (_int_or_none((recent or {}).get("gamesUsed")) or 0) == 0:
        flags.append("probable_pitcher_no_recent_logs")
    return {
        "volumePropRisk": "high" if "probable_pitcher_low_start_share" in flags else "medium" if flags else "low",
        "flags": flags,
        "gamesStarted": games_started,
        "gamesPlayed": games_played,
        "startShare": start_share,
    }


def _team_hitting_summary(stats: dict[str, Any]) -> dict[str, Any]:
    games = _float_or_none(stats.get("gamesPlayed"))
    strikeouts = _float_or_none(stats.get("strikeOuts"))
    walks = _float_or_none(stats.get("baseOnBalls"))
    plate_appearances = _float_or_none(stats.get("plateAppearances"))
    return {
        "gamesPlayed": stats.get("gamesPlayed"),
        "runs": stats.get("runs"),
        "hits": stats.get("hits"),
        "homeRuns": stats.get("homeRuns"),
        "walks": stats.get("baseOnBalls"),
        "strikeOuts": stats.get("strikeOuts"),
        "avg": stats.get("avg"),
        "obp": stats.get("obp"),
        "slg": stats.get("slg"),
        "ops": stats.get("ops"),
        "runsPerGame": round((_float_or_none(stats.get("runs")) or 0.0) / games, 4) if games else None,
        "hitsPerGame": round((_float_or_none(stats.get("hits")) or 0.0) / games, 4) if games else None,
        "strikeoutRate": round(strikeouts / plate_appearances, 4) if strikeouts is not None and plate_appearances else None,
        "walkRate": round(walks / plate_appearances, 4) if walks is not None and plate_appearances else None,
    }


async def _recent_team_form(
    engine: Any,
    team_id: int,
    slate_date: str,
) -> dict[str, Any] | None:
    if not slate_date or not hasattr(engine, "get_schedule_range"):
        return None
    end_date = _date_from_text(slate_date)
    if end_date is None:
        return None
    start_date = end_date - timedelta(days=60)
    schedule = await _cached_schedule_range(
        engine,
        start_date.date().isoformat(),
        (end_date - timedelta(days=1)).date().isoformat(),
    )
    results = []
    for game in schedule.get("games") or []:
        result = _team_result_for_game(game, team_id)
        if result:
            results.append(result)
    results.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    return {
        "last10": _team_window_summary(results, 10),
        "last15": _team_window_summary(results, 15),
    }


def _team_result_for_game(game: dict[str, Any], team_id: int) -> dict[str, Any] | None:
    status = str(game.get("status") or "").lower()
    if not status.startswith("final"):
        return None
    away = game.get("awayTeam") or {}
    home = game.get("homeTeam") or {}
    if away.get("mlbId") == team_id:
        team, opponent, is_home = away, home, False
    elif home.get("mlbId") == team_id:
        team, opponent, is_home = home, away, True
    else:
        return None
    scored = _int_or_none(team.get("score"))
    allowed = _int_or_none(opponent.get("score"))
    if scored is None or allowed is None:
        return None
    return {
        "gamePk": game.get("gamePk"),
        "date": str(game.get("gameDate") or "")[:10],
        "opponent": opponent.get("name"),
        "isHome": is_home,
        "won": scored > allowed,
        "runsScored": scored,
        "runsAllowed": allowed,
    }


def _team_window_summary(results: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    selected = results[:limit]
    runs_scored = sum(int(row.get("runsScored") or 0) for row in selected)
    runs_allowed = sum(int(row.get("runsAllowed") or 0) for row in selected)
    wins = sum(1 for row in selected if row.get("won"))
    return {
        "gamesUsed": len(selected),
        "wins": wins,
        "losses": len(selected) - wins,
        "runsScored": runs_scored,
        "runsAllowed": runs_allowed,
        "runDifferential": runs_scored - runs_allowed,
    }


def _lineup_handedness_summary(lineup: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not lineup:
        return None
    counts: dict[str, int] = {}
    for player in lineup:
        side = str(player.get("batSide") or "unknown").upper()
        counts[side] = counts.get(side, 0) + 1
    return {
        "confirmedBatters": len(lineup),
        "batSideCounts": counts,
        "battingOrder": [
            {
                "mlbId": player.get("mlbId"),
                "name": player.get("name"),
                "battingOrder": player.get("battingOrder"),
                "batSide": player.get("batSide"),
                "position": player.get("position"),
            }
            for player in lineup
        ],
    }


def _recent_home_away_splits(recent: dict[str, Any]) -> dict[str, Any]:
    games = recent.get("games") or []
    return {
        "home": _recent_split_summary([game for game in games if game.get("isHome") is True]),
        "away": _recent_split_summary([game for game in games if game.get("isHome") is False]),
    }


def _recent_split_summary(games: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, float] = {}
    for game in games:
        for key, value in (game.get("stats") or {}).items():
            numeric = _float_or_none(value)
            if numeric is not None:
                totals[key] = round(totals.get(key, 0.0) + numeric, 4)
    per_game = {
        key: round(value / len(games), 4)
        for key, value in totals.items()
    } if games else {}
    return {"gamesUsed": len(games), "totals": totals, "perGame": per_game}


def _compact_player_splits(splits: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for split in (splits or {}).get("splits") or []:
        stat = split.get("stat") or {}
        rows.append(
            {
                "split": split.get("split"),
                "type": split.get("type"),
                "stats": {
                    key: stat.get(key)
                    for key in (
                        "gamesPlayed",
                        "avg",
                        "obp",
                        "slg",
                        "ops",
                        "hits",
                        "homeRuns",
                        "rbi",
                        "runs",
                        "strikeOuts",
                        "baseOnBalls",
                        "totalBases",
                    )
                    if key in stat
                },
            }
        )
    return rows


def _date_from_text(value: str) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    if len(text) == 10:
        text = f"{text}T00:00:00+00:00"
    elif text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _timestamp_ms(value: Any) -> float | None:
    try:
        return float(value) / 1000
    except (TypeError, ValueError):
        return None


def _timestamp_text(value: Any) -> float | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


async def _cached_schedule(engine: Any, game_date: str) -> dict[str, Any]:
    return await _cached_call(
        ("schedule", _engine_cache_key(engine), game_date),
        lambda: engine.get_schedule(game_date),
    )


async def _cached_team_roster(
    engine: Any,
    team_id: int,
    season: int | None,
) -> dict[str, Any]:
    return await _cached_call(
        ("roster", _engine_cache_key(engine), team_id, season),
        lambda: engine.get_team_roster(team_id, season=season),
    )


async def _cached_search_players(
    engine: Any,
    query: str,
    limit: int,
) -> dict[str, Any]:
    return await _cached_call(
        ("search", _engine_cache_key(engine), query.lower().strip(), limit),
        lambda: engine.search_players(query, limit=limit),
    )


async def _cached_player_profile(
    engine: Any,
    player_id: int,
    season: int | None,
    group: str,
) -> dict[str, Any]:
    return await _cached_call(
        ("profile", _engine_cache_key(engine), player_id, season, group),
        lambda: engine.get_player_profile(player_id, season=season, group=group),
    )


async def _cached_recent_history(
    engine: Any,
    player_id: int,
    group: str,
    season: int | None,
    limit: int,
) -> dict[str, Any]:
    return await _cached_call(
        ("history", _engine_cache_key(engine), player_id, group, season, limit),
        lambda: engine.get_player_recent_history(
            player_id,
            group=group,
            season=season,
            limit=limit,
        ),
    )


async def _cached_player_splits(
    engine: Any,
    player_id: int,
    group: str,
    season: int | None,
    sit_codes: str | None,
) -> dict[str, Any]:
    return await _cached_call(
        ("splits", _engine_cache_key(engine), player_id, group, season, sit_codes),
        lambda: engine.get_player_splits(
            player_id,
            group=group,
            season=season,
            sit_codes=sit_codes,
        ),
    )


async def _cached_game_context(engine: Any, game_pk: int) -> dict[str, Any]:
    return await _cached_call(
        ("game-context", _engine_cache_key(engine), game_pk),
        lambda: engine.get_game_context(game_pk),
    )


async def _cached_team_profile(
    engine: Any,
    team_id: int,
    season: int | None,
    group: str,
) -> dict[str, Any]:
    return await _cached_call(
        ("team-profile", _engine_cache_key(engine), team_id, season, group),
        lambda: engine.get_team_profile(team_id, season=season, group=group),
    )


async def _cached_schedule_range(
    engine: Any,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    return await _cached_call(
        ("schedule-range", _engine_cache_key(engine), start_date, end_date),
        lambda: engine.get_schedule_range(start_date, end_date),
    )


async def _cached_call(cache_key: tuple[Any, ...], callback: Any) -> Any:
    cached = _LOOKUP_CACHE.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return copy.deepcopy(cached[1])

    payload = await callback()
    _LOOKUP_CACHE[cache_key] = (
        time.monotonic() + BRIDGE_CACHE_TTL_SECONDS,
        copy.deepcopy(payload),
    )
    return payload


def _engine_cache_key(engine: Any) -> Any:
    namespace = getattr(engine, "cache_namespace", None)
    if namespace:
        return namespace
    engine_type = type(engine)
    if engine_type.__module__.startswith("app.mlb_data"):
        return "mlb-stats-api"
    try:
        hash(engine)
    except TypeError:
        return (engine_type.__module__, engine_type.__qualname__, id(engine))
    return engine


def _schedule_team(schedule: dict[str, Any], team_key: str) -> dict[str, Any] | None:
    for game in schedule.get("games") or []:
        for side in ("awayTeam", "homeTeam"):
            team = game.get(side) or {}
            if _team_key(team) == team_key:
                return team
    return None


def _roster_player(
    roster: dict[str, Any],
    match: dict[str, Any],
    player_key: str,
) -> dict[str, Any] | None:
    matched_player = match.get("matchedPlayer")
    matched_id = matched_player.get("mlbId") if isinstance(matched_player, dict) else None
    for player in roster.get("players") or []:
        if matched_id is not None and player.get("mlbId") == matched_id:
            return player
        if slug_key(player.get("key") or player.get("name")) == player_key:
            return player
    return None


def _season_from_date(value: str) -> int | None:
    try:
        return int(value[:4])
    except (TypeError, ValueError):
        return None


def _stat_context(
    prop: dict[str, Any],
    market_mapping: dict[str, Any],
    profile: dict[str, Any],
    history: dict[str, Any],
) -> dict[str, Any]:
    stat_key = market_mapping.get("statKey")
    stat_formula = market_mapping.get("statFormula")
    season_stats = ((profile or {}).get("player") or {}).get("stats") or {}
    totals = (history or {}).get("totals") or {}
    per_game = (history or {}).get("perGame") or {}
    stat_value = stat_value_from_stats(market_mapping, season_stats)
    recent_total = stat_value_from_stats(market_mapping, totals)
    recent_per_game = stat_value_from_stats(market_mapping, per_game)

    context = {
        "marketKey": market_mapping["marketKey"],
        "group": market_mapping["group"],
        "statKey": stat_key,
        "label": market_mapping["label"],
        "supported": market_mapping["supported"],
        "contextQuality": market_mapping["contextQuality"],
        "line": prop.get("line"),
        "seasonValue": stat_value,
        "recentTotal": recent_total,
        "recentPerGame": recent_per_game,
        "gamesUsed": history.get("gamesUsed") if history else None,
    }
    if stat_formula:
        context["statFormula"] = stat_formula
    return context


def _audit_row(prop: dict[str, Any]) -> dict[str, Any]:
    match = prop.get("mlbMatch") or {}
    player = prop.get("player") or {}
    team = prop.get("team") or {}
    market = prop.get("market") or {}
    matched_player = match.get("matchedPlayer")
    issues = _audit_issues(match)

    return {
        "propId": prop.get("propId"),
        "player": player.get("name"),
        "team": team.get("name"),
        "market": market.get("name"),
        "status": match.get("status", "unmatched"),
        "confidence": match.get("confidence", 0.0),
        "candidateCount": match.get("candidateCount", 0),
        "matchedPlayer": matched_player.get("name") if isinstance(matched_player, dict) else None,
        "issues": issues,
    }


def _audit_issues(match: dict[str, Any]) -> list[str]:
    issues = []
    status = match.get("status", "unmatched")
    confidence = float(match.get("confidence") or 0.0)
    candidate_count = int(match.get("candidateCount") or 0)

    if status == "unmatched":
        issues.append("unmatched")
    if candidate_count > 1:
        issues.append("multiple_candidates")
    if status == "matched_exact_name":
        issues.append("team_not_confirmed")
    if confidence < 1.0:
        issues.append("low_confidence")

    return issues


def _clean_limit(limit: int) -> int:
    return max(1, min(limit, 100))


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
