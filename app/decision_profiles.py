from __future__ import annotations

from typing import Any

from .mlb_bridge import stat_value_from_stats
from .mlb_props import slug_key


WINDOWS = (5, 10, 15)


MARKET_PROFILES: dict[str, dict[str, Any]] = {
    "hits": {
        "volatility": "medium",
        "marketRisk": "medium",
        "requiredContext": ["recent_logs", "season_rate", "lineup_role"],
    },
    "runs": {
        "volatility": "high",
        "marketRisk": "high",
        "requiredContext": ["recent_logs", "season_rate", "lineup_role", "team_offense"],
    },
    "rbi": {
        "volatility": "high",
        "marketRisk": "high",
        "requiredContext": ["recent_logs", "season_rate", "lineup_role", "base_runner_context"],
    },
    "total-bases": {
        "volatility": "high",
        "marketRisk": "high",
        "requiredContext": ["recent_logs", "season_rate", "pitcher_matchup"],
    },
    "home-runs": {
        "volatility": "extreme",
        "marketRisk": "extreme",
        "requiredContext": ["recent_logs", "season_rate", "pitcher_matchup", "park_context"],
    },
    "strikeouts": {
        "volatility": "medium",
        "marketRisk": "medium",
        "requiredContext": ["recent_logs", "season_rate", "pitcher_role"],
    },
    "pitcher-strikeouts": {
        "volatility": "medium",
        "marketRisk": "medium",
        "requiredContext": ["recent_logs", "season_rate", "pitcher_role"],
    },
    "outs-recorded": {
        "volatility": "medium",
        "marketRisk": "medium",
        "requiredContext": ["recent_logs", "season_rate", "pitcher_role"],
    },
    "earned-runs": {
        "volatility": "high",
        "marketRisk": "high",
        "requiredContext": ["recent_logs", "season_rate", "opponent_offense"],
    },
    "hits-allowed": {
        "volatility": "high",
        "marketRisk": "high",
        "requiredContext": ["recent_logs", "season_rate", "opponent_contact"],
    },
    "walks-allowed": {
        "volatility": "high",
        "marketRisk": "high",
        "requiredContext": ["recent_logs", "season_rate", "control_profile"],
    },
    "batter-strikeouts": {
        "volatility": "medium",
        "marketRisk": "medium",
        "requiredContext": ["recent_logs", "season_rate", "pitcher_matchup"],
        "contextCap": "medium",
    },
    "singles": {
        "volatility": "medium",
        "marketRisk": "medium",
        "requiredContext": ["recent_logs", "season_rate", "lineup_role"],
        "contextCap": "medium",
    },
    "stolen-bases": {
        "volatility": "extreme",
        "marketRisk": "extreme",
        "requiredContext": ["recent_logs", "season_rate", "lineup_role"],
        "contextCap": "medium",
    },
    "batter-walks": {
        "volatility": "high",
        "marketRisk": "high",
        "requiredContext": ["recent_logs", "season_rate", "pitcher_matchup"],
        "contextCap": "medium",
    },
}


def market_profile(market_key: Any, context_quality: str | None = None) -> dict[str, Any]:
    key = slug_key(market_key)
    profile = {
        "marketKey": key,
        "volatility": "high",
        "marketRisk": "high",
        "requiredContext": ["recent_logs", "season_rate"],
        "contextCap": None,
    }
    profile.update(MARKET_PROFILES.get(key, {}))
    if context_quality == "partial" and profile.get("contextCap") is None:
        profile["contextCap"] = "medium"
    if context_quality == "unsupported":
        profile["contextCap"] = "low"
    return profile


def evidence_windows(
    recent: dict[str, Any] | None,
    stat_key: Any,
    line: float | None,
    side: str,
) -> dict[str, Any]:
    games = list((recent or {}).get("games") or [])
    return {
        str(window): _window_summary(games[:window], stat_key, line, side)
        for window in WINDOWS
    }


def season_evidence(
    profile: dict[str, Any] | None,
    stat_key: Any,
    line: float | None,
    side: str,
) -> dict[str, Any]:
    stats = (((profile or {}).get("player") or {}).get("stats") or {})
    total = stat_value_from_stats(stat_key, stats)
    games = (
        _float_or_none(stats.get("gamesPlayed"))
        or _float_or_none(stats.get("gamesPitched"))
        or _float_or_none(stats.get("gamesStarted"))
    )
    average = round(total / games, 4) if total is not None and games else None
    return {
        "gamesUsed": int(games) if games is not None else None,
        "total": total,
        "average": average,
        "sideMargin": _side_margin(average, line, side),
        "sideSupported": _side_margin(average, line, side) is not None
        and (_side_margin(average, line, side) or 0) > 0,
    }


