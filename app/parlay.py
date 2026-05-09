from __future__ import annotations

from itertools import combinations
from typing import Any

from .correlation import analyze_parlay_correlation

DEFAULT_EXCLUDED_RISK_FLAGS = {"market_moved_against_over"}
PROFILE_PRESETS = {
    "custom": {
        "allowRisk": None,
        "riskPolicy": "default",
        "minConfidence": None,
        "minLegOdds": None,
        "maxLegOdds": None,
        "defaultOddsMin": 3.0,
        "defaultOddsMax": 8.0,
    },
    "safe-ish": {
        "allowRisk": False,
        "riskPolicy": "none",
        "minConfidence": "high",
        "minLegOdds": None,
        "maxLegOdds": 2.0,
        "defaultOddsMin": 2.0,
        "defaultOddsMax": 6.0,
    },
    "longshot": {
        "allowRisk": True,
        "riskPolicy": "allow",
        "minConfidence": None,
        "minLegOdds": 2.0,
        "maxLegOdds": None,
        "defaultOddsMin": 8.0,
        "defaultOddsMax": 100.0,
    },
}


def build_parlay_candidates(
    decisions: list[dict[str, Any]],
    legs: int = 3,
    odds_min: float | None = None,
    odds_max: float | None = None,
    count: int = 5,
    mode: str = "standard",
    markets: set[str] | None = None,
    allow_risk: bool = False,
    buckets: set[str] | None = None,
    max_pool: int = 40,
    locked_prop_ids: set[str] | None = None,
    locked_players: set[str] | None = None,
    locked_pick_numbers: set[int] | None = None,
    profile: str = "custom",
    min_confidence: str | None = None,
    min_leg_odds: float | None = None,
    max_leg_odds: float | None = None,
) -> dict[str, Any]:
    clean_legs = _clean_int(legs, 2, 12)
    clean_count = _clean_int(count, 1, 10)
    clean_mode = str(mode or "standard").strip().lower()
    clean_profile = _clean_profile(profile)
    filters = _profile_filters(
        clean_profile,
        allow_risk=allow_risk,
        min_confidence=min_confidence,
        min_leg_odds=min_leg_odds,
        max_leg_odds=max_leg_odds,
    )
    odds_min_value = _float_or_none(odds_min)
    odds_max_value = _float_or_none(odds_max)
    if odds_min_value is None:
        odds_min_value = filters["defaultOddsMin"]
    if odds_max_value is None:
        odds_max_value = filters["defaultOddsMax"]
    if odds_min_value is not None and odds_max_value is not None:
        if odds_min_value > odds_max_value:
            odds_min_value, odds_max_value = odds_max_value, odds_min_value

    clean_markets = markets or set()
    clean_buckets = buckets or {"watchlist"}
    eligible = _with_pick_numbers(
        _eligible_legs(
            decisions,
            markets=clean_markets,
            buckets=clean_buckets,
            filters=filters,
        )
    )
    locked, lock_warnings = _locked_legs(
        eligible,
        locked_prop_ids=locked_prop_ids or set(),
        locked_players=locked_players or set(),
        locked_pick_numbers=locked_pick_numbers or set(),
    )
    locked_prop_id_values = {
        str(leg.get("propId") or "")
        for leg in locked
        if leg.get("propId")
    }
    pool = [
        leg
        for leg in eligible[:max_pool]
        if str(leg.get("propId") or "") not in locked_prop_id_values
    ]
    candidates = []
    constraint_warnings = _locked_constraint_warnings(
        locked,
        pool,
        clean_mode,
        clean_legs,
    )

    if len(locked) > clean_legs:
        candidates_iterable = []
    else:
        candidates_iterable = combinations(pool, clean_legs - len(locked))

    for combo in candidates_iterable:
        full_combo = tuple(locked + list(combo))
        if _has_duplicate_player(full_combo):
            continue
        if not _valid_combo_for_mode(full_combo, clean_mode):
            continue
        total_odds = _total_odds(full_combo)
        if total_odds is None:
            continue
        candidates.append(_candidate(full_combo, total_odds, odds_min_value, odds_max_value))

    candidates.sort(
        key=lambda candidate: (
            not candidate["withinRange"],
            candidate["oddsDistance"],
            -candidate["score"],
            candidate["totalOdds"],
        )
    )
    selected = candidates[:clean_count]
    warnings = list(lock_warnings)
    warnings.extend(
        warning for warning in constraint_warnings if warning not in warnings
    )
    if len(locked) > clean_legs:
        warnings.append("locked_legs_exceed_requested_legs")
    if candidates and not any(candidate["withinRange"] for candidate in candidates):
        warnings.append("no_candidates_in_requested_odds_range_showing_closest")

    return {
        "requested": {
            "legs": clean_legs,
            "oddsMin": odds_min_value,
            "oddsMax": odds_max_value,
            "count": clean_count,
            "mode": clean_mode,
            "profile": clean_profile,
            "markets": sorted(markets or []),
            "allowRisk": filters["allowRisk"],
            "minConfidence": filters["minConfidence"],
            "minLegOdds": filters["minLegOdds"],
            "maxLegOdds": filters["maxLegOdds"],
            "lockedPropIds": sorted(locked_prop_ids or []),
            "lockedPlayers": sorted(locked_players or []),
            "lockedPickNumbers": sorted(locked_pick_numbers or []),
        },
        "eligibleCount": len(eligible),
        "lockedCount": len(locked),
        "candidateCount": len(selected),
        "warnings": warnings,
        "candidates": [
            {**candidate, "rank": index + 1}
            for index, candidate in enumerate(selected)
        ],
    }


