from __future__ import annotations

from collections import Counter, defaultdict
from functools import reduce
from operator import mul
from typing import Any


def analyze_parlay_correlation(
    legs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    stake_quoted_odds: float | None = None,
) -> dict[str, Any]:
    enriched = [_leg_context(leg) for leg in legs]
    raw_product = _raw_product_odds(enriched)
    tags: set[str] = set()
    game_script_tags: set[str] = set()
    reasons: list[str] = []

    fixture_groups = _group_by(enriched, "fixture")
    for fixture, fixture_legs in fixture_groups.items():
        if len(fixture_legs) < 2:
            continue

        tags.add("same_fixture")
        reasons.append(f"{fixture} has {len(fixture_legs)} same-game legs")
        _tag_same_team(fixture_legs, tags, reasons)
        _tag_pitcher_relationships(fixture_legs, tags, reasons)
        _tag_low_scoring_scripts(fixture_legs, tags, game_script_tags, reasons)
        _tag_pitcher_dominance(fixture_legs, tags, game_script_tags, reasons)
        _tag_offense_stack(fixture_legs, tags, game_script_tags, reasons)

    if "low_scoring_game_script" in game_script_tags and "offense_stack" in game_script_tags:
        game_script_tags.add("conflicting_legs")
        reasons.append("combo mixes low-scoring and offense-friendly game scripts")

    comparison = _stake_reprice_comparison(raw_product, stake_quoted_odds)
    if comparison["repriceSignal"] == "confirmed_extreme_stake_reprice":
        game_script_tags.add("high_variance_rare_outcome")

    risk_level = _risk_level(tags, game_script_tags, comparison)
    sgp_repricing_likely = bool(tags.intersection(_SGP_SIGNAL_TAGS))
    quote_needed = sgp_repricing_likely and stake_quoted_odds is None
    warning = _warning(
        sgp_repricing_likely=sgp_repricing_likely,
        quote_needed=quote_needed,
        comparison=comparison,
    )

    return {
        "rawProductOdds": raw_product,
        "stakeQuotedOdds": comparison["stakeQuotedOdds"],
        "repriceFactor": comparison["repriceFactor"],
        "repricePercent": comparison["repricePercent"],
        "repriceSignal": comparison["repriceSignal"],
        "quoteNeeded": quote_needed,
        "sgpRepricingLikely": sgp_repricing_likely,
        "riskLevel": risk_level,
        "tags": sorted(tags),
        "gameScriptTags": sorted(game_script_tags),
        "reasons": _unique(reasons),
        "warning": warning,
    }


_SGP_SIGNAL_TAGS = {
    "same_fixture",
    "same_team",
    "opposing_pitchers",
    "pitcher_vs_batter",
    "multiple_unders_same_game",
    "opposing_pitcher_earned_runs_over",
}


def _tag_same_team(
    legs: list[dict[str, Any]],
    tags: set[str],
    reasons: list[str],
) -> None:
    team_counts = Counter(leg["team"] for leg in legs if leg["team"])
    for team, count in team_counts.items():
        if count >= 2:
            tags.add("same_team")
            reasons.append(f"{count} legs are tied to {team}")


def _tag_pitcher_relationships(
    legs: list[dict[str, Any]],
    tags: set[str],
    reasons: list[str],
) -> None:
    pitcher_legs = [leg for leg in legs if leg["isPitcherProp"]]
    batter_legs = [leg for leg in legs if leg["isBatterProp"]]
    pitcher_teams = {leg["team"] for leg in pitcher_legs if leg["team"]}

    if len(pitcher_legs) >= 2 and len(pitcher_teams) >= 2:
        tags.add("opposing_pitchers")
        reasons.append("opposing pitcher props are linked inside the same game")

    if pitcher_legs and batter_legs:
        tags.add("pitcher_vs_batter")
        reasons.append("pitcher props and batter props share the same game script")


