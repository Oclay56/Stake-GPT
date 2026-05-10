from __future__ import annotations

from typing import Any

from .mlb_bridge import group_for_market, stat_mapping_for_market
from .storage import SnapshotStore


async def settle_recommendation_legs(
    store: SnapshotStore,
    engine: Any,
    date_text: str | None = None,
    market: str | None = None,
    side: str | None = None,
    request_id: str | None = None,
    diversity_mode: str | None = None,
    season: int | None = None,
    limit: int = 500,
    history_limit: int = 30,
) -> dict[str, Any]:
    legs = store.list_recommendation_legs(
        date_text=date_text,
        market=market,
        side=side,
        request_id=request_id,
        diversity_mode=diversity_mode,
        limit=limit,
    )
    history_cache: dict[tuple[int, str, int | None], dict[str, Any]] = {}
    rows = []
    for leg in legs:
        rows.append(
            await _settle_leg(
                leg,
                engine,
                history_cache,
                season=season,
                fallback_date=date_text,
                history_limit=history_limit,
            )
        )

    store.save_recommendation_settlements(rows)
    return {
        "date": date_text,
        "market": market,
        "side": side,
        "requestId": request_id,
        "diversityMode": diversity_mode,
        "season": season,
        "legCount": len(legs),
        "counts": _counts(rows),
        "rows": rows,
    }