def build_pick_board(
    decisions: list[dict[str, Any]],
    markets: set[str] | None = None,
    allow_risk: bool = False,
    buckets: set[str] | None = None,
    limit: int = 50,
    profile: str = "custom",
    min_confidence: str | None = None,
    min_leg_odds: float | None = None,
    max_leg_odds: float | None = None,
) -> dict[str, Any]:
    clean_limit = _clean_int(limit, 1, 500)
    clean_markets = markets or set()
    clean_buckets = buckets or {"watchlist"}
    clean_profile = _clean_profile(profile)
    filters = _profile_filters(
        clean_profile,
        allow_risk=allow_risk,
        min_confidence=min_confidence,
        min_leg_odds=min_leg_odds,
        max_leg_odds=max_leg_odds,
    )
    eligible = _with_pick_numbers(
        _eligible_legs(
            decisions,
            markets=clean_markets,
            buckets=clean_buckets,
            filters=filters,
        )
    )
    return {
        "requested": {
            "profile": clean_profile,
            "markets": sorted(clean_markets),
            "allowRisk": filters["allowRisk"],
            "buckets": sorted(clean_buckets),
            "minConfidence": filters["minConfidence"],
            "minLegOdds": filters["minLegOdds"],
            "maxLegOdds": filters["maxLegOdds"],
            "limit": clean_limit,
        },
        "eligibleCount": len(eligible),
        "picks": eligible[:clean_limit],
    }


