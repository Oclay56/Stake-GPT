from __future__ import annotations

import json
import re
from hashlib import sha1
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse


DEFAULT_CDP_URL = "http://127.0.0.1:9222"
STAKE_MLB_URL = "https://stake.com/sports/baseball/usa/mlb"

MLB_TEAM_SLUGS = {
    "arizona-diamondbacks": "Arizona Diamondbacks",
    "atlanta-braves": "Atlanta Braves",
    "baltimore-orioles": "Baltimore Orioles",
    "boston-red-sox": "Boston Red Sox",
    "chicago-cubs": "Chicago Cubs",
    "chicago-white-sox": "Chicago White Sox",
    "cincinnati-reds": "Cincinnati Reds",
    "cleveland-guardians": "Cleveland Guardians",
    "colorado-rockies": "Colorado Rockies",
    "detroit-tigers": "Detroit Tigers",
    "houston-astros": "Houston Astros",
    "kansas-city-royals": "Kansas City Royals",
    "los-angeles-angels": "Los Angeles Angels",
    "los-angeles-dodgers": "Los Angeles Dodgers",
    "miami-marlins": "Miami Marlins",
    "milwaukee-brewers": "Milwaukee Brewers",
    "minnesota-twins": "Minnesota Twins",
    "new-york-mets": "New York Mets",
    "new-york-yankees": "New York Yankees",
    "oakland-athletics": "Oakland Athletics",
    "athletics": "Athletics",
    "philadelphia-phillies": "Philadelphia Phillies",
    "pittsburgh-pirates": "Pittsburgh Pirates",
    "san-diego-padres": "San Diego Padres",
    "san-francisco-giants": "San Francisco Giants",
    "seattle-mariners": "Seattle Mariners",
    "st-louis-cardinals": "St. Louis Cardinals",
    "tampa-bay-rays": "Tampa Bay Rays",
    "texas-rangers": "Texas Rangers",
    "toronto-blue-jays": "Toronto Blue Jays",
    "washington-nationals": "Washington Nationals",
}

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


