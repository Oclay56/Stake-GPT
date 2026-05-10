from __future__ import annotations

from typing import Any

from .storage import SnapshotStore


MARKET_PROFILES = {
    "hits": {
        "name": "standard_count",
        "minEdge": 0.25,
        "minGames": 3,
        "sparse": False,
        "highLine": 1.5,
    },
    "runs": {
        "name": "standard_count",
        "minEdge": 0.3,
        "minGames": 3,
        "sparse": False,
        "highLine": 1.5,
    },
    "rbi": {
        "name": "standard_count",
        "minEdge": 0.3,
        "minGames": 3,
        "sparse": False,
        "highLine": 1.5,
    },
    "total-bases": {
        "name": "power_count",
        "minEdge": 0.4,
        "minGames": 3,
        "sparse": False,
        "highLine": 2.5,
    },
    "home-runs": {
        "name": "sparse_power",
        "minEdge": 0.35,
        "minGames": 3,
        "sparse": True,
        "highLine": 1.5,
    },
    "strikeouts": {
        "name": "pitching_count",
        "minEdge": 0.75,
        "minGames": 3,
        "sparse": False,
        "highLine": 8.5,
    },
    "pitcher-strikeouts": {
        "name": "pitching_count",
        "minEdge": 0.75,
        "minGames": 3,
        "sparse": False,
        "highLine": 8.5,
    },
    "earned-runs": {
        "name": "pitcher_damage_count",
        "minEdge": 0.4,
        "minGames": 3,
        "sparse": False,
        "highLine": 3.5,
    },
    "pitcher-earned-runs": {
        "name": "pitcher_damage_count",
        "minEdge": 0.4,
        "minGames": 3,
        "sparse": False,
        "highLine": 3.5,
    },
    "first-earned-run": {
        "name": "pitcher_damage_count",
        "minEdge": 0.35,
        "minGames": 3,
        "sparse": False,
        "highLine": 1.5,
    },
    "hits-allowed": {
        "name": "pitcher_contact_allowed",
        "minEdge": 0.75,
        "minGames": 3,
        "sparse": False,
        "highLine": 7.5,
    },
    "walks-allowed": {
        "name": "pitcher_control_count",
        "minEdge": 0.45,
        "minGames": 3,
        "sparse": False,
        "highLine": 3.5,
    },
    "outs-recorded": {
        "name": "pitcher_workload_count",
        "minEdge": 1.5,
        "minGames": 3,
        "sparse": False,
        "highLine": 18.5,
    },
    "pitcher-outs": {
        "name": "pitcher_workload_count",
        "minEdge": 1.5,
        "minGames": 3,
        "sparse": False,
        "highLine": 18.5,
    },
}


def analyze_stored_props(
    store: SnapshotStore,
    date_text: str | None = None,
    market: str | None = None,
    snapshot_phase: str | None = None,
    min_edge: float = 0.25,
    limit: int = 50,
) -> dict[str, Any]:
    props = store.list_latest_prop_snapshots(
        date_text=date_text,
        market=market,
        snapshot_phase=snapshot_phase,
        limit=limit,
    )
    buckets: dict[str, list[dict[str, Any]]] = {
        "watchlist": [],
        "neutral": [],
        "avoid": [],
    }

    for prop in props:
        row = _analyze_prop(store, prop, min_edge)
        buckets[row["bucket"]].append(row)

    return {
        "date": date_text,
        "market": market,
        "snapshotPhase": snapshot_phase,
        "minEdge": min_edge,
        "propCount": len(props),
        "counts": {bucket: len(rows) for bucket, rows in buckets.items()},
        "buckets": buckets,
    }


