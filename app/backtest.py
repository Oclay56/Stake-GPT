from __future__ import annotations

from typing import Any

from .analyzer import analyze_stored_props
from .settlement import settle_stored_props
from .storage import SnapshotStore


async def backtest_stored_props(
    store: SnapshotStore,
    engine: Any,
    date_text: str | None = None,
    market: str | None = None,
    snapshot_phase: str | None = None,
    season: int | None = None,
    min_edge: float = 0.25,
    limit: int = 50,
) -> dict[str, Any]:
    saved_analysis = store.list_latest_analysis_snapshots(
        date_text=date_text,
        market=market,
        snapshot_phase=snapshot_phase,
        limit=limit,
    )
    if saved_analysis:
        analysis = _analysis_from_saved_rows(
            saved_analysis,
            date_text,
            market,
            snapshot_phase,
            min_edge,
        )
        source = "saved_analysis"
    else:
        analysis = analyze_stored_props(
            store,
            date_text=date_text,
            market=market,
            snapshot_phase=snapshot_phase,
            min_edge=min_edge,
            limit=limit,
        )
        source = "current_analyzer"

    settlement = await settle_stored_props(
        store,
        engine,
        date_text=date_text,
        market=market,
        snapshot_phase=snapshot_phase,
        season=season,
        limit=limit,
    )

    settlement_by_prop = {
        str(row.get("propId")): row
        for row in settlement.get("rows") or []
    }
    rows = []
    for bucket in ("watchlist", "avoid", "neutral"):
        for row in (analysis.get("buckets") or {}).get(bucket) or []:
            settled = settlement_by_prop.get(str(row.get("propId")), {})
            rows.append(_joined_row(row, settled))

    return {
        "date": date_text,
        "market": market,
        "snapshotPhase": snapshot_phase,
        "season": season,
        "minEdge": min_edge,
        "source": source,
        "counts": _counts(rows),
        "byBucket": _group_summary(rows, "bucket", ("watchlist", "neutral", "avoid")),
        "byConfidence": _group_summary(rows, "confidence", ("high", "medium", "low")),
        "rows": rows,
    }


def _analysis_from_saved_rows(
    rows: list[dict[str, Any]],
    date_text: str | None,
    market: str | None,
    snapshot_phase: str | None,
    min_edge: float,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {
        "watchlist": [],
        "neutral": [],
        "avoid": [],
    }
    for row in rows:
        bucket = str(row.get("bucket") or "neutral")
        buckets.setdefault(bucket, []).append(row)
    return {
        "date": date_text,
        "market": market,
        "snapshotPhase": snapshot_phase,
        "minEdge": min_edge,
        "propCount": len(rows),
        "counts": {bucket: len(values) for bucket, values in buckets.items()},
        "buckets": buckets,
    }


def _joined_row(analysis: dict[str, Any], settlement: dict[str, Any]) -> dict[str, Any]:
    return {
        "propId": analysis.get("propId"),
        "playerName": analysis.get("playerName"),
        "teamName": analysis.get("teamName"),
        "marketKey": analysis.get("marketKey"),
        "line": analysis.get("line"),
        "bucket": analysis.get("bucket"),
        "lean": analysis.get("lean"),
        "score": analysis.get("score"),
        "confidence": analysis.get("confidence"),
        "edge": analysis.get("edge"),
        "riskFlags": analysis.get("riskFlags") or [],
        "reasons": analysis.get("reasons") or [],
        "actualValue": settlement.get("actualValue"),
        "actualResult": settlement.get("actualResult"),
        "overOutcome": settlement.get("overOutcome"),
        "settlementReasons": settlement.get("reasons") or [],
        "decisionOutcome": _decision_outcome(
            str(analysis.get("lean") or "none"),
            str(settlement.get("overOutcome") or ""),
        ),
    }


def _decision_outcome(lean: str, over_outcome: str) -> str:
    if over_outcome not in {"win", "loss", "push"}:
        return "not_settled"
    if lean == "over":
        if over_outcome == "win":
            return "correct"
        if over_outcome == "loss":
            return "incorrect"
        return "push"
    if lean == "under_or_avoid_over":
        if over_outcome == "loss":
            return "correct"
        if over_outcome == "win":
            return "incorrect"
        return "push"
    return "no_decision"


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "rows": len(rows),
        "settled": 0,
        "pending": 0,
        "ungraded": 0,
        "decisions": 0,
        "correct": 0,
        "incorrect": 0,
        "push": 0,
        "noDecision": 0,
    }
    for row in rows:
        outcome = row.get("overOutcome")
        decision = row.get("decisionOutcome")
        if outcome in {"win", "loss", "push"}:
            counts["settled"] += 1
        elif outcome == "pending":
            counts["pending"] += 1
        else:
            counts["ungraded"] += 1

        if decision in {"correct", "incorrect"}:
            counts["decisions"] += 1
            counts[decision] += 1
        elif decision == "push":
            counts["push"] += 1
        elif decision == "no_decision":
            counts["noDecision"] += 1
    return counts


def _group_summary(
    rows: list[dict[str, Any]],
    key: str,
    known_groups: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    summary = {group: _empty_summary() for group in known_groups}
    for row in rows:
        group = str(row.get(key) or "unknown")
        summary.setdefault(group, _empty_summary())
        _add_row(summary[group], row)
    return {
        group: _with_accuracy(values)
        for group, values in summary.items()
    }


def _empty_summary() -> dict[str, int]:
    return {
        "total": 0,
        "decisions": 0,
        "correct": 0,
        "incorrect": 0,
        "push": 0,
        "noDecision": 0,
        "notSettled": 0,
    }


def _add_row(summary: dict[str, int], row: dict[str, Any]) -> None:
    summary["total"] += 1
    decision = row.get("decisionOutcome")
    if decision in {"correct", "incorrect"}:
        summary["decisions"] += 1
        summary[decision] += 1
    elif decision == "push":
        summary["push"] += 1
    elif decision == "no_decision":
        summary["noDecision"] += 1
    else:
        summary["notSettled"] += 1


def _with_accuracy(summary: dict[str, int]) -> dict[str, Any]:
    decisions = int(summary["decisions"])
    return {
        **summary,
        "accuracy": (
            round(float(summary["correct"]) / decisions, 4)
            if decisions
            else None
        ),
    }
