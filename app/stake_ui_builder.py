from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_STAKE_URL = "https://stake.com/sports/baseball"
DEFAULT_SCREENSHOT_DIR = Path("data") / "slip_builder_screenshots"


@dataclass(frozen=True)
class StakeUiBuildConfig:
    stake_url: str = DEFAULT_STAKE_URL
    mode: str = "dry_run"
    headless: bool = False
    browser_profile_dir: str | None = None
    chrome_cdp_url: str | None = None
    timeout_ms: int = 20_000
    slow_mo_ms: int = 50
    odds_policy: str = "warn"
    odds_tolerance: float = 0.02
    screenshot_dir: Path = DEFAULT_SCREENSHOT_DIR

    @classmethod
    def from_env(cls) -> "StakeUiBuildConfig":
        return cls(
            stake_url=os.getenv("AZP_BRIDGE_STAKE_URL") or DEFAULT_STAKE_URL,
            mode=_clean_mode(os.getenv("AZP_BRIDGE_UI_MODE") or "dry_run"),
            headless=_truthy(os.getenv("AZP_BRIDGE_HEADLESS")),
            browser_profile_dir=_text(os.getenv("AZP_BRIDGE_BROWSER_PROFILE_DIR")),
            chrome_cdp_url=_text(os.getenv("AZP_BRIDGE_CHROME_CDP_URL")),
            timeout_ms=_int_env("AZP_BRIDGE_UI_TIMEOUT_MS", 20_000),
            slow_mo_ms=_int_env("AZP_BRIDGE_UI_SLOW_MO_MS", 50),
            odds_policy=_clean_odds_policy(os.getenv("AZP_BRIDGE_ODDS_POLICY") or "warn"),
            odds_tolerance=_float_env("AZP_BRIDGE_ODDS_TOLERANCE", 0.02),
            screenshot_dir=Path(
                os.getenv("AZP_BRIDGE_SCREENSHOT_DIR") or DEFAULT_SCREENSHOT_DIR
            ),
        )


@dataclass(frozen=True)
class UiLeg:
    index: int
    selection_id: str | None
    prop_id: str | None
    fixture_slug: str | None
    player: str
    team: str | None
    market_key: str | None
    market_name: str
    side: str
    line: float
    odds: float | None
    required_terms: list[str] = field(default_factory=list)

    def as_result_base(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "selectionId": self.selection_id,
            "propId": self.prop_id,
            "fixtureSlug": self.fixture_slug,
            "player": self.player,
            "team": self.team,
            "market": {
                "key": self.market_key,
                "name": self.market_name,
            },
            "side": self.side,
            "line": self.line,
            "odds": self.odds,
        }


@dataclass(frozen=True)
class CandidateMatch:
    matched: bool
    missing: list[str]
    oddsWarning: str | None = None


def selection_to_ui_leg(selection: dict[str, Any], index: int) -> UiLeg:
    player = _nested_text(selection, "player", "name")
    market_name = (
        _nested_text(selection, "market", "name")
        or _nested_text(selection, "market", "key")
    )
    side = str(selection.get("side") or "").strip().lower()
    line = _float_or_none(selection.get("line"))

    if not player:
        raise ValueError(f"Selection {index} is missing player.name.")
    if not market_name:
        raise ValueError(f"Selection {index} is missing market.name/key.")
    if side not in {"over", "under"}:
        raise ValueError(f"Selection {index} side must be over or under.")
    if line is None:
        raise ValueError(f"Selection {index} is missing a numeric line.")

    market_key = _nested_text(selection, "market", "key")
    terms = [
        _normalize_text(player),
        _normalize_text(market_name),
        side,
        _line_text(line),
    ]
    return UiLeg(
        index=index,
        selection_id=_text(selection.get("selectionId")),
        prop_id=_text(selection.get("propId")),
        fixture_slug=_text(selection.get("fixtureSlug")),
        player=player,
        team=_nested_text(selection, "team", "name"),
        market_key=market_key,
        market_name=market_name,
        side=side,
        line=line,
        odds=_float_or_none(selection.get("odds")),
        required_terms=terms,
    )


