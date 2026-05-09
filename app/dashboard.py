from __future__ import annotations

import re
from functools import reduce
from operator import mul
from pathlib import Path
from typing import Any

from .analyzer import analyze_stored_props
from .correlation import analyze_parlay_correlation
from .parlay import build_parlay_candidates, build_pick_board
from .storage import SnapshotStore


PLAYER_ASSET_DIR = (
    Path(__file__).resolve().parent.parent
    / "ui"
    / "desktop-concept"
    / "assets"
    / "players"
)
PLAYER_ASSET_BASE_URL = "/app/assets/players"


def build_mlb_dashboard(
    store: SnapshotStore,
    date_text: str | None = None,
    limit: int = 5,
    snapshot_phase: str | None = None,
    profile: str = "custom",
    asset_dir: str | Path = PLAYER_ASSET_DIR,
    asset_base_url: str = PLAYER_ASSET_BASE_URL,
) -> dict[str, Any]:
    clean_limit = _clean_int(limit, 1, 25)
    resolved_date = date_text or _latest_date(store)
    decisions, source = _load_decisions(
        store=store,
        date_text=resolved_date,
        snapshot_phase=snapshot_phase,
        limit=max(clean_limit * 10, 100),
    )
    props = store.list_latest_prop_snapshots(
        date_text=resolved_date,
        snapshot_phase=snapshot_phase,
        limit=500,
    )
    sorted_decisions = _sort_decisions(_merge_prop_context(decisions, props))
    top_props = [
        _dashboard_prop(row, asset_dir, asset_base_url)
        for row in sorted_decisions[:clean_limit]
    ]
    slip = _build_slip(sorted_decisions, clean_limit, profile)
    generated_slips = _build_saved_slips(sorted_decisions, profile)

    return {
        "sport": "mlb",
        "date": resolved_date,
        "filters": {
            "limit": clean_limit,
            "snapshotPhase": snapshot_phase,
            "profile": profile,
        },
        "status": {
            "dataSynced": bool(props or decisions),
            "source": source,
            "lastUpdated": _latest_captured_at(props, decisions),
            "snapshotPhase": snapshot_phase or _first_non_empty(props, decisions, "snapshotPhase"),
        },
        "summary": _summary(props, decisions),
        "slateOverview": _slate_overview(sorted_decisions),
        "marketMovers": _market_movers(sorted_decisions[:clean_limit], asset_dir, asset_base_url),
        "topPlayerProps": top_props,
        "correlationMap": _correlation_map(top_props[:5]),
        "slip": slip,
        "savedSlips": generated_slips,
    }


def _load_decisions(
    store: SnapshotStore,
    date_text: str | None,
    snapshot_phase: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str]:
    saved = store.list_latest_analysis_snapshots(
        date_text=date_text,
        snapshot_phase=snapshot_phase,
        limit=limit,
    )
    if saved:
        return saved, "saved_analysis"

    analyzed = analyze_stored_props(
        store=store,
        date_text=date_text,
        snapshot_phase=snapshot_phase,
        limit=limit,
    )
    rows = _flatten_buckets(analyzed.get("buckets") or {})
    return rows, "latest_props_analyzed" if rows else "no_data"


