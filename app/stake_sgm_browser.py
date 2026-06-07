from __future__ import annotations

import json
import re
import time
from hashlib import sha1
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse


DEFAULT_CDP_URL = "http://127.0.0.1:9222"
STAKE_MLB_URL = "https://stake.com/sports/baseball/usa/mlb"
CLICK_ODDS_TOLERANCE = 0.006
MONEYLINE_MARKET_LABEL = "Winner (incl. Extra Innings)"
MONEYLINE_MARKET_KEY = "winner_including_extra_innings"
_SGM_ROW_ID_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
SGM_PLAYER_MARKET_DIAGNOSTIC_TARGETS: dict[str, dict[str, Any]] = {
    "singles": {
        "aliases": ["singles", "single", "player singles"],
        "batterOnly": True,
    },
    "stolen bases": {
        "aliases": [
            "stolen bases",
            "stolen base",
            "steals",
            "steal",
            "player stolen bases",
            "player steals",
        ],
        "batterOnly": True,
    },
    "batter walks": {
        "aliases": [
            "batter walks",
            "batter walk",
            "walks",
            "walk",
            "base on balls",
            "bases on balls",
        ],
        "batterOnly": True,
    },
    "batter strikeouts": {
        "aliases": [
            "batter strikeouts",
            "batter strikeout",
            "strikeouts",
            "strikeout",
            "failed attempts",
        ],
        "batterOnly": True,
    },
}

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
        visible_market_text = _read_visible_market_text(page)
        response = _fetch_sgm_board_in_browser(page, fixture_slug)
        return normalize_sgm_response(
            fixture_slug,
            response,
            warnings,
            visible_market_text=visible_market_text,
        )


def read_stake_sgm_boards_batch(
    *,
    fixture_slugs: list[str],
    cdp_url: str = DEFAULT_CDP_URL,
    max_fixtures: int = 20,
) -> dict[str, Any]:
    clean_slugs = [
        str(slug or "").strip()
        for slug in fixture_slugs
        if str(slug or "").strip()
    ][: max(1, min(int(max_fixtures or 20), 20))]
    boards: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for fixture_slug in clean_slugs:
        try:
            boards.append(read_stake_sgm_board(fixture_slug, cdp_url=cdp_url))
        except Exception as exc:
            errors.append(
                {
                    "fixtureSlug": fixture_slug,
                    "status": "failed",
                    "message": str(exc),
                }
            )

    return {
        "source": "stake_ui_sgm_board_batch",
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "fixtureCount": len(clean_slugs),
        "succeeded": len(boards),
        "failed": len(errors),
        "boards": boards,
        "errors": errors,
    }


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
        expansion = _expand_mlb_game_list(page, limit=limit)
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
            "expansion": expansion,
            "warnings": warnings,
        }