def summarize_recommendation_performance(
    store: SnapshotStore,
    date_text: str | None = None,
    market: str | None = None,
    side: str | None = None,
    request_id: str | None = None,
    diversity_mode: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    legs = store.list_recommendation_legs(
        date_text=date_text,
        market=market,
        side=side,
        request_id=request_id,
        diversity_mode=diversity_mode,
        limit=limit,
    )
    settlements = {
        (row["requestId"], row["rank"]): row
        for row in store.list_recommendation_settlements(
            date_text=date_text,
            request_id=request_id,
            limit=limit,
        )
    }
    return summarize_recommendation_rows(
        legs,
        list(settlements.values()),
        date_text=date_text,
        market=market,
        side=side,
        request_id=request_id,
        diversity_mode=diversity_mode,
    )


def summarize_recommendation_rows(
    legs: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
    date_text: str | None = None,
    market: str | None = None,
    side: str | None = None,
    request_id: str | None = None,
    diversity_mode: str | None = None,
) -> dict[str, Any]:
    settlement_index = {
        (row.get("requestId"), row.get("rank")): row
        for row in settlements
    }
    rows = [
        _joined_performance_row(
            leg,
            settlement_index.get((leg.get("requestId"), leg.get("rank"))),
        )
        for leg in legs
    ]
    return {
        "date": date_text,
        "market": market,
        "side": side,
        "requestId": request_id,
        "diversityMode": diversity_mode,
        "counts": _counts(rows),
        "byMarket": _group_summary(rows, "marketKey"),
        "bySide": _group_summary(rows, "side"),
        "byConfidence": _group_summary(rows, "confidence"),
        "byDiversityMode": _group_summary(rows, "diversityMode"),
        "byRiskFlag": _multi_value_group_summary(rows, "riskFlags"),
        "byContextualTag": _multi_value_group_summary(rows, "contextualTags"),
        "byConcentrationTag": _multi_value_group_summary(rows, "concentrationTags"),
        "rows": rows,
    }


async def _settle_leg(
    leg: dict[str, Any],
    engine: Any,
    history_cache: dict[tuple[int, str, int | None], dict[str, Any]],
    season: int | None,
    fallback_date: str | None,
    history_limit: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    player_id = _int_or_none(leg.get("playerMlbId"))
    game_pk = _int_or_none(leg.get("mlbGamePk"))
    line = _float_or_none(leg.get("line"))
    prop_date = str(leg.get("date") or fallback_date or "")
    market_key = str(leg.get("marketKey") or "")
    stat_key = leg.get("statKey") or stat_mapping_for_market(market_key).get("statKey")

    if player_id is None:
        reasons.append("missing_mlb_player")
    if not prop_date:
        reasons.append("missing_date")
    if line is None:
        reasons.append("missing_line")
    if not stat_key:
        reasons.append("unsupported_market")

    if reasons:
        return _settlement_row(leg, None, "ungraded", "ungraded", reasons)

    history_season = season if season is not None else _season_from_date(prop_date)
    history = await _cached_history(
        engine,
        history_cache,
        int(player_id),
        group_for_market(market_key),
        history_season,
        history_limit,
    )
    games_on_date = [
        game
        for game in history.get("games") or []
        if str(game.get("date") or "") == prop_date
    ]
    if game_pk is not None:
        games = [
            game
            for game in games_on_date
            if _int_or_none(game.get("gamePk")) == game_pk
        ]
        if not games and games_on_date:
            return _settlement_row(
                leg,
                None,
                "ungraded",
                "ungraded",
                ["game_pk_not_found_in_game_log"],
            )
    else:
        games = games_on_date

    if not games:
        return _settlement_row(
            leg,
            None,
            "pending",
            "pending",
            ["no_game_log_for_date"],
        )
    if len(games) > 1:
        return _settlement_row(
            leg,
            None,
            "ungraded",
            "ungraded",
            ["multiple_game_logs_for_date"],
        )

    actual_value = _float_or_none((games[0].get("stats") or {}).get(stat_key))
    if actual_value is None:
        return _settlement_row(
            leg,
            None,
            "ungraded",
            "ungraded",
            ["stat_not_found_in_game_log"],
        )

    actual_result = _actual_result(actual_value, float(line))
    return _settlement_row(leg, actual_value, actual_result, _over_outcome(actual_result), [])


async def _cached_history(
    engine: Any,
    cache: dict[tuple[int, str, int | None], dict[str, Any]],
    player_id: int,
    group: str,
    season: int | None,
    history_limit: int,
) -> dict[str, Any]:
    cache_key = (player_id, group, season)
    if cache_key not in cache:
        cache[cache_key] = await engine.get_player_recent_history(
            player_id,
            group=group,
            season=season,
            limit=history_limit,
        )
    return cache[cache_key]


def _settlement_row(
    leg: dict[str, Any],
    actual_value: float | None,
    actual_result: str,
    over_outcome: str,
    reasons: list[str],
) -> dict[str, Any]:
    decision = _decision_outcome(str(leg.get("side") or ""), over_outcome)
    return {
        "requestId": leg.get("requestId"),
        "rank": leg.get("rank"),
        "propId": leg.get("propId"),
        "date": leg.get("date"),
        "playerName": leg.get("playerName"),
        "teamName": leg.get("teamName"),
        "marketKey": leg.get("marketKey"),
        "side": leg.get("side"),
        "line": leg.get("line"),
        "actualValue": actual_value,
        "actualResult": actual_result,
        "overOutcome": over_outcome,
        "decisionOutcome": decision,
        "score": leg.get("score"),
        "confidence": leg.get("confidence"),
        "riskFlags": leg.get("riskFlags") or [],
        "contextualTags": leg.get("contextualTags") or [],
        "concentrationTags": leg.get("concentrationTags") or [],
        "diversityMode": leg.get("diversityMode"),
        "reasons": reasons,
    }


def _joined_performance_row(
    leg: dict[str, Any],
    settlement: dict[str, Any] | None,
) -> dict[str, Any]:
    settlement = settlement or {}
    return {
        **leg,
        "actualValue": settlement.get("actualValue"),
        "actualResult": settlement.get("actualResult"),
        "overOutcome": settlement.get("overOutcome"),
        "decisionOutcome": settlement.get("decisionOutcome") or "not_settled",
        "settlementReasons": settlement.get("reasons") or [],
    }


def _actual_result(actual_value: float, line: float) -> str:
    if actual_value > line:
        return "over"
    if actual_value < line:
        return "under"
    return "push"


def _over_outcome(actual_result: str) -> str:
    if actual_result == "over":
        return "win"
    if actual_result == "under":
        return "loss"
    if actual_result == "push":
        return "push"
    return actual_result


def _decision_outcome(side: str, over_outcome: str) -> str:
    if over_outcome not in {"win", "loss", "push"}:
        return over_outcome
    if side == "over":
        if over_outcome == "win":
            return "correct"
        if over_outcome == "loss":
            return "incorrect"
        return "push"
    if side == "under":
        if over_outcome == "loss":
            return "correct"
        if over_outcome == "win":
            return "incorrect"
        return "push"
    return "no_decision"


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "legs": len(rows),
        "settled": 0,
        "pending": 0,
        "ungraded": 0,
        "decisions": 0,
        "correct": 0,
        "incorrect": 0,
        "push": 0,
        "notSettled": 0,
    }
    for row in rows:
        over_outcome = row.get("overOutcome")
        decision = row.get("decisionOutcome")
        if over_outcome in {"win", "loss", "push"}:
            counts["settled"] += 1
        elif over_outcome == "pending":
            counts["pending"] += 1
        elif over_outcome in {"ungraded"}:
            counts["ungraded"] += 1
        else:
            counts["notSettled"] += 1

        if decision in {"correct", "incorrect"}:
            counts["decisions"] += 1
            counts[decision] += 1
        elif decision == "push":
            counts["push"] += 1
    return counts


def _group_summary(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        _add_to_summary(summary.setdefault(value, _empty_summary()), row)
    return {key: _with_accuracy(value) for key, value in sorted(summary.items())}


def _multi_value_group_summary(
    rows: list[dict[str, Any]],
    key: str,
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        values = row.get(key) or []
        if not values:
            values = ["none"]
        for value in values:
            _add_to_summary(summary.setdefault(str(value), _empty_summary()), row)
    return {key: _with_accuracy(value) for key, value in sorted(summary.items())}


def _empty_summary() -> dict[str, int]:
    return {
        "legs": 0,
        "settled": 0,
        "decisions": 0,
        "correct": 0,
        "incorrect": 0,
        "push": 0,
        "notSettled": 0,
    }


def _add_to_summary(summary: dict[str, int], row: dict[str, Any]) -> None:
    summary["legs"] += 1
    decision = row.get("decisionOutcome")
    if decision in {"correct", "incorrect"}:
        summary["settled"] += 1
        summary["decisions"] += 1
        summary[decision] += 1
    elif decision == "push":
        summary["settled"] += 1
        summary["push"] += 1
    else:
        summary["notSettled"] += 1


def _with_accuracy(summary: dict[str, int]) -> dict[str, Any]:
    decisions = int(summary["decisions"])
    return {
        **summary,
        "accuracy": round(float(summary["correct"]) / decisions, 4) if decisions else None,
    }


def _season_from_date(value: str | None) -> int | None:
    try:
        return int(str(value or "")[:4])
    except ValueError:
        return None


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