def _flatten_buckets(buckets: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bucket in ("watchlist", "neutral", "avoid"):
        for row in buckets.get(bucket) or []:
            rows.append({**row, "bucket": row.get("bucket") or bucket})
    return rows


def _sort_decisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bucket_rank = {"watchlist": 0, "neutral": 1, "avoid": 2}
    return sorted(
        rows,
        key=lambda row: (
            bucket_rank.get(str(row.get("bucket") or ""), 3),
            -_int_or_zero(row.get("score")),
            -_float_or_zero(_odds_for_row(row)),
            str(row.get("playerName") or ""),
        ),
    )


def _merge_prop_context(
    decisions: list[dict[str, Any]],
    props: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    props_by_id = {
        str(prop.get("propId") or ""): prop
        for prop in props
        if prop.get("propId")
    }
    merged = []
    for decision in decisions:
        prop = props_by_id.get(str(decision.get("propId") or ""))
        if prop is None:
            merged.append(decision)
            continue
        merged.append(
            {
                "playerMlbId": prop.get("playerMlbId"),
                "teamMlbId": prop.get("teamMlbId"),
                "recentGames": prop.get("recentGames") or [],
                "seasonStats": prop.get("seasonStats") or {},
                **prop,
                **decision,
            }
        )
    return merged


def _summary(
    props: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    count_source = props or decisions
    games = {
        str(row.get("fixtureSlug") or row.get("game") or "")
        for row in count_source
        if row.get("fixtureSlug") or row.get("game")
    }
    scores = [_int_or_zero(row.get("score")) for row in decisions if row.get("score") is not None]
    edges = [
        float(edge)
        for row in decisions
        if (edge := _float_or_none(row.get("edge"))) is not None
    ]
    risk_count = sum(len(row.get("riskFlags") or []) for row in decisions)

    return {
        "games": len(games),
        "playerProps": len(count_source),
        "topScore": max(scores) if scores else 0,
        "avgEdge": round(sum(edges) / len(edges), 3) if edges else 0,
        "alerts": risk_count,
    }


def _dashboard_prop(
    row: dict[str, Any],
    asset_dir: str | Path,
    asset_base_url: str,
) -> dict[str, Any]:
    market_key = str(row.get("marketKey") or row.get("statKey") or "")
    line = _float_or_none(row.get("line"))
    odds = _odds_for_row(row)
    return {
        "propId": row.get("propId"),
        "fixtureSlug": row.get("fixtureSlug") or _fixture_slug_from_prop_id(row.get("propId")),
        "game": row.get("game"),
        "playerName": row.get("playerName"),
        "teamName": row.get("teamName"),
        "playerImage": _player_image(
            row.get("playerName"),
            row.get("playerMlbId"),
            asset_dir,
            asset_base_url,
        ),
        "marketKey": market_key,
        "marketLabel": _market_label(market_key),
        "selection": _selection_label(row.get("lean"), line),
        "line": line,
        "lean": row.get("lean"),
        "odds": odds,
        "score": _int_or_zero(row.get("score")),
        "confidence": row.get("confidence"),
        "edge": _float_or_none(row.get("edge")),
        "riskFlags": list(row.get("riskFlags") or []),
        "reasons": list(row.get("reasons") or []),
        "recentPerGame": _float_or_none(row.get("recentPerGame")),
        "seasonValue": _float_or_none(row.get("seasonValue")),
        "seasonPerGame": _float_or_none(row.get("seasonPerGame")),
        "gamesUsed": _int_or_none(row.get("gamesUsed")),
        "recentGames": list(row.get("recentGames") or [])[:5],
        "seasonStats": row.get("seasonStats") or {},
        "capturedAt": row.get("capturedAt"),
        "snapshotPhase": row.get("snapshotPhase"),
        "snapshotLabel": row.get("snapshotLabel"),
    }


def _market_movers(
    rows: list[dict[str, Any]],
    asset_dir: str | Path,
    asset_base_url: str,
) -> list[dict[str, Any]]:
    movers = []
    for row in rows:
        movement = row.get("movement") or {}
        move = (
            _float_or_none(movement.get("overOdds"))
            or _float_or_none(movement.get("line"))
            or _float_or_none(row.get("edge"))
            or 0.0
        )
        direction = "up" if move >= 0 else "down"
        movers.append(
            {
                "playerName": row.get("playerName"),
                "playerImage": _player_image(
                    row.get("playerName"),
                    row.get("playerMlbId"),
                    asset_dir,
                    asset_base_url,
                ),
                "marketLabel": _market_label(row.get("marketKey") or row.get("statKey")),
                "selection": _selection_label(row.get("lean"), _float_or_none(row.get("line"))),
                "move": round(move, 3),
                "direction": direction,
                "odds": _odds_for_row(row),
                "score": _int_or_zero(row.get("score")),
            }
        )
    return movers


def _slate_overview(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = ["12AM", "4AM", "8AM", "12PM", "4PM", "8PM"]
    scores = [_int_or_zero(row.get("score")) for row in rows[: len(labels)]]
    if not scores:
        scores = [0]
    points = []
    for index, label in enumerate(labels):
        score = scores[index] if index < len(scores) else scores[-1]
        points.append({"label": label, "value": score})
    return {"metric": "analyzer_score", "points": points}


def _correlation_map(props: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [_short_name(prop.get("playerName")) for prop in props]
    cells = []
    for source in props:
        row = []
        for target in props:
            if source is target:
                row.append(None)
            elif source.get("fixtureSlug") == target.get("fixtureSlug"):
                row.append(1.0)
            elif source.get("teamName") == target.get("teamName"):
                row.append(0.35)
            else:
                row.append(0.0)
        cells.append(row)
    return {
        "method": "same_fixture_grouping",
        "labels": labels,
        "players": props,
        "cells": cells,
    }


def _build_slip(
    decisions: list[dict[str, Any]],
    limit: int,
    profile: str,
) -> dict[str, Any]:
    board = build_pick_board(decisions, limit=max(3, limit), profile=profile)
    candidates = build_parlay_candidates(
        decisions,
        legs=min(3, max(2, len(board["picks"]))),
        count=3,
        profile=profile,
    )
    if candidates["candidates"]:
        candidate = candidates["candidates"][0]
        legs = candidate["legs"]
        combined_odds = candidate["totalOdds"]
        warnings = candidate["conflictWarnings"]
        correlation_risk = candidate["correlationRisk"]
    else:
        legs = board["picks"][: max(1, min(3, len(board["picks"])))]
        combined_odds = _total_odds(legs)
        warnings = candidates["warnings"]
        correlation_risk = analyze_parlay_correlation(legs) if len(legs) >= 2 else None

    return {
        "mode": "manual_review",
        "legCount": len(legs),
        "combinedOdds": round(combined_odds, 4) if combined_odds else None,
        "rawProductOdds": round(combined_odds, 4) if combined_odds else None,
        "toWinUnits": round(combined_odds - 1, 4) if combined_odds else None,
        "warnings": warnings,
        "correlationRisk": correlation_risk,
        "legs": [_slip_leg(leg) for leg in legs],
    }


def _build_saved_slips(
    decisions: list[dict[str, Any]],
    profile: str,
) -> list[dict[str, Any]]:
    candidates = build_parlay_candidates(
        decisions,
        legs=3,
        count=4,
        profile=profile,
    )
    saved = []
    for candidate in candidates["candidates"]:
        saved.append(
            {
                "title": f"Generated Candidate {candidate['rank']}",
                "legCount": candidate["legCount"],
                "totalOdds": candidate["totalOdds"],
                "rawProductOdds": candidate["rawProductOdds"],
                "correlationRisk": candidate["correlationRisk"],
                "tag": "Research",
                "warnings": candidate["conflictWarnings"],
            }
        )
    return saved


def _slip_leg(row: dict[str, Any]) -> dict[str, Any]:
    line = _float_or_none(row.get("line"))
    return {
        "propId": row.get("propId"),
        "fixtureSlug": row.get("fixtureSlug"),
        "game": row.get("game"),
        "playerName": row.get("playerName"),
        "teamName": row.get("teamName"),
        "marketLabel": _market_label(row.get("marketKey") or row.get("statKey")),
        "selection": _selection_label(row.get("lean"), line),
        "line": line,
        "odds": _odds_for_row(row),
        "score": _int_or_zero(row.get("score")),
        "riskFlags": list(row.get("riskFlags") or []),
    }


def _latest_date(store: SnapshotStore) -> str | None:
    rows = store.list_latest_analysis_snapshots(limit=1)
    if not rows:
        rows = store.list_latest_prop_snapshots(limit=1)
    return str(rows[0].get("date")) if rows and rows[0].get("date") else None


def _latest_captured_at(
    props: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> str | None:
    values = [
        str(row.get("capturedAt"))
        for row in [*props, *decisions]
        if row.get("capturedAt")
    ]
    return max(values) if values else None


def _first_non_empty(
    props: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    key: str,
) -> Any:
    for row in [*props, *decisions]:
        if row.get(key):
            return row[key]
    return None


def _player_image(
    player_name: Any,
    player_mlb_id: Any,
    asset_dir: str | Path,
    asset_base_url: str,
) -> str | None:
    slug = _slugify(player_name)
    if slug:
        image_path = Path(asset_dir) / f"{slug}.png"
        if image_path.exists():
            return f"{asset_base_url.rstrip('/')}/{slug}.png"

    mlb_id = _int_or_none(player_mlb_id)
    if mlb_id is None:
        return None
    return (
        "https://img.mlbstatic.com/mlb-photos/image/upload/"
        f"w_96,q_auto:best,f_auto/v1/people/{mlb_id}/headshot/67/current"
    )


def _market_label(value: Any) -> str:
    text = str(value or "").strip().replace("-", " ")
    if not text:
        return "Prop"
    upper = {"rbi": "RBI", "hr": "HR"}
    lowered = text.lower()
    return upper.get(lowered, text.title())


def _selection_label(lean: Any, line: float | None) -> str:
    side = "Over" if str(lean or "").startswith("over") else "Under"
    return f"{side} {_format_number(line)}" if line is not None else side


def _short_name(value: Any) -> str:
    parts = str(value or "").split()
    if len(parts) < 2:
        return str(value or "N/A")
    return f"{parts[0][0]}. {parts[-1]}"


def _odds_for_row(row: dict[str, Any]) -> float | None:
    lean = str(row.get("lean") or "")
    if lean == "over":
        return _float_or_none(row.get("overOdds") or row.get("odds"))
    if lean.startswith("under"):
        return _float_or_none(row.get("underOdds") or row.get("odds"))
    return _float_or_none(row.get("overOdds") or row.get("odds") or row.get("underOdds"))


def _total_odds(legs: list[dict[str, Any]]) -> float | None:
    odds_values = [_float_or_none(leg.get("odds")) for leg in legs]
    if not odds_values or any(value is None for value in odds_values):
        return None
    return float(reduce(mul, odds_values, 1.0))


def _fixture_slug_from_prop_id(value: Any) -> str | None:
    text = str(value or "")
    return text.split(":", 1)[0] if ":" in text else None


def _slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


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


def _float_or_zero(value: Any) -> float:
    parsed = _float_or_none(value)
    return parsed if parsed is not None else 0.0


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else 0