def _tag_low_scoring_scripts(
    legs: list[dict[str, Any]],
    tags: set[str],
    game_script_tags: set[str],
    reasons: list[str],
) -> None:
    under_legs = [leg for leg in legs if leg["side"] == "under"]
    pitcher_under_legs = [
        leg
        for leg in under_legs
        if leg["isPitcherProp"] or leg["isRunPreventionMarket"]
    ]

    if len(under_legs) >= 2:
        tags.add("multiple_unders_same_game")
        reasons.append("multiple unders in one game can trigger SGP repricing")

    if len(pitcher_under_legs) >= 2:
        game_script_tags.add("low_scoring_game_script")
        reasons.append("multiple pitcher/run-prevention unders imply a low-scoring script")

    pitcher_under_teams = {leg["team"] for leg in pitcher_under_legs if leg["team"]}
    if len(pitcher_under_legs) >= 2 and len(pitcher_under_teams) >= 2:
        game_script_tags.add("mutual_starter_suppression")
        reasons.append("both opposing pitchers need run suppression for the combo to hit")


def _tag_pitcher_dominance(
    legs: list[dict[str, Any]],
    tags: set[str],
    game_script_tags: set[str],
    reasons: list[str],
) -> None:
    dominance_legs = [leg for leg in legs if leg["isPitcherDominance"]]
    batter_unders = [leg for leg in legs if leg["isBatterProp"] and leg["side"] == "under"]
    if dominance_legs and batter_unders:
        game_script_tags.add("pitcher_dominance_stack")
        reasons.append("pitcher dominance legs pair with batter unders")
    if len(dominance_legs) >= 2:
        game_script_tags.add("pitcher_dominance_stack")
        reasons.append("multiple pitcher dominance legs share a narrow game script")


def _tag_offense_stack(
    legs: list[dict[str, Any]],
    tags: set[str],
    game_script_tags: set[str],
    reasons: list[str],
) -> None:
    batter_overs_by_team: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pitcher_damage_overs = []

    for leg in legs:
        if leg["isBatterProp"] and leg["side"] == "over" and leg["team"]:
            batter_overs_by_team[leg["team"]].append(leg)
        if leg["isPitcherDamageOver"]:
            pitcher_damage_overs.append(leg)

    if any(len(team_legs) >= 2 for team_legs in batter_overs_by_team.values()):
        game_script_tags.add("offense_stack")
        reasons.append("multiple batter overs on the same team imply an offense stack")

    for pitcher_leg in pitcher_damage_overs:
        for team, batter_overs in batter_overs_by_team.items():
            if team != pitcher_leg["team"] and batter_overs:
                tags.add("opposing_pitcher_earned_runs_over")
                game_script_tags.add("offense_stack")
                reasons.append("batter overs pair with opposing pitcher damage over")
                return


def _stake_reprice_comparison(
    raw_product_odds: float | None,
    stake_quoted_odds: float | None,
) -> dict[str, Any]:
    quote = _float_or_none(stake_quoted_odds)
    if raw_product_odds is None or raw_product_odds <= 0 or quote is None:
        return {
            "stakeQuotedOdds": quote,
            "repriceFactor": None,
            "repricePercent": None,
            "repriceSignal": "unconfirmed",
        }

    factor = quote / raw_product_odds
    percent = (factor - 1.0) * 100.0
    signal = "no_confirmed_reprice"
    if factor >= 5.0 or factor <= 0.2:
        signal = "confirmed_extreme_stake_reprice"
    elif factor >= 1.25 or factor <= 0.8:
        signal = "confirmed_stake_reprice"

    return {
        "stakeQuotedOdds": round(quote, 4),
        "repriceFactor": round(factor, 2),
        "repricePercent": round(percent, 2),
        "repriceSignal": signal,
    }


def _risk_level(
    tags: set[str],
    game_script_tags: set[str],
    comparison: dict[str, Any],
) -> str:
    if comparison["repriceSignal"] == "confirmed_extreme_stake_reprice":
        return "extreme"
    if comparison["repriceSignal"] == "confirmed_stake_reprice":
        return "high"

    score = 0
    score += 2 if "same_fixture" in tags else 0
    score += 1 if "same_team" in tags else 0
    score += 2 if "opposing_pitchers" in tags else 0
    score += 2 if "pitcher_vs_batter" in tags else 0
    score += 2 if "multiple_unders_same_game" in tags else 0
    score += 2 if "opposing_pitcher_earned_runs_over" in tags else 0
    score += len(game_script_tags)

    if score >= 7:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def _warning(
    sgp_repricing_likely: bool,
    quote_needed: bool,
    comparison: dict[str, Any],
) -> str | None:
    signal = comparison["repriceSignal"]
    factor = comparison["repriceFactor"]
    if signal == "confirmed_extreme_stake_reprice":
        direction = "above" if factor and factor >= 1 else "below"
        return f"Confirmed extreme Stake SGP reprice: {factor}x {direction} raw product."
    if signal == "confirmed_stake_reprice":
        direction = "above" if factor and factor >= 1 else "below"
        return f"Confirmed Stake SGP reprice: {factor}x {direction} raw product."
    if quote_needed:
        return "SGP repricing likely; Stake quote needed before treating raw odds as final."
    if sgp_repricing_likely:
        return "Correlation-sensitive combo; compare against Stake quote if available."
    return None