def evaluate_candidate_text(
    leg: UiLeg,
    text: str,
    config: StakeUiBuildConfig,
) -> CandidateMatch:
    normalized = _normalize_text(text)
    missing: list[str] = []
    for term in [leg.required_terms[0], leg.required_terms[1], leg.side]:
        if term and term not in normalized:
            missing.append(term)

    if not _line_appears(leg.line, normalized):
        missing.append(f"line:{_line_text(leg.line)}")

    odds_warning = None
    clean_policy = _clean_odds_policy(config.odds_policy)
    if clean_policy == "exact" and leg.odds is not None:
        visible_odds = _decimal_numbers(normalized)
        if not any(
            abs(candidate - leg.odds) <= config.odds_tolerance
            for candidate in visible_odds
        ):
            missing.append(f"odds:{_odds_text(leg.odds)}")
    elif clean_policy == "warn" and leg.odds is not None:
        visible_odds = _decimal_numbers(normalized)
        if visible_odds and not any(
            abs(candidate - leg.odds) <= config.odds_tolerance
            for candidate in visible_odds
        ):
            odds_warning = (
                f"UI odds may differ from validated odds {leg.odds}; "
                "final manual review is required."
            )

    return CandidateMatch(
        matched=not missing,
        missing=missing,
        oddsWarning=odds_warning,
    )


def choose_unique_click_candidate(
    leg: UiLeg,
    candidates: list[dict[str, Any]],
    config: StakeUiBuildConfig,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    missing_samples: list[list[str]] = []
    warnings: list[str] = []
    for candidate in candidates:
        match = evaluate_candidate_text(leg, str(candidate.get("text") or ""), config)
        if match.matched:
            matches.append(candidate)
            if match.oddsWarning:
                warnings.append(match.oddsWarning)
        elif match.missing and len(missing_samples) < 3:
            missing_samples.append(match.missing)

    if len(matches) == 1:
        return {
            **leg.as_result_base(),
            "status": "matched",
            "domIndex": matches[0].get("domIndex"),
            "text": matches[0].get("text"),
            "warnings": sorted(set(warnings)),
        }
    if len(matches) > 1:
        return {
            **leg.as_result_base(),
            "status": "blocked",
            "reason": "ambiguous_ui_matches",
            "candidateCount": len(matches),
            "warnings": [
                "Multiple visible UI elements matched the same leg. Nothing was clicked."
            ],
        }
    return {
        **leg.as_result_base(),
        "status": "blocked",
        "reason": "no_exact_ui_match",
        "candidateCount": 0,
        "missing": missing_samples or [leg.required_terms],
        "warnings": ["Required player, market, side, and exact line were not found together."],
    }


async def build_stake_ui_slip(
    job: dict[str, Any],
    config: StakeUiBuildConfig,
) -> dict[str, Any]:
    legs = _job_legs(job)
    mode = _clean_mode(config.mode)
    if mode == "dry_run":
        return _dry_run_result(legs)

    try:
        return await _run_playwright_ui_build(job, legs, config)
    except RuntimeError as exc:
        return {
            "mode": mode,
            "uiAutomationEnabled": False,
            "matched": 0,
            "clicked": 0,
            "blocked": len(legs),
            "requiresManualReview": True,
            "message": str(exc),
            "legs": [
                {
                    **leg.as_result_base(),
                    "status": "blocked",
                    "reason": "browser_automation_unavailable",
                }
                for leg in legs
            ],
        }


async def _run_playwright_ui_build(
    job: dict[str, Any],
    legs: list[UiLeg],
    config: StakeUiBuildConfig,
) -> dict[str, Any]:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - depends on local install
        raise RuntimeError(
            "Playwright is not installed for the local bridge. Run: "
            "pip install playwright && python -m playwright install chromium"
        ) from exc

    mode = _clean_mode(config.mode)
    config.screenshot_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())[:8]
    page = None
    browser = None
    context = None
    playwright = await async_playwright().start()
    try:
        context, browser = await _open_context(playwright, config)
        page = await _active_page(context, config)
        await page.goto(config.stake_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
        await _settle(page)
        if job.get("matchup"):
            await _soft_search(page, str(job["matchup"]), config)

        results: list[dict[str, Any]] = []
        for leg in legs:
            await _soft_search(page, leg.player, config)
            candidates = await _visible_click_candidates(page)
            choice = choose_unique_click_candidate(leg, candidates, config)
            if choice["status"] == "matched" and mode == "click":
                clicked = await _click_dom_candidate(page, choice.get("domIndex"))
                choice["status"] = "clicked" if clicked else "blocked"
                choice["reason"] = None if clicked else "click_failed_after_match"
                await _settle(page)
            elif choice["status"] == "matched":
                choice["status"] = "verified_visible"
            if choice["status"] != "clicked":
                choice["screenshot"] = await _safe_screenshot(
                    page,
                    config.screenshot_dir / f"{run_id}-leg-{leg.index}.png",
                )
            results.append(choice)

        clicked_count = sum(1 for row in results if row.get("status") == "clicked")
        verified_count = sum(1 for row in results if row.get("status") == "verified_visible")
        blocked_count = sum(1 for row in results if row.get("status") == "blocked")
        return {
            "mode": mode,
            "uiAutomationEnabled": True,
            "stakeUrl": config.stake_url,
            "matched": clicked_count + verified_count,
            "clicked": clicked_count,
            "blocked": blocked_count,
            "requiresManualReview": True,
            "message": _result_message(mode, clicked_count, verified_count, blocked_count),
            "safety": {
                "enteredWagerAmount": False,
                "submittedBet": False,
                "exactLineRequired": True,
                "ambiguousMatchesBlocked": True,
            },
            "legs": results,
        }
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"Stake UI timed out: {exc}") from exc
    finally:
        if config.chrome_cdp_url:
            if browser:
                await browser.close()
        elif context:
            await context.close()
        await playwright.stop()