def trend_labels(
    windows: dict[str, Any],
    season: dict[str, Any],
    side: str,
) -> list[str]:
    labels: list[str] = []
    side = side if side in {"over", "under"} else "under"
    w5 = windows.get("5") or {}
    w10 = windows.get("10") or {}
    w15 = windows.get("15") or {}
    w5_rate = _float_or_none((w5.get("hitRates") or {}).get(side))
    w10_rate = _float_or_none((w10.get("hitRates") or {}).get(side))
    w15_rate = _float_or_none((w15.get("hitRates") or {}).get(side))
    season_margin = _float_or_none(season.get("sideMargin"))

    if (w15.get("gamesUsed") or 0) < 10:
        labels.append("thin_sample_warning")

    baseline_rate = w15_rate if w15_rate is not None else w10_rate
    baseline_support = (
        baseline_rate is not None and baseline_rate >= 0.55
    ) or (season_margin is not None and season_margin > 0)

    if w5_rate is not None and baseline_rate is not None:
        if w5_rate >= 0.6 and baseline_rate <= 0.4:
            labels.extend(["recent_spike_against_baseline", "last5_overreaction_risk"])
        elif w5_rate <= 0.4 and baseline_rate >= 0.6:
            labels.append("recent_cold_against_baseline")

    if w5_rate is not None and baseline_support and w5_rate >= 0.55:
        labels.append("recent_and_season_agree")
    elif w5_rate is not None and season_margin is not None:
        if (w5_rate >= 0.6 and season_margin <= 0) or (
            w5_rate <= 0.4 and season_margin > 0
        ):
            labels.append("season_disagrees_with_recent")

    return sorted(set(labels))


def evidence_check(
    windows: dict[str, Any],
    season: dict[str, Any],
    side: str,
) -> dict[str, Any]:
    side = side if side in {"over", "under"} else "under"
    window_checks = {
        window: _window_evidence_state(
            windows.get(window) or {},
            side,
            expected_games=int(window),
        )
        for window in ("5", "10", "15")
    }
    season_check = _season_evidence_state(season or {})
    broader = [window_checks["10"], window_checks["15"], season_check]
    broader_available = [
        item for item in broader if item["state"] not in {"missing", "partial"}
    ]
    broader_support = [item for item in broader_available if item["state"] == "supports"]
    broader_oppose = [item for item in broader_available if item["state"] == "opposes"]
    last5_supports = window_checks["5"]["state"] == "supports"
    last5_opposes = window_checks["5"]["state"] == "opposes"
    broader_supports = bool(broader_support)
    broader_opposes = bool(broader_oppose)
    missing_broader = [
        name
        for name, item in (
            ("last10", window_checks["10"]),
            ("last15", window_checks["15"]),
            ("season", season_check),
        )
        if item["state"] in {"missing", "partial"}
    ]

    last5_only_support = last5_supports and not broader_supports
    overreaction_risk = last5_supports and (
        broader_opposes or len(broader_available) < 2 or last5_only_support
    )
    if last5_supports and broader_supports and not broader_opposes:
        alignment = "aligned"
    elif last5_supports and broader_opposes:
        alignment = "conflicting"
    elif last5_only_support:
        alignment = "last5_only"
    elif last5_opposes and broader_supports:
        alignment = "longer_sample_supports_but_recent_does_not"
    else:
        alignment = "neutral_or_mixed"

    guidance: list[str] = []
    if missing_broader:
        guidance.append("Do not upgrade confidence from last 5 without missing broader evidence.")
    if overreaction_risk:
        guidance.append("Last 5 is not enough; disclose last-5 overreaction risk before recommending.")
    if alignment == "aligned":
        guidance.append("Last 5 agrees with broader evidence; still mention market and role risk.")
    if not guidance:
        guidance.append("Use as supporting evidence only; do not treat any single window as probability.")

    return {
        "requestedSide": side,
        "last5": window_checks["5"],
        "last10": window_checks["10"],
        "last15": window_checks["15"],
        "season": season_check,
        "broaderSampleAvailable": len(broader_available) >= 2,
        "missingBroaderEvidence": missing_broader,
        "broaderEvidenceSupportsSide": broader_supports,
        "broaderEvidenceOpposesSide": broader_opposes,
        "last5OnlySupport": last5_only_support,
        "last5OverreactionRisk": overreaction_risk,
        "alignment": alignment,
        "guidance": guidance,
    }


