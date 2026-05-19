from __future__ import annotations

import asyncio

import httpx
import pytest

from app.stake_graphql_reference import (
    StakeGraphQLClient,
    StakeGraphQLError,
    StakeGraphQLCredentials,
    build_stake_graphql_headers,
    extract_stake_credentials_from_curl,
    normalize_user_balances,
)


def test_extracts_stake_credentials_from_curl_headers_and_cookie():
    curl = """
    curl "https://stake.com/_api/graphql" ^
      -H "user-agent: Mozilla/5.0 Test Browser" ^
      -H "x-access-token: access-token-123" ^
      -H "cookie: session=session-456; cf_clearance=clearance-789; theme=dark" ^
      --data-raw "{\\"query\\":\\"query UserBalances { user { id } }\\"}"
    """

    credentials = extract_stake_credentials_from_curl(curl)

    assert credentials.access_token == "access-token-123"
    assert credentials.session_cookie == "session-456"
    assert credentials.cf_clearance == "clearance-789"
    assert credentials.user_agent == "Mozilla/5.0 Test Browser"


def test_extracts_stake_credentials_from_curl_cookie_flag():
    curl = """
    curl "https://stake.com/_api/graphql" \
      -H "x-access-token: token-from-header" \
      -b "session=session-from-cookie-flag; cf_clearance=clearance-from-cookie-flag"
    """

    credentials = extract_stake_credentials_from_curl(curl)

    assert credentials.access_token == "token-from-header"
    assert credentials.session_cookie == "session-from-cookie-flag"
    assert credentials.cf_clearance == "clearance-from-cookie-flag"


def test_builds_browser_like_graphql_headers_without_logging_secrets():
    credentials = StakeGraphQLCredentials(
        access_token="access-secret-token",
        session_cookie="session-secret-cookie",
        cf_clearance="cloudflare-clearance-secret",
        user_agent="Mozilla/5.0 Custom",
    )

    headers = build_stake_graphql_headers(credentials)

    assert headers["Accept"] == "application/graphql+json, application/json"
    assert headers["Origin"] == "https://stake.com"
    assert headers["Referer"] == "https://stake.com/"
    assert headers["User-Agent"] == "Mozilla/5.0 Custom"
    assert headers["X-Access-Token"] == "access-secret-token"
    assert headers["Cookie"] == (
        "session=session-secret-cookie; "
        "cf_clearance=cloudflare-clearance-secret"
    )
    assert credentials.masked()["accessToken"] == "acce...oken"


def test_graphql_client_posts_expected_payload_and_returns_data():
    asyncio.run(_run_graphql_client_posts_expected_payload_and_returns_data())


async def _run_graphql_client_posts_expected_payload_and_returns_data():
    seen_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"data": {"user": {"id": "u1"}}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://stake.com",
    ) as http_client:
        client = StakeGraphQLClient(
            http_client=http_client,
            credentials=StakeGraphQLCredentials(access_token="local-token"),
        )
        result = await client.query(
            "query Test($first: Int) { user { id } }",
            variables={"first": 1},
            operation_name="Test",
        )

    assert result == {"user": {"id": "u1"}}
    assert seen_requests[0].url == httpx.URL("https://stake.com/_api/graphql")
    assert seen_requests[0].headers["X-Access-Token"] == "local-token"
    assert seen_requests[0].read() == (
        b'{"query":"query Test($first: Int) { user { id } }",'
        b'"variables":{"first":1},"operationName":"Test"}'
    )


def test_graphql_client_raises_clean_error_for_graphql_errors():
    asyncio.run(_run_graphql_client_raises_clean_error_for_graphql_errors())


async def _run_graphql_client_raises_clean_error_for_graphql_errors():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"errors": [{"message": "Cannot query field"}]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://stake.com",
    ) as http_client:
        client = StakeGraphQLClient(http_client=http_client)

        with pytest.raises(StakeGraphQLError) as exc:
            await client.query("query Bad { badField }", operation_name="Bad")

    assert "Cannot query field" in exc.value.message
    assert exc.value.status_code == 200


def test_graphql_client_raises_clean_error_for_cloudflare_blocks():
    asyncio.run(_run_graphql_client_raises_clean_error_for_cloudflare_blocks())


async def _run_graphql_client_raises_clean_error_for_cloudflare_blocks():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Just a moment...")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://stake.com",
    ) as http_client:
        client = StakeGraphQLClient(http_client=http_client)

        with pytest.raises(StakeGraphQLError) as exc:
            await client.query("query UserBalances { user { id } }")

    assert "Cloudflare" in exc.value.message
    assert exc.value.status_code == 403


def test_normalizes_user_balances_from_stake_shape():
    payload = {
        "user": {
            "balances": [
                {
                    "available": {"amount": "12.5", "currency": "usd"},
                    "vault": {"amount": "3.0", "currency": "usd"},
                },
                {
                    "available": {"amount": "0.25", "currency": "ltc"},
                    "vault": {"amount": None, "currency": "ltc"},
                },
            ]
        }
    }

    assert normalize_user_balances(payload) == {
        "available": {"usd": 12.5, "ltc": 0.25},
        "vault": {"usd": 3.0, "ltc": 0.0},
    }
