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

    async def get_schedule_range(
        self,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        payload = await self._client.get_schedule_range(start_date, end_date)
        games = [
            _normalize_game(game)
            for date_entry in payload.get("dates") or []
            for game in date_entry.get("games") or []
        ]
        return {
            "startDate": start_date,
            "endDate": end_date,
            "gameCount": len(games),
            "games": games,
        }

    async def get_standings(self, season: int) -> dict[str, Any]:
        payload = await self._client.get_standings(season)
        teams = [
            _normalize_standing(team)
            for record in payload.get("records") or []
            for team in record.get("teamRecords") or []
        ]
        return {
            "season": season,
            "teamCount": len(teams),
            "teams": teams,
            "teamsById": {
                team["mlbId"]: team
                for team in teams
                if team.get("mlbId") is not None
            },
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

    async def get_team_profile(
        self,
        team_id: int,
        season: int | None = None,
        group: str = "hitting",
    ) -> dict[str, Any]:
        payload = await self._client.get_team_stats(
            team_id,
            group=group,
            season=season,
        )
        return {
            "teamId": team_id,
            "season": season,
            "group": group,
            "stats": _first_split_stat(payload),
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

    async def get_player_splits(
        self,
        player_id: int,
        group: str = "hitting",
        season: int | None = None,
        sit_codes: str | None = "h,a,vr,vl",
    ) -> dict[str, Any]:
        payload = await self._client.get_player_stat_splits(
            player_id,
            group=group,
            season=season,
            sit_codes=sit_codes,
        )
        splits = [_normalize_stat_split(split) for split in _game_splits(payload)]
        return {
            "playerId": player_id,
            "group": group,
            "season": season,
            "sitCodes": sit_codes,
            "splitCount": len(splits),
            "splits": splits,
        }

    async def get_game_context(self, game_pk: int) -> dict[str, Any]:
        payload = await self._client.get_game_feed(game_pk)
        return _normalize_live_game_context(payload)


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
        "score": raw_side.get("score"),
        "isWinner": raw_side.get("isWinner"),
        "probablePitcher": _normalize_pitcher(pitcher),
    }


def _normalize_standing(raw_team: dict[str, Any]) -> dict[str, Any]:
    team = raw_team.get("team") or {}
    name = str(team.get("name") or "")
    return {
        "mlbId": team.get("id"),
        "name": name,
        "key": slug_key(name),
        "wins": raw_team.get("wins"),
        "losses": raw_team.get("losses"),
        "pct": raw_team.get("winningPercentage"),
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


def _normalize_live_game_context(payload: dict[str, Any]) -> dict[str, Any]:
    game_data = payload.get("gameData") or {}
    live_data = payload.get("liveData") or {}
    boxscore = live_data.get("boxscore") or {}
    status = game_data.get("status") or {}
    datetime_payload = game_data.get("datetime") or {}
    game_payload = game_data.get("game") or {}
    venue = game_data.get("venue") or {}
    teams_payload = boxscore.get("teams") or {}
    game_players = game_data.get("players") or {}

    return {
        "gamePk": game_payload.get("pk") or payload.get("gamePk"),
        "gameDate": datetime_payload.get("dateTime"),
        "officialDate": datetime_payload.get("officialDate"),
        "status": {
            "abstractGameState": status.get("abstractGameState"),
            "detailedState": status.get("detailedState"),
            "codedGameState": status.get("codedGameState"),
            "statusCode": status.get("statusCode"),
            "reason": status.get("reason"),
            "startTimeTBD": status.get("startTimeTBD"),
        },
        "statusRiskFlags": _game_status_risk_flags(status),
        "gameInfo": {
            "gameType": game_payload.get("type"),
            "doubleHeader": game_payload.get("doubleHeader"),
            "gameNumber": game_payload.get("gameNumber"),
            "dayNight": datetime_payload.get("dayNight"),
        },
        "venue": _normalize_venue(venue),
        "weather": _normalize_weather(game_data.get("weather") or {}),
        "teams": {
            "away": _normalize_boxscore_team(
                teams_payload.get("away") or {},
                game_players,
            ),
            "home": _normalize_boxscore_team(
                teams_payload.get("home") or {},
                game_players,
            ),
        },
    }


def _normalize_venue(venue: dict[str, Any]) -> dict[str, Any] | None:
    if not venue:
        return None
    field_info = venue.get("fieldInfo") or {}
    location = venue.get("location") or {}
    return {
        "mlbId": venue.get("id"),
        "name": venue.get("name"),
        "roofType": field_info.get("roofType"),
        "turfType": field_info.get("turfType"),
        "capacity": field_info.get("capacity"),
        "city": location.get("city"),
        "state": location.get("state"),
        "timeZone": (venue.get("timeZone") or {}).get("id"),
    }


def _normalize_weather(weather: dict[str, Any]) -> dict[str, Any] | None:
    if not weather:
        return None
    return {
        "condition": weather.get("condition"),
        "temp": weather.get("temp"),
        "wind": weather.get("wind"),
    }


def _normalize_boxscore_team(
    raw_team: dict[str, Any],
    game_players: dict[str, Any],
) -> dict[str, Any]:
    team = raw_team.get("team") or {}
    batting_order = [_int_or_none(player_id) for player_id in raw_team.get("battingOrder") or []]
    batting_order = [player_id for player_id in batting_order if player_id is not None]
    players_by_id = {}
    for raw_player in (raw_team.get("players") or {}).values():
        player = _normalize_boxscore_player(raw_player, game_players, batting_order)
        player_id = player.get("mlbId")
        if player_id is not None:
            players_by_id[str(player_id)] = player

    lineup = [
        players_by_id[str(player_id)]
        for player_id in batting_order
        if str(player_id) in players_by_id
    ]
    return {
        "team": _normalize_team(team) if team else None,
        "lineupConfirmed": bool(batting_order),
        "battingOrder": batting_order,
        "lineup": lineup,
        "playersById": players_by_id,
        "batters": [_int_or_none(player_id) for player_id in raw_team.get("batters") or []],
        "pitchers": [_int_or_none(player_id) for player_id in raw_team.get("pitchers") or []],
        "teamStats": raw_team.get("teamStats") or {},
    }


def _normalize_boxscore_player(
    raw_player: dict[str, Any],
    game_players: dict[str, Any],
    batting_order: list[int],
) -> dict[str, Any]:
    person = raw_player.get("person") or {}
    player_id = person.get("id")
    game_player = game_players.get(f"ID{player_id}") or {}
    position = raw_player.get("position") or game_player.get("primaryPosition") or {}
    order_index = batting_order.index(player_id) + 1 if player_id in batting_order else None
    return {
        "mlbId": player_id,
        "name": person.get("fullName") or game_player.get("fullName"),
        "key": slug_key(person.get("fullName") or game_player.get("fullName")),
        "batSide": (game_player.get("batSide") or {}).get("code"),
        "pitchHand": (game_player.get("pitchHand") or {}).get("code"),
        "position": position.get("abbreviation"),
        "positionName": position.get("name"),
        "battingOrder": order_index,
        "confirmedStarter": order_index is not None,
        "stats": raw_player.get("stats") or {},
        "gameStatus": raw_player.get("gameStatus") or {},
        "seasonStats": raw_player.get("seasonStats") or {},
    }


def _normalize_stat_split(split: dict[str, Any]) -> dict[str, Any]:
    return {
        "split": split.get("split"),
        "type": split.get("type"),
        "season": split.get("season"),
        "stat": split.get("stat") or {},
    }


def _game_status_risk_flags(status: dict[str, Any]) -> list[str]:
    text = " ".join(
        str(status.get(key) or "")
        for key in ("abstractGameState", "detailedState", "codedGameState", "statusCode", "reason")
    ).lower()
    flags = []
    if "postpon" in text:
        flags.append("game_postponed")
    if "suspend" in text:
        flags.append("game_suspended")
    if "delay" in text:
        flags.append("game_delay_risk")
    if "cancel" in text:
        flags.append("game_cancelled")
    if status.get("startTimeTBD"):
        flags.append("start_time_tbd")
    return flags


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


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
