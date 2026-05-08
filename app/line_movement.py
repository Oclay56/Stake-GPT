from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any


_MAX_HISTORY_PER_PROP = 50
_LINE_MOVEMENT_HISTORY: dict[str, list[dict[str, Any]]] = {}


def clear_line_movement_history() -> None:
    _LINE_MOVEMENT_HISTORY.clear()


def get_line_movement_history() -> dict[str, Any]:
    props = [
        {"propId": prop_id, "snapshots": copy.deepcopy(snapshots)}
        for prop_id, snapshots in sorted(_LINE_MOVEMENT_HISTORY.items())
    ]
    return {
        "trackedPropCount": len(props),
        "props": props,
    }


def record_line_movements(
    props: list[dict[str, Any]],
    recorded_at: datetime | None = None,
) -> list[dict[str, Any]]:
    recorded_at = recorded_at or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []

    for prop in props:
        row = copy.deepcopy(prop)
        prop_id = str(row.get("propId") or "")
        if not prop_id:
            row["movement"] = {
                "snapshotCount": 0,
                "previous": None,
                "change": None,
            }
            rows.append(row)
            continue

        history = _LINE_MOVEMENT_HISTORY.setdefault(prop_id, [])
        snapshot = _snapshot_from_prop(row, recorded_at)
        previous = history[-1] if history else None

        if previous is None or _snapshot_changed(previous, snapshot):
            history.append(snapshot)
            del history[:-_MAX_HISTORY_PER_PROP]

        row["movement"] = {
            "snapshotCount": len(history),
            "previous": copy.deepcopy(previous),
            "change": _change(previous, snapshot) if previous else None,
        }
        rows.append(row)

    return rows


def _snapshot_from_prop(
    prop: dict[str, Any],
    recorded_at: datetime,
) -> dict[str, Any]:
    odds = prop.get("odds") or {}
    return {
        "recordedAt": recorded_at.isoformat(),
        "line": prop.get("line"),
        "over": odds.get("over"),
        "under": odds.get("under"),
    }


def _snapshot_changed(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    return any(
        previous.get(field) != current.get(field)
        for field in ("line", "over", "under")
    )


def _change(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, float | None]:
    return {
        "line": _delta(current.get("line"), previous.get("line")),
        "over": _delta(current.get("over"), previous.get("over")),
        "under": _delta(current.get("under"), previous.get("under")),
    }


def _delta(current: Any, previous: Any) -> float | None:
    try:
        return round(float(current) - float(previous), 4)
    except (TypeError, ValueError):
        return None