def _eligible_legs(
    decisions: list[dict[str, Any]],
    markets: set[str],
    buckets: set[str],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    legs = []
    for row in decisions:
        if str(row.get("bucket") or "") not in buckets:
            continue
        if markets and str(row.get("marketKey") or "") not in markets:
            continue
        risk_flags = set(row.get("riskFlags") or [])
        if filters["riskPolicy"] == "none" and risk_flags:
            continue
        if (
            filters["riskPolicy"] != "allow"
            and not filters["allowRisk"]
            and risk_flags.intersection(DEFAULT_EXCLUDED_RISK_FLAGS)
        ):
            continue

        odds = _odds_for_lean(row)
        if odds is None:
            continue
        min_leg_odds_value = _float_or_none(filters["minLegOdds"])
        max_leg_odds_value = _float_or_none(filters["maxLegOdds"])
        if min_leg_odds_value is not None and odds < min_leg_odds_value:
            continue
        if max_leg_odds_value is not None and odds > max_leg_odds_value:
            continue
        if not _confidence_allows(row.get("confidence"), filters["minConfidence"]):
            continue

        explanations = _leg_explanations(row, odds)
        legs.append(
            {
                "propId": row.get("propId"),
                "fixtureSlug": _fixture_slug(row),
                "game": row.get("game"),
                "playerName": row.get("playerName"),
                "teamName": row.get("teamName"),
                "marketKey": row.get("marketKey"),
                "statKey": row.get("statKey") or row.get("marketKey"),
                "line": row.get("line"),
                "lean": row.get("lean"),
                "odds": odds,
                "overOdds": row.get("overOdds"),
                "underOdds": row.get("underOdds"),
                "score": _int_or_zero(row.get("score")),
                "confidence": row.get("confidence"),
                "recentPerGame": row.get("recentPerGame"),
                "seasonValue": row.get("seasonValue"),
                "seasonPerGame": row.get("seasonPerGame"),
                "seasonEdge": row.get("seasonEdge"),
                "gamesUsed": row.get("gamesUsed"),
                "recentGames": list(row.get("recentGames") or [])[:5],
                "seasonStats": row.get("seasonStats") or {},
                "locked": False,
                "riskFlags": list(row.get("riskFlags") or []),
                "reasons": list(row.get("reasons") or []),
                "whyIncluded": explanations["whyIncluded"],
                "whyNotStronger": explanations["whyNotStronger"],
            }
        )

    return sorted(
        legs,
        key=lambda leg: (
            -int(leg["score"]),
            -float(leg["odds"]),
            str(leg.get("playerName") or ""),
        ),
    )


def _with_pick_numbers(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**leg, "pickNumber": index}
        for index, leg in enumerate(legs, start=1)
    ]


def _locked_legs(
    eligible: list[dict[str, Any]],
    locked_prop_ids: set[str],
    locked_players: set[str],
    locked_pick_numbers: set[int],
) -> tuple[list[dict[str, Any]], list[str]]:
    locked: list[dict[str, Any]] = []
    warnings: list[str] = []
    used_prop_ids: set[str] = set()

    for prop_id in sorted(_clean_string_set(locked_prop_ids)):
        match = next(
            (
                leg
                for leg in eligible
                if str(leg.get("propId") or "") == prop_id
            ),
            None,
        )
        if match is None:
            warnings.append(f"locked_prop_not_found:{prop_id}")
            continue
        _append_locked_leg(locked, match, used_prop_ids)

    for number in sorted(_clean_int_set(locked_pick_numbers)):
        if number < 1 or number > len(eligible):
            warnings.append(f"locked_pick_not_found:{number}")
            continue
        _append_locked_leg(locked, eligible[number - 1], used_prop_ids)

    for player in sorted(_clean_string_set(locked_players)):
        match = next(
            (
                leg
                for leg in eligible
                if _normalize_key(leg.get("playerName")) == _normalize_key(player)
                and str(leg.get("propId") or "") not in used_prop_ids
            ),
            None,
        )
        if match is None:
            warnings.append(f"locked_player_not_found:{player}")
            continue
        _append_locked_leg(locked, match, used_prop_ids)

    return locked, warnings


def _append_locked_leg(
    locked: list[dict[str, Any]],
    leg: dict[str, Any],
    used_prop_ids: set[str],
) -> None:
    prop_id = str(leg.get("propId") or "")
    if prop_id in used_prop_ids:
        return
    locked.append({**leg, "locked": True})
    if prop_id:
        used_prop_ids.add(prop_id)


def _locked_constraint_warnings(
    locked: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    mode: str,
    requested_legs: int,
) -> list[str]:
    if not locked:
        return []

    warnings: list[str] = []
    locked_tuple = tuple(locked)
    if _has_duplicate_player(locked_tuple):
        warnings.append("locked_legs_have_duplicate_player")

    locked_fixture_counts = _fixture_counts(locked_tuple)
    if mode == "standard":
        if any(count > 1 for count in locked_fixture_counts.values()):
            warnings.append("locked_legs_conflict_with_standard_mode")
        return warnings

    if mode == "sgp":
        if len(locked) >= requested_legs and any(
            count < 2 for count in locked_fixture_counts.values()
        ):
            warnings.append("locked_legs_conflict_with_sgp_mode")
        available_fixture_counts = _fixture_counts(tuple(pool))
        for fixture, count in locked_fixture_counts.items():
            if count == 1 and available_fixture_counts.get(fixture, 0) <= 0:
                warnings.append(f"locked_sgp_fixture_needs_second_leg:{fixture}")
    return warnings