async def _open_context(playwright: Any, config: StakeUiBuildConfig) -> tuple[Any, Any | None]:
    if config.chrome_cdp_url:
        browser = await playwright.chromium.connect_over_cdp(config.chrome_cdp_url)
        if browser.contexts:
            return browser.contexts[0], browser
        return await browser.new_context(), browser

    launch_options = {
        "headless": config.headless,
        "slow_mo": config.slow_mo_ms,
    }
    if config.browser_profile_dir:
        context = await playwright.chromium.launch_persistent_context(
            config.browser_profile_dir,
            channel="chrome",
            **launch_options,
        )
        return context, None

    try:
        browser = await playwright.chromium.launch(channel="chrome", **launch_options)
    except Exception:
        browser = await playwright.chromium.launch(**launch_options)
    return await browser.new_context(), browser


async def _active_page(context: Any, config: StakeUiBuildConfig) -> Any:
    pages = context.pages
    if pages:
        return pages[-1]
    return await context.new_page()


async def _soft_search(page: Any, text: str, config: StakeUiBuildConfig) -> None:
    if not text:
        return
    selectors = [
        "input[type='search']",
        "input[placeholder*='Search']",
        "input[aria-label*='Search']",
        "[contenteditable='true']",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() <= 0:
                continue
            await locator.fill(text, timeout=2_000)
            await _settle(page)
            return
        except Exception:
            continue


async def _visible_click_candidates(page: Any) -> list[dict[str, Any]]:
    return await page.evaluate(
        """
        () => {
          const selector = 'button,[role="button"],a,[tabindex]';
          const nodes = Array.from(document.querySelectorAll(selector));
          const visible = (node) => {
            const style = window.getComputedStyle(node);
            const box = node.getBoundingClientRect();
            return style.visibility !== 'hidden'
              && style.display !== 'none'
              && box.width > 0
              && box.height > 0;
          };
          const contextText = (node) => {
            const parts = [];
            let current = node;
            for (let depth = 0; current && depth < 7; depth += 1) {
              const text = (current.innerText || current.textContent || '').trim();
              if (text) parts.push(text);
              current = current.parentElement;
            }
            return [...new Set(parts)].join(' ');
          };
          return nodes
            .map((node, domIndex) => ({
              domIndex,
              text: contextText(node),
              ownText: (node.innerText || node.textContent || '').trim(),
              role: node.getAttribute('role') || '',
              aria: node.getAttribute('aria-label') || ''
            }))
            .filter((row, index) => visible(nodes[index]) && row.text);
        }
        """
    )


async def _click_dom_candidate(page: Any, dom_index: Any) -> bool:
    if dom_index is None:
        return False
    marker = f"azp-{uuid.uuid4()}"
    marked = await page.evaluate(
        """
        ({ domIndex, marker }) => {
          const nodes = Array.from(document.querySelectorAll('button,[role="button"],a,[tabindex]'));
          const node = nodes[domIndex];
          if (!node) return false;
          node.setAttribute('data-azp-click-target', marker);
          return true;
        }
        """,
        {"domIndex": dom_index, "marker": marker},
    )
    if not marked:
        return False
    await page.locator(f"[data-azp-click-target='{marker}']").click(timeout=5_000)
    return True


async def _safe_screenshot(page: Any, path: Path) -> str | None:
    try:
        await page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return None


async def _settle(page: Any) -> None:
    await page.wait_for_timeout(500)


def _job_legs(job: dict[str, Any]) -> list[UiLeg]:
    selections = job.get("selections") or []
    if not isinstance(selections, list) or not selections:
        raise RuntimeError("Slip job has no selections to build in the Stake UI.")
    return [
        selection_to_ui_leg(selection, index=index)
        for index, selection in enumerate(selections, start=1)
    ]


def _dry_run_result(legs: list[UiLeg]) -> dict[str, Any]:
    return {
        "mode": "dry_run",
        "uiAutomationEnabled": False,
        "matched": 0,
        "clicked": 0,
        "blocked": 0,
        "requiresManualReview": True,
        "message": "Dry-run only. No Stake UI elements were clicked.",
        "legs": [
            {
                **leg.as_result_base(),
                "status": "review_only_not_clicked",
            }
            for leg in legs
        ],
    }


def _result_message(
    mode: str,
    clicked_count: int,
    verified_count: int,
    blocked_count: int,
) -> str:
    if blocked_count:
        return (
            f"Blocked {blocked_count} leg(s). Nothing is final until every leg is "
            "visible with exact player, market, side, and line."
        )
    if mode == "click":
        return (
            f"Clicked {clicked_count} leg(s). Review the Stake slip manually. "
            "No wager amount was entered and no bet was submitted."
        )
    return f"Verified {verified_count} visible leg(s). Click mode was not enabled."


def _nested_text(source: dict[str, Any], key: str, nested_key: str) -> str | None:
    value = source.get(key)
    if not isinstance(value, dict):
        return None
    return _text(value.get(nested_key))


def _line_appears(line: float, normalized: str) -> bool:
    text = _line_text(line)
    variants = {
        text,
        text.rstrip("0").rstrip(".") if "." in text else text,
    }
    for variant in variants:
        if re.search(rf"(?<!\d){re.escape(variant)}(?!\d)", normalized):
            return True
    return False


def _line_text(value: float) -> str:
    return f"{value:g}"


def _odds_text(value: float) -> str:
    return f"{value:g}"


def _decimal_numbers(text: str) -> list[float]:
    numbers: list[float] = []
    for match in re.finditer(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", text):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if value >= 1.0:
            numbers.append(value)
    return numbers


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_mode(value: str | None) -> str:
    mode = str(value or "dry_run").strip().lower().replace("-", "_")
    if mode in {"dryrun", "dry"}:
        return "dry_run"
    return mode if mode in {"dry_run", "audit", "click"} else "dry_run"


def _clean_odds_policy(value: str | None) -> str:
    policy = str(value or "warn").strip().lower()
    return policy if policy in {"warn", "exact", "ignore"} else "warn"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
