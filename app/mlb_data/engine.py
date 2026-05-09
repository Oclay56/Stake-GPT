from __future__ import annotations

from typing import Any

from app.mlb_props import slug_key


class MLBDataEngine:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def get_teams(self, season: int | None = None) -> dict[str, Any]:
        payload = await self._client.get_teams(season=season)
        teams = [_normalize_team(team) for team in payload.get("teams") or []]
        return {
            "season": season,
            "teamCount": len(teams),
            "teams": teams,
        }

    async def get_schedule(self, game_date: str) -> dict[str, Any]:
        payload = await self._client.get_schedule(game_date)
        games = [
            _normalize_game(game)
            for date_entry in payload.get("dates") or []
            for game in date_entry.get("games") or []
        ]
        return {
            "date": game_date,
            "gameCount": len(games),
            "games": games,
        }

    async def get_team_roster(
        self,
        team_id: int,
        season: int | None = None,
    ) -> dict[str, Any]:
        payload = await self._client.get_team_roster(team_id, season=season)
        players = [
            _normalize_roster_player(player)
            for player in payload.get("roster") or []
        ]
        return {
            "teamId": team_id,
            "season": season,
            "playerCount": len(players),
            "players": players,
        }

    async def search_players(
        self,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        payload = await self._client.search_players(query)
        players = [
            _normalize_person(person)
            for person in (payload.get("people") or [])[: _clean_limit(limit)]
        ]
        return {
            "query": query,
            "playerCount": len(players),
            "players": players,
        }

    async def get_player_profile(
        self,
        player_id: int,
        season: int | None = None,
        group: str = "hitting",
    ) -> dict[str, Any]:
        player_payload = await self._client.get_player(player_id)
        stats_payload = await self._client.get_player_stats(
            player_id,
            group=group,
            season=season,
        )
        person = (player_payload.get("people") or [{}])[0]
        player = _normalize_person(person)
        player["stats"] = _first_split_stat(stats_payload)
        return {
            "player": player,
            "season": season,
            "group": group,
        }

    async def get_player_recent_history(
        self,
        player_id: int,
        group: str = "hitting",
        season: int | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        payload = await self._client.get_player_game_log(
            player_id,
            group=group,
            season=season,
        )
        splits = _game_splits(payload)
        splits.sort(key=lambda game: str(game.get("date") or ""), reverse=True)
        games = [_normalize_game_log(split) for split in splits[: _clean_limit(limit)]]
        totals = _sum_game_stats(games)
        return {
            "playerId": player_id,
            "group": group,
            "season": season,
            "gamesUsed": len(games),
            "games": games,
            "totals": totals,
            "perGame": _per_game(totals, len(games)),
        }


def _normalize_team(raw_team: dict[str, Any]) -> dict[str, Any]:
    name = str(raw_team.get("name") or "")
    return {
        "mlbId": raw_team.get("id"),
        "name": name,
        "key": slug_key(name),
        "abbreviation": raw_team.get("abbreviation"),
        "clubName": raw_team.get("clubName"),
        "league": (raw_team.get("league") or {}).get("name"),
        "division": (raw_team.get("division") or {}).get("name"),
    }


def _normalize_game(game: dict[str, Any]) -> dict[str, Any]:
    away = (game.get("teams") or {}).get("away") or {}
    home = (game.get("teams") or {}).get("home") or {}
    return {
        "gamePk": game.get("gamePk"),
        "gameDate": game.get("gameDate"),
        "status": (game.get("status") or {}).get("detailedState"),
        "awayTeam": _normalize_game_team(away),
        "homeTeam": _normalize_game_team(home),
    }


def _normalize_game_team(raw_side: dict[str, Any]) -> dict[str, Any]:
    team = raw_side.get("team") or {}
    pitcher = raw_side.get("probablePitcher") or {}
    name = str(team.get("name") or "")
    return {
        "mlbId": team.get("id"),
        "name": name,
        "key": slug_key(name),
        "probablePitcher": _normalize_pitcher(pitcher),
    }


def _normalize_pitcher(raw_pitcher: dict[str, Any]) -> dict[str, Any] | None:
    pitcher_id = raw_pitcher.get("id")
    name = raw_pitcher.get("fullName")
    if pitcher_id is None and not name:
        return None
    name = str(name or "TBD")
    return {
        "mlbId": pitcher_id,
        "name": name,
        "key": slug_key(name),
    }


def _normalize_roster_player(raw_player: dict[str, Any]) -> dict[str, Any]:
    person = raw_player.get("person") or {}
    player = _normalize_person(person)
    player["position"] = (raw_player.get("position") or {}).get("abbreviation")
    player["positionName"] = (raw_player.get("position") or {}).get("name")
    player["status"] = (raw_player.get("status") or {}).get("description")
    return player


def _normalize_person(raw_person: dict[str, Any]) -> dict[str, Any]:
    name = str(raw_person.get("fullName") or "")
    team = raw_person.get("currentTeam") or {}
    return {
        "mlbId": raw_person.get("id"),
        "name": name,
        "key": slug_key(name),
        "position": (raw_person.get("primaryPosition") or {}).get("abbreviation"),
        "team": {
            "mlbId": team.get("id"),
            "name": team.get("name"),
            "key": slug_key(team.get("name")),
        }
        if team
        else None,
        "batSide": (raw_person.get("batSide") or {}).get("code"),
        "pitchHand": (raw_person.get("pitchHand") or {}).get("code"),
        "active": raw_person.get("active"),
    }


def _first_split_stat(payload: dict[str, Any]) -> dict[str, Any]:
    for stat_group in payload.get("stats") or []:
        splits = stat_group.get("splits") or []
        if splits:
            return splits[0].get("stat") or {}
    return {}


def _game_splits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for stat_group in payload.get("stats") or []:
        splits = stat_group.get("splits")
        if isinstance(splits, list):
            return list(splits)
    return []


def _normalize_game_log(split: dict[str, Any]) -> dict[str, Any]:
    game = split.get("game") or {}
    return {
        "gamePk": split.get("gamePk") or game.get("gamePk"),
        "date": split.get("date"),
        "opponent": (split.get("opponent") or {}).get("name"),
        "isHome": split.get("isHome"),
        "stats": split.get("stat") or {},
    }


def _sum_game_stats(games: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for game in games:
        for key, value in (game.get("stats") or {}).items():
            numeric_value = _numeric(value)
            if numeric_value is None:
                continue
            totals[key] = round(totals.get(key, 0.0) + numeric_value, 4)
    return totals


def _per_game(totals: dict[str, float], game_count: int) -> dict[str, float]:
    if game_count <= 0:
        return {}
    return {
        key: round(value / game_count, 4)
        for key, value in totals.items()
    }


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_limit(limit: int) -> int:
    return max(1, min(limit, 100))