def _analyze_prop(
    store: SnapshotStore,
    prop: dict[str, Any],
    min_edge: float,
) -> dict[str, Any]:
    line = _float_or_none(prop.get("line"))
    recent_per_game = _float_or_none(prop.get("recentPerGame"))
    season_value = _float_or_none(prop.get("seasonValue"))
    games_used = _int_or_none(prop.get("gamesUsed"))
    over_odds = _float_or_none(prop.get("overOdds"))
    reasons: list[str] = []
    risk_flags: list[str] = []
    bucket = "neutral"
    lean = "none"
    edge = None
    profile = _market_profile(prop.get("marketKey"))
    movement = _latest_movement(store, str(prop.get("propId") or ""))
    threshold = max(min_edge, float(profile["minEdge"]))
    season_per_game = _season_per_game(prop, season_value)
    season_edge = (
        round(season_per_game - line, 4)
        if season_per_game is not None and line is not None
        else None
    )

    if prop.get("matchStatus") != "matched_exact_name_team":
        reasons.append("weak_match")
    if not prop.get("statKey"):
        reasons.append("unsupported_market")
    if line is None:
        reasons.append("missing_line")
    if recent_per_game is None:
        reasons.append("missing_recent_context")

    if profile["sparse"]:
        risk_flags.append("sparse_market")
    if games_used is not None and games_used < int(profile["minGames"]):
        risk_flags.append("small_recent_sample")
    if over_odds is not None and over_odds >= 4.0:
        risk_flags.append("long_over_odds")
    if movement and _float_or_none(movement.get("overOdds")) is not None:
        over_delta = float(movement["overOdds"])
        if over_delta >= 0.2:
            risk_flags.append("market_moved_against_over")
        elif over_delta <= -0.2:
            reasons.append("market_moved_toward_over")

    if reasons:
        bucket = "avoid"
    else:
        edge = round(recent_per_game - line, 4)
        if edge >= threshold:
            bucket = "watchlist"
            lean = "over"
            reasons.append(_positive_edge_reason(profile))
            if line >= float(profile["highLine"]):
                risk_flags.append("high_line")
            _apply_season_context(
                line=line,
                recent_per_game=recent_per_game,
                season_edge=season_edge,
                threshold=threshold,
                reasons=reasons,
                risk_flags=risk_flags,
            )
        elif edge <= -threshold:
            bucket = "avoid"
            lean = "under_or_avoid_over"
            reasons.append("recent_per_game_below_market_threshold")
        else:
            reasons.append("near_line")

    score = _score(bucket, edge, threshold, risk_flags, reasons)
    confidence = _confidence(bucket, reasons, risk_flags)

    return {
        "bucket": bucket,
        "propId": prop.get("propId"),
        "fixtureSlug": prop.get("fixtureSlug"),
        "game": prop.get("game"),
        "playerName": prop.get("playerName"),
        "teamName": prop.get("teamName"),
        "marketKey": prop.get("marketKey"),
        "statKey": prop.get("statKey"),
        "line": line,
        "lean": lean,
        "edge": edge,
        "score": score,
        "confidence": confidence,
        "marketProfile": profile["name"],
        "riskFlags": risk_flags,
        "marketThreshold": threshold,
        "recentPerGame": recent_per_game,
        "seasonValue": season_value,
        "seasonPerGame": season_per_game,
        "seasonEdge": season_edge,
        "gamesUsed": games_used,
        "recentGames": prop.get("recentGames") or [],
        "seasonStats": prop.get("seasonStats") or {},
        "overOdds": prop.get("overOdds"),
        "underOdds": prop.get("underOdds"),
        "matchStatus": prop.get("matchStatus"),
        "snapshotPhase": prop.get("snapshotPhase"),
        "snapshotLabel": prop.get("snapshotLabel"),
        "capturedAt": prop.get("capturedAt"),
        "reasons": reasons,
        "movement": movement,
    }


def _market_profile(market_key: Any) -> dict[str, Any]:
    normalized = str(market_key or "").strip().lower()
    return MARKET_PROFILES.get(
        normalized,
        {
            "name": "generic",
            "minEdge": 0.35,
            "minGames": 3,
            "sparse": False,
            "highLine": 2.5,
        },
    )


def _positive_edge_reason(profile: dict[str, Any]) -> str:
    if profile["name"] == "pitching_count":
        return "pitching_recent_average_clears_strikeout_line"
    if profile["name"].startswith("pitcher_"):
        return "pitcher_recent_average_clears_market_line"
    if profile["name"] == "standard_count":
        return "recent_per_game_above_line"
    return "recent_per_game_above_market_threshold"


def _apply_season_context(
    line: float,
    recent_per_game: float,
    season_edge: float | None,
    threshold: float,
    reasons: list[str],
    risk_flags: list[str],
) -> None:
    if season_edge is None:
        return

    if season_edge >= 0:
        reasons.append("season_baseline_supports_over")
        reasons.append("recent_and_season_agree")
        return

    if season_edge <= -threshold:
        risk_flags.append("season_baseline_below_line")
        reasons.append("recent_form_clears_line_but_season_does_not")

    season_per_game = line + season_edge
    if recent_per_game - season_per_game >= max(threshold * 1.25, 0.75):
        risk_flags.append("recent_form_spike")


def _season_per_game(
    prop: dict[str, Any],
    season_value: float | None,
) -> float | None:
    if season_value is None:
        return None

    stats = prop.get("seasonStats") or {}
    game_count = _season_game_count(stats)
    if game_count is None or game_count <= 0:
        return None

    return round(season_value / game_count, 4)


def _season_game_count(stats: dict[str, Any]) -> float | None:
    for key in (
        "gamesStarted",
        "gamesPlayed",
        "gamesPitched",
        "games",
    ):
        value = _float_or_none(stats.get(key))
        if value is not None and value > 0:
            return value
    return None


def _score(
    bucket: str,
    edge: float | None,
    threshold: float,
    risk_flags: list[str],
    reasons: list[str],
) -> int:
    if edge is None:
        return 20 if bucket == "avoid" else 45
    if bucket == "watchlist":
        score = 72 + min(18, int(round((edge / max(threshold, 0.01)) * 6)))
    elif bucket == "avoid":
        score = 35 - min(20, int(round(abs(edge / max(threshold, 0.01)) * 5)))
    else:
        score = 50 + int(round(edge * 10))
    if "season_baseline_supports_over" in reasons:
        score += 5
    if "recent_and_season_agree" in reasons:
        score += 3
    score -= min(15, len(risk_flags) * 3)
    return max(0, min(100, score))


def _confidence(
    bucket: str,
    reasons: list[str],
    risk_flags: list[str],
) -> str:
    hard_failures = {
        "weak_match",
        "unsupported_market",
        "missing_line",
        "missing_recent_context",
    }
    if bucket == "avoid" and any(reason in hard_failures for reason in reasons):
        return "low"
    if risk_flags:
        return "medium"
    return "high"


def _latest_movement(store: SnapshotStore, prop_id: str) -> dict[str, Any] | None:
    if not prop_id:
        return None
    movement = store.get_prop_movement(prop_id, limit=10)
    changes = movement.get("changes") or []
    if not changes:
        return None
    return changes[-1]


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
