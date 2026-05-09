from __future__ import annotations

import copy
import time
from datetime import datetime, timezone
from typing import Any

from .mlb_props import slug_key


BRIDGE_CACHE_TTL_SECONDS = 60.0
_LOOKUP_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}

MARKET_STAT_MAP = {
    "hits": {"group": "hitting", "statKey": "hits", "label": "Hits"},
    "hit": {"group": "hitting", "statKey": "hits", "label": "Hits"},
    "total-bases": {
        "group": "hitting",
        "statKey": "totalBases",
        "label": "Total Bases",
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
        return {
            "marketKey": normalized,
            "group": mapping["group"],
            "statKey": mapping["statKey"],
            "label": mapping["label"],
            "supported": True,
        }

    return {
        "marketKey": normalized,
        "group": "pitching" if normalized in PITCHING_MARKET_KEYS else "hitting",
        "statKey": None,
        "label": str(market_key or normalized),
        "supported": False,
    }


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

    if match["status"] == "unmatched":
        player["mlbId"] = None
        player["matchStatus"] = "unmatched"
        row["player"] = player
        row["mlbProfile"] = None
        row["recentHistory"] = None
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
        ("schedule", id(engine), game_date),
        lambda: engine.get_schedule(game_date),
    )


async def _cached_team_roster(
    engine: Any,
    team_id: int,
    season: int | None,
) -> dict[str, Any]:
    return await _cached_call(
        ("roster", id(engine), team_id, season),
        lambda: engine.get_team_roster(team_id, season=season),
    )


async def _cached_search_players(
    engine: Any,
    query: str,
    limit: int,
) -> dict[str, Any]:
    return await _cached_call(
        ("search", id(engine), query.lower().strip(), limit),
        lambda: engine.search_players(query, limit=limit),
    )


async def _cached_player_profile(
    engine: Any,
    player_id: int,
    season: int | None,
    group: str,
) -> dict[str, Any]:
    return await _cached_call(
        ("profile", id(engine), player_id, season, group),
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
        ("history", id(engine), player_id, group, season, limit),
        lambda: engine.get_player_recent_history(
            player_id,
            group=group,
            season=season,
            limit=limit,
        ),
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
    season_stats = ((profile or {}).get("player") or {}).get("stats") or {}
    totals = (history or {}).get("totals") or {}
    per_game = (history or {}).get("perGame") or {}

    return {
        "marketKey": market_mapping["marketKey"],
        "group": market_mapping["group"],
        "statKey": stat_key,
        "label": market_mapping["label"],
        "supported": market_mapping["supported"],
        "line": prop.get("line"),
        "seasonValue": season_stats.get(stat_key) if stat_key else None,
        "recentTotal": totals.get(stat_key) if stat_key else None,
        "recentPerGame": per_game.get(stat_key) if stat_key else None,
        "gamesUsed": history.get("gamesUsed") if history else None,
    }


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
