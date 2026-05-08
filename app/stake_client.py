from __future__ import annotations

import copy
import os
import time
from typing import Any
from urllib.parse import quote

import httpx


DEFAULT_BASE_URL = "https://odds-data.stake.com"
DEFAULT_CACHE_TTL_SECONDS = 20.0
_GET_CACHE: dict[tuple[str, str, bool], tuple[float, Any]] = {}


class StakeAPIError(Exception):
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


class StakeClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str | None = None,
        cache_ttl_seconds: float | None = None,
    ) -> None:
        self._http_client = http_client
        self._headers = {"X-API-KEY": api_key} if api_key else None
        self._cache_ttl_seconds = _cache_ttl_seconds(cache_ttl_seconds)

    async def get_sports(self) -> Any:
        return await self._get("/sports")

    async def get_sport_categories(self, sport: str) -> Any:
        return await self._get(f"/sports/{_segment(sport)}/categories")

    async def get_sport_schedule(self, sport: str) -> Any:
        return await self._get(f"/schedule/sport/{_segment(sport)}")

    async def get_tournament_schedule(
        self,
        sport: str,
        category: str,
        tournament: str,
    ) -> Any:
        return await self._get(
            "/schedule/sport/"
            f"{_segment(sport)}/{_segment(category)}/tournament/{_segment(tournament)}"
        )

    async def get_fixture(self, fixture_slug: str) -> dict[str, Any]:
        payload = await self._get(f"/fixtures/{_segment(fixture_slug)}")
        return normalize_fixture_odds(payload)

    async def get_odds(self, fixture_slug: str) -> dict[str, Any]:
        payload = await self._get(f"/odds/{_segment(fixture_slug)}")
        return normalize_fixture_odds(payload)

    async def _get(self, path: str) -> Any:
        cache_key = self._cache_key(path)
        if self._cache_ttl_seconds > 0:
            cached = _GET_CACHE.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return copy.deepcopy(cached[1])

        try:
            response = await self._http_client.get(path, headers=self._headers)
            response.raise_for_status()
            payload = response.json()
            if self._cache_ttl_seconds > 0:
                expires_at = time.monotonic() + self._cache_ttl_seconds
                _GET_CACHE[cache_key] = (expires_at, copy.deepcopy(payload))
            return payload
        except httpx.HTTPStatusError as exc:
            raise _stake_error_from_response(exc.response) from exc
        except httpx.RequestError as exc:
            raise StakeAPIError(
                502,
                f"Stake request failed: {exc}",
            ) from exc

    def _cache_key(self, path: str) -> tuple[str, str, bool]:
        return (str(self._http_client.base_url), path, bool(self._headers))


def build_http_client() -> httpx.AsyncClient:
    base_url = os.getenv("STAKE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    timeout = float(os.getenv("STAKE_TIMEOUT_SECONDS", "20"))
    return httpx.AsyncClient(base_url=base_url, timeout=timeout)


def clear_stake_cache() -> None:
    _GET_CACHE.clear()


def normalize_fixture_odds(payload: dict[str, Any]) -> dict[str, Any]:
    fixture = dict(payload.get("fixture") or {})
    groups = payload.get("groups")
    swish_markets = payload.get("swishMarkets")

    if groups is None:
        groups = fixture.pop("groups", [])
    else:
        fixture.pop("groups", None)

    if swish_markets is None:
        swish_markets = fixture.pop("swishMarkets", [])
    else:
        fixture.pop("swishMarkets", None)

    return {
        "fixture": fixture,
        "groups": groups or [],
        "swishMarkets": swish_markets or [],
    }


def _segment(value: str) -> str:
    stripped = value.strip().strip("/")
    return quote(stripped, safe="-_.~")


def _cache_ttl_seconds(configured_value: float | None) -> float:
    if configured_value is not None:
        return max(0.0, float(configured_value))

    raw_value = os.getenv("STAKE_CACHE_TTL_SECONDS")
    if raw_value is None:
        return DEFAULT_CACHE_TTL_SECONDS

    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS


def _stake_error_from_response(response: httpx.Response) -> StakeAPIError:
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

    return StakeAPIError(response.status_code, message, payload)