def build_decision_profile(
    selection: dict[str, Any],
    stat_context: dict[str, Any],
    metrics: dict[str, Any],
    flags: list[str],
    mlb_match: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market = market_profile(
        (stat_context or {}).get("marketKey"),
        (stat_context or {}).get("contextQuality"),
    )
    flags = sorted(set(flags or []))
    trend = list(metrics.get("trendLabels") or [])
    all_flags = sorted(set(flags + trend))
    playable = bool(selection.get("playable"))
    context_quality = str((stat_context or {}).get("contextQuality") or "unsupported")
    match_status = str((mlb_match or {}).get("status") or "")

    data_quality = _data_quality(playable, context_quality, match_status, all_flags)
    evidence = _evidence_strength(metrics)
    line_value = _line_value(metrics)
    odds_value = _odds_value(selection.get("odds"))
    sample_reliability = _sample_reliability(metrics)
    evidence_guard = metrics.get("evidenceCheck") or {}
    final_status = _final_status(
        playable=playable,
        data_quality=data_quality,
        evidence=evidence,
        volatility=str(market.get("volatility")),
        flags=all_flags,
    )

    return {
        "finalStatus": final_status,
        "dataQuality": data_quality,
        "evidenceStrength": evidence,
        "trendAlignment": _trend_alignment(metrics),
        "lineValue": line_value,
        "oddsValue": odds_value,
        "volatility": market.get("volatility"),
        "roleRisk": _role_risk(all_flags),
        "marketRisk": market.get("marketRisk"),
        "sampleReliability": sample_reliability,
        "recencyTrap": "last5_overreaction_risk" in all_flags
        or bool(evidence_guard.get("last5OverreactionRisk")),
        "evidenceCheck": evidence_guard,
        "riskFlags": all_flags,
        "contextCap": market.get("contextCap"),
    }


def decision_profile_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "finalStatus": _count_nested(rows, "decisionProfile", "finalStatus"),
        "dataQuality": _count_nested(rows, "decisionProfile", "dataQuality"),
        "evidenceStrength": _count_nested(rows, "decisionProfile", "evidenceStrength"),
        "riskFlags": _risk_flag_counts(rows),
    }


def market_heatmap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_market: dict[str, dict[str, Any]] = {}
    for row in rows:
        market = row.get("market") or {}
        key = str(market.get("key") or "unknown")
        heat = by_market.setdefault(
            key,
            {
                "marketKey": key,
                "marketName": market.get("name"),
                "candidateCount": 0,
                "playableCount": 0,
                "contextSupportedCount": 0,
                "statusCounts": {},
                "topHelperStrength": None,
            },
        )
        heat["candidateCount"] += 1
        if row.get("playable"):
            heat["playableCount"] += 1
        stat_context = row.get("statContext") or {}
        if stat_context.get("supported"):
            heat["contextSupportedCount"] += 1
        status = ((row.get("decisionProfile") or {}).get("finalStatus") or "unknown")
        heat["statusCounts"][status] = heat["statusCounts"].get(status, 0) + 1
        helper = _float_or_none(row.get("helperStrength"))
        if helper is not None and (
            heat["topHelperStrength"] is None or helper > heat["topHelperStrength"]
        ):
            heat["topHelperStrength"] = helper
    return sorted(by_market.values(), key=lambda item: str(item.get("marketKey") or ""))


def _window_summary(
    games: list[dict[str, Any]],
    stat_key: Any,
    line: float | None,
    side: str,
) -> dict[str, Any]:
    values = [
        stat_value_from_stats(stat_key, game.get("stats") or {})
        for game in games
        if stat_key
    ]
    values = [value for value in values if value is not None]
    total = round(sum(values), 4) if values else None
    average = round(total / len(values), 4) if total is not None and values else None
    hit_rates = _hit_rates(values, line)
    return {
        "gamesUsed": len(values),
        "total": total,
        "average": average,
        "hitRates": hit_rates,
        "sideHitRate": hit_rates.get(side),
        "sideMargin": _side_margin(average, line, side),
    }


def _window_evidence_state(
    window: dict[str, Any],
    side: str,
    expected_games: int,
) -> dict[str, Any]:
    games = _int_or_none(window.get("gamesUsed")) or 0
    rate = _float_or_none((window.get("hitRates") or {}).get(side))
    margin = _float_or_none(window.get("sideMargin"))
    if games <= 0 or rate is None:
        state = "missing"
    elif games < expected_games:
        state = "partial"
    elif rate >= 0.6:
        state = "supports"
    elif rate <= 0.4:
        state = "opposes"
    else:
        state = "neutral"
    return {
        "state": state,
        "gamesUsed": games,
        "expectedGames": expected_games,
        "sideHitRate": rate,
        "average": _float_or_none(window.get("average")),
        "sideMargin": margin,
    }


