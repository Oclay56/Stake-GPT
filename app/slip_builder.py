from __future__ import annotations

import math
from typing import Any

from .mlb_props import slug_key


FINAL_STATUS_POINTS = {
    "playable": 18,
    "playable_but_volatile": 8,
    "borderline": -12,
    "avoid": -40,
    "blocked": -100,
    "needs_recheck": -25,
}

MAX_LEGS_PER_GROUP = 16
MAX_GROUP_ODDS = 501.0


def build_slip_candidate_response(
    rows: list[dict[str, Any]],
    *,
    target_odds_min: float,
    target_odds_max: float | None,
    min_legs: int,
    max_legs: int,
    preferred_markets: list[str],
    preferred_side: str,
    mode: str,
    quality_floor: float,
    allow_no_pick: bool,
) -> dict[str, Any]:
    preferred_market_keys = {slug_key(market) for market in preferred_markets if slug_key(market)}
    clean_mode = _clean_mode(mode)
    clean_side = preferred_side if preferred_side in {"over", "under"} else "any"
    max_legs = max(1, min(int(max_legs), MAX_LEGS_PER_GROUP))
    min_legs = max(1, min(int(min_legs), max_legs))
    product_odds_cap = min(_float_or_none(target_odds_max) or MAX_GROUP_ODDS, MAX_GROUP_ODDS)
    candidates = [
        _candidate_row(row, preferred_market_keys, clean_side, clean_mode)
        for row in rows
    ]
    hard_filtered = [
        candidate
        for candidate in candidates
        if _eligible_for_mode(candidate, preferred_market_keys, clean_side, clean_mode)
    ]
    clean = [
        candidate
        for candidate in hard_filtered
        if candidate["candidateScore"] >= quality_floor
        and candidate["decisionProfile"].get("finalStatus") not in {"avoid", "blocked"}
    ]
    borderline = [
        candidate
        for candidate in hard_filtered
        if candidate not in clean
        and candidate["decisionProfile"].get("finalStatus") not in {"avoid", "blocked"}
    ]
    rejected = [
        candidate
        for candidate in candidates
        if candidate not in clean and candidate not in borderline
    ]

    best_clean = _build_greedy_slip(
        clean,
        target_odds_min=target_odds_min,
        target_odds_max=target_odds_max,
        min_legs=min_legs,
        max_legs=max_legs,
        product_odds_cap=product_odds_cap,
    )
    build_status = _build_status(
        best_clean,
        clean,
        target_odds_min=target_odds_min,
        min_legs=min_legs,
        allow_no_pick=allow_no_pick,
    )
    best_clean["buildStatus"] = build_status
    best_clean["targetReachableCleanly"] = (
        best_clean["rawProductOdds"] >= target_odds_min
        and best_clean["legCount"] >= min_legs
    )

    return {
        "purpose": "slip_candidate_builder",
        "decisionOwner": "custom_gpt",
        "builderRole": "candidate_support_not_final_recommendation",
        "mode": clean_mode,
        "target": {
            "oddsMin": target_odds_min,
            "oddsMax": target_odds_max,
            "minLegs": min_legs,
            "maxLegs": max_legs,
            "qualityFloor": quality_floor,
            "preferredSide": clean_side,
            "preferredMarkets": sorted(preferred_market_keys),
        },
        "guardrails": {
            "maxLegsPerGroup": MAX_LEGS_PER_GROUP,
            "maxGroupOdds": int(MAX_GROUP_ODDS),
            "stakeLast5OnlyHardBlock": True,
            "noForcedWeakPicks": True,
        },
        "buildStatus": build_status,
        "targetReachableCleanly": best_clean["targetReachableCleanly"],
        "cleanCandidateCount": len(clean),
        "borderlineCandidateCount": len(borderline),
        "rejectedCandidateCount": len(rejected),
        "bestCleanSlip": best_clean,
        "rejectedSummary": _rejected_summary(rejected),
        "marketCoverage": _market_coverage(candidates),
        "notes": [
            "Raw product odds are not a final Stake same-game parlay quote.",
            "GPT must validate exact selections before answering.",
            "If targetReachableCleanly is false, fewer clean legs beat forced weak legs.",
        ],
    }


def _candidate_row(
    row: dict[str, Any],
    preferred_markets: set[str],
    preferred_side: str,
    mode: str,
) -> dict[str, Any]:
    profile = row.get("decisionProfile") or {}
    helper = _float_or_none(row.get("helperStrength")) or 50.0
    market_key = slug_key((row.get("market") or {}).get("key"))
    score = helper + FINAL_STATUS_POINTS.get(profile.get("finalStatus"), 0)
    if preferred_side in {"over", "under"} and row.get("side") == preferred_side:
        score += 5
    if preferred_markets and market_key in preferred_markets:
        score += 5
    if mode == "strict_diversity":
        score += _diversity_seed(market_key)
    if mode == "longshot":
        score += min(12, (_float_or_none(row.get("odds")) or 1.0) * 2)
    return {
        **row,
        "candidateScore": round(score, 2),
        "candidateReason": _candidate_reason(row, profile),
    }


