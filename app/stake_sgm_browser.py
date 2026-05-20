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
        warnings = _check_page_ready(page, fixture_slug=fixture_slug)
        response = _fetch_sgm_board_in_browser(page, fixture_slug)
        return normalize_sgm_response(fixture_slug, response, warnings)


def build_stake_sgm_review_slip(
    fixture_slug: str,
    selections: list[dict[str, Any]],
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _find_or_open_fixture_page(browser.contexts[0], fixture_slug)
        warnings = _check_page_ready(page, fixture_slug=fixture_slug)
        response = _fetch_sgm_board_in_browser(page, fixture_slug)
        board = normalize_sgm_response(fixture_slug, response, warnings)
        if _has_logged_out_warning(warnings):
            return _review_slip_result(
                fixture_slug=fixture_slug,
                status="blocked_login_required",
                board=board,
                selected_rows=[],
                missing_selections=[],
                click_results=[],
            )
        match_result = match_sgm_review_selections(board, selections)

        if match_result["missingSelections"]:
            return _review_slip_result(
                fixture_slug=fixture_slug,
                status="blocked_exact_ui_match_failed",
                board=board,
                selected_rows=match_result["matchedRows"],
                missing_selections=match_result["missingSelections"],
                click_results=[],
            )

        click_results = _click_sgm_review_selections(page, match_result["matchedRows"])
        failed_clicks = [row for row in click_results if row.get("status") != "clicked"]
        status = "built_for_review" if not failed_clicks else "blocked_click_failed"
        return _review_slip_result(
            fixture_slug=fixture_slug,
            status=status,
            board=board,
            selected_rows=match_result["matchedRows"],
            missing_selections=[],
            click_results=click_results,
        )


def match_sgm_review_selections(
    board: dict[str, Any],
    selections: list[dict[str, Any]],
    *,
    odds_tolerance: float = 0.000001,
) -> dict[str, list[dict[str, Any]]]:
    source_rows = list(board.get("playerProps") or []) + list(board.get("teamMarkets") or [])
    matched_rows: list[dict[str, Any]] = []
    missing_selections: list[dict[str, Any]] = []

    for selection in selections:
        match = _find_exact_selection_row(
            source_rows,
            selection,
            odds_tolerance=odds_tolerance,
        )
        if match:
            matched_rows.append(match)
        else:
            missing_selections.append(
                {
                    "selection": selection,
                    "reason": "no exact playable UI row matched",
                }
            )

    return {"matchedRows": matched_rows, "missingSelections": missing_selections}


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


def _find_exact_selection_row(
    source_rows: list[dict[str, Any]],
    selection: dict[str, Any],
    *,
    odds_tolerance: float,
) -> dict[str, Any] | None:
    side = str(selection.get("side") or "").strip().lower()
    if side not in {"over", "under"}:
        return None

    selection_line = _float_or_none(selection.get("line"))
    selection_odds = _float_or_none(selection.get("odds"))
    selection_player = _text_key(selection.get("player"))
    selection_team = _text_key(selection.get("team"))
    selection_market = _text_key(selection.get("market"))

    for row in source_rows:
        if not row.get("playable"):
            continue
        if selection_team and selection_team != _text_key(row.get("team")):
            continue
        if selection_player and selection_player != _text_key(row.get("player")):
            continue
        if selection_market and selection_market != _text_key(row.get("market")):
            continue
        if selection_line is None or not _numbers_equal(selection_line, row.get("line")):
            continue
        row_odds = _float_or_none(row.get(side))
        if selection_odds is None or row_odds is None:
            continue
        if abs(selection_odds - row_odds) > odds_tolerance:
            continue

        return {
            "player": row.get("player"),
            "team": row.get("team"),
            "position": row.get("position"),
            "scope": row.get("scope"),
            "market": row.get("market"),
            "side": side,
            "line": row.get("line"),
            "odds": row_odds,
            "playable": bool(row.get("playable")),
            "suspended": bool(row.get("suspended")),
            "customBet": bool(row.get("customBet")),
            "liveCustomBetAvailable": bool(row.get("liveCustomBetAvailable")),
            "playerId": row.get("playerId"),
            "marketId": row.get("marketId"),
            "lineId": row.get("lineId"),
            "swishStatId": row.get("swishStatId"),
        }

    return None


def _click_sgm_review_selections(page: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    click_results: list[dict[str, Any]] = []
    _open_same_game_multi_tab(page)

    for row in rows:
        result = _click_one_sgm_selection(page, row)
        click_results.append(result)
        if result.get("status") != "clicked":
            break
    return click_results


def _open_same_game_multi_tab(page: Any) -> None:
    try:
        for label in ("Same Game Multi", "Same-Game Multi"):
            tab = page.get_by_text(label, exact=True)
            if tab.count():
                tab.first.click(timeout=5_000)
                page.wait_for_timeout(500)
                return
    except Exception:
        # The fixture page may already be on the SGM board, and board validation is
        # still the hard source of truth.
        return


def _click_one_sgm_selection(page: Any, row: dict[str, Any]) -> dict[str, Any]:
    player_or_team = row.get("player") or row.get("team") or ""
    if player_or_team:
        _filter_sgm_board(page, str(player_or_team))
        _expand_sgm_owner(page, str(player_or_team))

    click_result = page.evaluate(
        """
        ({ row, oddsText }) => {
          const norm = (value) => String(value || "")
            .toLowerCase()
            .replace(/[^a-z0-9.]+/g, " ")
            .replace(/\\s+/g, " ")
            .trim();
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden"
              && style.display !== "none"
              && rect.width > 0
              && rect.height > 0;
          };
          const wanted = {
            player: norm(row.player),
            team: norm(row.team),
            market: norm(row.market),
            line: norm(row.line),
            side: norm(row.side),
          };
          const oddsVariants = [String(oddsText), String(oddsText).replace(".", ",")];
          const clickableSelector = "button,[role='button'],[tabindex='0']";
          const candidates = Array.from(document.querySelectorAll(clickableSelector))
            .filter(visible)
            .filter((el) => {
              const text = String(el.innerText || el.textContent || "").trim();
              return oddsVariants.some((odds) => text === odds || text.includes(odds));
            });

          const scopedCandidates = [];
          for (const el of candidates) {
            let current = el;
            for (let depth = 0; depth < 7 && current; depth += 1) {
              const text = norm(current.innerText || current.textContent || "");
              const hasOwner = wanted.player
                ? text.includes(wanted.player)
                : text.includes(wanted.team);
              const hasSide = wanted.side ? text.includes(wanted.side) : true;
              if (hasOwner && hasSide && text.includes(wanted.line)) {
                scopedCandidates.push({ el, text: text.slice(0, 400) });
                break;
              }
              current = current.parentElement;
            }
          }

          if (scopedCandidates.length !== 1) {
            return {
              status: "not_clicked",
              reason: scopedCandidates.length === 0
                ? "no visible exact clickable odds cell found"
                : "ambiguous clickable odds cells found",
              candidateCount: scopedCandidates.length,
              oddsVariants,
              candidateSamples: scopedCandidates.slice(0, 5).map((candidate) => candidate.text),
            };
          }

          scopedCandidates[0].el.click();
          return { status: "clicked", candidateCount: 1 };
        }
        """,
        {"row": row, "oddsText": _display_number(row.get("odds"))},
    )
    return {
        "selection": _compact_click_row(row),
        **click_result,
    }


def _expand_sgm_owner(page: Any, value: str) -> None:
    try:
        owner = page.get_by_text(value, exact=False)
        if owner.count():
            owner.first.click(timeout=3_000)
            page.wait_for_timeout(700)
    except Exception:
        return


def _filter_sgm_board(page: Any, value: str) -> None:
    try:
        search = page.get_by_placeholder("Search")
        if search.count():
            search.first.fill(value, timeout=3_000)
            page.wait_for_timeout(500)
            return
        inputs = page.locator("input")
        if inputs.count():
            inputs.first.fill(value, timeout=3_000)
            page.wait_for_timeout(500)
    except Exception:
        return


def _review_slip_result(
    *,
    fixture_slug: str,
    status: str,
    board: dict[str, Any],
    selected_rows: list[dict[str, Any]],
    missing_selections: list[dict[str, Any]],
    click_results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source": "stake_ui_sgm_build_slip",
        "fixtureSlug": fixture_slug,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reviewOnly": True,
        "clickedLegs": sum(1 for row in click_results if row.get("status") == "clicked"),
        "selectedRows": [_compact_click_row(row) for row in selected_rows],
        "missingSelections": missing_selections,
        "clickResults": click_results,
        "warnings": board.get("warnings") or [],
        "safety": {
            "enteredStakeAmount": False,
            "clickedPlaceBet": False,
        },
    }


def _compact_click_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "player": row.get("player"),
        "team": row.get("team"),
        "market": row.get("market"),
        "side": row.get("side"),
        "line": row.get("line"),
        "odds": row.get("odds"),
        "scope": row.get("scope"),
        "playerId": row.get("playerId"),
        "marketId": row.get("marketId"),
        "lineId": row.get("lineId"),
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


def _check_page_ready(page: Any, fixture_slug: str | None = None) -> list[str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    warnings: list[str] = []
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        warnings.append("page did not reach networkidle before continuing")

    body = page.locator("body").inner_text(timeout=8_000)
    if _is_region_blocked_body(body) and fixture_slug:
        page.goto(fixture_url(fixture_slug), wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            warnings.append("page did not reach networkidle after region-block reload")
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
    if _is_region_blocked_body(body):
        raise RuntimeError(
            "Stake is still region-blocked in this browser session. "
            "Turn on the desktop VPN before starting the helper, close this helper, "
            "then retry."
        )
    if "Login" in body and "Register" in body and "Wallet" not in body:
        warnings.append(
            "browser appears logged out; read-only SGM data may still load, "
            "but account-only actions will not"
        )
    if not _has_same_game_multi_tab(body):
        raise RuntimeError("Same Game Multi tab is not visible on this fixture page.")

    return warnings


def _is_region_blocked_body(body: str) -> bool:
    return "not available in your region" in str(body or "").lower()


def _has_same_game_multi_tab(body: str) -> bool:
    normalized = str(body or "").lower().replace("-", " ")
    return "same game multi" in normalized


def _has_logged_out_warning(warnings: list[str]) -> bool:
    return any("appears logged out" in warning for warning in warnings)


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


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _numbers_equal(left: float, right: Any, tolerance: float = 0.000001) -> bool:
    right_float = _float_or_none(right)
    return right_float is not None and abs(left - right_float) <= tolerance


def _display_number(value: Any) -> str:
    parsed = _float_or_none(value)
    if parsed is None:
        return str(value)
    return f"{parsed:.2f}".rstrip("0").rstrip(".")


def _text_key(value: Any) -> str:
    return " ".join(
        "".join(char.lower() if char.isalnum() else " " for char in str(value or "")).split()
    )