def read_stake_mlb_games(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    limit: int = 50,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _find_or_open_mlb_page(browser.contexts[0])
        warnings = _check_stake_page_access(page)
        games = _extract_mlb_game_links(page, limit=limit)
        if not games:
            warnings.append(
                "No MLB fixture links were visible on the Stake MLB page. "
                "The page may still be loading or Stake may have virtualized the list."
            )
        return {
            "source": "stake_ui_mlb_games",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "url": page.url,
            "returnedGames": len(games),
            "games": games,
            "warnings": warnings,
        }


def read_stake_ui_state(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    fixture_slug: str | None = None,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _diagnostic_page(browser.contexts[0], fixture_slug=fixture_slug)
        return _read_stake_ui_state_from_page(page)


def clear_stake_sgm_selections(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    fixture_slug: str | None = None,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _diagnostic_page(browser.contexts[0], fixture_slug=fixture_slug)
        state_before = _read_stake_ui_state_from_page(page)
        if fixture_slug:
            _open_same_game_multi_tab(page)
        _clear_sgm_working_selection(page)
        state_after = _read_stake_ui_state_from_page(page)
        return {
            "source": "stake_ui_sgm_clear_selections",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "status": "cleared",
            "fixtureSlug": fixture_slug or state_after.get("currentFixtureSlug"),
            "sgmVisible": bool(state_after.get("sgmVisible")),
            "clearedWorkingSelection": True,
            "stateBefore": state_before,
            "stateAfter": state_after,
            "slip": state_after.get("slip") or {},
        }


def remove_stake_sidebar_group(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    fixture_slug: str | None = None,
    matchup: str | None = None,
) -> dict[str, Any]:
    if not fixture_slug and not matchup:
        raise RuntimeError("fixtureSlug or matchup is required to remove a sidebar group.")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _diagnostic_page(browser.contexts[0], fixture_slug=fixture_slug)
        state_before = _read_stake_ui_state_from_page(page)
        target = _sidebar_group_target(fixture_slug=fixture_slug, matchup=matchup)
        remove_result = _remove_sidebar_group_from_page(page, target)
        state_after = _read_stake_ui_state_from_page(page)
        removed = _sidebar_remove_confirmed(
            remove_result=remove_result,
            before_state=state_before.get("slip") or {},
            after_state=state_after.get("slip") or {},
        )
        return {
            "source": "stake_ui_remove_sidebar_group",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "status": "removed" if removed else "not_removed",
            "fixtureSlug": fixture_slug,
            "matchup": target.get("matchup"),
            "teams": target.get("teams") or [],
            "removeResult": remove_result,
            "stateBefore": state_before,
            "stateAfter": state_after,
            "slip": state_after.get("slip") or {},
            "safety": {
                "enteredStakeAmount": False,
                "clickedPlaceBet": False,
                "removedSidebarGroupOnly": True,
            },
        }


def clear_stake_sidebar(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _diagnostic_page(browser.contexts[0], fixture_slug=None)
        state_before = _read_stake_ui_state_from_page(page)
        clear_result = _clear_sidebar_from_page(page)
        state_after = _read_stake_ui_state_from_page(page)
        cleared = _sidebar_clear_confirmed(
            clear_result=clear_result,
            before_state=state_before.get("slip") or {},
            after_state=state_after.get("slip") or {},
        )
        return {
            "source": "stake_ui_clear_sidebar",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "status": "cleared" if cleared else "not_cleared",
            "clearResult": clear_result,
            "stateBefore": state_before,
            "stateAfter": state_after,
            "slip": state_after.get("slip") or {},
            "safety": {
                "enteredStakeAmount": False,
                "clickedPlaceBet": False,
                "clearedEntireSidebar": cleared,
            },
        }


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
        if failed_clicks:
            _clear_sgm_working_selection(page)
        add_bet_result = (
            _click_sgm_add_bet_button(page, expected_legs=len(match_result["matchedRows"]))
            if not failed_clicks
            else {"status": "not_attempted", "reason": "selection_click_failed"}
        )
        status = (
            "built_for_review"
            if not failed_clicks and add_bet_result.get("status") == "clicked"
            else "blocked_add_bet_failed"
            if not failed_clicks
            else "blocked_click_failed"
        )
        return _review_slip_result(
            fixture_slug=fixture_slug,
            status=status,
            board=board,
            selected_rows=match_result["matchedRows"],
            missing_selections=[],
            click_results=click_results,
            add_bet_result=add_bet_result,
        )


def build_stake_sgm_review_slip_batch(
    groups: list[dict[str, Any]],
    *,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        context = browser.contexts[0]
        page = _shared_stake_page(context)
        results: list[dict[str, Any]] = []
        stop_reason: str | None = None

        for group in groups:
            fixture_slug = str(group.get("fixtureSlug") or "").strip()
            if not fixture_slug:
                results.append(
                    {
                        "source": "stake_ui_sgm_build_slip",
                        "status": "blocked_missing_fixture_slug",
                        "reviewOnly": True,
                        "clickedLegs": 0,
                        "request": group,
                        "safety": {
                            "enteredStakeAmount": False,
                            "clickedAddBet": False,
                            "clickedPlaceBet": False,
                        },
                    }
                )
                stop_reason = "missing_fixture_slug"
                break

            page.goto(fixture_url(fixture_slug), wait_until="domcontentloaded", timeout=45_000)
            warnings = _check_page_ready(page, fixture_slug=fixture_slug)
            response = _fetch_sgm_board_in_browser(page, fixture_slug)
            board = normalize_sgm_response(fixture_slug, response, warnings)
            selections = _group_review_selections(group)
            if _has_logged_out_warning(warnings):
                result = _review_slip_result(
                    fixture_slug=fixture_slug,
                    status="blocked_login_required",
                    board=board,
                    selected_rows=[],
                    missing_selections=[],
                    click_results=[],
                )
            else:
                match_result = match_sgm_review_selections(board, selections)
                if match_result["missingSelections"]:
                    result = _review_slip_result(
                        fixture_slug=fixture_slug,
                        status="blocked_exact_ui_match_failed",
                        board=board,
                        selected_rows=match_result["matchedRows"],
                        missing_selections=match_result["missingSelections"],
                        click_results=[],
                    )
                else:
                    click_results = _click_sgm_review_selections(page, match_result["matchedRows"])
                    failed_clicks = [
                        row for row in click_results if row.get("status") != "clicked"
                    ]
                    if failed_clicks:
                        _clear_sgm_working_selection(page)
                    add_bet_result = (
                        _click_sgm_add_bet_button(
                            page,
                            expected_legs=len(match_result["matchedRows"]),
                        )
                        if not failed_clicks
                        else {"status": "not_attempted", "reason": "selection_click_failed"}
                    )
                    status = (
                        "built_for_review"
                        if not failed_clicks and add_bet_result.get("status") == "clicked"
                        else "blocked_add_bet_failed"
                        if not failed_clicks
                        else "blocked_click_failed"
                    )
                    result = _review_slip_result(
                        fixture_slug=fixture_slug,
                        status=status,
                        board=board,
                        selected_rows=match_result["matchedRows"],
                        missing_selections=[],
                        click_results=click_results,
                        add_bet_result=add_bet_result,
                    )

            result["matchup"] = group.get("matchup")
            results.append(result)
            if result.get("status") != "built_for_review":
                stop_reason = str(result.get("status") or "blocked")
                break

        clicked_groups = sum(1 for result in results if result.get("status") == "built_for_review")
        clicked_legs = sum(int(result.get("clickedLegs") or 0) for result in results)
        status = (
            "built_for_review"
            if clicked_groups == len(groups) and not stop_reason
            else "partial_review_slip"
            if clicked_groups
            else "blocked"
        )
        return {
            "source": "stake_ui_sgm_review_slip_batch",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "reviewOnly": True,
            "fixtureCount": len(groups),
            "processedGroups": len(results),
            "clickedGroups": clicked_groups,
            "clickedLegs": clicked_legs,
            "stopReason": stop_reason,
            "groups": results,
            "safety": {
                "enteredStakeAmount": False,
                "clickedAddBet": bool(clicked_groups),
                "clickedPlaceBet": False,
            },
        }


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
        match = _find_selection_row_by_row_id(
            source_rows,
            str(board.get("fixtureSlug") or ""),
            selection,
        )
        if match:
            matched_rows.append(match)
            continue

        match = _find_exact_selection_row(
            source_rows,
            selection,
            fixture_slug=str(board.get("fixtureSlug") or ""),
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


def _group_review_selections(group: dict[str, Any]) -> list[dict[str, Any]]:
    raw_selections = group.get("selections")
    selections = list(raw_selections) if isinstance(raw_selections, list) else []
    raw_row_ids = group.get("rowIds") or group.get("row_ids")
    if raw_row_ids is not None and not isinstance(raw_row_ids, list):
        return selections
    for row_id in raw_row_ids or []:
        if str(row_id or "").strip():
            selections.append({"rowId": str(row_id).strip()})
    return selections


def _find_selection_row_by_row_id(
    source_rows: list[dict[str, Any]],
    fixture_slug: str,
    selection: dict[str, Any],
) -> dict[str, Any] | None:
    row_id = str(
        selection.get("rowId")
        or selection.get("row_id")
        or (
            selection.get("selectionId")
            if str(selection.get("selectionId") or "").startswith("sgm_")
            else ""
        )
        or ""
    ).strip()
    if not row_id:
        return None

    for row in source_rows:
        if not row.get("playable"):
            continue
        for side in ("over", "under"):
            if row.get(side) is None:
                continue
            current_row_id = make_sgm_selection_row_id(fixture_slug, row, side)
            if current_row_id == row_id:
                return _matched_selection_row(row, side, current_row_id)

    return None


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
    fixture_slug: str,
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

        return _matched_selection_row(
            row,
            side,
            make_sgm_selection_row_id(fixture_slug, row, side),
        )

    return None


def _matched_selection_row(row: dict[str, Any], side: str, row_id: str) -> dict[str, Any]:
    return {
        "rowId": row_id,
        "player": row.get("player"),
        "team": row.get("team"),
        "position": row.get("position"),
        "scope": row.get("scope"),
        "market": row.get("market"),
        "side": side,
        "line": row.get("line"),
        "odds": _float_or_none(row.get(side)),
        "playable": bool(row.get("playable")),
        "suspended": bool(row.get("suspended")),
        "customBet": bool(row.get("customBet")),
        "liveCustomBetAvailable": bool(row.get("liveCustomBetAvailable")),
        "playerId": row.get("playerId"),
        "marketId": row.get("marketId"),
        "lineId": row.get("lineId"),
        "swishStatId": row.get("swishStatId"),
    }


def _click_sgm_review_selections(page: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    click_results: list[dict[str, Any]] = []
    _open_same_game_multi_tab(page)
    _clear_sgm_working_selection(page)

    for row in rows:
        result = _click_one_sgm_selection(page, row)
        click_results.append(result)
        if result.get("status") != "clicked":
            break
    return click_results


def _click_sgm_add_bet_button(page: Any, *, expected_legs: int) -> dict[str, Any]:
    try:
        before_state = _read_bet_slip_state(page)
        sticky_result = _click_custom_bet_sticky_add(page, before_state=before_state)
        if sticky_result.get("status") == "clicked":
            return sticky_result

        result = page.evaluate(
            """
            async ({ expectedLegs }) => {
              const norm = (value) => String(value || "")
                .replace(/[üÜ]/g, "u")
                .toLowerCase()
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
              const disabled = (el) => Boolean(el.disabled)
                || el.getAttribute("aria-disabled") === "true"
                || el.classList.contains("disabled");
              const ancestorText = (el, depthLimit = 8) => {
                let current = el;
                const parts = [];
                for (let depth = 0; depth < depthLimit && current; depth += 1) {
                  parts.push(norm(current.innerText || current.textContent || ""));
                  current = current.parentElement;
                }
                return parts.join(" ");
              };
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              let buttons = [];
              let candidates = [];
              let candidate = null;
              for (let attempt = 0; attempt < 24 && !candidate; attempt += 1) {
                buttons = Array.from(document.querySelectorAll("button,[role='button']"))
                  .filter(visible)
                  .filter((el) => norm(el.innerText || el.textContent || "").includes("add bet"));
                candidates = buttons
                  .map((el) => {
                  const rect = el.getBoundingClientRect();
                  const context = ancestorText(el);
                  const selectedOutcomeCount = Array.from(document.querySelectorAll('button[data-testid="fixture-outcome"]'))
                    .filter(visible)
                    .filter((button) => {
                      const text = norm(button.innerText || button.textContent || "");
                      const classText = norm(button.className || "");
                      const ariaPressed = button.getAttribute("aria-pressed") === "true";
                      const ariaSelected = button.getAttribute("aria-selected") === "true";
                      const isBlue = window.getComputedStyle(button).backgroundColor.includes("33, 126, 226")
                        || window.getComputedStyle(button).backgroundColor.includes("29, 110, 201");
                      return (text.includes("over") || text.includes("under") || text.includes("uber") || text.includes("über") || text.includes("unter"))
                        && (ariaPressed || ariaSelected || classText.includes("active") || classText.includes("selected") || isBlue);
                    }).length;
                  return {
                    el,
                    disabled: disabled(el),
                    text: String(el.innerText || el.textContent || "").trim(),
                    context,
                    selectedOutcomeCount,
                    score:
                      (context.includes("total odds") ? 100 : 0)
                      + (context.includes("clear all") ? 50 : 0)
                      + (selectedOutcomeCount >= expectedLegs ? 20 : 0)
                      - Math.round(rect.y / 1000),
                    rect: {
                      x: Math.round(rect.x),
                      y: Math.round(rect.y),
                      width: Math.round(rect.width),
                      height: Math.round(rect.height),
                    },
                  };
                })
                .sort((a, b) => b.score - a.score);

                candidate = candidates.find((item) => !item.disabled);
                if (!candidate) {
                  await sleep(250);
                }
              }
              if (!candidate) {
                return {
                  status: "not_clicked",
                  reason: buttons.length ? "add_bet_button_disabled" : "add_bet_button_not_found",
                  candidateCount: candidates.length,
                  candidateSamples: candidates.slice(0, 5).map((item) => ({
                    text: item.text,
                    disabled: item.disabled,
                    selectedOutcomeCount: item.selectedOutcomeCount,
                    rect: item.rect,
                  })),
                };
              }

              candidate.el.scrollIntoView({ block: "center", inline: "center" });
              candidate.el.click();
              return {
                status: "clicked",
                clickedText: candidate.text,
                clickedRect: candidate.rect,
                selectedOutcomeCount: candidate.selectedOutcomeCount,
                expectedLegs,
              };
            }
            """,
            {"expectedLegs": expected_legs},
        )
        page.wait_for_timeout(1_000)
        result["postClick"] = _read_bet_slip_state(page)
        result["beforeClick"] = before_state
        result["addBetConfirmed"] = _add_bet_confirmed(before_state, result["postClick"])
        if result.get("status") == "clicked" and not result["addBetConfirmed"]:
            return {
                "status": "not_clicked",
                "reason": "add_bet_click_did_not_update_sidebar",
                "initialStickyClick": sticky_result,
                "initialAddBetClick": result,
                "postClick": result["postClick"],
            }
        return result
    except Exception as exc:
        return {"status": "not_clicked", "reason": str(exc)}


def _click_custom_bet_sticky_add(
    page: Any,
    *,
    before_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        button = page.locator("#custom-bet-sticky-add")
        if not button.count():
            return {"status": "not_clicked", "reason": "custom_bet_sticky_add_not_found"}
        button.first.scroll_into_view_if_needed(timeout=3_000)
        button.first.click(timeout=5_000)
        post_click: dict[str, Any] = {}
        for _ in range(16):
            page.wait_for_timeout(250)
            post_click = _read_bet_slip_state(page)
            if _add_bet_confirmed(before_state or {}, post_click):
                return {
                    "status": "clicked",
                    "clickedText": "custom-bet-sticky-add",
                    "clickedBy": "playwright_locator",
                    "addBetConfirmed": True,
                    "beforeClick": before_state or {},
                    "postClick": post_click,
                }
        return {
            "status": "not_clicked",
            "reason": "custom_bet_sticky_add_did_not_update_bet_slip",
            "clickedText": "custom-bet-sticky-add",
            "clickedBy": "playwright_locator",
            "addBetConfirmed": False,
            "beforeClick": before_state or {},
            "postClick": post_click,
        }
    except Exception as exc:
        return {"status": "not_clicked", "reason": f"custom_bet_sticky_add_click_failed: {exc}"}


def _add_bet_confirmed(before_state: dict[str, Any], after_state: dict[str, Any]) -> bool:
    if not after_state or after_state.get("rightPanelEmpty", True):
        return False
    if before_state.get("rightPanelEmpty", True):
        return True

    before_count = _int_or_none(before_state.get("rightPanelSelectionCount")) or 0
    after_count = _int_or_none(after_state.get("rightPanelSelectionCount")) or 0
    if after_count > before_count:
        return True

    before_digest = str(before_state.get("rightPanelTextDigest") or "")
    after_digest = str(after_state.get("rightPanelTextDigest") or "")
    before_length = _int_or_none(before_state.get("rightPanelTextLength")) or 0
    after_length = _int_or_none(after_state.get("rightPanelTextLength")) or 0
    return bool(after_digest and after_digest != before_digest and after_length > before_length + 10)


def _read_bet_slip_state(page: Any) -> dict[str, Any]:
    try:
        return dict(
            page.evaluate(
                """
                () => {
                  const norm = (value) => String(value || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/\\s+/g, " ")
                    .trim();
                  const bodyText = norm(document.body.innerText || document.body.textContent || "");
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== "hidden"
                      && style.display !== "none"
                      && rect.width > 0
                      && rect.height > 0;
                  };
                  const emptyPhrases = [
                    "bet slip is empty",
                    "betting slip is empty",
                    "wettschein ist leer",
                  ];
                  const hasEmptyPhrase = (text) => emptyPhrases.some((phrase) => text.includes(phrase));
                  const textDigest = (text) => {
                    let hash = 0;
                    for (let index = 0; index < text.length; index += 1) {
                      hash = ((hash << 5) - hash + text.charCodeAt(index)) | 0;
                    }
                    return String(hash);
                  };
                  const rightPanel = document.querySelector("#right-sidebar") || Array.from(document.querySelectorAll("aside,[role='complementary'],body *"))
                    .filter(visible)
                    .find((el) => {
                      const rect = el.getBoundingClientRect();
                      const text = norm(el.innerText || el.textContent || "");
                      return rect.width >= 220
                        && rect.x > window.innerWidth * 0.55
                        && (text.includes("bet slip") || text.includes("betting slip") || text.includes("wettschein"));
                    });
                  const panelText = rightPanel ? norm(rightPanel.innerText || rightPanel.textContent || "") : "";
                  const selectionWords = panelText.match(/\\b(over|under|above|below|uber|unter|mehr|weniger)\\b/g) || [];
                  return {
                    betSlipEmpty: hasEmptyPhrase(bodyText),
                    rightPanelFound: Boolean(rightPanel),
                    rightPanelEmpty: rightPanel ? hasEmptyPhrase(panelText) : true,
                    rightPanelHasTotalStake: panelText.includes("total stake") || panelText.includes("total deployment"),
                    rightPanelHasPlaceBet: panelText.includes("place bet") || panelText.includes("placing bets"),
                    rightPanelSelectionCount: selectionWords.length,
                    rightPanelTextDigest: textDigest(panelText),
                    rightPanelTextLength: panelText.length,
                    rightPanelTextSample: panelText.slice(0, 260),
                  };
                }
                """
            )
        )
    except Exception:
        return {}


def _sidebar_group_target(
    *,
    fixture_slug: str | None,
    matchup: str | None,
) -> dict[str, Any]:
    fixture_matchup = _fixture_matchup_from_slug(fixture_slug) if fixture_slug else {}
    target_matchup = str(matchup or fixture_matchup.get("matchup") or "").strip()
    teams = list(fixture_matchup.get("teams") or [])
    if target_matchup and len(teams) < 2:
        parts = [
            part.strip()
            for part in re.split(
                r"\s+(?:vs\.?|v\.?|versus)\s+|\s+-\s+",
                target_matchup,
                flags=re.IGNORECASE,
            )
            if part.strip()
        ]
        if len(parts) >= 2:
            teams = [parts[0], parts[1]]
    return {
        "fixtureSlug": fixture_slug,
        "matchup": target_matchup,
        "teams": teams[:2],
    }


def _remove_sidebar_group_from_page(page: Any, target: dict[str, Any]) -> dict[str, Any]:
    try:
        result = page.evaluate(
            """
            async ({ fixtureSlug, matchup, teams }) => {
              const norm = (value) => String(value || "")
                .normalize("NFD")
                .replace(/[\\u0300-\\u036f]/g, "")
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, " ")
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
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const textDigest = (text) => {
                let hash = 0;
                for (let index = 0; index < text.length; index += 1) {
                  hash = ((hash << 5) - hash + text.charCodeAt(index)) | 0;
                }
                return String(hash);
              };
              const rightPanel = document.querySelector("#right-sidebar") || Array.from(document.querySelectorAll("aside,[role='complementary'],body *"))
                .filter(visible)
                .find((el) => {
                  const rect = el.getBoundingClientRect();
                  const text = norm(el.innerText || el.textContent || "");
                  return rect.width >= 220
                    && rect.x > window.innerWidth * 0.55
                    && (text.includes("bet slip") || text.includes("betting slip") || text.includes("wettschein"));
                });
              if (!rightPanel) {
                return { status: "not_removed", reason: "right_panel_missing" };
              }

              const aliasesForTeam = (team) => {
                const value = norm(team);
                if (!value) return [];
                const parts = value.split(" ").filter(Boolean);
                const aliases = [value];
                if (value.startsWith("new york ") && parts.length > 2) {
                  aliases.push(`ny ${parts.slice(2).join(" ")}`);
                }
                if (parts.length >= 2) {
                  aliases.push(parts.slice(-2).join(" "));
                }
                if (parts.length >= 1) {
                  aliases.push(parts[parts.length - 1]);
                }
                return Array.from(new Set(aliases.filter((item) => item.length >= 3)));
              };
              const teamAliases = Array.isArray(teams)
                ? teams.map(aliasesForTeam).filter((aliases) => aliases.length)
                : [];
              const targetText = norm(matchup);
              const matchesTarget = (text) => {
                const value = norm(text);
                if (teamAliases.length >= 2) {
                  return teamAliases.every((aliases) => aliases.some((alias) => value.includes(alias)));
                }
                return targetText.length >= 6 && value.includes(targetText.replace(/\\bvs\\b/g, " "));
              };
              const nearestClickable = (el) => {
                let current = el;
                for (let depth = 0; depth < 4 && current; depth += 1) {
                  const tag = String(current.tagName || "").toLowerCase();
                  const role = current.getAttribute("role") || "";
                  if (tag === "button" || role === "button") {
                    return current;
                  }
                  current = current.parentElement;
                }
                return el;
              };
              const removeButtonFor = (container) => {
                const crect = container.getBoundingClientRect();
                const raw = Array.from(container.querySelectorAll("button,[role='button'],[aria-label],svg"))
                  .map(nearestClickable)
                  .filter((el, index, items) => items.indexOf(el) === index)
                  .filter(visible)
                  .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const text = norm(`${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${el.innerText || el.textContent || ""}`);
                    const looksRemove = text === "x"
                      || text === "close"
                      || text.includes("remove")
                      || text.includes("delete")
                      || text.includes("clear")
                      || text.includes("close");
                    const topRightScore =
                      ((rect.x - crect.x) / Math.max(crect.width, 1)) * 100
                      - ((rect.y - crect.y) / Math.max(crect.height, 1)) * 25;
                    return { el, text, looksRemove, rect, topRightScore };
                  })
                  .filter((item) => item.looksRemove || item.rect.x > crect.x + crect.width * 0.65);
                raw.sort((a, b) => b.topRightScore - a.topRightScore);
                return raw[0] || null;
              };

              const panelTextBefore = norm(rightPanel.innerText || rightPanel.textContent || "");
              const starts = Array.from(rightPanel.querySelectorAll("*"))
                .filter(visible)
                .filter((el) => matchesTarget(el.innerText || el.textContent || ""));
              const candidates = [];
              for (const start of starts) {
                let current = start;
                for (let depth = 0; depth < 8 && current && current !== rightPanel.parentElement; depth += 1) {
                  if (!visible(current) || !matchesTarget(current.innerText || current.textContent || "")) {
                    current = current.parentElement;
                    continue;
                  }
                  const rect = current.getBoundingClientRect();
                  if (current === rightPanel || rect.height > rightPanel.getBoundingClientRect().height * 0.9) {
                    current = current.parentElement;
                    continue;
                  }
                  const remove = removeButtonFor(current);
                  if (remove) {
                    candidates.push({
                      container: current,
                      button: remove.el,
                      buttonText: remove.text,
                      area: rect.width * rect.height,
                      textLength: String(current.innerText || current.textContent || "").length,
                      sample: String(current.innerText || current.textContent || "").trim().replace(/\\s+/g, " ").slice(0, 220),
                      rect: {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                      },
                    });
                  }
                  current = current.parentElement;
                }
              }
              candidates.sort((a, b) => (a.area - b.area) || (a.textLength - b.textLength));
              if (!candidates.length) {
                return {
                  status: "not_removed",
                  reason: "sidebar_group_not_found",
                  target: { fixtureSlug, matchup, teams },
                  targetMatchedInPanel: matchesTarget(panelTextBefore),
                  rightPanelTextDigest: textDigest(panelTextBefore),
                  rightPanelTextSample: panelTextBefore.slice(0, 260),
                };
              }

              const selected = candidates[0];
              selected.button.scrollIntoView({ block: "center", inline: "center" });
              selected.button.click();
              await sleep(800);
              const panelTextAfter = norm(rightPanel.innerText || rightPanel.textContent || "");
              return {
                status: "clicked",
                target: { fixtureSlug, matchup, teams },
                candidateCount: candidates.length,
                clickedButtonText: selected.buttonText,
                clickedGroupSample: selected.sample,
                clickedGroupRect: selected.rect,
                targetStillVisible: matchesTarget(panelTextAfter),
                sidebarDigestBefore: textDigest(panelTextBefore),
                sidebarDigestAfter: textDigest(panelTextAfter),
              };
            }
            """,
            target,
        )
        page.wait_for_timeout(300)
        return dict(result or {})
    except Exception as exc:
        return {"status": "not_removed", "reason": str(exc)}


def _sidebar_remove_confirmed(
    *,
    remove_result: dict[str, Any],
    before_state: dict[str, Any],
    after_state: dict[str, Any],
) -> bool:
    if remove_result.get("status") != "clicked":
        return False
    if remove_result.get("targetStillVisible") is False:
        return True

    before_digest = str(before_state.get("rightPanelTextDigest") or "")
    after_digest = str(after_state.get("rightPanelTextDigest") or "")
    before_length = _int_or_none(before_state.get("rightPanelTextLength")) or 0
    after_length = _int_or_none(after_state.get("rightPanelTextLength")) or 0
    before_count = _int_or_none(before_state.get("rightPanelSelectionCount")) or 0
    after_count = _int_or_none(after_state.get("rightPanelSelectionCount")) or 0
    if before_count and after_count < before_count:
        return True
    return bool(before_digest and before_digest != after_digest and after_length + 10 < before_length)


def _clear_sidebar_from_page(page: Any) -> dict[str, Any]:
    try:
        result = page.evaluate(
            """
            async () => {
              const norm = (value) => String(value || "")
                .normalize("NFD")
                .replace(/[\\u0300-\\u036f]/g, "")
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, " ")
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
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const textDigest = (text) => {
                let hash = 0;
                for (let index = 0; index < text.length; index += 1) {
                  hash = ((hash << 5) - hash + text.charCodeAt(index)) | 0;
                }
                return String(hash);
              };
              const rightPanel = document.querySelector("#right-sidebar") || Array.from(document.querySelectorAll("aside,[role='complementary'],body *"))
                .filter(visible)
                .find((el) => {
                  const rect = el.getBoundingClientRect();
                  const text = norm(el.innerText || el.textContent || "");
                  return rect.width >= 220
                    && rect.x > window.innerWidth * 0.55
                    && (text.includes("bet slip") || text.includes("betting slip") || text.includes("wettschein"));
                });
              if (!rightPanel) {
                return { status: "not_cleared", reason: "right_panel_missing" };
              }

              const panelTextBefore = norm(rightPanel.innerText || rightPanel.textContent || "");
              const emptyPhrases = [
                "bet slip is empty",
                "betting slip is empty",
                "wettschein ist leer",
              ];
              if (emptyPhrases.some((phrase) => panelTextBefore.includes(phrase))) {
                return {
                  status: "already_empty",
                  sidebarDigestBefore: textDigest(panelTextBefore),
                };
              }

              const buttonCandidates = Array.from(rightPanel.querySelectorAll("button,[role='button']"))
                .filter(visible)
                .map((el) => {
                  const text = norm(`${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${el.innerText || el.textContent || ""}`);
                  const rect = el.getBoundingClientRect();
                  return {
                    el,
                    text,
                    y: rect.y,
                    area: rect.width * rect.height,
                    disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
                  };
                })
                .filter((item) => !item.disabled)
                .filter((item) => {
                  if (!item.text) return false;
                  if (item.text.includes("place bet") || item.text.includes("placing bets")) return false;
                  return item.text === "clear bet"
                    || item.text === "clear bets"
                    || item.text === "delete bet"
                    || item.text === "delete bets"
                    || item.text.includes("clear bet")
                    || item.text.includes("delete bet")
                    || item.text.includes("remove all bets");
                });
              if (!buttonCandidates.length) {
                return {
                  status: "not_cleared",
                  reason: "clear_button_not_found",
                  rightPanelTextDigest: textDigest(panelTextBefore),
                  rightPanelTextSample: panelTextBefore.slice(0, 260),
                };
              }

              buttonCandidates.sort((a, b) => (b.y - a.y) || (b.area - a.area));
              const selected = buttonCandidates[0];
              selected.el.scrollIntoView({ block: "center", inline: "center" });
              selected.el.click();
              await sleep(900);
              const panelTextAfter = norm(rightPanel.innerText || rightPanel.textContent || "");
              return {
                status: "clicked",
                clickedButtonText: selected.text,
                sidebarDigestBefore: textDigest(panelTextBefore),
                sidebarDigestAfter: textDigest(panelTextAfter),
              };
            }
            """
        )
        page.wait_for_timeout(300)
        return dict(result or {})
    except Exception as exc:
        return {"status": "not_cleared", "reason": str(exc)}


def _sidebar_clear_confirmed(
    *,
    clear_result: dict[str, Any],
    before_state: dict[str, Any],
    after_state: dict[str, Any],
) -> bool:
    if clear_result.get("status") == "already_empty":
        return bool(after_state.get("rightPanelEmpty", True))
    if clear_result.get("status") != "clicked":
        return False

    after_count = _int_or_none(after_state.get("rightPanelSelectionCount"))
    if after_state.get("rightPanelEmpty") is True:
        return True
    if after_count == 0:
        return True

    before_digest = str(before_state.get("rightPanelTextDigest") or "")
    after_digest = str(after_state.get("rightPanelTextDigest") or "")
    before_length = _int_or_none(before_state.get("rightPanelTextLength")) or 0
    after_length = _int_or_none(after_state.get("rightPanelTextLength")) or 0
    return bool(before_digest and after_digest != before_digest and after_length + 10 < before_length)


def _clear_sgm_working_selection(page: Any) -> None:
    try:
        page.evaluate(
            """
            () => {
              const norm = (value) => String(value || "")
                .toLowerCase()
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
              const button = Array.from(document.querySelectorAll("button,[role='button']"))
                .filter(visible)
                .find((el) => norm(el.innerText || el.textContent || "") === "remove all");
              if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") {
                button.click();
              }
            }
            """
        )
        page.wait_for_timeout(300)
    except Exception:
        return


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
    player_or_team = "" if row.get("scope") == "match_props" else row.get("player") or row.get("team") or ""
    click_row = {
        **row,
        "marketAliases": _market_display_aliases(str(row.get("market") or "")),
    }
    if player_or_team:
        _filter_sgm_board(page, str(player_or_team))
        _expand_sgm_owner(page, str(player_or_team))
    elif row.get("market"):
        _filter_sgm_board(page, _market_search_text(str(row.get("market"))))
        _expand_sgm_market(page, str(row.get("market")))

    click_result = page.evaluate(
        """
        async ({ row, oddsText }) => {
          const norm = (value) => String(value || "")
            .replace(/[üÜ]/g, "u")
            .toLowerCase()
            .replace(/[^a-z0-9.]+/g, " ")
            .replace(/\\s+/g, " ")
            .trim();
          const numberValue = (value) => {
            const parsed = Number(String(value || "").replace(",", "."));
            return Number.isFinite(parsed) ? parsed : null;
          };
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
            scope: norm(row.scope),
          };
          const sideAliases = wanted.side === "under"
            ? ["under", "unter"]
            : wanted.side === "over"
            ? ["over", "uber", "über"]
            : [wanted.side].filter(Boolean);
          const oppositeSideAliases = wanted.side === "under"
            ? ["over", "uber"]
            : wanted.side === "over"
            ? ["under", "unter"]
            : [];
          const marketAliases = {
            "earned runs": ["earned runs", "runs achieved", "runs allowed"],
            "failed attempts": ["failed attempts", "strikeouts"],
            "first er": ["first er", "first earned run", "first well deserved run"],
            "first so": ["first so", "first strike out", "first strikeout"],
            "hits allowed": ["hits allowed"],
            "match home runs": ["match home runs", "play home runs", "home runs"],
            "match singles": ["match singles", "singles"],
            "match triples": ["match triples", "triples"],
            "outs": ["outs", "eliminated"],
            "rbi": ["rbi", "rbis", "runs batted in"],
            "strikeouts": ["strikeouts", "failed attempts"],
            "team hits": ["team hits", "hits"],
            "team rbi": ["team rbi", "team rbis", "rbi", "rbis", "runs batted in"],
            "team rbis": ["team rbis", "team rbi", "rbi", "rbis", "runs batted in"],
            "team runs": ["team runs", "runs"],
            "team total bases": ["team total bases", "total bases"],
            "walks": ["walks"],
            "win probability": ["win probability", "probability of winning"],
          };
          const aliases = Array.isArray(row.marketAliases) && row.marketAliases.length
            ? row.marketAliases.map(norm).filter(Boolean)
            : (marketAliases[wanted.market] || [wanted.market]).filter(Boolean);
          const targetOdds = numberValue(row.odds) ?? numberValue(row[wanted.side]) ?? numberValue(oddsText);
          const targetLine = numberValue(row.line);
          const oddsVariants = [
            String(oddsText),
            String(oddsText).replace(".", ","),
            targetOdds == null ? "" : targetOdds.toFixed(2),
            targetOdds == null ? "" : targetOdds.toFixed(2).replace(".", ","),
          ].filter(Boolean);
          const textHasNumber = (text, target, tolerance) => {
            if (target == null) {
              return false;
            }
            const matches = String(text || "").match(/\\d+(?:[.,]\\d+)?/g) || [];
            return matches.some((value) => {
              const parsed = numberValue(value);
              return parsed != null && Math.abs(parsed - target) <= tolerance;
            });
          };
          const textHasLine = (text) => (
            wanted.line ? text.includes(wanted.line) : true
          ) || textHasNumber(text, targetLine, 0.001);
          const rowHasMarket = (text) => !aliases.length || aliases.some((alias) => text.includes(alias));
          const buttonOdds = (text) => {
            const matches = String(text || "").match(/\\d+(?:[.,]\\d+)?/g) || [];
            const values = matches.map(numberValue).filter((value) => value != null);
            return values.length ? values[values.length - 1] : null;
          };
          const directButtonSide = (el) => {
            const text = norm(`${el.getAttribute("aria-label") || ""} ${el.innerText || el.textContent || ""}`);
            const wantedSide = sideAliases.some((side) => text.includes(side));
            const oppositeSide = oppositeSideAliases.some((side) => text.includes(side));
            return {
              text,
              hasSide: wantedSide || oppositeSide,
              matchesWanted: wantedSide && !oppositeSide,
            };
          };
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const candidateElements = () => {
            const sideButtons = Array.from(document.querySelectorAll("button[data-testid='fixture-outcome']"))
              .filter(visible);
            if (sideButtons.length) {
              return sideButtons;
            }
            return Array.from(document.querySelectorAll("button,[role='button'],[tabindex='0'],body *"))
              .filter(visible)
              .filter((el) => {
                const rect = el.getBoundingClientRect();
                const text = String(el.innerText || el.textContent || "").trim();
                return rect.width <= 360
                  && rect.height <= 100
                  && wanted.side
                  && sideAliases.some((side) => norm(text).includes(side));
              });
          };

          let scopedCandidates = [];
          let lastCandidateSamples = [];
          for (let attempt = 0; attempt < 24; attempt += 1) {
            const candidates = candidateElements();
            lastCandidateSamples = candidates.slice(0, 8).map((el) => String(el.innerText || el.textContent || "").trim());
            scopedCandidates = [];
            for (const el of candidates) {
              const buttonSide = directButtonSide(el);
              if (buttonSide.hasSide && !buttonSide.matchesWanted) {
                continue;
              }
              let current = el;
              let rowContainer = null;
              let matchedText = "";
              const leafText = String(el.innerText || el.textContent || "").trim();
              const clickedOdds = buttonOdds(leafText);
              let lineSideMatched = false;
              let marketMatched = false;
              let combinedText = "";
              for (let depth = 0; depth < 13 && current; depth += 1) {
                const rect = current.getBoundingClientRect();
                const text = norm(current.innerText || current.textContent || "");
                const hasSide = buttonSide.hasSide
                  ? buttonSide.matchesWanted
                  : sideAliases.length
                  ? sideAliases.some((side) => text.includes(side))
                  : true;
                if (depth <= 2 && hasSide && textHasLine(text)) {
                  lineSideMatched = true;
                }
                if (rowHasMarket(text) && rect.height <= 180) {
                  marketMatched = true;
                }
                combinedText = `${text} ${combinedText}`.slice(0, 1000);
                if (lineSideMatched && marketMatched) {
                  rowContainer = current;
                  matchedText = combinedText.slice(0, 500);
                  break;
                }
                current = current.parentElement;
              }
              if (!rowContainer) {
                continue;
              }

              let ownerMatched = wanted.scope === "match props" || wanted.scope === "match_props";
              current = rowContainer;
              for (let depth = 0; depth < 16 && current && !ownerMatched; depth += 1) {
                const text = norm(current.innerText || current.textContent || "");
                ownerMatched = wanted.player
                  ? text.includes(wanted.player)
                  : wanted.team
                  ? text.includes(wanted.team)
                  : true;
                current = current.parentElement;
              }
              if (ownerMatched) {
                const rect = el.getBoundingClientRect();
                scopedCandidates.push({
                  el,
                  text: matchedText,
                  leafText,
                  clickedOdds,
                  requestedOdds: targetOdds,
                  oddsChanged: targetOdds != null && clickedOdds != null
                    ? Math.abs(targetOdds - clickedOdds) > 0.006
                    : false,
                  area: rect.width * rect.height,
                  rect: {
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                  },
                });
                  break;
              }
            }
            scopedCandidates.sort((a, b) => a.area - b.area);
            if (scopedCandidates.length > 0) {
              break;
            }
            await sleep(250);
          }

          if (scopedCandidates.length < 1) {
            return {
              status: "not_clicked",
              reason: "no visible exact clickable selection button found",
              candidateCount: scopedCandidates.length,
              oddsVariants,
              marketAliases: aliases,
              matchedBy: "player_or_scope_market_line_side",
              candidateSamples: lastCandidateSamples,
            };
          }

          scopedCandidates[0].el.scrollIntoView({ block: "center", inline: "center" });
          scopedCandidates[0].el.click();
          return {
            status: "clicked",
            candidateCount: scopedCandidates.length,
            clickedSample: scopedCandidates[0].text,
            clickedLeafText: scopedCandidates[0].leafText,
            clickedOdds: scopedCandidates[0].clickedOdds,
            requestedOdds: scopedCandidates[0].requestedOdds,
            oddsChanged: scopedCandidates[0].oddsChanged,
            clickedRect: scopedCandidates[0].rect,
          };
        }
        """,
        {"row": click_row, "oddsText": _display_number(row.get("odds"))},
    )
    return {
        "selection": _compact_click_row(row),
        **click_result,
    }


def _expand_sgm_owner(page: Any, value: str) -> None:
    try:
        if _sgm_owner_has_visible_outcomes(page, value):
            return
        owner = page.get_by_text(value, exact=False)
        if owner.count():
            owner.first.click(timeout=3_000)
            for _ in range(10):
                page.wait_for_timeout(250)
                if _sgm_owner_has_visible_outcomes(page, value):
                    return
    except Exception:
        return


def _sgm_owner_has_visible_outcomes(page: Any, value: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                (value) => {
                  const norm = (input) => String(input || "")
                    .toLowerCase()
                    .replace(/[^a-z0-9.]+/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim();
                  const wanted = norm(value);
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== "hidden"
                      && style.display !== "none"
                      && rect.width > 0
                      && rect.height > 0;
                  };
                  const outcomes = Array.from(document.querySelectorAll('button[data-testid="fixture-outcome"]'))
                    .filter(visible);
                  return outcomes.some((button) => {
                    let current = button;
                    for (let depth = 0; depth < 16 && current; depth += 1) {
                      const text = norm(current.innerText || current.textContent || "");
                      if (text.includes(wanted)) {
                        return true;
                      }
                      current = current.parentElement;
                    }
                    return false;
                  });
                }
                """,
                value,
            )
        )
    except Exception:
        return False


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


def _expand_sgm_market(page: Any, value: str) -> None:
    try:
        result = page.evaluate(
            """
            (aliases) => {
              const norm = (input) => String(input || "")
                .replace(/[üÜ]/g, "u")
                .toLowerCase()
                .replace(/[^a-z0-9.]+/g, " ")
                .replace(/\\s+/g, " ")
                .trim();
              const wanted = aliases.map(norm).filter(Boolean);
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== "hidden"
                  && style.display !== "none"
                  && rect.width > 0
                  && rect.height > 0;
              };
              const headers = Array.from(document.querySelectorAll(".secondary-accordion,.header,button,[role='button']"))
                .filter(visible)
                .map((el) => {
                  const rect = el.getBoundingClientRect();
                  return {
                    el,
                    rect,
                    text: norm(el.innerText || el.textContent || ""),
                    accordion: el.closest(".secondary-accordion") || el,
                  };
                })
                .filter((item) => item.rect.height <= 90)
                .filter((item) => wanted.some((alias) => item.text === alias || item.text.startsWith(alias)));

              const target = headers.find((item) => !String(item.accordion.className || "").includes("is-open"))
                || headers[0];
              if (!target) {
                return { status: "not_found" };
              }
              if (String(target.accordion.className || "").includes("is-open")) {
                return { status: "already_open", text: target.text };
              }
              const clickTarget = target.accordion.querySelector(".header") || target.el;
              clickTarget.scrollIntoView({ block: "center", inline: "center" });
              clickTarget.click();
              return { status: "clicked", text: target.text };
            }
            """,
            _market_display_aliases(value),
        )
        if result.get("status") == "clicked":
            page.wait_for_timeout(500)
    except Exception:
        return


def _market_search_text(value: str) -> str:
    normalized = value.strip().lower()
    aliases = {
        "failed attempts": "strikeouts",
        "match home runs": "home runs",
        "match singles": "singles",
        "match triples": "triples",
        "team hits": "hits",
        "team rbi": "rbi",
        "team rbis": "rbi",
        "team runs": "runs",
        "team total bases": "total bases",
    }
    return aliases.get(normalized, value)


def _market_display_aliases(value: str) -> list[str]:
    normalized = value.strip().lower()
    aliases = {
        "earned runs": ["Earned Runs", "Runs Achieved", "Runs Allowed"],
        "failed attempts": ["Failed Attempts", "Strikeouts"],
        "first er": ["First ER", "First Earned Run", "First Well Deserved Run"],
        "first so": ["First SO", "First Strike Out", "First Strikeout"],
        "hits allowed": ["Hits Allowed"],
        "match home runs": ["Play Home Runs", "Match Home Runs", "Home Runs"],
        "match singles": ["Match Singles", "Singles"],
        "match triples": ["Match Triples", "Triples"],
        "outs": ["Outs", "Eliminated"],
        "rbi": ["RBI", "RBIs", "Runs Batted In"],
        "strikeouts": ["Strikeouts", "Failed Attempts"],
        "team hits": ["Team Hits", "Hits"],
        "team rbi": ["Team RBI", "Team RBIs", "RBI", "RBIs", "Runs Batted In"],
        "team rbis": ["Team RBIs", "Team RBI", "RBIs", "RBI", "Runs Batted In"],
        "team runs": ["Team Runs", "Runs"],
        "team total bases": ["Team Total Bases", "Total Bases"],
        "walks": ["Walks"],
        "win probability": ["Win Probability", "Probability of Winning"],
    }
    return aliases.get(normalized, [value])


def _review_slip_result(
    *,
    fixture_slug: str,
    status: str,
    board: dict[str, Any],
    selected_rows: list[dict[str, Any]],
    missing_selections: list[dict[str, Any]],
    click_results: list[dict[str, Any]],
    add_bet_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    matchup = _fixture_matchup_from_slug(fixture_slug).get("matchup")
    add_summary = _review_add_summary(
        fixture_slug=fixture_slug,
        matchup=matchup,
        selected_rows=selected_rows,
        click_results=click_results,
        add_bet_result=add_bet_result or {},
    )
    return {
        "source": "stake_ui_sgm_build_slip",
        "fixtureSlug": fixture_slug,
        "matchup": matchup,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reviewOnly": True,
        "clickedLegs": sum(1 for row in click_results if row.get("status") == "clicked"),
        "selectedRows": [_compact_click_row(row) for row in selected_rows],
        "missingSelections": missing_selections,
        "clickResults": click_results,
        "addBetResult": add_bet_result or {},
        "addSummary": add_summary,
        "warnings": board.get("warnings") or [],
        "safety": {
            "enteredStakeAmount": False,
            "clickedAddBet": bool((add_bet_result or {}).get("status") == "clicked"),
            "clickedPlaceBet": False,
        },
    }


def _review_add_summary(
    *,
    fixture_slug: str,
    matchup: str | None,
    selected_rows: list[dict[str, Any]],
    click_results: list[dict[str, Any]],
    add_bet_result: dict[str, Any],
) -> dict[str, Any]:
    before_state = dict(add_bet_result.get("beforeClick") or {})
    after_state = dict(add_bet_result.get("postClick") or {})
    before_count = _int_or_none(before_state.get("rightPanelSelectionCount"))
    after_count = _int_or_none(after_state.get("rightPanelSelectionCount"))
    sidebar_changed = _add_bet_confirmed(before_state, after_state)
    add_bet_confirmed = bool(
        add_bet_result.get("addBetConfirmed")
        if "addBetConfirmed" in add_bet_result
        else sidebar_changed
    )

    return {
        "fixtureSlug": fixture_slug,
        "matchup": matchup,
        "gameAdded": bool(add_bet_result.get("status") == "clicked" and add_bet_confirmed),
        "requestedLegs": len(selected_rows),
        "clickedLegs": sum(1 for row in click_results if row.get("status") == "clicked"),
        "addBetClicked": bool(add_bet_result.get("status") == "clicked"),
        "addBetConfirmed": add_bet_confirmed,
        "clickedBy": add_bet_result.get("clickedBy") or add_bet_result.get("clickedText"),
        "sidebarBefore": _compact_sidebar_state(before_state),
        "sidebarAfter": _compact_sidebar_state(after_state),
        "sidebarSelectionDelta": (
            after_count - before_count
            if before_count is not None and after_count is not None
            else None
        ),
        "sidebarChanged": sidebar_changed,
    }


def _compact_sidebar_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "empty": bool(state.get("rightPanelEmpty", True)),
        "selectionCount": _int_or_none(state.get("rightPanelSelectionCount")),
        "textLength": _int_or_none(state.get("rightPanelTextLength")),
    }


def _compact_click_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "rowId": row.get("rowId"),
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


def make_sgm_selection_row_id(fixture_slug: str, row: dict[str, Any], side: str) -> str:
    identity_parts = [
        str(fixture_slug or ""),
        str(row.get("scope") or ""),
        str(row.get("team") or ""),
        str(row.get("playerId") or row.get("player") or ""),
        str(row.get("marketId") or row.get("market") or ""),
        str(row.get("swishStatId") or row.get("statId") or ""),
        str(row.get("lineId") or ""),
        _display_number(row.get("line")),
        str(side or "").lower(),
    ]
    canonical = "|".join(_text_key(part) for part in identity_parts)
    return f"sgm_{sha1(canonical.encode('utf-8')).hexdigest()[:16]}"


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


def _shared_stake_page(context: Any) -> Any:
    for page in context.pages:
        if "stake.com" in str(page.url):
            return page
    return context.pages[0] if context.pages else context.new_page()


def _find_or_open_mlb_page(context: Any) -> Any:
    for page in context.pages:
        if "stake.com" in str(page.url) and "/sports/baseball/usa/mlb" in str(page.url):
            if _restricted_region_url(page.url):
                page.goto(STAKE_MLB_URL, wait_until="domcontentloaded", timeout=45_000)
            return page

    page = _shared_stake_page(context)
    page.goto(STAKE_MLB_URL, wait_until="domcontentloaded", timeout=45_000)
    return page


def _diagnostic_page(context: Any, *, fixture_slug: str | None = None) -> Any:
    if fixture_slug:
        return _find_or_open_fixture_page(context, fixture_slug)
    return _shared_stake_page(context)


def _read_stake_ui_state_from_page(page: Any) -> dict[str, Any]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    warnings: list[str] = []
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except PlaywrightTimeoutError:
        warnings.append("page did not reach domcontentloaded before diagnostics")

    body = ""
    try:
        body = page.locator("body").inner_text(timeout=5_000)
    except Exception:
        warnings.append("could not read Stake page body text")

    normalized_body = str(body or "").lower()
    url = str(page.url or "")
    current_fixture_slug = _fixture_slug_from_url(url)
    is_stake_page = "stake.com" in url
    is_mlb_fixture_page = bool(current_fixture_slug)
    sgm_visible = _has_same_game_multi_tab(body)
    region_blocked = _is_region_blocked_body(body) or _restricted_region_url(url)
    cloudflare_required = (
        "performing security verification" in normalized_body
        or "protect against malicious bots" in normalized_body
        or ("cloudflare" in normalized_body and "verification" in normalized_body)
    )
    login_required = (
        "login" in normalized_body
        and "register" in normalized_body
        and "wallet" not in normalized_body
    ) or ("einloggen" in normalized_body and "registrieren" in normalized_body)

    failure_reasons: list[str] = []
    if not is_stake_page:
        failure_reasons.append("not_stake_page")
    if region_blocked:
        failure_reasons.append("region_blocked")
    if cloudflare_required:
        failure_reasons.append("cloudflare_required")
    if login_required:
        failure_reasons.append("login_required")
    if is_mlb_fixture_page and not sgm_visible:
        failure_reasons.append("sgm_tab_missing")

    slip = _read_bet_slip_state(page)
    if not slip.get("rightPanelFound"):
        failure_reasons.append("right_panel_missing")

    status = "ok" if not failure_reasons else "attention_required"
    matchup = _fixture_matchup_from_slug(current_fixture_slug) if current_fixture_slug else {}
    return {
        "source": "stake_ui_state",
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "url": url,
        "currentFixtureSlug": current_fixture_slug,
        "matchup": matchup.get("matchup"),
        "teams": matchup.get("teams") or [],
        "isStakePage": is_stake_page,
        "isMlbFixturePage": is_mlb_fixture_page,
        "sgmVisible": sgm_visible,
        "access": {
            "regionBlocked": region_blocked,
            "cloudflareRequired": cloudflare_required,
            "loginRequired": login_required,
        },
        "failureReasons": failure_reasons,
        "slip": slip,
        "warnings": warnings,
    }


def _fixture_slug_from_url(url: str) -> str | None:
    path = urlparse(str(url or "")).path.strip("/")
    match = re.search(r"(?:^|/)sports/baseball/usa/mlb/(\d+[a-z0-9-]*)$", path)
    return match.group(1) if match else None


def _check_stake_page_access(page: Any) -> list[str]:
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
    if _is_region_blocked_body(body):
        raise RuntimeError(
            "Stake is still region-blocked in this browser session. "
            "Turn on the desktop VPN before starting the helper, close this helper, "
            "then retry."
        )
    if "Login" in body and "Register" in body and "Wallet" not in body:
        warnings.append(
            "browser appears logged out; read-only UI data may still load, "
            "but account-only actions will not"
        )
    return warnings


def _extract_mlb_game_links(page: Any, *, limit: int) -> list[dict[str, Any]]:
    raw_links = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href*="/sports/baseball/usa/mlb/"]'))
          .map((anchor) => {
            const href = anchor.href || anchor.getAttribute('href') || '';
            const card = anchor.closest('a, article, section, div');
            return {
              href,
              text: (card?.innerText || anchor.innerText || '').trim().replace(/\\s+/g, ' ')
            };
          })
        """
    )
    games: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_links or []:
        link = _normalize_mlb_game_link((raw or {}).get("href"))
        if not link or link["fixtureSlug"] in seen:
            continue
        seen.add(link["fixtureSlug"])
        status_text = _fixture_status_text_from_card_text((raw or {}).get("text"))
        if status_text:
            link["statusText"] = status_text
        games.append(link)
        if len(games) >= max(limit, 1):
            break
    return games


def _normalize_mlb_game_link(href: Any) -> dict[str, Any] | None:
    if not href:
        return None
    absolute = urljoin("https://stake.com", str(href))
    parsed = urlparse(absolute)
    path = parsed.path.strip("/")
    match = re.search(r"(?:^|/)sports/baseball/usa/mlb/(\d+[a-z0-9-]*)$", path)
    if not match:
        return None

    fixture_slug = match.group(1)
    matchup = _fixture_matchup_from_slug(fixture_slug)
    return {
        "fixtureSlug": fixture_slug,
        "url": absolute,
        "matchup": matchup["matchup"],
        "teams": matchup["teams"],
    }


def _fixture_matchup_from_slug(fixture_slug: str) -> dict[str, Any]:
    slug_without_id = re.sub(r"^\d+-", "", str(fixture_slug or "").strip().lower())
    for left_slug, left_name in MLB_TEAM_SLUGS.items():
        prefix = f"{left_slug}-"
        if not slug_without_id.startswith(prefix):
            continue
        right_slug = slug_without_id[len(prefix) :]
        right_name = MLB_TEAM_SLUGS.get(right_slug)
        if right_name:
            return {
                "matchup": f"{left_name} vs {right_name}",
                "teams": [left_name, right_name],
            }

    parts = [part for part in slug_without_id.split("-") if part]
    midpoint = max(len(parts) // 2, 1)
    teams = [
        " ".join(parts[:midpoint]).title(),
        " ".join(parts[midpoint:]).title(),
    ]
    return {"matchup": f"{teams[0]} vs {teams[1]}", "teams": teams}


def _fixture_status_text_from_card_text(text: Any) -> str | None:
    normalized = str(text or "").upper()
    for marker in ("NOT STARTED", "STARTS AT", "LIVE", "IN PLAY"):
        if marker in normalized:
            return marker
    return None


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


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
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
