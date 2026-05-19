from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import httpx


DEFAULT_STAKE_GRAPHQL_BASE_URL = "https://stake.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

USER_BALANCES_QUERY = """
query UserBalances {
  user {
    id
    balances {
      available {
        amount
        currency
        __typename
      }
      vault {
        amount
        currency
        __typename
      }
      __typename
    }
    __typename
  }
}
"""

USER_PROFILE_QUERY = """
query UserProfile {
  user {
    id
    name
    email
    isEmailVerified
    country
    level
    __typename
  }
}
"""

BET_HISTORY_QUERY = """
query BetHistory($first: Int, $after: String) {
  user {
    bets(first: $first, after: $after) {
      edges {
        node {
          id
          amount
          currency
          multiplier
          payout
          createdAt
          updatedAt
          outcome
          game {
            name
            slug
            __typename
          }
          __typename
        }
      }
      pageInfo {
        hasNextPage
        endCursor
        __typename
      }
      __typename
    }
    __typename
  }
}
"""


class StakeGraphQLError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class StakeGraphQLCredentials:
    access_token: str | None = None
    session_cookie: str | None = None
    cf_clearance: str | None = None
    user_agent: str | None = None

    @classmethod
    def from_env(cls, prefix: str = "STAKE_LOCAL") -> "StakeGraphQLCredentials":
        return cls(
            access_token=_blank_to_none(os.getenv(f"{prefix}_ACCESS_TOKEN")),
            session_cookie=_blank_to_none(os.getenv(f"{prefix}_SESSION_COOKIE")),
            cf_clearance=_blank_to_none(os.getenv(f"{prefix}_CF_CLEARANCE")),
            user_agent=_blank_to_none(os.getenv(f"{prefix}_USER_AGENT")),
        )

    @classmethod
    def from_curl(cls, curl_command: str) -> "StakeGraphQLCredentials":
        return extract_stake_credentials_from_curl(curl_command)

    def has_auth_material(self) -> bool:
        return bool(self.access_token or self.session_cookie or self.cf_clearance)

    def masked(self) -> dict[str, str | None]:
        return {
            "accessToken": _mask_secret(self.access_token),
            "sessionCookie": _mask_secret(self.session_cookie),
            "cfClearance": _mask_secret(self.cf_clearance),
            "userAgent": self.user_agent,
        }