def _season_evidence_state(season: dict[str, Any]) -> dict[str, Any]:
    games = _int_or_none(season.get("gamesUsed")) or 0
    margin = _float_or_none(season.get("sideMargin"))
    if games <= 0 or margin is None:
        state = "missing"
    elif margin > 0:
        state = "supports"
    elif margin < 0:
        state = "opposes"
    else:
        state = "neutral"
    return {
        "state": state,
        "gamesUsed": games,
        "average": _float_or_none(season.get("average")),
        "sideMargin": margin,
    }


def _hit_rates(values: list[float], line: float | None) -> dict[str, float | None]:
    if not values or line is None:
        return {"over": None, "under": None}
    return {
        "over": round(sum(1 for value in values if value > line) / len(values), 4),
        "under": round(sum(1 for value in values if value < line) / len(values), 4),
    }


def _side_margin(value: float | None, line: float | None, side: str) -> float | None:
    if value is None or line is None:
        return None
    return round(value - line if side == "over" else line - value, 4)


def _data_quality(
    playable: bool,
    context_quality: str,
    match_status: str,
    flags: list[str],
) -> str:
    hard_block_flags = {
        "game_postponed",
        "game_suspended",
        "game_cancelled",
        "lineup_not_starting",
    }
    if not playable or "unplayable_current_odds" in flags or "missing_line" in flags:
        return "blocked"
    if hard_block_flags.intersection(flags):
        return "blocked"
    if context_quality == "unsupported" or match_status == "unmatched":
        return "low"
    if context_quality == "partial" or "team_unconfirmed" in flags:
        return "medium"
    if "alternate_or_unconfirmed_primary_line" in flags:
        return "medium"
    return "high"


def _evidence_strength(metrics: dict[str, Any]) -> str:
    score = _float_or_none(metrics.get("agreementScore"))
    labels = set(metrics.get("trendLabels") or [])
    if score is None:
        return "weak"
    if "season_disagrees_with_recent" in labels:
        return "conflicting"
    if score >= 72:
        return "strong"
    if score >= 58:
        return "moderate"
    if score >= 45:
        return "weak"
    return "conflicting"


def _line_value(metrics: dict[str, Any]) -> str:
    margin = _float_or_none(metrics.get("seasonSideMargin"))
    recent_margin = _float_or_none(metrics.get("recentSideMargin"))
    best_margin = max(
        [value for value in (margin, recent_margin) if value is not None],
        default=None,
    )
    if best_margin is None:
        return "unknown"
    if best_margin >= 0.5:
        return "strong"
    if best_margin >= 0.2:
        return "fair"
    if best_margin >= 0:
        return "thin"
    return "bad"


def _odds_value(odds: Any) -> str:
    value = _float_or_none(odds)
    if value is None:
        return "unknown"
    if value < 1.15:
        return "overpriced"
    if value < 1.8:
        return "fair"
    if value < 3.0:
        return "worth_price"
    return "lottery"


def _sample_reliability(metrics: dict[str, Any]) -> str:
    games = _int_or_none(metrics.get("recentGamesUsed")) or 0
    if games >= 15:
        return "high"
    if games >= 10:
        return "medium"
    if games >= 5:
        return "low"
    return "thin"


def _trend_alignment(metrics: dict[str, Any]) -> str:
    labels = set(metrics.get("trendLabels") or [])
    if "last5_overreaction_risk" in labels:
        return "recent_spike_only"
    if "season_disagrees_with_recent" in labels:
        return "mixed"
    if "recent_and_season_agree" in labels:
        return "mostly_aligned"
    return "neutral"


def _role_risk(flags: list[str]) -> str:
    if any("probable_pitcher_low_start_share" == flag for flag in flags):
        return "high"
    if any("pitcher" in flag or "role" in flag for flag in flags):
        return "medium"
    return "unknown"


def _final_status(
    playable: bool,
    data_quality: str,
    evidence: str,
    volatility: str,
    flags: list[str],
) -> str:
    hard_block_flags = {
        "game_postponed",
        "game_suspended",
        "game_cancelled",
        "lineup_not_starting",
    }
    if not playable or data_quality == "blocked":
        return "blocked"
    if hard_block_flags.intersection(flags):
        return "blocked"
    if "game_delay_risk" in flags:
        return "borderline"
    if data_quality == "low" or evidence == "conflicting":
        return "avoid"
    if "lineup_unconfirmed" in flags:
        return "playable_but_volatile"
    if evidence == "weak" or "last5_overreaction_risk" in flags:
        return "borderline"
    if volatility in {"high", "extreme"}:
        return "playable_but_volatile"
    return "playable"


def _count_nested(rows: list[dict[str, Any]], parent: str, child: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(((row.get(parent) or {}).get(child)) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _risk_flag_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for flag in (row.get("decisionProfile") or {}).get("riskFlags") or []:
            counts[flag] = counts.get(flag, 0) + 1
    return counts


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