def _leg_context(leg: dict[str, Any]) -> dict[str, Any]:
    market = _normalize(leg.get("marketKey"))
    stat = _normalize(leg.get("statKey"))
    text = f"{market} {stat}"
    side = _side(leg)
    is_pitcher = _is_pitcher_market(text)
    is_batter = _is_batter_market(text) and not is_pitcher
    is_run_prevention = _is_run_prevention_market(text)
    return {
        "fixture": str(leg.get("fixtureSlug") or "unknown-fixture"),
        "team": str(leg.get("teamName") or "").strip(),
        "player": str(leg.get("playerName") or "").strip(),
        "market": market,
        "stat": stat,
        "side": side,
        "odds": _odds_for_leg(leg),
        "isPitcherProp": is_pitcher,
        "isBatterProp": is_batter,
        "isRunPreventionMarket": is_run_prevention,
        "isPitcherDominance": _is_pitcher_dominance(text, side),
        "isPitcherDamageOver": _is_pitcher_damage_over(text, side),
    }


def _raw_product_odds(legs: list[dict[str, Any]]) -> float | None:
    odds = [leg["odds"] for leg in legs]
    if not odds or any(value is None for value in odds):
        return None
    return round(float(reduce(mul, odds, 1.0)), 4)


def _odds_for_leg(leg: dict[str, Any]) -> float | None:
    direct = _float_or_none(leg.get("odds"))
    if direct is not None:
        return direct
    side = _side(leg)
    if side == "under":
        return _float_or_none(leg.get("underOdds"))
    if side == "over":
        return _float_or_none(leg.get("overOdds"))
    return _float_or_none(leg.get("overOdds") or leg.get("underOdds"))


def _side(leg: dict[str, Any]) -> str:
    lean = str(leg.get("lean") or "").strip().lower()
    selection = str(leg.get("selection") or "").strip().lower()
    text = f"{lean} {selection}"
    if "under" in text:
        return "under"
    if "over" in text:
        return "over"
    return "unknown"


def _is_pitcher_market(text: str) -> bool:
    return any(
        token in text
        for token in (
            "pitcher",
            "strikeout",
            "earned run",
            "earnedrun",
            "earned-runs",
            "first-earned-run",
            "hits allowed",
            "hits-allowed",
            "outs",
            "walks allowed",
            "runs allowed",
        )
    )


def _is_batter_market(text: str) -> bool:
    return any(
        token in text
        for token in (
            "hits",
            "runs",
            "rbi",
            "total bases",
            "total-bases",
            "home runs",
            "home-runs",
            "walks",
        )
    )


def _is_run_prevention_market(text: str) -> bool:
    return any(
        token in text
        for token in (
            "earned run",
            "earnedrun",
            "earned-runs",
            "first-earned-run",
            "runs allowed",
            "hits allowed",
            "hits-allowed",
        )
    )


def _is_pitcher_dominance(text: str, side: str) -> bool:
    if side == "over" and any(token in text for token in ("strikeout", "outs")):
        return True
    if side == "under" and _is_run_prevention_market(text):
        return True
    if side == "under" and "walk" in text:
        return True
    return False


def _is_pitcher_damage_over(text: str, side: str) -> bool:
    return side == "over" and _is_run_prevention_market(text)


def _group_by(
    legs: list[dict[str, Any]],
    key: str,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leg in legs:
        grouped[str(leg.get(key) or "unknown")].append(leg)
    return grouped


def _unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