def _eligible_for_mode(
    candidate: dict[str, Any],
    preferred_markets: set[str],
    preferred_side: str,
    mode: str,
) -> bool:
    if preferred_markets and slug_key((candidate.get("market") or {}).get("key")) not in preferred_markets:
        return False
    if preferred_side in {"over", "under"} and candidate.get("side") != preferred_side:
        return False
    if mode == "mega_under" and candidate.get("side") != "under":
        return False
    return True


def _build_greedy_slip(
    candidates: list[dict[str, Any]],
    *,
    target_odds_min: float,
    target_odds_max: float | None,
    min_legs: int,
    max_legs: int,
    product_odds_cap: float,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    used_players: set[str] = set()
    market_counts: dict[str, int] = {}
    sorted_candidates = sorted(
        candidates,
        key=lambda row: (row["candidateScore"], _float_or_none(row.get("odds")) or 0.0),
        reverse=True,
    )
    for candidate in sorted_candidates:
        if len(selected) >= max_legs:
            break
        candidate_odds = _float_or_none(candidate.get("odds")) or 1.0
        if selected and _raw_product_odds(selected) * candidate_odds > product_odds_cap:
            continue
        if not selected and candidate_odds > product_odds_cap:
            continue
        player_key = slug_key((candidate.get("player") or {}).get("key") or (candidate.get("player") or {}).get("name"))
        if player_key in used_players:
            continue
        market_key = slug_key((candidate.get("market") or {}).get("key"))
        if _market_repeat_penalty(market_counts.get(market_key, 0)) > candidate["candidateScore"] - 50:
            continue
        selected.append(candidate)
        used_players.add(player_key)
        market_counts[market_key] = market_counts.get(market_key, 0) + 1
        if len(selected) >= min_legs and _raw_product_odds(selected) >= target_odds_min:
            if target_odds_max is None or _raw_product_odds(selected) <= target_odds_max:
                break

    return {
        "legCount": len(selected),
        "rawProductOdds": _raw_product_odds(selected),
        "legs": selected,
        "integrityReport": {
            "allLinesStakeFeedValidatedOnly": True,
            "requiresFinalUiQuote": True,
            "maxGroupOdds": int(MAX_GROUP_ODDS),
            "marketConcentration": _concentration(selected, "market"),
            "sideConcentration": _concentration(selected, "side"),
            "riskFlags": _risk_flags(selected),
        },
    }


def _build_status(
    slip: dict[str, Any],
    clean: list[dict[str, Any]],
    *,
    target_odds_min: float,
    min_legs: int,
    allow_no_pick: bool,
) -> str:
    if len(clean) < min_legs:
        return "insufficient_clean_candidates"
    if slip["legCount"] < min_legs:
        return "insufficient_clean_candidates"
    if slip["rawProductOdds"] < target_odds_min:
        return "target_requires_weak_legs" if allow_no_pick else "build_not_recommended"
    return "build_recommended"


def _candidate_reason(row: dict[str, Any], profile: dict[str, Any]) -> str:
    return " / ".join(
        str(part)
        for part in [
            profile.get("finalStatus"),
            profile.get("evidenceStrength"),
            profile.get("trendAlignment"),
        ]
        if part
    )


def _rejected_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = (row.get("decisionProfile") or {}).get("finalStatus") or "filtered_out"
        counts[str(status)] = counts.get(str(status), 0) + 1
    return counts


def _market_coverage(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = slug_key((row.get("market") or {}).get("key")) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _raw_product_odds(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return round(math.prod(_float_or_none(row.get("odds")) or 1.0 for row in rows), 4)


def _concentration(rows: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        if kind == "market":
            value = slug_key((row.get("market") or {}).get("key")) or "unknown"
        else:
            value = str(row.get("side") or "unknown")
        counts[value] = counts.get(value, 0) + 1
    top_key = max(counts, key=counts.get) if counts else None
    return {
        "top": top_key,
        "topCount": counts.get(top_key, 0) if top_key else 0,
        "counts": counts,
    }


def _risk_flags(rows: list[dict[str, Any]]) -> list[str]:
    flags = set()
    for row in rows:
        flags.update((row.get("decisionProfile") or {}).get("riskFlags") or [])
    return sorted(flags)


def _market_repeat_penalty(count: int) -> float:
    if count <= 0:
        return 0
    return {1: 2, 2: 6, 3: 10}.get(count, 14)


def _diversity_seed(market_key: str) -> float:
    return (sum(ord(char) for char in market_key) % 7) / 10


def _clean_mode(value: str) -> str:
    mode = str(value or "balanced").strip().lower().replace("-", "_")
    return mode if mode in {
        "balanced",
        "best_available",
        "safe_volume",
        "compact_power",
        "mega_under",
        "strict_diversity",
        "longshot",
    } else "balanced"


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
