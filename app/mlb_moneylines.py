from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .mlb_props import slug_key


MONEYLINE_MARKET_KEY = "winner_including_extra_innings"
RECENT_LOOKBACK_DAYS = 60


async def enrich_stake_ui_moneylines(
    raw_board: dict[str, Any],
    mlb_engine: Any,
    *,
    slate_date: date,
    fixture_slugs: list[str] | None = None,
    matchups: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    games = _filter_raw_games(
        raw_board.get("games") or [],
        fixture_slugs=fixture_slugs,
        matchups=matchups,
        limit=limit,
    )
    warnings = list(raw_board.get("warnings") or [])
    if not games:
        return _response(raw_board, games=[], warnings=warnings)

    season = slate_date.year
    teams = await mlb_engine.get_teams(season=season)
    schedule = await mlb_engine.get_schedule(slate_date.isoformat())
    recent = await mlb_engine.get_schedule_range(
        (slate_date - timedelta(days=RECENT_LOOKBACK_DAYS)).isoformat(),
        (slate_date - timedelta(days=1)).isoformat(),
    )
    standings = await mlb_engine.get_standings(season=season)

    team_index = _team_index(teams.get("teams") or [])
    schedule_index = _schedule_index(schedule.get("games") or [])
    recent_by_team = _recent_results_by_team(recent.get("games") or [])
    standings_by_id = standings.get("teamsById") or {}

    enriched_games = []
    for game in games:
        enriched = dict(game)
        selections = []
        game_warnings = list(game.get("warnings") or [])
        target_game = _target_schedule_game(game, team_index, schedule_index)
        for raw_selection in game.get("selections") or []:
            selection = dict(raw_selection)
            team = _find_team(raw_selection.get("team"), team_index)
            context, selection_warnings = _context_for_selection(
                team=team,
                target_game=target_game,
                recent_by_team=recent_by_team,
                standings_by_id=standings_by_id,
            )
            selection["teamContext"] = context
            selection["warnings"] = selection_warnings
            game_warnings.extend(selection_warnings)
            selections.append(selection)
        enriched["selections"] = selections
        enriched["warnings"] = _dedupe(game_warnings)
        enriched_games.append(enriched)

    return _response(raw_board, games=enriched_games, warnings=warnings)


def _response(
    raw_board: dict[str, Any],
    *,
    games: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "source": "stake_ui_mlb_moneylines",
        "decisionOwner": "custom_gpt",
        "builderRole": "read_only_moneyline_research_not_final_recommendation",
        "market": MONEYLINE_MARKET_KEY,
        "pregameOnly": True,
        "capturedAt": raw_board.get("capturedAt"),
        "url": raw_board.get("url"),
        "returnedGames": len(games),
        "games": games,
        "warnings": _dedupe(warnings),
    }


def _filter_raw_games(
    games: list[dict[str, Any]],
    *,
    fixture_slugs: list[str] | None,
    matchups: list[str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    slug_filter = {
        str(fixture_slug or "").strip()
        for fixture_slug in fixture_slugs or []
        if str(fixture_slug or "").strip()
    }
    matchup_filter = [
        str(matchup or "").strip()
        for matchup in matchups or []
        if str(matchup or "").strip()
    ]
    result = []
    for game in games:
        fixture_slug = str(game.get("fixtureSlug") or "").strip()
        if slug_filter and fixture_slug not in slug_filter:
            continue
        if matchup_filter and not any(
            _same_matchup(matchup, game.get("matchup") or "")
            for matchup in matchup_filter
        ):
            continue
        result.append(dict(game))
        if len(result) >= max(1, int(limit or 1)):
            break
    return result


def _team_index(teams: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(team.get("key") or slug_key(team.get("name"))): team
        for team in teams
        if team.get("mlbId") is not None and (team.get("key") or team.get("name"))
    }


def _schedule_index(games: list[dict[str, Any]]) -> dict[frozenset[int], dict[str, Any]]:
    result = {}
    for game in games:
        team_ids = _game_team_ids(game)
        if len(team_ids) == 2:
            result[frozenset(team_ids)] = game
    return result


def _recent_results_by_team(
    games: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for game in games:
        if not _is_completed_game(game):
            continue
        for side, opponent_side in (("awayTeam", "homeTeam"), ("homeTeam", "awayTeam")):
            team_result = _team_result_for_game(
                game,
                side=side,
                opponent_side=opponent_side,
            )
            if not team_result:
                continue
            result.setdefault(team_result["mlbTeamId"], []).append(team_result)
    for rows in result.values():
        rows.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    return result


def _is_completed_game(game: dict[str, Any]) -> bool:
    status = str(game.get("status") or "").strip().lower()
    return status == "final" or status.startswith("final:")


def _team_result_for_game(
    game: dict[str, Any],
    *,
    side: str,
    opponent_side: str,
) -> dict[str, Any] | None:
    team = game.get(side) or {}
    opponent = game.get(opponent_side) or {}
    team_id = _int_or_none(team.get("mlbId"))
    runs_scored = _int_or_none(team.get("score"))
    runs_allowed = _int_or_none(opponent.get("score"))
    if team_id is None or runs_scored is None or runs_allowed is None:
        return None
    return {
        "gamePk": game.get("gamePk"),
        "date": str(game.get("gameDate") or "")[:10],
        "mlbTeamId": team_id,
        "opponent": opponent.get("name"),
        "isHome": side == "homeTeam",
        "won": bool(team.get("isWinner"))
        if team.get("isWinner") is not None
        else runs_scored > runs_allowed,
        "runsScored": runs_scored,
        "runsAllowed": runs_allowed,
    }


def _window_summary(
    results: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    selected = results[: max(1, int(limit or 1))]
    wins = sum(1 for result in selected if result.get("won") is True)
    runs_scored = sum(int(result.get("runsScored") or 0) for result in selected)
    runs_allowed = sum(int(result.get("runsAllowed") or 0) for result in selected)
    return {
        "gamesUsed": len(selected),
        "wins": wins,
        "losses": len(selected) - wins,
        "runsScored": runs_scored,
        "runsAllowed": runs_allowed,
        "runDifferential": runs_scored - runs_allowed,
        "results": selected,
    }


def _context_for_selection(
    *,
    team: dict[str, Any] | None,
    target_game: dict[str, Any] | None,
    recent_by_team: dict[int, list[dict[str, Any]]],
    standings_by_id: dict[Any, dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    if not team:
        return None, ["team_identity_unmatched"]

    team_id = team["mlbId"]
    results = recent_by_team.get(team_id) or []
    last5 = _window_summary(results, 5)
    last10 = _window_summary(results, 10)
    last15 = _window_summary(results, 15)
    warnings = []
    if len(results) < 15:
        warnings.append("partial_recent_sample")

    standing = standings_by_id.get(team_id) or standings_by_id.get(str(team_id)) or {}
    if not standing:
        warnings.append("season_record_unavailable")

    side, opponent = _team_side_and_opponent(target_game, team_id)
    if side is None:
        warnings.append("scheduled_matchup_unavailable")
    probable_pitcher = (side or {}).get("probablePitcher")
    if not probable_pitcher:
        warnings.append("probable_pitcher_unavailable")

    home_away_split = None
    if side is not None:
        is_home = side is (target_game or {}).get("homeTeam")
        split_results = [
            result
            for result in results
            if result.get("isHome") == is_home
        ]
        home_away_split = {
            "scope": "recent_completed_games",
            "role": "home" if is_home else "away",
            **_window_summary(split_results, 15),
        }
    return {
        "mlbTeamId": team_id,
        "team": {
            "name": team.get("name"),
            "key": team.get("key"),
        },
        "seasonRecord": {
            "wins": standing.get("wins"),
            "losses": standing.get("losses"),
            "pct": standing.get("pct"),
        }
        if standing
        else None,
        "last5": last5,
        "last10": last10,
        "last15": last15,
        "runsScored": {
            "last5": last5["runsScored"],
            "last10": last10["runsScored"],
            "last15": last15["runsScored"],
        },
        "runsAllowed": {
            "last5": last5["runsAllowed"],
            "last10": last10["runsAllowed"],
            "last15": last15["runsAllowed"],
        },
        "homeAwaySplit": home_away_split,
        "opponent": {
            "mlbTeamId": opponent.get("mlbId"),
            "name": opponent.get("name"),
            "key": opponent.get("key"),
        }
        if opponent
        else None,
        "probablePitcher": probable_pitcher,
    }, warnings


def _target_schedule_game(
    game: dict[str, Any],
    team_index: dict[str, dict[str, Any]],
    schedule_index: dict[frozenset[int], dict[str, Any]],
) -> dict[str, Any] | None:
    team_ids = {
        team["mlbId"]
        for selection in game.get("selections") or []
        if (team := _find_team(selection.get("team"), team_index))
    }
    return schedule_index.get(frozenset(team_ids)) if len(team_ids) == 2 else None


def _find_team(
    team_name: Any,
    team_index: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    key = slug_key(team_name)
    if key in team_index:
        return team_index[key]
    matches = [
        team
        for team_key, team in team_index.items()
        if key and (key in team_key or team_key in key)
    ]
    return matches[0] if len(matches) == 1 else None


def _team_side_and_opponent(
    game: dict[str, Any] | None,
    team_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not game:
        return None, None
    away = game.get("awayTeam") or {}
    home = game.get("homeTeam") or {}
    if away.get("mlbId") == team_id:
        return away, home
    if home.get("mlbId") == team_id:
        return home, away
    return None, None


def _game_team_ids(game: dict[str, Any]) -> set[int]:
    return {
        team_id
        for key in ("awayTeam", "homeTeam")
        if (team_id := _int_or_none((game.get(key) or {}).get("mlbId"))) is not None
    }


def _same_matchup(requested: str, actual: str) -> bool:
    requested_parts = _matchup_parts(requested)
    actual_parts = _matchup_parts(actual)
    if len(requested_parts) != 2 or len(actual_parts) != 2:
        return slug_key(requested) == slug_key(actual)
    return all(
        any(
            requested_part == actual_part
            or requested_part in actual_part
            or actual_part in requested_part
            for actual_part in actual_parts
        )
        for requested_part in requested_parts
    )


def _matchup_parts(value: Any) -> list[str]:
    return [
        slug_key(part)
        for part in re.split(r"\s+(?:vs|at|@)\s+", str(value or ""), flags=re.IGNORECASE)
        if slug_key(part)
    ]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))
