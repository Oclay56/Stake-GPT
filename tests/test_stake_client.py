import asyncio

import pytest
import httpx

from app import stake_client as stake_client_module
from app.stake_client import StakeAPIError, StakeClient, normalize_fixture_odds


@pytest.fixture(autouse=True)
def clear_stake_cache_between_tests():
    if hasattr(stake_client_module, "clear_stake_cache"):
        stake_client_module.clear_stake_cache()


def test_get_sports_uses_expected_url_without_api_key_header():
    asyncio.run(_run_get_sports_uses_expected_url_without_api_key_header())


async def _run_get_sports_uses_expected_url_without_api_key_header():
    seen_requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json=[{"slug": "baseball", "enabled": True}])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://odds-data.stake.com",
    ) as http_client:
        client = StakeClient(http_client=http_client)
        result = await client.get_sports()

    assert result == [{"slug": "baseball", "enabled": True}]
    assert seen_requests[0].url == httpx.URL("https://odds-data.stake.com/sports")
    assert "X-API-KEY" not in seen_requests[0].headers


def test_client_sends_api_key_header_when_configured():
    asyncio.run(_run_client_sends_api_key_header_when_configured())


async def _run_client_sends_api_key_header_when_configured():
    seen_requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://odds-data.stake.com",
    ) as http_client:
        client = StakeClient(http_client=http_client, api_key="test-key")
        await client.get_sports()

    assert seen_requests[0].headers["X-API-KEY"] == "test-key"


def test_client_caches_successful_gets_when_enabled():
    asyncio.run(_run_client_caches_successful_gets_when_enabled())


async def _run_client_caches_successful_gets_when_enabled():
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=[{"slug": "baseball"}])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://odds-data.stake.com",
    ) as http_client:
        client = StakeClient(http_client=http_client, cache_ttl_seconds=60)
        first = await client.get_sports()
        second = await client.get_sports()

    assert first == [{"slug": "baseball"}]
    assert second == first
    assert request_count == 1


def test_client_skips_cache_when_ttl_is_zero():
    asyncio.run(_run_client_skips_cache_when_ttl_is_zero())


async def _run_client_skips_cache_when_ttl_is_zero():
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=[{"call": request_count}])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://odds-data.stake.com",
    ) as http_client:
        client = StakeClient(http_client=http_client, cache_ttl_seconds=0)
        first = await client.get_sports()
        second = await client.get_sports()

    assert first == [{"call": 1}]
    assert second == [{"call": 2}]
    assert request_count == 2


@pytest.mark.parametrize(
    ("call_name", "args", "expected_path"),
    [
        ("get_sport_categories", ("baseball",), "/sports/baseball/categories"),
        ("get_sport_schedule", ("baseball",), "/schedule/sport/baseball"),
        (
            "get_tournament_schedule",
            ("baseball", "usa", "mlb"),
            "/schedule/sport/baseball/usa/tournament/mlb",
        ),
        ("get_fixture", ("fixture-123",), "/fixtures/fixture-123"),
        ("get_odds", ("fixture-123",), "/odds/fixture-123"),
    ],
)
def test_client_uses_expected_endpoint_paths(call_name, args, expected_path):
    asyncio.run(_run_client_uses_expected_endpoint_paths(call_name, args, expected_path))


async def _run_client_uses_expected_endpoint_paths(call_name, args, expected_path):
    seen_requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://odds-data.stake.com",
    ) as http_client:
        client = StakeClient(http_client=http_client)
        await getattr(client, call_name)(*args)

    assert seen_requests[0].url.path == expected_path


def test_client_raises_stake_error_for_missing_fixture():
    asyncio.run(_run_client_raises_stake_error_for_missing_fixture())


async def _run_client_raises_stake_error_for_missing_fixture():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "notFound"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://odds-data.stake.com",
    ) as http_client:
        client = StakeClient(http_client=http_client)

        with pytest.raises(StakeAPIError) as exc:
            await client.get_fixture("missing-fixture")

    assert exc.value.status_code == 404
    assert "notFound" in exc.value.message


def test_normalize_fixture_odds_keeps_live_response_shape_clean():
    payload = {
        "fixture": {"slug": "fixture-123", "name": "Home - Away"},
        "groups": [{"name": "main", "markets": []}],
        "swishMarkets": [{"playerProps": []}],
    }

    assert normalize_fixture_odds(payload) == payload


def test_normalize_fixture_odds_supports_schema_nested_groups_shape():
    payload = {
        "fixture": {
            "slug": "fixture-123",
            "groups": [{"name": "main"}],
            "swishMarkets": [{"matchMarkets": []}],
        }
    }

    assert normalize_fixture_odds(payload) == {
        "fixture": {"slug": "fixture-123"},
        "groups": [{"name": "main"}],
        "swishMarkets": [{"matchMarkets": []}],
    }
