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
    _clear_sgm_working_selection(page)

    for row in rows:
        result = _click_one_sgm_selection(page, row)
        click_results.append(result)
        if result.get("status") != "clicked":
            break
    return click_results


def _click_sgm_add_bet_button(page: Any, *, expected_legs: int) -> dict[str, Any]:
    try:
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
        return result
    except Exception as exc:
        return {"status": "not_clicked", "reason": str(exc)}


def _read_bet_slip_state(page: Any) -> dict[str, Any]:
    try:
        return dict(
            page.evaluate(
                """
                () => {
                  const norm = (value) => String(value || "")
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
                  const rightPanel = Array.from(document.querySelectorAll("aside,[role='complementary'],body *"))
                    .filter(visible)
                    .find((el) => {
                      const rect = el.getBoundingClientRect();
                      const text = norm(el.innerText || el.textContent || "");
                      return rect.width >= 220
                        && rect.x > window.innerWidth * 0.55
                        && text.includes("bet slip");
                    });
                  const panelText = rightPanel ? norm(rightPanel.innerText || rightPanel.textContent || "") : "";
                  return {
                    betSlipEmpty: bodyText.includes("bet slip is empty"),
                    rightPanelEmpty: panelText.includes("bet slip is empty"),
                    rightPanelHasTotalStake: panelText.includes("total stake"),
                    rightPanelHasPlaceBet: panelText.includes("place bet"),
                  };
                }
                """
            )
        )
    except Exception:
        return {}


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
            "first er": ["first er", "first earned run", "first well deserved run"],
            "first so": ["first so", "first strike out", "first strikeout"],
            "hits allowed": ["hits allowed"],
            "match home runs": ["match home runs", "play home runs", "home runs"],
            "match singles": ["match singles", "singles"],
            "match triples": ["match triples", "triples"],
            "outs": ["outs", "eliminated"],
            "strikeouts": ["strikeouts", "failed attempts"],
            "walks": ["walks"],
            "win probability": ["win probability", "probability of winning"],
          };
          const aliases = (marketAliases[wanted.market] || [wanted.market]).filter(Boolean);
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
              reason: "no visible exact clickable odds cell found",
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
        {"row": row, "oddsText": _display_number(row.get("odds"))},
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
        "match home runs": "home runs",
        "match singles": "singles",
        "match triples": "triples",
    }
    return aliases.get(normalized, value)


def _market_display_aliases(value: str) -> list[str]:
    normalized = value.strip().lower()
    aliases = {
        "match home runs": ["Play Home Runs", "Match Home Runs", "Home Runs"],
        "match singles": ["Match Singles", "Singles"],
        "match triples": ["Match Triples", "Triples"],
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
        "addBetResult": add_bet_result or {},
        "warnings": board.get("warnings") or [],
        "safety": {
            "enteredStakeAmount": False,
            "clickedAddBet": bool((add_bet_result or {}).get("status") == "clicked"),
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