def _candidate(
    legs: tuple[dict[str, Any], ...],
    total_odds: float,
    odds_min: float | None,
    odds_max: float | None,
) -> dict[str, Any]:
    correlation_risk = analyze_parlay_correlation(legs)
    return {
        "rank": None,
        "legCount": len(legs),
        "totalOdds": round(total_odds, 4),
        "rawProductOdds": correlation_risk["rawProductOdds"],
        "score": sum(int(leg.get("score") or 0) for leg in legs),
        "withinRange": _within_range(total_odds, odds_min, odds_max),
        "oddsDistance": _odds_distance(total_odds, odds_min, odds_max),
        "fixtureCounts": _fixture_counts(legs),
        "conflictWarnings": _candidate_conflict_warnings(legs),
        "correlationRisk": correlation_risk,
        "legs": list(legs),
    }


def _candidate_conflict_warnings(legs: tuple[dict[str, Any], ...]) -> list[str]:
    warnings = []
    for fixture, count in _fixture_counts(legs).items():
        if count >= 2:
            warnings.append(f"same_game_correlation:{fixture}:{count}")
    for market, count in _value_counts(legs, "marketKey").items():
        if count >= 3:
            warnings.append(f"repeated_market:{market}:{count}")
    for team, count in _value_counts(legs, "teamName").items():
        if count >= 3:
            warnings.append(f"team_cluster:{team}:{count}")
    return warnings


def _valid_combo_for_mode(legs: tuple[dict[str, Any], ...], mode: str) -> bool:
    if mode == "sgp":
        return _valid_sgp_combo(legs)
    return _valid_standard_combo(legs)


def _valid_standard_combo(legs: tuple[dict[str, Any], ...]) -> bool:
    return all(count == 1 for count in _fixture_counts(legs).values())


def _valid_sgp_combo(legs: tuple[dict[str, Any], ...]) -> bool:
    return all(count >= 2 for count in _fixture_counts(legs).values())


