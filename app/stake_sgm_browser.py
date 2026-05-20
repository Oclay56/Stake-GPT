from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


DEFAULT_CDP_URL = "http://127.0.0.1:9222"

SGM_BOARD_QUERY = """
query AzpSgmBoard($fixture: String!) {
  slugFixture(fixture: $fixture) {
    id
    status
    provider
    swishGame {
      id
      status
      swishSportId
    }
    swishGameTeams {
      id
      name
      markets {
        trading {
          betFactor
        }
        stat {
          type
          swishStatId
          name
          value
          customBet
          liveCustomBetAvailable
          id
        }
        id
        lines {
          id
          line
          over
          under
          push
          suspended
          balanced
        }
        competitor {
          id
          name
        }
      }
      players {
        id
        name
        position
        markets {
          trading {
            betFactor
          }
          stat {
            type
            swishStatId
            name
            value
            customBet
            liveCustomBetAvailable
            id
          }
          id
          lines {
            id
            line
            over
            under
            push
            suspended
            balanced
          }
          competitor {
            id
            name
          }
        }
      }
    }
  }
}
"""


def fixture_url(fixture_slug: str) -> str:
    return f"https://stake.com/sports/baseball/usa/mlb/{fixture_slug}"


def read_stake_sgm_board(
    fixture_slug: str,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _find_or_open_fixture_page(browser.contexts[0], fixture_slug)
        warnings = _check_page_ready(page)
        response = _fetch_sgm_board_in_browser(page, fixture_slug)
        return normalize_sgm_response(fixture_slug, response, warnings)


def normalize_sgm_response(
    fixture_slug: str,
    response: dict[str, Any],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    slug_fixture = ((response.get("data") or {}).get("slugFixture")) or {}
    teams = slug_fixture.get("swishGameTeams") or []

    team_markets: list[dict[str, Any]] = []
    player_props: list[dict[str, Any]] = []
    team_summaries: list[dict[str, Any]] = []

    for team in teams:
        team_name = team.get("name")
        team_summaries.append(
            {
                "id": team.get("id"),
                "name": team_name,
                "teamMarketCount": len(team.get("markets") or []),
                "playerCount": len(team.get("players") or []),
            }
        )

        for market in team.get("markets") or []:
            team_markets.extend(_line_rows(market.get("lines") or [], market, team_name))

        for player in team.get("players") or []:
            for market in player.get("markets") or []:
                player_props.extend(
                    _line_rows(market.get("lines") or [], market, team_name, player)
                )

    return {
        "source": "stake_ui_sgm",
        "fixtureSlug": fixture_slug,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "fixture": {
            "id": slug_fixture.get("id"),
            "status": slug_fixture.get("status"),
            "provider": slug_fixture.get("provider"),
            "swishGame": slug_fixture.get("swishGame"),
        },
        "teams": team_summaries,
        "counts": {
            "teams": len(team_summaries),
            "teamMarkets": len(team_markets),
            "teamMarketsPlayable": sum(1 for row in team_markets if row["playable"]),
            "playerProps": len(player_props),
            "playerPropsPlayable": sum(1 for row in player_props if row["playable"]),
        },
        "warnings": warnings or [],
        "teamMarkets": team_markets,
        "playerProps": player_props,
    }


def _line_rows(
    lines: list[dict[str, Any]],
    market: dict[str, Any],
    team_name: str | None,
    player: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    stat = market.get("stat") or {}
    rows = []

    for line in lines or []:
        playable = bool(
            stat.get("customBet")
            and stat.get("liveCustomBetAvailable")
            and not line.get("suspended")
            and line.get("over") is not None
            and line.get("under") is not None
        )

        row = {
            "team": team_name,
            "scope": stat.get("type"),
            "market": stat.get("name"),
            "statValue": stat.get("value"),
            "line": _float_or_original(line.get("line")),
            "over": _float_or_original(line.get("over")),
            "under": _float_or_original(line.get("under")),
            "push": line.get("push"),
            "suspended": bool(line.get("suspended")),
            "balanced": line.get("balanced"),
            "customBet": bool(stat.get("customBet")),
            "liveCustomBetAvailable": bool(stat.get("liveCustomBetAvailable")),
            "playable": playable,
            "marketId": market.get("id"),
            "lineId": line.get("id"),
            "swishStatId": stat.get("swishStatId"),
            "statId": stat.get("id"),
        }

        if player:
            row.update(
                {
                    "player": player.get("name"),
                    "position": player.get("position"),
                    "playerId": player.get("id"),
                }
            )

        rows.append(row)

    return rows


def _find_or_open_fixture_page(context: Any, fixture_slug: str) -> Any:
    expected = fixture_url(fixture_slug)
    for page in context.pages:
        if fixture_slug in page.url and "stake.com" in page.url:
            if _restricted_region_url(page.url):
                page.goto(expected, wait_until="domcontentloaded", timeout=45_000)
            return page

    page = context.pages[0] if context.pages else context.new_page()
    page.goto(expected, wait_until="domcontentloaded", timeout=45_000)
    return page


def _restricted_region_url(url: str) -> bool:
    return (
        "modal=restrictedRegion" in url
        or "regionKey=US" in url
        or "country=US" in url
    )


def _check_page_ready(page: Any) -> list[str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    warnings: list[str] = []
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        warnings.append("page did not reach networkidle before continuing")

    body = page.locator("body").inner_text(timeout=8_000)
    normalized_body = body.lower()
    if (
        "performing security verification" in normalized_body
        or "protect against malicious bots" in normalized_body
        or "cloudflare" in normalized_body and "verification" in normalized_body
    ):
        raise RuntimeError(
            "Stake Cloudflare verification is required in the helper Chrome session. "
            "Complete the browser verification manually, then retry."
        )
    if "not available in your region" in body:
        raise RuntimeError(
            "Stake is still region-blocked in this browser session. "
            "Turn on the desktop VPN and retry."
        )
    if "Login" in body and "Register" in body and "Wallet" not in body:
        warnings.append(
            "browser appears logged out; read-only SGM data may still load, "
            "but account-only actions will not"
        )
    if "Same Game Multi" not in body:
        raise RuntimeError("Same Game Multi tab is not visible on this fixture page.")

    return warnings


def _fetch_sgm_board_in_browser(page: Any, fixture_slug: str) -> dict[str, Any]:
    result = page.evaluate(
        """
        async ({ query, variables }) => {
          const res = await fetch('/_api/graphql', {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-language': 'en' },
            body: JSON.stringify({ query, variables })
          });
          return { status: res.status, text: await res.text() };
        }
        """,
        {"query": SGM_BOARD_QUERY, "variables": {"fixture": fixture_slug}},
    )

    if result["status"] != 200:
        raise RuntimeError(
            f"Stake SGM replay returned HTTP {result['status']}: "
            f"{result['text'][:300]}"
        )

    data = json.loads(result["text"])
    if data.get("errors"):
        raise RuntimeError(f"Stake SGM replay returned GraphQL errors: {data['errors']}")
    return data


def _float_or_original(value: Any) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return value
