from __future__ import annotations

import os
from typing import Any

import httpx


DEFAULT_MLB_BASE_URL = "https://statsapi.mlb.com/api/v1"
DEFAULT_MLB_GAME_FEED_BASE_URL = "https://statsapi.mlb.com/api/v1.1"


class MLBAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.payload = payload


class MLBStatsClient:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    async def get_teams(self, season: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"sportId": 1}
        if season:
            params["season"] = season
        return await self._get("/teams", params=params)

    async def get_schedule(self, game_date: str) -> dict[str, Any]:
        return await self._get(
            "/schedule",
            params={
                "sportId": 1,
                "date": game_date,
                "hydrate": "probablePitcher",
            },
        )

    async def get_schedule_range(
        self,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        return await self._get(
            "/schedule",
            params={
                "sportId": 1,
                "startDate": start_date,
                "endDate": end_date,
                "hydrate": "probablePitcher",
            },
        )

    async def get_standings(self, season: int) -> dict[str, Any]:
        return await self._get(
            "/standings",
            params={
                "leagueId": "103,104",
                "season": season,
                "standingsTypes": "regularSeason",
            },
        )

    async def get_team_roster(
        self,
        team_id: int,
        season: int | None = None,
        roster_type: str = "active",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"rosterType": roster_type}
        if season:
            params["season"] = season
        return await self._get(f"/teams/{team_id}/roster", params=params)

    async def get_team_stats(
        self,
        team_id: int,
        group: str,
        season: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"stats": "season", "group": group}
        if season:
            params["season"] = season
        return await self._get(f"/teams/{team_id}/stats", params=params)

    async def search_players(self, query: str) -> dict[str, Any]:
        return await self._get(
            "/people/search",
            params={
                "names": query,
                "sportId": 1,
            },
        )

    async def get_player(self, player_id: int) -> dict[str, Any]:
        return await self._get(f"/people/{player_id}")

    async def get_player_stats(
        self,
        player_id: int,
        group: str,
        season: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"stats": "season", "group": group}
        if season:
            params["season"] = season
        return await self._get(f"/people/{player_id}/stats", params=params)

    async def get_player_stat_splits(
        self,
        player_id: int,
        group: str,
        season: int | None = None,
        sit_codes: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"stats": "statSplits", "group": group}
        if season:
            params["season"] = season
        if sit_codes:
            params["sitCodes"] = sit_codes
        return await self._get(f"/people/{player_id}/stats", params=params)

    async def get_player_game_log(
        self,
        player_id: int,
        group: str,
        season: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"stats": "gameLog", "group": group}
        if season:
            params["season"] = season
        return await self._get(f"/people/{player_id}/stats", params=params)

    async def get_game_feed(self, game_pk: int) -> dict[str, Any]:
        base_url = os.getenv(
            "MLB_GAME_FEED_BASE_URL",
            DEFAULT_MLB_GAME_FEED_BASE_URL,
        ).rstrip("/")
        return await self._get(f"{base_url}/game/{game_pk}/feed/live")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http_client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise _mlb_error_from_response(exc.response) from exc
        except httpx.RequestError as exc:
            raise MLBAPIError(502, f"MLB request failed: {exc}") from exc


def build_mlb_http_client() -> httpx.AsyncClient:
    base_url = os.getenv("MLB_STATS_BASE_URL", DEFAULT_MLB_BASE_URL).rstrip("/")
    timeout = float(os.getenv("MLB_STATS_TIMEOUT_SECONDS", "20"))
    return httpx.AsyncClient(base_url=base_url, timeout=timeout)


def _mlb_error_from_response(response: httpx.Response) -> MLBAPIError:
    payload: Any | None = None
    message = response.reason_phrase

    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        if text:
            message = text
    else:
        if isinstance(payload, dict):
            message = str(
                payload.get("message")
                or payload.get("detail")
                or payload.get("error")
                or payload
            )
        else:
            message = str(payload)

    return MLBAPIError(response.status_code, message, payload)