def _fixture_counts(legs: tuple[dict[str, Any], ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for leg in legs:
        fixture = str(leg.get("fixtureSlug") or "unknown-fixture")
        counts[fixture] = counts.get(fixture, 0) + 1
    return counts


def _value_counts(
    legs: tuple[dict[str, Any], ...],
    key: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for leg in legs:
        value = str(leg.get(key) or "unknown").strip() or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _has_duplicate_player(legs: tuple[dict[str, Any], ...]) -> bool:
    seen = set()
    for leg in legs:
        key = str(leg.get("playerName") or "").strip().lower()
        if key in seen:
            return True
        seen.add(key)
    return False


def _clean_string_set(values: set[str]) -> set[str]:
    return {str(value).strip() for value in values if str(value).strip()}


def _clean_profile(profile: str | None) -> str:
    cleaned = str(profile or "custom").strip().lower()
    return cleaned if cleaned in PROFILE_PRESETS else "custom"


def _profile_filters(
    profile: str,
    allow_risk: bool,
    min_confidence: str | None,
    min_leg_odds: float | None,
    max_leg_odds: float | None,
) -> dict[str, Any]:
    settings = PROFILE_PRESETS[_clean_profile(profile)]
    configured_allow_risk = settings["allowRisk"]
    return {
        "allowRisk": allow_risk if configured_allow_risk is None else configured_allow_risk,
        "riskPolicy": settings["riskPolicy"],
        "minConfidence": min_confidence or settings["minConfidence"],
        "minLegOdds": (
            _float_or_none(min_leg_odds)
            if min_leg_odds is not None
            else settings["minLegOdds"]
        ),
        "maxLegOdds": (
            _float_or_none(max_leg_odds)
            if max_leg_odds is not None
            else settings["maxLegOdds"]
        ),
        "defaultOddsMin": settings["defaultOddsMin"],
        "defaultOddsMax": settings["defaultOddsMax"],
    }


def _confidence_allows(value: Any, minimum: str | None) -> bool:
    if not minimum:
        return True
    return _confidence_rank(value) >= _confidence_rank(minimum)


def _confidence_rank(value: Any) -> int:
    ranks = {"low": 1, "medium": 2, "high": 3}
    return ranks.get(str(value or "").strip().lower(), 0)


def _clean_int_set(values: set[int]) -> set[int]:
    cleaned = set()
    for value in values:
        try:
            cleaned.add(int(value))
        except (TypeError, ValueError):
            continue
    return cleaned


def _normalize_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _total_odds(legs: tuple[dict[str, Any], ...]) -> float | None:
    total = 1.0
    for leg in legs:
        odds = _float_or_none(leg.get("odds"))
        if odds is None:
            return None
        total *= odds
    return total


def _within_range(
    value: float,
    odds_min: float | None,
    odds_max: float | None,
) -> bool:
    if odds_min is not None and value < odds_min:
        return False
    if odds_max is not None and value > odds_max:
        return False
    return True


def _odds_distance(
    value: float,
    odds_min: float | None,
    odds_max: float | None,
) -> float:
    if _within_range(value, odds_min, odds_max):
        return 0.0
    distances = []
    if odds_min is not None:
        distances.append(abs(value - odds_min))
    if odds_max is not None:
        distances.append(abs(value - odds_max))
    return round(min(distances) if distances else 0.0, 4)


def _odds_for_lean(row: dict[str, Any]) -> float | None:
    lean = str(row.get("lean") or "")
    if lean == "over":
        return _float_or_none(row.get("overOdds"))
    if lean == "under_or_avoid_over":
        return _float_or_none(row.get("underOdds"))
    return None


def _leg_explanations(row: dict[str, Any], odds: float) -> dict[str, list[str]]:
    reasons = set(row.get("reasons") or [])
    risk_flags = set(row.get("riskFlags") or [])
    why_included = []
    why_not_stronger = []

    if reasons.intersection(
        {
            "recent_per_game_above_line",
            "recent_per_game_above_market_threshold",
            "pitching_recent_average_clears_strikeout_line",
        }
    ):
        why_included.append("recent form clears market line")
    if "season_baseline_supports_over" in reasons:
        why_included.append("season baseline supports the lean")
    if "recent_and_season_agree" in reasons:
        why_included.append("recent and season form agree")
    if _int_or_zero(row.get("score")) >= 85:
        why_included.append("high analyzer score")
    if str(row.get("confidence") or "").lower() == "high":
        why_included.append("high confidence match")
    if not why_included:
        why_included.append("best available analyzer match")

    risk_text = {
        "market_moved_against_over": "market moved against the over",
        "small_recent_sample": "recent sample is small",
        "sparse_market": "market is naturally volatile",
        "long_over_odds": "long odds carry higher variance",
        "high_line": "line is above the normal market tier",
        "season_baseline_below_line": "season baseline does not clear the line",
        "recent_form_spike": "recent form is much hotter than season baseline",
    }
    for flag in sorted(risk_flags):
        why_not_stronger.append(risk_text.get(flag, flag.replace("_", " ")))
    if odds >= 4.0 and "long odds carry higher variance" not in why_not_stronger:
        why_not_stronger.append("long odds carry higher variance")
    confidence = str(row.get("confidence") or "").lower()
    if confidence and confidence != "high":
        why_not_stronger.append(f"{confidence} confidence")

    return {
        "whyIncluded": why_included,
        "whyNotStronger": why_not_stronger,
    }


def _fixture_slug(row: dict[str, Any]) -> str:
    fixture = row.get("fixtureSlug")
    if fixture:
        return str(fixture)
    prop_id = str(row.get("propId") or "")
    if ":" in prop_id:
        return prop_id.split(":", 1)[0]
    return "unknown-fixture"


def _clean_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(parsed, maximum))


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
