from __future__ import annotations

import hashlib
import json
from typing import Any


FINGERPRINT_VERSION = "bet_history_fingerprint_v4"


def history_fingerprint(legs: list[dict[str, Any]]) -> str:
    canonical = [_history_fingerprint_leg(leg) for leg in legs]
    payload = json.dumps(
        canonical,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _history_fingerprint_leg(leg: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticketId": leg.get("ticketId"),
        "betDate": leg.get("betDate"),
        "matchup": leg.get("matchup"),
        "playerName": leg.get("playerName"),
        "teamName": leg.get("teamName"),
        "marketKey": leg.get("marketKey"),
        "side": leg.get("side"),
        "line": leg.get("line"),
        "odds": leg.get("odds"),
        "stakeAmount": leg.get("stakeAmount"),
        "payoutAmount": leg.get("payoutAmount"),
        "resultStatus": leg.get("resultStatus"),
        "actualStat": leg.get("actualStat"),
    }
