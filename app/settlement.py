from __future__ import annotations

from typing import Any

from .mlb_bridge import group_for_market, stat_mapping_for_market
from .storage import SnapshotStore


async def settle_stored_props(
    store: SnapshotStore,
    engine: Any,
    date_text: str | None = None,
    market: str | None = None,
    snapshot_phase: str | None = None,
    season: int | None = None,
    limit: int = 50,
    history_limit: int = 30,
) -> dict[str, Any]:
    props = store.list_latest_prop_snapshots(
        date_text=date_text,
        market=market,
        snapshot_phase=snapshot_phase,
        limit=limit,
    )
    history_cache: dict[tuple[int, str, int | None], dict[str, Any]] = {}
    rows = []

    for prop in props:
        rows.append(
            await _settle_prop(
                prop,
                engine,
                history_cache,
                season=season,
                fallback_date=date_text,
                history_limit=history_limit,
            )
        )

    return {
        "date": date_text,
        "market": market,
        "snapshotPhase": snapshot_phase,
        "season": season,
        "propCount": len(props),
        "counts": _counts(rows),
        "rows": rows,
    }


async def _settle_prop(
    prop: dict[str, Any],
    engine: Any,
    history_cache: dict[tuple[int, str, int | None], dict[str, Any]],
    season: int | None,
    fallback_date: str | None,
    history_limit: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    player_id = _int_or_none(prop.get("playerMlbId"))
    game_pk = _int_or_none(prop.get("mlbGamePk"))
    line = _float_or_none(prop.get("line"))
    prop_date = str(prop.get("date") or fallback_date or "")
    market_key = str(prop.get("marketKey") or "")
    mapping = stat_mapping_for_market(market_key)
    stat_key = prop.get("statKey") or mapping.get("statKey")

    if prop.get("matchStatus") != "matched_exact_name_team":
        reasons.append("weak_match")
    if player_id is None:
        reasons.append("missing_mlb_player")
    if not prop_date:
        reasons.append("missing_date")
    if line is None:
        reasons.append("missing_line")
    if not stat_key:
        reasons.append("unsupported_market")

    if reasons:
        return _row(prop, None, "ungraded", "ungraded", reasons)

    stat_group = group_for_market(market_key)
    history_season = season if season is not None else _season_from_date(prop_date)
    history = await _cached_history(
        engine,
        history_cache,
        int(player_id),
        stat_group,
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
            return _row(
                prop,
                None,
                "ungraded",
                "ungraded",
                ["game_pk_not_found_in_game_log"],
            )
    else:
        games = games_on_date

    if not games:
        return _row(
            prop,
            None,
            "pending",
            "pending",
            ["no_game_log_for_date"],
        )
    if len(games) > 1:
        return _row(
            prop,
            None,
            "ungraded",
            "ungraded",
            ["multiple_game_logs_for_date"],
        )

    actual_value = _float_or_none((games[0].get("stats") or {}).get(stat_key))
    if actual_value is None:
        return _row(
            prop,
            None,
            "ungraded",
            "ungraded",
            ["stat_not_found_in_game_log"],
        )

    actual_result = _actual_result(actual_value, float(line))
    return _row(
        prop,
        actual_value,
        actual_result,
        _over_outcome(actual_result),
        [],
    )


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


def _row(
    prop: dict[str, Any],
    actual_value: float | None,
    actual_result: str,
    over_outcome: str,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "propId": prop.get("propId"),
        "date": prop.get("date"),
        "playerName": prop.get("playerName"),
        "teamName": prop.get("teamName"),
        "marketKey": prop.get("marketKey"),
        "line": prop.get("line"),
        "mlbGamePk": prop.get("mlbGamePk"),
        "actualValue": actual_value,
        "actualResult": actual_result,
        "overOutcome": over_outcome,
        "snapshotPhase": prop.get("snapshotPhase"),
        "snapshotLabel": prop.get("snapshotLabel"),
        "capturedAt": prop.get("capturedAt"),
        "reasons": reasons,
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


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "settled": 0,
        "pending": 0,
        "ungraded": 0,
        "over": 0,
        "under": 0,
        "push": 0,
    }
    for row in rows:
        outcome = row.get("overOutcome")
        result = row.get("actualResult")
        if outcome in {"win", "loss", "push"}:
            counts["settled"] += 1
        elif outcome == "pending":
            counts["pending"] += 1
        else:
            counts["ungraded"] += 1

        if result in {"over", "under", "push"}:
            counts[result] += 1
    return counts


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