def read_stake_mlb_moneylines(
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
        expansion = _expand_mlb_game_list(page, limit=limit)
        raw_cards = _extract_mlb_moneyline_cards(page)
        normalized = _normalize_mlb_moneyline_cards(raw_cards, limit=limit)
        return {
            "source": "stake_ui_mlb_moneylines_raw",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "url": page.url,
            "returnedGames": len(normalized["games"]),
            "games": normalized["games"],
            "expansion": expansion,
            "warnings": warnings + normalized["warnings"],
        }


def build_stake_mlb_moneyline_review_slip(
    selections: list[dict[str, Any]],
    *,
    execution_timeout_seconds: int | float | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    prepared = _prepare_moneyline_build_selections(selections)
    if prepared["status"] != "ready":
        return _moneyline_build_result(
            status=prepared["status"],
            requested=prepared["selections"],
            added=[],
            already_present=[],
            remaining=[
                {
                    "selection": error.get("selection"),
                    "reason": error.get("reason"),
                }
                for error in prepared["errors"]
            ],
            warnings=[],
            sidebar={"mode": "not_read"},
        )

    from playwright.sync_api import sync_playwright

    deadline = _build_execution_deadline(execution_timeout_seconds)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _find_or_open_mlb_page(browser.contexts[0])
        _expand_mlb_game_list(page, limit=100)
        state = _read_stake_ui_state_from_page(page)
        sidebar = _classify_moneyline_sidebar_state(
            state.get("slip") or {},
            requested=prepared["selections"],
        )
        if sidebar["mode"] == "blocked_mixed_or_unknown":
            return _moneyline_build_result(
                status="blocked_sidebar_not_moneyline_only",
                requested=prepared["selections"],
                added=[],
                already_present=[],
                remaining=[],
                warnings=[],
                sidebar=sidebar,
            )

        already_present: list[dict[str, Any]] = []
        added: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        warnings: list[str] = []
        present_row_ids = set(sidebar.get("alreadyPresentRowIds") or [])
        for selection in prepared["selections"]:
            if _execution_deadline_expired(deadline, reserve_seconds=2.0):
                remaining.append({**selection, "reason": "local_helper_execution_timeout"})
                continue
            if selection["rowId"] in present_row_ids:
                already_present.append({**selection, "reason": "selection_already_present"})
                warnings.append("selection_already_present")
                continue

            click_result = _click_mlb_moneyline_selection_with_retry(
                page,
                selection,
                deadline=deadline,
            )
            if click_result.get("status") == "added":
                added.append(click_result["selection"])
                if click_result["selection"].get("oddsMoved"):
                    warnings.append("odds_moved")
            else:
                remaining.append({**selection, "reason": click_result.get("reason")})

        final_state = _read_stake_ui_state_from_page(page)
        final_sidebar = _classify_moneyline_sidebar_state(
            final_state.get("slip") or {},
            requested=prepared["selections"],
        )
        return _moneyline_build_result(
            status=None,
            requested=prepared["selections"],
            added=added,
            already_present=already_present,
            remaining=remaining,
            warnings=warnings,
            sidebar=final_sidebar,
        )


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
    row_id: str | None = None,
    team: str | None = None,
) -> dict[str, Any]:
    if not fixture_slug and not matchup and not row_id:
        raise RuntimeError("fixtureSlug, matchup, or rowId is required to remove a sidebar item.")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _diagnostic_page(browser.contexts[0], fixture_slug=fixture_slug)
        state_before = _read_stake_ui_state_from_page(page)
        target = _sidebar_group_target(
            fixture_slug=fixture_slug,
            matchup=matchup,
            row_id=row_id,
            team=team,
        )
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
    *,
    fallback_selections: list[dict[str, Any]] | None = None,
    required_legs: int | None = None,
    execution_timeout_seconds: int | float | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    deadline = _build_execution_deadline(execution_timeout_seconds)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _find_or_open_fixture_page(browser.contexts[0], fixture_slug)
        warnings = _check_page_ready(page, fixture_slug=fixture_slug)
        visible_market_text = _read_visible_market_text(page)
        response = _fetch_sgm_board_in_browser(page, fixture_slug)
        board = normalize_sgm_response(
            fixture_slug,
            response,
            warnings,
            visible_market_text=visible_market_text,
        )
        if _has_logged_out_warning(warnings):
            return _review_slip_result(
                fixture_slug=fixture_slug,
                status="blocked_login_required",
                board=board,
                selected_rows=[],
                missing_selections=[],
                click_results=[],
            )
        transaction = _prepare_transactional_review_rows(
            page,
            board,
            selections,
            fallback_selections=fallback_selections or [],
            required_legs=required_legs,
            deadline=deadline,
        )
        if transaction["status"] == "timeout":
            return _review_build_timeout_result(
                source="stake_ui_sgm_build_slip",
                fixture_slug=fixture_slug,
                phase="preflight",
                transaction=transaction,
            )

        if transaction["status"] != "ready":
            return _review_slip_result(
                fixture_slug=fixture_slug,
                status=transaction["status"],
                board=board,
                selected_rows=[],
                missing_selections=transaction["missingSelections"],
                click_results=[],
                transaction_plan=transaction["plan"],
            )

        selected_rows = transaction["selectedRows"]
        if _execution_deadline_expired(deadline, reserve_seconds=2.0):
            return _review_build_timeout_result(
                source="stake_ui_sgm_build_slip",
                fixture_slug=fixture_slug,
                phase="click",
                transaction=transaction,
                selected_rows=selected_rows,
            )
        click_results = _click_sgm_review_selections(
            page,
            selected_rows,
            deadline=deadline,
        )
        if any(result.get("status") == "timeout" for result in click_results):
            return _review_build_timeout_result(
                source="stake_ui_sgm_build_slip",
                fixture_slug=fixture_slug,
                phase="click",
                transaction=transaction,
                selected_rows=selected_rows,
                click_results=click_results,
            )
        failed_clicks = [row for row in click_results if row.get("status") != "clicked"]
        if failed_clicks:
            _clear_sgm_working_selection(page)
        add_bet_result = (
            _click_sgm_add_bet_button(page, expected_legs=len(selected_rows))
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
            selected_rows=selected_rows,
            missing_selections=transaction["missingSelections"],
            click_results=click_results,
            add_bet_result=add_bet_result,
            transaction_plan=transaction["plan"],
        )


def build_stake_sgm_review_slip_batch(
    groups: list[dict[str, Any]],
    *,
    continue_on_group_failure: bool = False,
    min_groups_required: int | None = None,
    execution_timeout_seconds: int | float | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    deadline = _build_execution_deadline(execution_timeout_seconds)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        context = browser.contexts[0]
        page = _shared_stake_page(context)
        results: list[dict[str, Any]] = []
        stop_reason: str | None = None
        required_groups = max(1, min(len(groups) or 1, int(min_groups_required or 1)))

        for group in groups:
            if _execution_deadline_expired(deadline, reserve_seconds=2.0):
                stop_reason = "local_helper_execution_timeout"
                break
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
                if not continue_on_group_failure:
                    break
                continue

            page.goto(fixture_url(fixture_slug), wait_until="domcontentloaded", timeout=45_000)
            warnings = _check_page_ready(page, fixture_slug=fixture_slug)
            visible_market_text = _read_visible_market_text(page)
            response = _fetch_sgm_board_in_browser(page, fixture_slug)
            board = normalize_sgm_response(
                fixture_slug,
                response,
                warnings,
                visible_market_text=visible_market_text,
            )
            selections = _group_review_selections(group)
            fallback_selections = _group_review_fallback_selections(group)
            required_legs = _group_required_legs(group, len(selections))
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
                transaction = _prepare_transactional_review_rows(
                    page,
                    board,
                    selections,
                    fallback_selections=fallback_selections,
                    required_legs=required_legs,
                    deadline=deadline,
                )
                if transaction["status"] == "timeout":
                    result = _review_build_timeout_result(
                        source="stake_ui_sgm_build_slip",
                        fixture_slug=fixture_slug,
                        matchup=group.get("matchup"),
                        phase="preflight",
                        transaction=transaction,
                    )
                elif transaction["status"] != "ready":
                    result = _review_slip_result(
                        fixture_slug=fixture_slug,
                        status=transaction["status"],
                        board=board,
                        selected_rows=[],
                        missing_selections=transaction["missingSelections"],
                        click_results=[],
                        transaction_plan=transaction["plan"],
                    )
                else:
                    selected_rows = transaction["selectedRows"]
                    if _execution_deadline_expired(deadline, reserve_seconds=2.0):
                        result = _review_build_timeout_result(
                            source="stake_ui_sgm_build_slip",
                            fixture_slug=fixture_slug,
                            matchup=group.get("matchup"),
                            phase="click",
                            transaction=transaction,
                            selected_rows=selected_rows,
                        )
                    else:
                        click_results = _click_sgm_review_selections(
                            page,
                            selected_rows,
                            deadline=deadline,
                        )
                        if any(result.get("status") == "timeout" for result in click_results):
                            result = _review_build_timeout_result(
                                source="stake_ui_sgm_build_slip",
                                fixture_slug=fixture_slug,
                                matchup=group.get("matchup"),
                                phase="click",
                                transaction=transaction,
                                selected_rows=selected_rows,
                                click_results=click_results,
                            )
                            result["matchup"] = group.get("matchup")
                            results.append(result)
                            stop_reason = "local_helper_execution_timeout"
                            break
                        failed_clicks = [
                            row for row in click_results if row.get("status") != "clicked"
                        ]
                        if failed_clicks:
                            _clear_sgm_working_selection(page)
                        add_bet_result = (
                            _click_sgm_add_bet_button(
                                page,
                                expected_legs=len(selected_rows),
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
                            selected_rows=selected_rows,
                            missing_selections=transaction["missingSelections"],
                            click_results=click_results,
                            add_bet_result=add_bet_result,
                            transaction_plan=transaction["plan"],
                        )

            result["matchup"] = group.get("matchup")
            results.append(result)
            if _batch_should_stop_after_group_result(
                result,
                continue_on_group_failure=continue_on_group_failure,
            ):
                stop_reason = (
                    "local_helper_execution_timeout"
                    if result.get("status") == "timeout"
                    else str(result.get("status") or "blocked")
                )
                break

        clicked_groups = sum(1 for result in results if result.get("status") == "built_for_review")
        clicked_legs = sum(int(result.get("clickedLegs") or 0) for result in results)
        status = (
            "built_for_review"
            if clicked_groups == len(groups) and not stop_reason
            else "partial_review_slip"
            if clicked_groups and clicked_groups >= required_groups
            else "timeout"
            if stop_reason == "local_helper_execution_timeout"
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
            "continueOnGroupFailure": continue_on_group_failure,
            "minGroupsRequired": required_groups,
            "groups": results,
            "safety": {
                "enteredStakeAmount": False,
                "clickedAddBet": bool(clicked_groups),
                "clickedPlaceBet": False,
            },
        }


def _batch_should_stop_after_group_result(
    result: dict[str, Any],
    *,
    continue_on_group_failure: bool,
) -> bool:
    if result.get("status") == "built_for_review":
        return False
    return not continue_on_group_failure


def _build_execution_deadline(execution_timeout_seconds: int | float | None) -> float | None:
    seconds = _float_or_none(execution_timeout_seconds)
    if seconds is None or seconds <= 0:
        return None
    return time.monotonic() + max(seconds, 1.0)


def _execution_deadline_expired(
    deadline: float | None,
    *,
    reserve_seconds: float = 0.0,
) -> bool:
    if deadline is None:
        return False
    return time.monotonic() + max(reserve_seconds, 0.0) >= deadline


def _preflight_timeout_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "timeout",
        "phase": "preflight",
        "reason": "local_helper_execution_timeout",
        "lastAction": "preflight_row_match",
        "lastAttemptedRowId": row.get("rowId"),
    }


def _review_build_timeout_result(
    *,
    source: str,
    fixture_slug: str | None,
    phase: str,
    matchup: Any = None,
    transaction: dict[str, Any] | None = None,
    selected_rows: list[dict[str, Any]] | None = None,
    click_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    transaction_plan = (transaction or {}).get("plan") or {}
    attempted_rows = [_compact_click_row(row) for row in selected_rows or []]
    clicked_legs = sum(1 for result in click_results or [] if result.get("status") == "clicked")
    last_attempted_row_id = _last_attempted_row_id(transaction_plan, attempted_rows)
    return {
        "source": source,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "status": "timeout",
        "phase": phase,
        "reason": "local_helper_execution_timeout",
        "lastAction": f"{phase}_timeout",
        "fixtureSlug": fixture_slug,
        "currentFixtureSlug": fixture_slug,
        "matchup": matchup,
        "reviewOnly": True,
        "clickedLegs": clicked_legs,
        "attemptedRows": attempted_rows,
        "clickResults": click_results or [],
        "lastAttemptedRowId": last_attempted_row_id,
        "missingSelections": (transaction or {}).get("missingSelections") or [],
        "transactionPlan": transaction_plan,
        "safety": {
            "enteredStakeAmount": False,
            "clickedAddBet": False,
            "clickedPlaceBet": False,
        },
    }


def _last_attempted_row_id(
    transaction_plan: dict[str, Any],
    attempted_rows: list[dict[str, Any]],
) -> Any:
    for group_name in ("preflightFailures", "fallbackFailures"):
        for failure in reversed(transaction_plan.get(group_name) or []):
            preflight = failure.get("preflight") or {}
            row_id = preflight.get("lastAttemptedRowId")
            if row_id:
                return row_id
            selection = failure.get("selection") or {}
            row_id = selection.get("rowId")
            if row_id:
                return row_id
    if attempted_rows:
        return attempted_rows[-1].get("rowId")
    return None


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


def _group_review_fallback_selections(group: dict[str, Any]) -> list[dict[str, Any]]:
    selections: list[dict[str, Any]] = []
    for key in ("fallbackSelections", "replacementSelections", "backupSelections"):
        raw_selections = group.get(key)
        if isinstance(raw_selections, list):
            selections.extend(item for item in raw_selections if isinstance(item, dict))

    for key in ("fallbackRowIds", "replacementRowIds", "backupRowIds"):
        raw_row_ids = group.get(key) or group.get(_snake_case_key(key))
        if not isinstance(raw_row_ids, list):
            continue
        for row_id in raw_row_ids:
            if str(row_id or "").strip():
                selections.append({"rowId": str(row_id).strip()})
    return selections


def _group_required_legs(group: dict[str, Any], default_count: int) -> int:
    for key in ("requiredLegs", "targetLegs", "legCount"):
        raw_value = group.get(key) or group.get(_snake_case_key(key))
        if raw_value is None:
            continue
        try:
            return max(1, min(20, int(raw_value)))
        except (TypeError, ValueError):
            continue
    return max(1, min(20, default_count))


def _snake_case_key(value: str) -> str:
    chars: list[str] = []
    for char in value:
        if char.isupper() and chars:
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars)


def _prepare_transactional_review_rows(
    page: Any,
    board: dict[str, Any],
    selections: list[dict[str, Any]],
    *,
    fallback_selections: list[dict[str, Any]] | None = None,
    required_legs: int | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    requested_required_legs = max(1, min(20, int(required_legs or len(selections) or 1)))
    primary_match = match_sgm_review_selections(board, selections)
    fallback_match = match_sgm_review_selections(board, fallback_selections or [])
    primary_rows = primary_match["matchedRows"]
    fallback_rows = fallback_match["matchedRows"]

    primary_preflight = _preflight_sgm_review_selections(
        page,
        primary_rows,
        deadline=deadline,
    )
    plan = _transactional_selection_plan(
        primary_rows=primary_rows,
        primary_preflight=primary_preflight,
        fallback_rows=[],
        fallback_preflight=[],
        required_legs=requested_required_legs,
        primary_missing=primary_match["missingSelections"],
    )
    if plan["status"] != "ready" and fallback_rows:
        fallback_preflight = _preflight_sgm_review_selections(
            page,
            fallback_rows,
            deadline=deadline,
        )
        plan = _transactional_selection_plan(
            primary_rows=primary_rows,
            primary_preflight=primary_preflight,
            fallback_rows=fallback_rows,
            fallback_preflight=fallback_preflight,
            required_legs=requested_required_legs,
            primary_missing=primary_match["missingSelections"],
            fallback_missing=fallback_match["missingSelections"],
        )
    missing = list(primary_match["missingSelections"])
    if plan["status"] != "ready":
        missing.extend(fallback_match["missingSelections"])
    return {
        "status": "ready" if plan["status"] == "ready" else plan["status"],
        "selectedRows": plan["selectedRows"],
        "missingSelections": missing,
        "plan": plan,
    }


def _transactional_selection_plan(
    *,
    primary_rows: list[dict[str, Any]],
    primary_preflight: list[dict[str, Any]],
    fallback_rows: list[dict[str, Any]],
    fallback_preflight: list[dict[str, Any]],
    required_legs: int,
    primary_missing: list[dict[str, Any]] | None = None,
    fallback_missing: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    required = max(1, min(20, int(required_legs or len(primary_rows) or 1)))
    selected_rows: list[dict[str, Any]] = []
    buildable_rows: list[dict[str, Any]] = []
    preflight_failures: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    used_row_ids: set[str] = set()
    timed_out = False

    for missing in primary_missing or []:
        preflight_failures.append(
            {
                "selection": missing.get("selection"),
                "reason": missing.get("reason") or "no exact playable UI row matched",
                "phase": "primary_match",
            }
        )

    for row, check in zip(primary_rows, primary_preflight):
        if _preflight_result_is_buildable(check):
            buildable_rows.append(row)
            if len(selected_rows) < required:
                selected_rows.append(row)
                used_row_ids.add(str(row.get("rowId") or ""))
        else:
            timed_out = timed_out or str(check.get("status") or "") == "timeout"
            preflight_failures.append(
                {
                    "selection": _compact_click_row(row),
                    "reason": check.get("reason") or check.get("status") or "not_buildable",
                    "diagnosticStatus": (
                        "market_parsed_with_row_id_but_click_preflight_failed"
                    ),
                    "phase": "primary_preflight",
                    "preflight": _compact_preflight_result(check),
                }
            )

    for row, check in zip(fallback_rows, fallback_preflight):
        row_id = str(row.get("rowId") or "")
        if len(selected_rows) >= required:
            break
        if row_id and row_id in used_row_ids:
            continue
        if not _preflight_result_is_buildable(check):
            timed_out = timed_out or str(check.get("status") or "") == "timeout"
            continue
        selected_rows.append(row)
        buildable_rows.append(row)
        used_row_ids.add(row_id)
        replacements.append(
            {
                "reason": "primary_not_buildable",
                "replacement": _compact_click_row(row),
            }
        )

    fallback_failures = []
    for row, check in zip(fallback_rows, fallback_preflight):
        if _preflight_result_is_buildable(check):
            continue
        timed_out = timed_out or str(check.get("status") or "") == "timeout"
        fallback_failures.append(
            {
                "selection": _compact_click_row(row),
                "reason": check.get("reason") or check.get("status") or "not_buildable",
                "diagnosticStatus": "market_parsed_with_row_id_but_click_preflight_failed",
                "phase": "fallback_preflight",
                "preflight": _compact_preflight_result(check),
            }
        )
    for missing in fallback_missing or []:
        fallback_failures.append(
            {
                "selection": missing.get("selection"),
                "reason": missing.get("reason") or "no exact playable UI row matched",
                "phase": "fallback_match",
            }
        )

    ready = len(selected_rows) >= required
    return {
        "status": "ready" if ready else "timeout" if timed_out else "blocked_preflight_failed",
        "requiredLegs": required,
        "selectedRows": selected_rows if ready else [],
        "buildableRows": [_compact_click_row(row) for row in buildable_rows],
        "missingLegs": max(0, required - len(selected_rows)),
        "preflightFailures": preflight_failures,
        "fallbackFailures": fallback_failures,
        "replacements": replacements,
        "transactionalBuild": True,
    }


def _preflight_result_is_buildable(result: dict[str, Any]) -> bool:
    return str(result.get("status") or "") in {"buildable", "clicked"} and _click_result_identity_verified(
        result
    )


def _click_result_identity_verified(result: dict[str, Any]) -> bool:
    if result.get("oddsChanged") is True:
        return False
    requested_odds = _float_or_none(result.get("requestedOdds"))
    clicked_odds = _float_or_none(result.get("clickedOdds"))
    if requested_odds is None:
        return True
    if clicked_odds is None:
        return False
    return abs(requested_odds - clicked_odds) <= CLICK_ODDS_TOLERANCE


def _click_result_selection_verified(result: dict[str, Any]) -> bool:
    if "selectedAfterClick" not in result:
        return True
    return result.get("selectedAfterClick") is True


def _compact_preflight_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "phase": result.get("phase"),
        "reason": result.get("reason"),
        "lastAction": result.get("lastAction"),
        "lastAttemptedRowId": result.get("lastAttemptedRowId"),
        "candidateCount": result.get("candidateCount"),
        "requestedMarket": result.get("requestedMarket"),
        "clickedOdds": result.get("clickedOdds"),
        "requestedOdds": result.get("requestedOdds"),
        "oddsChanged": result.get("oddsChanged"),
        "matchedBy": result.get("matchedBy"),
        "candidateSamples": result.get("candidateSamples") or [],
        "rowCandidateSamples": result.get("rowCandidateSamples") or [],
        "visibleRowSamples": result.get("visibleRowSamples") or [],
        "marketMismatchSamples": result.get("marketMismatchSamples") or [],
        "oddsMismatchSamples": result.get("oddsMismatchSamples") or [],
    }


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
            row_id_aliases = _sgm_selection_row_id_aliases(fixture_slug, row, side)
            if row_id in row_id_aliases:
                return _matched_selection_row(
                    row,
                    side,
                    make_sgm_selection_row_id(fixture_slug, row, side),
                )

    cached_row = _find_cached_selection_row_by_row_id(source_rows, fixture_slug, row_id)
    if cached_row:
        return cached_row

    return None


def _find_cached_selection_row_by_row_id(
    source_rows: list[dict[str, Any]],
    fixture_slug: str,
    row_id: str,
) -> dict[str, Any] | None:
    cached = _SGM_ROW_ID_CACHE.get((fixture_slug, row_id))
    if not cached:
        return None
    side = str(cached.get("side") or "").strip().lower()
    if side not in {"over", "under"}:
        return None

    cached_line = _float_or_none(cached.get("line"))
    cached_player = _text_key(cached.get("player"))
    cached_team = _text_key(cached.get("team"))
    cached_market = _text_key(cached.get("market"))
    cached_scope = _text_key(cached.get("scope"))

    for row in source_rows:
        if not row.get("playable") or row.get(side) is None:
            continue
        if cached_scope and cached_scope != _text_key(row.get("scope")):
            continue
        if cached_team and cached_team != _text_key(row.get("team")):
            continue
        if cached_player and cached_player != _text_key(row.get("player")):
            continue
        if cached_market and cached_market != _text_key(row.get("market")):
            continue
        if cached_line is None or not _numbers_equal(cached_line, row.get("line")):
            continue
        return _matched_selection_row(
            row,
            side,
            make_sgm_selection_row_id(fixture_slug, row, side),
        )

    return None


def _remember_sgm_board_rows(board: dict[str, Any]) -> None:
    fixture_slug = str(board.get("fixtureSlug") or "").strip()
    if not fixture_slug:
        return
    source_rows = list(board.get("playerProps") or []) + list(board.get("teamMarkets") or [])
    for row in source_rows:
        if not row.get("playable"):
            continue
        for side in ("over", "under"):
            if row.get(side) is None:
                continue
            matched = _matched_selection_row(
                row,
                side,
                make_sgm_selection_row_id(fixture_slug, row, side),
            )
            for row_id in _sgm_selection_row_id_aliases(fixture_slug, row, side):
                _SGM_ROW_ID_CACHE[(fixture_slug, row_id)] = matched


def normalize_sgm_response(
    fixture_slug: str,
    response: dict[str, Any],
    warnings: list[str] | None = None,
    visible_market_text: str | None = None,
) -> dict[str, Any]:
    slug_fixture = ((response.get("data") or {}).get("slugFixture")) or {}
    playability_context = _sgm_playability_context(slug_fixture)
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
            team_markets.extend(
                _line_rows(
                    market.get("lines") or [],
                    market,
                    team_name,
                    playability_context=playability_context,
                )
            )

        for player in team.get("players") or []:
            for market in player.get("markets") or []:
                player_props.extend(
                    _line_rows(
                        market.get("lines") or [],
                        market,
                        team_name,
                        player,
                        playability_context=playability_context,
                    )
                )

    board = {
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
        "marketDiagnostics": _sgm_market_diagnostics(
            fixture_slug,
            player_props,
            visible_market_text=visible_market_text,
        ),
        "marketCatalog": _sgm_market_catalog(team_markets, player_props),
        "teamMarkets": team_markets,
        "playerProps": player_props,
    }
    _remember_sgm_board_rows(board)
    return board


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
        if selection_market and not sgm_market_filter_matches(row, selection.get("market")):
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
        "balanced": row.get("balanced"),
        "push": row.get("push"),
        "betFactor": row.get("betFactor"),
        "customBet": bool(row.get("customBet")),
        "liveCustomBetAvailable": bool(row.get("liveCustomBetAvailable")),
        "playabilityMode": row.get("playabilityMode"),
        "playabilityWarnings": row.get("playabilityWarnings") or [],
        "playerId": row.get("playerId"),
        "marketId": row.get("marketId"),
        "lineId": row.get("lineId"),
        "swishStatId": row.get("swishStatId"),
    }


def _click_sgm_review_selections(
    page: Any,
    rows: list[dict[str, Any]],
    *,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    click_results: list[dict[str, Any]] = []
    _open_same_game_multi_tab(page)
    _clear_sgm_working_selection(page)

    for row in rows:
        if _execution_deadline_expired(deadline, reserve_seconds=2.0):
            if click_results:
                _clear_sgm_working_selection(page)
            click_results.append(
                {
                    "selection": _compact_click_row(row),
                    **_preflight_timeout_result(row),
                    "phase": "click",
                    "lastAction": "click_row_selection",
                }
            )
            break
        result = _click_one_sgm_selection(page, row)
        if result.get("status") == "clicked" and not _click_result_identity_verified(result):
            result = {
                **result,
                "status": "clicked_but_odds_mismatch_unverified",
                "reason": "clicked_odds_mismatch",
            }
        if result.get("status") == "clicked" and not _click_result_selection_verified(result):
            result = {
                **result,
                "status": "clicked_but_selection_unverified",
                "reason": "clicked_selection_not_verified",
            }
        click_results.append(result)
        if result.get("status") != "clicked":
            break
    return click_results


def _preflight_sgm_review_selections(
    page: Any,
    rows: list[dict[str, Any]],
    *,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    preflight_results: list[dict[str, Any]] = []
    _open_same_game_multi_tab(page)
    _clear_sgm_working_selection(page)

    for row in rows:
        if _execution_deadline_expired(deadline, reserve_seconds=2.0):
            preflight_results.append(_preflight_timeout_result(row))
            break
        preflight_results.append(_preflight_one_sgm_selection(page, row))
    _clear_sgm_working_selection(page)
    return preflight_results


def _click_sgm_add_bet_button(page: Any, *, expected_legs: int) -> dict[str, Any]:
    try:
        before_state = _read_bet_slip_state(page)
        sticky_result = _click_custom_bet_sticky_add(page, before_state=before_state)
        if sticky_result.get("status") == "clicked":
            return sticky_result

        outcome_audit = _wait_for_selected_outcome_audit(page, expected_legs=expected_legs)
        if not _selected_outcome_audit_is_valid(outcome_audit, expected_legs=expected_legs):
            return {
                "status": "not_clicked",
                "reason": "selected_outcome_count_mismatch",
                "expectedLegs": expected_legs,
                "selectedOutcomeAudit": outcome_audit,
                "beforeClick": before_state,
                "initialStickyClick": sticky_result,
            }

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
              const selectionEvidence = (button) => {
                const classText = norm(button.className || "");
                const classes = classText.split(" ").filter(Boolean);
                const evidence = [];
                if (button.getAttribute("aria-pressed") === "true") evidence.push("aria_pressed");
                if (button.getAttribute("aria-selected") === "true") evidence.push("aria_selected");
                if (norm(button.getAttribute("data-state") || "") === "checked") evidence.push("data_state_checked");
                if (norm(button.getAttribute("data-selected") || "") === "true") evidence.push("data_selected");
                if (classes.includes("active")) evidence.push("class_active");
                if (classes.includes("selected")) evidence.push("class_selected");
                let current = button.parentElement;
                for (let depth = 0; depth < 5 && current; depth += 1) {
                  const ancestorClassText = norm(current.className || "");
                  const ancestorClasses = ancestorClassText.split(" ").filter(Boolean);
                  if (current.getAttribute("aria-pressed") === "true") evidence.push("ancestor_aria_pressed");
                  if (current.getAttribute("aria-selected") === "true") evidence.push("ancestor_aria_selected");
                  if (norm(current.getAttribute("data-state") || "") === "checked") evidence.push("ancestor_data_state_checked");
                  if (norm(current.getAttribute("data-selected") || "") === "true") evidence.push("ancestor_data_selected");
                  if (ancestorClasses.includes("active")) evidence.push("ancestor_class_active");
                  if (ancestorClasses.includes("selected")) evidence.push("ancestor_class_selected");
                  current = current.parentElement;
                }
                return evidence;
              };
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
                    .filter((button) => selectionEvidence(button).length > 0)
                    .length;
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


def _wait_for_selected_outcome_audit(page: Any, *, expected_legs: int) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for _ in range(16):
        latest = _read_sgm_selected_outcome_audit(page, expected_legs=expected_legs)
        if _selected_outcome_audit_is_valid(latest, expected_legs=expected_legs):
            return latest
        try:
            page.wait_for_timeout(250)
        except Exception:
            break
    return latest


def _selected_outcome_audit_is_valid(audit: dict[str, Any], *, expected_legs: int) -> bool:
    selected_count = _int_or_none(audit.get("selectedOutcomeCount"))
    if selected_count != expected_legs:
        return False
    selected_outcomes = audit.get("selectedOutcomes")
    if not isinstance(selected_outcomes, list) or len(selected_outcomes) != expected_legs:
        return False
    reliable_evidence = {
        "aria_pressed",
        "aria_selected",
        "data_state_checked",
        "data_selected",
        "class_active",
        "class_selected",
        "ancestor_aria_pressed",
        "ancestor_aria_selected",
        "ancestor_data_state_checked",
        "ancestor_data_selected",
        "ancestor_class_active",
        "ancestor_class_selected",
    }
    for outcome in selected_outcomes:
        evidence = set(outcome.get("selectionEvidence") or [])
        if not evidence.intersection(reliable_evidence):
            return False
    return True


def _read_sgm_selected_outcome_audit(page: Any, *, expected_legs: int) -> dict[str, Any]:
    try:
        return dict(
            page.evaluate(
                """
                ({ expectedLegs }) => {
                  const norm = (value) => String(value || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/\\s+/g, " ")
                    .trim();
                  const sample = (value) => String(value || "")
                    .trim()
                    .replace(/\\s+/g, " ")
                    .slice(0, 280);
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== "hidden"
                      && style.display !== "none"
                      && rect.width > 0
                      && rect.height > 0;
                  };
                  const selectionEvidence = (button) => {
                    const classText = norm(button.className || "");
                    const classes = classText.split(" ").filter(Boolean);
                    const dataState = norm(button.getAttribute("data-state") || "");
                    const dataSelected = norm(button.getAttribute("data-selected") || "");
                    const evidence = [];
                    if (button.getAttribute("aria-pressed") === "true") evidence.push("aria_pressed");
                    if (button.getAttribute("aria-selected") === "true") evidence.push("aria_selected");
                    if (dataState === "checked") evidence.push("data_state_checked");
                    if (dataSelected === "true") evidence.push("data_selected");
                    if (classes.includes("active")) evidence.push("class_active");
                    if (classes.includes("selected")) evidence.push("class_selected");
                    let current = button.parentElement;
                    for (let depth = 0; depth < 5 && current; depth += 1) {
                      const ancestorClassText = norm(current.className || "");
                      const ancestorClasses = ancestorClassText.split(" ").filter(Boolean);
                      if (current.getAttribute("aria-pressed") === "true") evidence.push("ancestor_aria_pressed");
                      if (current.getAttribute("aria-selected") === "true") evidence.push("ancestor_aria_selected");
                      if (norm(current.getAttribute("data-state") || "") === "checked") evidence.push("ancestor_data_state_checked");
                      if (norm(current.getAttribute("data-selected") || "") === "true") evidence.push("ancestor_data_selected");
                      if (ancestorClasses.includes("active")) evidence.push("ancestor_class_active");
                      if (ancestorClasses.includes("selected")) evidence.push("ancestor_class_selected");
                      current = current.parentElement;
                    }
                    return evidence;
                  };
                  const buttons = Array.from(document.querySelectorAll('button[data-testid="fixture-outcome"]'))
                    .filter(visible);
                  const selectedButtons = buttons
                    .map((button) => ({ button, evidence: selectionEvidence(button) }))
                    .filter((item) => item.evidence.length > 0);
                  return {
                    expectedLegs,
                    selectedOutcomeCount: selectedButtons.length,
                    visibleOutcomeCount: buttons.length,
                    selectedOutcomes: selectedButtons.slice(0, 20).map(({ button, evidence }, index) => {
                      let current = button;
                      const ancestors = [];
                      for (let depth = 0; depth < 8 && current; depth += 1) {
                        ancestors.push(sample(current.innerText || current.textContent || ""));
                        current = current.parentElement;
                      }
                      return {
                        index,
                        text: sample(button.innerText || button.textContent || ""),
                        ariaPressed: button.getAttribute("aria-pressed"),
                        ariaSelected: button.getAttribute("aria-selected"),
                        className: sample(button.className || ""),
                        selectionEvidence: evidence,
                        rowTextSample: ancestors.find((text) => text.length > 20) || ancestors[0] || "",
                      };
                    }),
                  };
                }
                """,
                {"expectedLegs": expected_legs},
            )
        )
    except Exception as exc:
        return {
            "expectedLegs": expected_legs,
            "selectedOutcomeCount": None,
            "selectedOutcomes": [],
            "error": str(exc),
        }


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


def _classify_moneyline_sidebar_state(
    slip: dict[str, Any],
    *,
    requested: list[dict[str, Any]],
) -> dict[str, Any]:
    text = str(
        (slip or {}).get("rightPanelText")
        or (slip or {}).get("rightPanelTextSample")
        or ""
    ).strip()
    if bool((slip or {}).get("rightPanelEmpty")) or not text:
        return {
            "mode": "empty",
            "blockingReason": None,
            "alreadyPresentRowIds": [],
            "moneylineSelections": [],
        }

    lowered = text.lower()
    if "same game multi" in lowered or "custom bet" in lowered:
        return {
            "mode": "blocked_mixed_or_unknown",
            "blockingReason": "contains_sgm_or_custom_bet_group",
            "alreadyPresentRowIds": [],
            "moneylineSelections": [],
        }

    sidebar_selections = [
        item
        for item in (slip or {}).get("rightPanelSelections") or []
        if _text_key((item or {}).get("market")) == _text_key(MONEYLINE_MARKET_LABEL)
        and str((item or {}).get("rowId") or "").startswith("mlb_ml_")
    ]

    requested_keys = {
        (
            str(item.get("fixtureSlug") or "").strip(),
            _text_key(item.get("team")),
            str(item.get("rowId") or "").strip(),
        )
        for item in requested or []
    }
    already_present = []
    for item in sidebar_selections:
        key = (
            str(item.get("fixtureSlug") or "").strip(),
            _text_key(item.get("team")),
            str(item.get("rowId") or "").strip(),
        )
        if key in requested_keys:
            already_present.append(str(item.get("rowId") or "").strip())

    if sidebar_selections:
        return {
            "mode": "moneyline_only",
            "blockingReason": None,
            "alreadyPresentRowIds": already_present,
            "moneylineSelections": sidebar_selections,
        }

    requested_matches = []
    text_key = _text_key(text)
    for item in requested or []:
        row_id = str(item.get("rowId") or "").strip()
        team = str(item.get("team") or "").strip()
        if team and _text_key(team) in text_key:
            requested_matches.append(row_id)

    if requested_matches and not any(word in lowered for word in ("over", "under", "above", "below")):
        return {
            "mode": "moneyline_only",
            "blockingReason": None,
            "alreadyPresentRowIds": list(dict.fromkeys(requested_matches)),
            "moneylineSelections": [],
        }

    return {
        "mode": "blocked_mixed_or_unknown",
        "blockingReason": "unknown_sidebar_selection",
        "alreadyPresentRowIds": [],
        "moneylineSelections": [],
    }


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
    row_id: str | None = None,
    team: str | None = None,
) -> dict[str, Any]:
    if row_id and str(row_id).startswith("mlb_ml_"):
        clean_fixture_slug = str(fixture_slug or "").strip()
        clean_team = str(team or "").strip()
        if not clean_fixture_slug or not clean_team:
            raise RuntimeError("fixtureSlug and team are required to remove an MLB moneyline.")
        return {
            "type": "mlb_moneyline",
            "fixtureSlug": clean_fixture_slug,
            "rowId": str(row_id).strip(),
            "team": clean_team,
            "teams": [clean_team],
            "matchup": matchup,
        }

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
    return _interact_one_sgm_selection(page, row, click=True)


def _preflight_one_sgm_selection(page: Any, row: dict[str, Any]) -> dict[str, Any]:
    return _interact_one_sgm_selection(page, row, click=False)


def _interact_one_sgm_selection(page: Any, row: dict[str, Any], *, click: bool) -> dict[str, Any]:
    player_or_team = "" if row.get("scope") == "match_props" else row.get("player") or row.get("team") or ""
    click_row = {
        **row,
        "marketAliases": _market_display_aliases(str(row.get("market") or "")),
        "marketClickIdentity": _market_click_identity(str(row.get("market") or "")),
    }
    if player_or_team:
        _filter_sgm_board(page, str(player_or_team))
        _expand_sgm_owner(page, str(player_or_team))
    elif row.get("market"):
        _filter_sgm_board(page, _market_search_text(str(row.get("market"))))
        _expand_sgm_market(page, str(row.get("market")))

    click_result = page.evaluate(
        """
        async ({ row, oddsText, click }) => {
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
            "first h": ["first h", "first hit"],
            "first h.": ["first h", "first hit"],
            "first hit": ["first h", "first hit"],
            "first hits": ["first h", "first hit", "first hits"],
            "first hr": ["first hr", "first home run"],
            "first-hr": ["first hr", "first home run"],
            "first home run": ["first hr", "first home run"],
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
          const marketIdentity = row.marketClickIdentity || {};
          const aliases = Array.isArray(marketIdentity.aliases) && marketIdentity.aliases.length
            ? marketIdentity.aliases.map(norm).filter(Boolean)
            : Array.isArray(row.marketAliases) && row.marketAliases.length
            ? row.marketAliases.map(norm).filter(Boolean)
            : (marketAliases[wanted.market] || [wanted.market]).filter(Boolean);
          const blockedAliases = Array.isArray(marketIdentity.blockedAliases)
            ? marketIdentity.blockedAliases.map(norm).filter(Boolean)
            : [];
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
          const phraseInText = (text, phrase) => {
            if (!phrase) {
              return false;
            }
            const parts = phrase.split(" ").filter(Boolean);
            if (parts.length === 1) {
              return text.split(" ").includes(phrase);
            }
            return text.includes(phrase);
          };
          const rowMarketMatch = (text) => {
            const blockedAlias = blockedAliases.find((alias) => phraseInText(text, alias));
            if (blockedAlias) {
              return { matched: false, blockedAlias };
            }
            if (!aliases.length) {
              return { matched: true, blockedAlias: null };
            }
            const matchedAlias = aliases.find((alias) => phraseInText(text, alias));
            return { matched: Boolean(matchedAlias), blockedAlias: null, matchedAlias };
          };
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
          const selectionEvidence = (button) => {
            const classText = norm(button.className || "");
            const classes = classText.split(" ").filter(Boolean);
            const evidence = [];
            if (button.getAttribute("aria-pressed") === "true") evidence.push("aria_pressed");
            if (button.getAttribute("aria-selected") === "true") evidence.push("aria_selected");
            if (norm(button.getAttribute("data-state") || "") === "checked") evidence.push("data_state_checked");
            if (norm(button.getAttribute("data-selected") || "") === "true") evidence.push("data_selected");
            if (classes.includes("active")) evidence.push("class_active");
            if (classes.includes("selected")) evidence.push("class_selected");
            let current = button.parentElement;
            for (let depth = 0; depth < 5 && current; depth += 1) {
              const ancestorClassText = norm(current.className || "");
              const ancestorClasses = ancestorClassText.split(" ").filter(Boolean);
              if (current.getAttribute("aria-pressed") === "true") evidence.push("ancestor_aria_pressed");
              if (current.getAttribute("aria-selected") === "true") evidence.push("ancestor_aria_selected");
              if (norm(current.getAttribute("data-state") || "") === "checked") evidence.push("ancestor_data_state_checked");
              if (norm(current.getAttribute("data-selected") || "") === "true") evidence.push("ancestor_data_selected");
              if (ancestorClasses.includes("active")) evidence.push("ancestor_class_active");
              if (ancestorClasses.includes("selected")) evidence.push("ancestor_class_selected");
              current = current.parentElement;
            }
            return evidence;
          };
          const isCompactOutcomeButton = (el) => {
            const rect = el.getBoundingClientRect();
            const text = String(el.innerText || el.textContent || "").trim();
            const normalizedText = norm(text);
            return rect.width <= 360
              && rect.height <= 100
              && text.length <= 90
              && wanted.side
              && sideAliases.some((side) => normalizedText.includes(side))
              && (targetOdds == null || textHasNumber(text, targetOdds, 0.006));
          };
          const ownerMatchesText = (text) => {
            if (wanted.scope === "match props" || wanted.scope === "match_props") {
              return true;
            }
            return wanted.player
              ? text.includes(wanted.player)
              : wanted.team
              ? text.includes(wanted.team)
              : true;
          };
          const textSample = (value) => String(value || "")
            .trim()
            .replace(/\\s+/g, " ")
            .slice(0, 260);
          const visibleRowSamples = () => {
            const ownerText = wanted.player || wanted.team;
            if (!ownerText) {
              return [];
            }
            return Array.from(document.querySelectorAll("*"))
              .filter(visible)
              .map((el) => {
                const rect = el.getBoundingClientRect();
                const text = norm(el.innerText || el.textContent || "");
                return {
                  text,
                  raw: textSample(el.innerText || el.textContent || ""),
                  area: rect.width * rect.height,
                  height: rect.height,
                };
              })
              .filter((item) => item.text.includes(ownerText))
              .filter((item) => item.height <= 900)
              .sort((a, b) => a.area - b.area)
              .slice(0, 8)
              .map((item) => item.raw);
          };
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const candidateElements = () => {
            const sideButtons = Array.from(document.querySelectorAll("button[data-testid='fixture-outcome']"))
              .filter(visible)
              .filter(isCompactOutcomeButton);
            if (sideButtons.length) {
              return sideButtons;
            }
            return Array.from(document.querySelectorAll("button,[role='button']"))
              .filter(visible)
              .filter(isCompactOutcomeButton);
          };

          let scopedCandidates = [];
          let lastCandidateSamples = [];
          let marketMismatchSamples = [];
          let oddsMismatchSamples = [];
          let rowCandidateSamples = [];
          let latestVisibleRowSamples = [];
          for (let attempt = 0; attempt < 24; attempt += 1) {
            const candidates = candidateElements();
            lastCandidateSamples = candidates.slice(0, 8).map((el) => String(el.innerText || el.textContent || "").trim());
            latestVisibleRowSamples = visibleRowSamples();
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
                const lineMatchedHere = textHasLine(text);
                const ownerMatchedHere = ownerMatchesText(text);
                if ((hasSide || buttonSide.matchesWanted) && lineMatchedHere) {
                  lineSideMatched = true;
                }
                const marketMatch = rowMarketMatch(text);
                if (marketMatch.blockedAlias) {
                  marketMismatchSamples.push({
                    requestedMarket: row.market,
                    blockedAlias: marketMatch.blockedAlias,
                    sample: text.slice(0, 220),
                  });
                }
                if (rowCandidateSamples.length < 12 && (ownerMatchedHere || marketMatch.matched || lineMatchedHere)) {
                  rowCandidateSamples.push({
                    buttonText: leafText,
                    rowTextSample: textSample(current.innerText || current.textContent || ""),
                    ownerMatched: ownerMatchedHere,
                    marketMatched: Boolean(marketMatch.matched),
                    blockedAlias: marketMatch.blockedAlias || null,
                    lineMatched: lineMatchedHere,
                    sideMatched: Boolean(hasSide || buttonSide.matchesWanted),
                    rect: {
                      x: Math.round(rect.x),
                      y: Math.round(rect.y),
                      width: Math.round(rect.width),
                      height: Math.round(rect.height),
                    },
                  });
                }
                if (marketMatch.matched && rect.height <= 720) {
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
                const oddsMismatch = targetOdds != null
                  && (clickedOdds == null || Math.abs(targetOdds - clickedOdds) > 0.006);
                if (oddsMismatch) {
                  oddsMismatchSamples.push({
                    requestedOdds: targetOdds,
                    clickedOdds,
                    buttonText: leafText,
                    rowTextSample: textSample(rowContainer.innerText || rowContainer.textContent || ""),
                  });
                  continue;
                }
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
              reason: marketMismatchSamples.length
                ? "market_mismatch_requested_visible_row_conflict"
                : oddsMismatchSamples.length
                ? "odds_mismatch_requested_visible_button"
                : "no visible exact clickable selection button found",
              requestedMarket: row.market,
              candidateCount: scopedCandidates.length,
              oddsVariants,
              marketAliases: aliases,
              blockedMarketAliases: blockedAliases,
              marketMismatchSamples: marketMismatchSamples.slice(0, 5),
              oddsMismatchSamples: oddsMismatchSamples.slice(0, 5),
              matchedBy: "player_or_scope_market_line_side",
              candidateSamples: lastCandidateSamples,
              rowCandidateSamples: rowCandidateSamples.slice(0, 8),
              visibleRowSamples: latestVisibleRowSamples,
            };
          }

          scopedCandidates[0].el.scrollIntoView({ block: "center", inline: "center" });
          if (!click) {
            return {
              status: "buildable",
              candidateCount: scopedCandidates.length,
              clickedSample: scopedCandidates[0].text,
              clickedLeafText: scopedCandidates[0].leafText,
              clickedOdds: scopedCandidates[0].clickedOdds,
              requestedOdds: scopedCandidates[0].requestedOdds,
              oddsChanged: scopedCandidates[0].oddsChanged,
              clickedRect: scopedCandidates[0].rect,
              rowCandidateSamples: rowCandidateSamples.slice(0, 8),
            };
          }
          scopedCandidates[0].el.click();
          await sleep(250);
          const postClickEvidence = selectionEvidence(scopedCandidates[0].el);
          return {
            status: "clicked",
            candidateCount: scopedCandidates.length,
            clickedSample: scopedCandidates[0].text,
            clickedLeafText: scopedCandidates[0].leafText,
            clickedOdds: scopedCandidates[0].clickedOdds,
            requestedOdds: scopedCandidates[0].requestedOdds,
            oddsChanged: scopedCandidates[0].oddsChanged,
            clickedRect: scopedCandidates[0].rect,
            selectedAfterClick: postClickEvidence.length > 0,
            selectionEvidence: postClickEvidence,
            rowCandidateSamples: rowCandidateSamples.slice(0, 8),
          };
        }
        """,
        {"row": click_row, "oddsText": _display_number(row.get("odds")), "click": click},
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


def sgm_market_filter_matches(row: dict[str, Any], market_filter: Any) -> bool:
    filter_key = _text_key(market_filter)
    if not filter_key:
        return True

    canonical_target = _canonical_sgm_player_market_target(filter_key)
    if canonical_target:
        target = SGM_PLAYER_MARKET_DIAGNOSTIC_TARGETS[canonical_target]
        if target.get("batterOnly") and _is_pitcher_row(row):
            return False
        aliases = {_text_key(alias) for alias in target["aliases"]}
        aliases.add(_text_key(canonical_target))
        row_values = {
            _text_key(row.get("market")),
            _text_key(row.get("swishStatId")),
            _text_key(row.get("statId")),
            _text_key(row.get("statValue")),
        }
        return bool(aliases.intersection(row_values))

    row_aliases = _market_alias_keys(row.get("market"))
    filter_aliases = _market_alias_keys(market_filter)
    if row_aliases.intersection(filter_aliases):
        return True
    return any(
        filter_alias and row_alias and filter_alias in row_alias
        for filter_alias in filter_aliases
        for row_alias in row_aliases
    )


def _canonical_sgm_player_market_target(value: Any) -> str | None:
    value_key = _text_key(value)
    for target, config in SGM_PLAYER_MARKET_DIAGNOSTIC_TARGETS.items():
        if value_key == _text_key(target):
            return target
        aliases = {_text_key(alias) for alias in config["aliases"]}
        if value_key in aliases:
            return target
    return None


def _visible_text_mentions_target_market(
    visible_market_text: str | None,
    target_market: str,
) -> bool:
    visible_key = _text_key(visible_market_text)
    if not visible_key:
        return False
    target = SGM_PLAYER_MARKET_DIAGNOSTIC_TARGETS[target_market]
    return any(_text_key(alias) in visible_key for alias in target["aliases"])


def _is_pitcher_row(row: dict[str, Any]) -> bool:
    return _text_key(row.get("position")) in {"p", "sp", "rp", "pitcher"}


def _market_search_text(value: str) -> str:
    normalized = value.strip().lower()
    aliases = {
        "batter strikeouts": "strikeouts",
        "batter walks": "walks",
        "failed attempts": "strikeouts",
        "match home runs": "home runs",
        "match singles": "singles",
        "match triples": "triples",
        "first h": "first hit",
        "first h.": "first hit",
        "first hit": "first hit",
        "first hits": "first hit",
        "first hr": "first home run",
        "first-hr": "first home run",
        "first home run": "first home run",
        "stolen bases": "steals",
        "team hits": "hits",
        "team rbi": "rbi",
        "team rbis": "rbi",
        "team runs": "runs",
        "team total bases": "total bases",
    }
    return aliases.get(normalized, value)


def _market_alias_keys(value: Any) -> set[str]:
    text = str(value or "")
    keys = {
        _text_key(text),
        _text_key(_market_search_text(text)),
        *(_text_key(alias) for alias in _market_display_aliases(text)),
    }
    return {key for key in keys if key}


def _market_display_aliases(value: str) -> list[str]:
    normalized = value.strip().lower()
    aliases = {
        "earned runs": ["Earned Runs", "Runs Achieved", "Runs Allowed"],
        "failed attempts": ["Failed Attempts", "Strikeouts"],
        "first er": ["First ER", "First Earned Run", "First Well Deserved Run"],
        "first h": ["First H.", "First H", "First Hit"],
        "first h.": ["First H.", "First H", "First Hit"],
        "first hit": ["First H.", "First H", "First Hit"],
        "first hits": ["First H.", "First H", "First Hit", "First Hits"],
        "first hr": ["First HR", "First Home Run"],
        "first-hr": ["First HR", "First Home Run"],
        "first home run": ["First HR", "First Home Run"],
        "first so": ["First SO", "First Strike Out", "First Strikeout"],
        "hits allowed": ["Hits Allowed"],
        "match home runs": ["Play Home Runs", "Match Home Runs", "Home Runs"],
        "match singles": ["Match Singles", "Singles"],
        "match triples": ["Match Triples", "Triples"],
        "outs": ["Outs", "Eliminated"],
        "rbi": ["RBI", "RBIs", "Runs Batted In"],
        "batter strikeouts": ["Batter Strikeouts", "Strikeouts", "Failed Attempts"],
        "batter walks": ["Batter Walks", "Walks", "Base on Balls", "Bases on Balls"],
        "singles": ["Singles"],
        "strikeouts": ["Strikeouts", "Failed Attempts"],
        "stolen bases": ["Stolen Bases", "Steals"],
        "team hits": ["Team Hits", "Hits"],
        "team rbi": ["Team RBI", "Team RBIs", "RBI", "RBIs", "Runs Batted In"],
        "team rbis": ["Team RBIs", "Team RBI", "RBIs", "RBI", "Runs Batted In"],
        "team runs": ["Team Runs", "Runs"],
        "team total bases": ["Team Total Bases", "Total Bases"],
        "walks": ["Walks"],
        "win probability": ["Win Probability", "Probability of Winning"],
    }
    return aliases.get(normalized, [value])


def _market_click_identity(value: str) -> dict[str, list[str]]:
    normalized = _text_key(value)
    aliases = _unique_nonempty(_text_key(alias) for alias in _market_display_aliases(value))
    blocked_aliases_by_market = {
        "hits": ["hits allowed", "team hits"],
        "rbi": ["team rbi", "team rbis"],
        "rbis": ["team rbi", "team rbis"],
        "runs": [
            "home runs",
            "match home runs",
            "play home runs",
            "earned runs",
            "runs achieved",
            "runs allowed",
            "team runs",
            "first earned run",
            "first well deserved run",
        ],
        "team hits": ["hits allowed"],
        "team runs": [
            "home runs",
            "match home runs",
            "play home runs",
            "earned runs",
            "runs achieved",
            "runs allowed",
            "first earned run",
            "first well deserved run",
        ],
        "team rbi": ["player rbi"],
        "team rbis": ["player rbi"],
        "total bases": ["team total bases"],
    }
    blocked_aliases = _unique_nonempty(
        _text_key(alias) for alias in blocked_aliases_by_market.get(normalized, [])
    )
    return {"aliases": aliases, "blockedAliases": blocked_aliases}


def _unique_nonempty(values) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            unique.append(text)
            seen.add(text)
    return unique


def _review_slip_result(
    *,
    fixture_slug: str,
    status: str,
    board: dict[str, Any],
    selected_rows: list[dict[str, Any]],
    missing_selections: list[dict[str, Any]],
    click_results: list[dict[str, Any]],
    add_bet_result: dict[str, Any] | None = None,
    transaction_plan: dict[str, Any] | None = None,
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
        "transactionPlan": transaction_plan or {},
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
    return _make_sgm_selection_row_id(fixture_slug, row, side, include_provider_line_id=False)


def _sgm_selection_row_id_aliases(fixture_slug: str, row: dict[str, Any], side: str) -> set[str]:
    return {
        _make_sgm_selection_row_id(fixture_slug, row, side, include_provider_line_id=False),
        _make_sgm_selection_row_id(fixture_slug, row, side, include_provider_line_id=True),
    }


def _make_sgm_selection_row_id(
    fixture_slug: str,
    row: dict[str, Any],
    side: str,
    *,
    include_provider_line_id: bool,
) -> str:
    identity_parts = [
        str(fixture_slug or ""),
        str(row.get("scope") or ""),
        str(row.get("team") or ""),
        str(row.get("playerId") or row.get("player") or ""),
        str(row.get("marketId") or row.get("market") or ""),
        str(row.get("swishStatId") or row.get("statId") or ""),
        _display_number(row.get("line")),
        str(side or "").lower(),
    ]
    if include_provider_line_id:
        identity_parts.insert(6, str(row.get("lineId") or ""))
    canonical = "|".join(_text_key(part) for part in identity_parts)
    return f"sgm_{sha1(canonical.encode('utf-8')).hexdigest()[:16]}"


def _line_rows(
    lines: list[dict[str, Any]],
    market: dict[str, Any],
    team_name: str | None,
    player: dict[str, Any] | None = None,
    *,
    playability_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    stat = market.get("stat") or {}
    rows = []

    for line in lines or []:
        identifier_warnings = _sgm_identifier_warnings(market, line)
        (
            playable,
            non_playable_reasons,
            playability_mode,
            playability_warnings,
        ) = _sgm_line_playability(
            stat,
            line,
            identifier_warnings,
            playability_context=playability_context,
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
            "betFactor": ((market.get("trading") or {}).get("betFactor")),
            "customBet": bool(stat.get("customBet")),
            "liveCustomBetAvailable": bool(stat.get("liveCustomBetAvailable")),
            "playable": playable,
            "playabilityMode": playability_mode,
            "playabilityWarnings": playability_warnings,
            "nonPlayableReasons": non_playable_reasons,
            "identifierWarnings": identifier_warnings,
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


def _sgm_playability_context(slug_fixture: dict[str, Any]) -> dict[str, Any]:
    swish_game = slug_fixture.get("swishGame") or {}
    fixture_status = _text_key(slug_fixture.get("status"))
    swish_status = _text_key(swish_game.get("status"))
    live_statuses = {"live", "inprogress", "in play", "inplay"}
    pregame_statuses = {"pregame", "pre game", "scheduled", "not started", "notstarted"}
    is_live = fixture_status in live_statuses or swish_status in live_statuses
    is_pregame = not is_live and (
        swish_status in pregame_statuses
        or fixture_status in {"active", "open", "scheduled", "not started", "notstarted"}
    )
    return {
        "fixtureStatus": fixture_status,
        "swishStatus": swish_status,
        "isLive": is_live,
        "isPregame": is_pregame,
    }


def _sgm_line_playability(
    stat: dict[str, Any],
    line: dict[str, Any],
    identifier_warnings: list[str],
    *,
    playability_context: dict[str, Any] | None = None,
) -> tuple[bool, list[str], str, list[str]]:
    reasons: list[str] = []
    if not stat.get("customBet"):
        reasons.append("customBet_false")
    if line.get("suspended"):
        reasons.append("suspended")
    if line.get("over") is None:
        reasons.append("missing_over_odds")
    if line.get("under") is None:
        reasons.append("missing_under_odds")
    if not reasons and identifier_warnings:
        reasons.extend(identifier_warnings)

    if reasons:
        return False, reasons, "blocked", []

    live_custom_bet_available = bool(stat.get("liveCustomBetAvailable"))
    context = playability_context or {}
    if live_custom_bet_available:
        if context.get("isLive"):
            return True, [], "live_custom_bet", []
        return True, [], "custom_bet", []

    if context.get("isPregame"):
        return True, [], "pregame_custom_bet", ["liveCustomBetAvailable_false"]

    return False, ["liveCustomBetAvailable_false"], "blocked", []


def _sgm_identifier_warnings(
    market: dict[str, Any],
    line: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if not line.get("id"):
        warnings.append("missing_line_id")
    if not market.get("id"):
        warnings.append("missing_market_id")
    return warnings


def _sgm_market_diagnostics(
    fixture_slug: str,
    player_props: list[dict[str, Any]],
    *,
    visible_market_text: str | None = None,
) -> dict[str, Any]:
    return {
        "playerTargets": [
            _sgm_player_market_diagnostic(
                fixture_slug,
                player_props,
                target_market,
                visible_market_text=visible_market_text,
            )
            for target_market in SGM_PLAYER_MARKET_DIAGNOSTIC_TARGETS
        ]
    }


def _sgm_market_catalog(
    team_markets: list[dict[str, Any]],
    player_props: list[dict[str, Any]],
) -> dict[str, Any]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for scope, source_rows in (("team", team_markets), ("player", player_props)):
        for row in source_rows:
            key = (scope, _text_key(row.get("market")))
            item = rows.setdefault(
                key,
                {
                    "scope": scope,
                    "market": row.get("market"),
                    "rowCount": 0,
                    "playableRowCount": 0,
                    "suspendedRowCount": 0,
                    "customBetRowCount": 0,
                    "lines": set(),
                    "nonPlayableReasons": {},
                },
            )
            item["rowCount"] += 1
            if row.get("playable"):
                item["playableRowCount"] += 1
            if row.get("suspended"):
                item["suspendedRowCount"] += 1
            if row.get("customBet"):
                item["customBetRowCount"] += 1
            if row.get("line") is not None:
                item["lines"].add(row.get("line"))
            for reason in row.get("nonPlayableReasons") or []:
                reasons = item["nonPlayableReasons"]
                reasons[reason] = reasons.get(reason, 0) + 1

    return {
        "marketCount": len(rows),
        "markets": [
            {
                **item,
                "lines": sorted(item["lines"], key=lambda value: str(value)),
            }
            for item in sorted(rows.values(), key=lambda value: (value["scope"], str(value["market"] or "")))
        ],
    }


def _sgm_player_market_diagnostic(
    fixture_slug: str,
    player_props: list[dict[str, Any]],
    target_market: str,
    *,
    visible_market_text: str | None,
) -> dict[str, Any]:
    matching_rows = [
        row for row in player_props if sgm_market_filter_matches(row, target_market)
    ]
    playable_rows = [row for row in matching_rows if row.get("playable")]
    row_samples = [
        _sgm_market_diagnostic_row(fixture_slug, row)
        for row in matching_rows[:5]
    ]
    row_id_count = sum(
        2
        for row in playable_rows
        if row.get("over") is not None or row.get("under") is not None
    )
    missing_required_ids = sum(
        1
        for row in matching_rows
        if not row.get("lineId") or not row.get("marketId")
    )
    visible = _visible_text_mentions_target_market(visible_market_text, target_market)

    if row_id_count:
        status = "market_parsed_with_row_id"
    elif playable_rows:
        status = "market_parsed_but_missing_row_id"
    elif matching_rows:
        status = "market_parsed_not_playable"
    elif visible:
        status = "market_visible_but_not_parsed"
    else:
        status = "market_not_offered"

    return {
        "market": target_market,
        "aliases": SGM_PLAYER_MARKET_DIAGNOSTIC_TARGETS[target_market]["aliases"],
        "status": status,
        "parsedRows": len(matching_rows),
        "playableRows": len(playable_rows),
        "rowIdCount": row_id_count,
        "missingRequiredIds": missing_required_ids,
        "visibleInPageText": visible,
        "sampleRows": row_samples,
    }


def _sgm_market_diagnostic_row(fixture_slug: str, row: dict[str, Any]) -> dict[str, Any]:
    row_ids = {}
    if row.get("playable"):
        for side in ("over", "under"):
            if row.get(side) is not None:
                row_ids[side] = make_sgm_selection_row_id(fixture_slug, row, side)
    return {
        "player": row.get("player"),
        "team": row.get("team"),
        "position": row.get("position"),
        "market": row.get("market"),
        "line": row.get("line"),
        "playable": bool(row.get("playable")),
        "playabilityMode": row.get("playabilityMode"),
        "playabilityWarnings": row.get("playabilityWarnings") or [],
        "nonPlayableReasons": row.get("nonPlayableReasons") or [],
        "identifierWarnings": row.get("identifierWarnings") or [],
        "lineId": row.get("lineId"),
        "marketId": row.get("marketId"),
        "swishStatId": row.get("swishStatId"),
        "rowIds": row_ids,
    }


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


def _read_visible_market_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return ""


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


def _extract_mlb_moneyline_cards(page: Any) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        () => {
          const marketLabel = "winner (incl. extra innings)";
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
          const cardFor = (anchor) => {
            let current = anchor;
            let fallback = anchor.parentElement;
            for (let depth = 0; current && depth < 8; depth += 1) {
              const text = norm(current.innerText || current.textContent || "");
              if (text.includes(marketLabel)) return current;
              fallback = current;
              current = current.parentElement;
            }
            return fallback || anchor;
          };
          const seen = new Set();
          return Array.from(document.querySelectorAll('a[href*="/sports/baseball/usa/mlb/"]'))
            .map((anchor) => {
              const href = anchor.href || anchor.getAttribute("href") || "";
              if (!href || seen.has(href)) return null;
              seen.add(href);
              const card = cardFor(anchor);
              const text = (card.innerText || card.textContent || "")
                .trim()
                .replace(/\\s+/g, " ");
              const outcomeTexts = Array.from(
                card.querySelectorAll("button,[role='button'],a,div")
              )
                .filter(visible)
                .map((el) => (el.innerText || el.textContent || "").trim().replace(/\\s+/g, " "))
                .filter((value) => {
                  const odds = value.match(/(?<!\\d)(\\d+\\.\\d+)(?!\\d)/g) || [];
                  return value && value.length <= 160 && odds.length === 1;
                });
              return {
                href,
                text,
                statusText: text,
                outcomeTexts: Array.from(new Set(outcomeTexts)),
              };
            })
            .filter(Boolean);
        }
        """
    )


def _normalize_mlb_moneyline_cards(
    raw_cards: list[dict[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    games = []
    warnings = []
    seen = set()
    for raw_card in raw_cards or []:
        link = _normalize_mlb_game_link((raw_card or {}).get("href"))
        if not link or link["fixtureSlug"] in seen:
            continue
        if _is_live_mlb_moneyline_card(raw_card):
            warnings.append("live_fixture_skipped")
            continue
        if not _has_main_winner_market(raw_card):
            warnings.append("moneyline_card_not_normalized")
            continue

        selections = _moneyline_selections_from_card(raw_card, link["teams"])
        if len(selections) != 2:
            warnings.append("moneyline_card_not_normalized")
            continue

        seen.add(link["fixtureSlug"])
        games.append(
            {
                **link,
                "status": "pregame",
                "statusText": _fixture_status_text_from_card_text(
                    (raw_card or {}).get("statusText") or (raw_card or {}).get("text")
                ),
                "marketLabel": MONEYLINE_MARKET_LABEL,
                "selections": [
                    {
                        **selection,
                        "rowId": make_mlb_moneyline_row_id(
                            link["fixtureSlug"],
                            selection["team"],
                        ),
                    }
                    for selection in selections
                ],
                "warnings": [],
            }
        )
        if len(games) >= max(1, int(limit or 1)):
            break
    return {
        "games": games,
        "warnings": list(dict.fromkeys(warnings)),
    }


def make_mlb_moneyline_row_id(fixture_slug: str, team: str) -> str:
    identity = "|".join(
        [
            str(fixture_slug or "").strip(),
            MONEYLINE_MARKET_KEY,
            _text_key(team),
        ]
    )
    return f"mlb_ml_{sha1(identity.encode('utf-8')).hexdigest()[:16]}"


def _prepare_moneyline_build_selections(selections: list[dict[str, Any]]) -> dict[str, Any]:
    prepared: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for raw in selections or []:
        row_id = str((raw or {}).get("rowId") or "").strip()
        fixture_slug = str((raw or {}).get("fixtureSlug") or "").strip()
        team = str((raw or {}).get("team") or "").strip()
        researched_odds = _float_or_none((raw or {}).get("odds"))

        if not row_id or not fixture_slug or not team:
            errors.append({"selection": raw, "reason": "missing_row_id_fixture_or_team"})
            continue
        if not row_id.startswith("mlb_ml_"):
            errors.append({"selection": raw, "reason": "row_id_must_start_with_mlb_ml"})
            continue
        expected_row_id = make_mlb_moneyline_row_id(fixture_slug, team)
        if row_id != expected_row_id:
            errors.append({"selection": raw, "reason": "row_id_does_not_match_fixture_team"})
            continue

        identity = (fixture_slug, _text_key(team), row_id)
        if identity in seen:
            continue
        seen.add(identity)
        prepared.append(
            {
                "rowId": row_id,
                "fixtureSlug": fixture_slug,
                "team": team,
                "market": MONEYLINE_MARKET_LABEL,
                "marketKey": MONEYLINE_MARKET_KEY,
                "researchedOdds": researched_odds,
            }
        )

    if errors:
        return {
            "status": "blocked_invalid_row_id",
            "selections": prepared,
            "errors": errors,
        }
    if not prepared:
        return {
            "status": "blocked_missing_selection_identity",
            "selections": [],
            "errors": [{"reason": "no_valid_moneyline_selections"}],
        }
    return {"status": "ready", "selections": prepared, "errors": []}


def _moneyline_build_result(
    *,
    status: str | None,
    requested: list[dict[str, Any]],
    added: list[dict[str, Any]],
    already_present: list[dict[str, Any]],
    remaining: list[dict[str, Any]],
    warnings: list[str],
    sidebar: dict[str, Any],
) -> dict[str, Any]:
    if status is None:
        if remaining and (added or already_present):
            status = "partial_review_slip"
        elif remaining:
            status = "blocked"
        elif added:
            status = "built_for_review"
        elif already_present:
            status = "already_built_for_review"
        else:
            status = "blocked_missing_selection_identity"

    return {
        "source": "stake_ui_mlb_moneyline_review_slip",
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reviewOnly": True,
        "requestedSelections": len(requested),
        "addedSelections": added,
        "alreadyPresentSelections": already_present,
        "remainingSelections": remaining,
        "warnings": list(dict.fromkeys(warnings)),
        "sidebar": sidebar,
        "safety": {
            "enteredStakeAmount": False,
            "clickedPlaceBet": False,
        },
    }


def _click_mlb_moneyline_selection_with_retry(
    page: Any,
    selection: dict[str, Any],
    *,
    deadline: float | None,
) -> dict[str, Any]:
    first = _click_mlb_moneyline_selection_once(page, selection)
    if first.get("status") == "added":
        return first

    _expand_mlb_game_list(page, limit=100)
    second = _click_mlb_moneyline_selection_once(page, selection)
    if second.get("status") == "added":
        return second

    if _execution_deadline_expired(deadline, reserve_seconds=2.0):
        return {"status": "not_added", "reason": "local_helper_execution_timeout"}

    page.goto(STAKE_MLB_URL, wait_until="domcontentloaded", timeout=45_000)
    _expand_mlb_game_list(page, limit=100)
    third = _click_mlb_moneyline_selection_once(page, selection)
    if third.get("status") == "added":
        return third
    return {
        "status": "not_added",
        "reason": third.get("reason") or "visible_moneyline_selection_not_found_after_retry",
    }


def _click_mlb_moneyline_selection_once(page: Any, selection: dict[str, Any]) -> dict[str, Any]:
    raw_cards = _extract_mlb_moneyline_cards(page)
    board = _normalize_mlb_moneyline_cards(raw_cards, limit=100)
    game = next(
        (
            item
            for item in board.get("games") or []
            if item.get("fixtureSlug") == selection.get("fixtureSlug")
        ),
        None,
    )
    if not game:
        return {"status": "not_added", "reason": "fixture_not_visible"}

    current = next(
        (
            item
            for item in game.get("selections") or []
            if item.get("rowId") == selection.get("rowId")
            and _text_key(item.get("team")) == _text_key(selection.get("team"))
        ),
        None,
    )
    if not current:
        return {"status": "not_added", "reason": "visible_moneyline_selection_not_found"}

    clicked = _click_visible_moneyline_outcome_button(page, selection)
    if not clicked.get("clicked"):
        return {"status": "not_added", "reason": clicked.get("reason")}

    state_after = _read_stake_ui_state_from_page(page)
    sidebar = _classify_moneyline_sidebar_state(
        state_after.get("slip") or {},
        requested=[selection],
    )
    if selection["rowId"] not in set(sidebar.get("alreadyPresentRowIds") or []):
        return {"status": "not_added", "reason": "sidebar_not_updated_after_click"}

    clicked_odds = _float_or_none(current.get("odds"))
    researched_odds = _float_or_none(selection.get("researchedOdds"))
    return {
        "status": "added",
        "selection": {
            **selection,
            "clickedOdds": clicked_odds,
            "oddsMoved": (
                researched_odds is not None
                and clicked_odds is not None
                and abs(researched_odds - clicked_odds) > 0.000001
            ),
        },
    }


def _click_visible_moneyline_outcome_button(
    page: Any,
    selection: dict[str, Any],
) -> dict[str, Any]:
    fixture_slug = str(selection.get("fixtureSlug") or "").strip()
    team = str(selection.get("team") or "").strip()
    other_teams = [
        item
        for item in _fixture_matchup_from_slug(fixture_slug).get("teams", [])
        if _text_key(item) and _text_key(item) != _text_key(team)
    ]
    try:
        result = page.evaluate(
            """
            async ({ fixtureSlug, team, otherTeams }) => {
              const norm = (value) => String(value || "")
                .normalize("NFD")
                .replace(/[\\u0300-\\u036f]/g, "")
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
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const marketLabel = "winner incl. extra innings";
              const teamKey = norm(team);
              const otherTeamKeys = Array.isArray(otherTeams)
                ? otherTeams.map(norm).filter(Boolean)
                : [];
              const hrefNeedle = `/sports/baseball/usa/mlb/${fixtureSlug}`;
              const anchors = Array.from(document.querySelectorAll('a[href*="/sports/baseball/usa/mlb/"]'))
                .filter((anchor) => String(anchor.href || anchor.getAttribute("href") || "").includes(hrefNeedle));
              const cardFor = (anchor) => {
                let current = anchor;
                let fallback = anchor.parentElement;
                for (let depth = 0; current && depth < 8; depth += 1) {
                  const text = norm(current.innerText || current.textContent || "");
                  if (text.includes(marketLabel)) return current;
                  fallback = current;
                  current = current.parentElement;
                }
                return fallback || anchor;
              };
              const nearestButton = (el) => {
                let current = el;
                for (let depth = 0; current && depth < 5; depth += 1) {
                  const tag = String(current.tagName || "").toLowerCase();
                  const role = current.getAttribute("role") || "";
                  if (tag === "button" || role === "button") return current;
                  current = current.parentElement;
                }
                return el;
              };
              for (const anchor of anchors) {
                const card = cardFor(anchor);
                if (!card || !visible(card)) continue;
                const buttons = Array.from(card.querySelectorAll("button,[role='button'],a,div"))
                  .filter(visible)
                  .map(nearestButton)
                  .filter((item, index, all) => all.indexOf(item) === index);
                const candidates = buttons
                  .map((el) => {
                    const text = String(el.innerText || el.textContent || "").trim().replace(/\\s+/g, " ");
                    const key = norm(text);
                    const rect = el.getBoundingClientRect();
                    const odds = text.match(/(?<!\\d)(\\d+\\.\\d+)(?!\\d)/g) || [];
                    const containsOtherTeam = otherTeamKeys.some((otherKey) => key.includes(otherKey));
                    return { el, text, key, rect, odds, containsOtherTeam };
                  })
                  .filter((item) => item.key.includes(teamKey) && item.odds.length === 1 && !item.containsOtherTeam);
                candidates.sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
                if (!candidates.length) continue;
                const selected = candidates[0];
                selected.el.scrollIntoView({ block: "center", inline: "center" });
                await sleep(120);
                const rect = selected.el.getBoundingClientRect();
                selected.el.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2 }));
                selected.el.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2 }));
                selected.el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2 }));
                await sleep(80);
                selected.el.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2 }));
                selected.el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2 }));
                selected.el.click();
                await sleep(500);
                return {
                  clicked: true,
                  team,
                  fixtureSlug,
                  clickedBy: "dom_pointer_sequence",
                  clickedText: selected.text,
                };
              }
              return { clicked: false, reason: "visible_moneyline_button_not_clicked" };
            }
            """,
            {
                "fixtureSlug": fixture_slug,
                "team": team,
                "otherTeams": other_teams,
            },
        )
        return dict(result or {})
    except Exception as exc:
        return {"clicked": False, "reason": f"moneyline_click_failed:{exc}"}


def _moneyline_selections_from_card(
    raw_card: dict[str, Any],
    teams: list[str],
) -> list[dict[str, Any]]:
    market_outcomes = []
    for market in raw_card.get("markets") or []:
        if _text_key((market or {}).get("label")) == _text_key(MONEYLINE_MARKET_LABEL):
            market_outcomes.extend((market or {}).get("outcomes") or [])

    selections = []
    for team in teams:
        outcome = _find_moneyline_outcome(team, market_outcomes)
        if outcome is None:
            outcome = _find_moneyline_outcome_text(
                team,
                raw_card.get("outcomeTexts") or [],
                teams=teams,
            )
        if not outcome or outcome.get("disabled") is True:
            continue
        odds = _float_or_none(outcome.get("odds") or outcome.get("oddsText"))
        if odds is None or odds < 1:
            continue
        selections.append(
            {
                "team": team,
                "odds": odds,
                "playable": True,
            }
        )
    return selections


def _has_main_winner_market(raw_card: dict[str, Any]) -> bool:
    expected = _text_key(MONEYLINE_MARKET_LABEL)
    if expected in _text_key(raw_card.get("text")):
        return True
    return any(
        _text_key((market or {}).get("label")) == expected
        for market in raw_card.get("markets") or []
    )


def _find_moneyline_outcome(
    team: str,
    outcomes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    team_key = _text_key(team)
    for outcome in outcomes:
        if _text_key((outcome or {}).get("team")) == team_key:
            return outcome
    return None


def _find_moneyline_outcome_text(
    team: str,
    values: list[Any],
    *,
    teams: list[str] | None = None,
) -> dict[str, Any] | None:
    team_key = _text_key(team)
    other_team_keys = [
        _text_key(other)
        for other in teams or []
        if _text_key(other) and _text_key(other) != team_key
    ]
    for value in values:
        text = str(value or "")
        text_key = _text_key(text)
        if team_key not in text_key:
            continue
        if any(other_key in text_key for other_key in other_team_keys):
            continue
        matches = re.findall(r"(?<!\d)(\d+\.\d+)(?!\d)", text)
        if len(matches) == 1:
            return {"team": team, "oddsText": matches[0], "disabled": False}
    return None


def _is_live_mlb_moneyline_card(raw_card: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(raw_card.get("statusText") or ""),
            str(raw_card.get("text") or ""),
        ]
    ).upper()
    return "LIVE" in text or "IN PLAY" in text


def _expand_mlb_game_list(page: Any, *, limit: int) -> dict[str, Any]:
    target = max(1, int(limit or 1))
    last_count = 0
    clicks = 0
    for _ in range(12):
        result = page.evaluate(
            """
            async ({ target }) => {
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
              const visibleGameCount = () => Array.from(document.links || [])
                .filter((anchor) => String(anchor.href || anchor.getAttribute("href") || "").includes("/sports/baseball/usa/mlb/"))
                .length;
              const beforeCount = visibleGameCount();
              if (beforeCount >= target) {
                return { status: "already_expanded", visibleGameCount: beforeCount };
              }
              window.scrollTo({ top: document.body.scrollHeight, behavior: "instant" });
              const loadMore = Array.from(document.querySelectorAll("button,[role='button'],a"))
                .filter(visible)
                .find((el) => {
                  const text = norm(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
                  return text === "load more"
                    || text.includes("load more")
                    || text.includes("show more")
                    || text.includes("view more");
                });
              if (!loadMore) {
                return { status: "not_found", visibleGameCount: beforeCount };
              }
              loadMore.scrollIntoView({ block: "center", inline: "center" });
              loadMore.click();
              return { status: "clicked", visibleGameCount: beforeCount };
            }
            """,
            {"target": target},
        )
        status = str((result or {}).get("status") or "")
        visible_count = _int_or_none((result or {}).get("visibleGameCount")) or last_count
        last_count = max(last_count, visible_count)
        if status != "clicked":
            return {
                "status": "expanded" if last_count >= target else status or "not_found",
                "clicks": clicks,
                "visibleGameCount": last_count,
                "targetGameCount": target,
            }
        clicks += 1
        try:
            page.wait_for_timeout(750)
        except Exception:
            break
    return {
        "status": "expanded" if last_count >= target else "max_attempts_reached",
        "clicks": clicks,
        "visibleGameCount": last_count,
        "targetGameCount": target,
    }


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