class StakeGraphQLClient:
    """Small read-only GraphQL helper for local Stake session experiments.

    This is intentionally not wired into the hosted GPT action API. It exists so
    the future local slip builder can reuse known GraphQL request mechanics while
    the visible Stake UI remains the final source of truth.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        credentials: StakeGraphQLCredentials | None = None,
    ) -> None:
        self._http_client = http_client
        self._credentials = credentials or StakeGraphQLCredentials()

    async def query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        if operation_name:
            payload["operationName"] = operation_name

        response = await self._http_client.post(
            "/_api/graphql",
            json=payload,
            headers=build_stake_graphql_headers(self._credentials),
        )

        if response.status_code in {401, 403, 429} or response.status_code >= 500:
            raise StakeGraphQLError(
                _stake_graphql_http_message(response),
                status_code=response.status_code,
                payload=_response_payload(response),
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise StakeGraphQLError(
                "Stake GraphQL returned a non-JSON response.",
                status_code=response.status_code,
                payload=response.text,
            ) from exc

        if response.status_code >= 400:
            raise StakeGraphQLError(
                f"Stake GraphQL HTTP error: {response.status_code}",
                status_code=response.status_code,
                payload=body,
            )

        errors = body.get("errors")
        if errors:
            messages = [
                str(error.get("message") or "Unknown GraphQL error")
                for error in errors
                if isinstance(error, dict)
            ]
            raise StakeGraphQLError(
                "Stake GraphQL errors: " + ", ".join(messages),
                status_code=response.status_code,
                payload=body,
            )

        data = body.get("data")
        return data if isinstance(data, dict) else {}

    async def get_user_balances(self) -> dict[str, dict[str, float]]:
        data = await self.query(USER_BALANCES_QUERY, operation_name="UserBalances")
        return normalize_user_balances(data)

    async def get_user_profile(self) -> dict[str, Any]:
        data = await self.query(USER_PROFILE_QUERY, operation_name="UserProfile")
        user = data.get("user")
        return user if isinstance(user, dict) else {}

    async def get_bet_history(
        self,
        first: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        return await self.query(
            BET_HISTORY_QUERY,
            variables={"first": first, "after": after},
            operation_name="BetHistory",
        )


def build_local_stake_graphql_client(
    credentials: StakeGraphQLCredentials | None = None,
) -> httpx.AsyncClient:
    base_url = os.getenv("STAKE_LOCAL_BASE_URL", DEFAULT_STAKE_GRAPHQL_BASE_URL)
    timeout = float(os.getenv("STAKE_LOCAL_TIMEOUT_SECONDS", "20"))
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)


def build_stake_graphql_headers(
    credentials: StakeGraphQLCredentials | None = None,
) -> dict[str, str]:
    creds = credentials or StakeGraphQLCredentials()
    headers = {
        "Accept": "application/graphql+json, application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": DEFAULT_STAKE_GRAPHQL_BASE_URL,
        "Referer": DEFAULT_STAKE_GRAPHQL_BASE_URL + "/",
        "User-Agent": creds.user_agent or DEFAULT_USER_AGENT,
        "X-Language": "en",
    }
    if creds.access_token:
        headers["X-Access-Token"] = creds.access_token

    cookie_header = build_stake_cookie_header(creds)
    if cookie_header:
        headers["Cookie"] = cookie_header

    return headers


def build_stake_cookie_header(credentials: StakeGraphQLCredentials) -> str:
    cookies: list[str] = []
    if credentials.session_cookie:
        cookies.append(f"session={credentials.session_cookie}")
    if credentials.cf_clearance:
        cookies.append(f"cf_clearance={credentials.cf_clearance}")
    return "; ".join(cookies)


def extract_stake_credentials_from_curl(curl_command: str) -> StakeGraphQLCredentials:
    access_token = _extract_header_value(curl_command, "x-access-token")
    user_agent = _extract_header_value(curl_command, "user-agent")
    cookie_blob = _extract_cookie_blob(curl_command)
    cookies = _parse_cookie_blob(cookie_blob)

    return StakeGraphQLCredentials(
        access_token=access_token,
        session_cookie=cookies.get("session"),
        cf_clearance=cookies.get("cf_clearance"),
        user_agent=user_agent,
    )


def normalize_user_balances(data: dict[str, Any]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {"available": {}, "vault": {}}
    user = data.get("user")
    if not isinstance(user, dict):
        return result

    balances = user.get("balances")
    if not isinstance(balances, list):
        return result

    for entry in balances:
        if not isinstance(entry, dict):
            continue
        for bucket in ("available", "vault"):
            balance = entry.get(bucket)
            if not isinstance(balance, dict):
                continue
            currency = str(balance.get("currency") or "").lower()
            if not currency:
                continue
            result[bucket][currency] = _float_or_zero(balance.get("amount"))

    return result


def _extract_header_value(curl_command: str, header_name: str) -> str | None:
    pattern = (
        r"(?:-H|--header)\s+"
        r"(?P<quote>['\"])"
        + re.escape(header_name)
        + r"\s*:\s*(?P<value>.*?)(?P=quote)"
    )
    match = re.search(pattern, curl_command, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _blank_to_none(match.group("value").strip())


def _extract_cookie_blob(curl_command: str) -> str:
    header_cookie = _extract_header_value(curl_command, "cookie")
    if header_cookie:
        return header_cookie

    match = re.search(
        r"(?:-b|--cookie)\s+(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
        curl_command,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return match.group("value").strip()


def _parse_cookie_blob(cookie_blob: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_blob.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            cookies[name] = value
    return cookies


def _response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _stake_graphql_http_message(response: httpx.Response) -> str:
    if response.status_code == 401:
        return "Stake GraphQL rejected the token or session."
    if response.status_code == 403:
        return "Stake GraphQL was blocked, likely by Cloudflare or missing session cookies."
    if response.status_code == 429:
        return "Stake GraphQL rate limit exceeded."
    return f"Stake GraphQL HTTP error: {response.status_code}"


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"
