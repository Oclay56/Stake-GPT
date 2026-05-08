from __future__ import annotations

import re
import unicodedata
from typing import Any

from .line_movement import record_line_movements


def build_stable_props_payload(
    slate: dict[str, Any],
    include_movement: bool = True,
) -> dict[str, Any]:
    props = [
        _stable_prop_row(fixture, prop)
        for fixture in slate.get("fixtures") or []
        if not fixture.get("oddsError")
        for prop in fixture.get("playerProps") or []
    ]
    if include_movement:
        props = record_line_movements(props)

    return {
        "league": slate.get("league") or "MLB",
        "date": slate.get("date"),
        "timezone": slate.get("timezone"),
        "filters": slate.get("filters") or {"markets": [], "excludeMarkets": []},
        "fixtureCount": slate.get("fixtureCount", 0),
        "propCount": len(props),
        "props": props,
    }


def slug_key(value: Any) -> str:
    text = str(value or "").strip()
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower())
    return text.strip("-")


def _stable_prop_row(
    fixture: dict[str, Any],
    prop: dict[str, Any],
) -> dict[str, Any]:
    game = str(fixture.get("name") or fixture.get("slug") or "")
    player = _player_identity(prop.get("player"))
    team = _team_identity(prop.get("team"), game)
    market = _market_identity(prop.get("market"))
    fixture_slug = str(fixture.get("slug") or "")

    return {
        "propId": _prop_id(fixture_slug, player["key"], team["key"], market["key"]),
        "fixtureSlug": fixture_slug,
        "game": game,
        "startTime": fixture.get("startTime"),
        "status": fixture.get("status"),
        "type": fixture.get("type"),
        "player": player,
        "team": team,
        "market": market,
        "sportStatType": prop.get("sportStatType"),
        "line": prop.get("line"),
        "odds": {
            "over": prop.get("over"),
            "under": prop.get("under"),
        },
    }


def _player_identity(player_name: Any) -> dict[str, Any]:
    name = str(player_name or "")
    return {
        "name": name,
        "key": slug_key(name),
        "mlbId": None,
        "matchStatus": "needs_mlb_lookup",
    }


def _team_identity(team_name: Any, game: str) -> dict[str, Any]:
    name = str(team_name or "")
    key = slug_key(name)
    fixture_teams = _fixture_teams(game)
    matched_index = next(
        (
            index
            for index, fixture_team in enumerate(fixture_teams)
            if slug_key(fixture_team) == key
        ),
        None,
    )
    matched_name = fixture_teams[matched_index] if matched_index is not None else None

    return {
        "name": name,
        "key": key,
        "fixtureTeamMatched": matched_index is not None,
        "fixtureTeamIndex": matched_index,
        "fixtureTeamName": matched_name,
        "mlbId": None,
        "matchStatus": (
            "fixture_team_match" if matched_index is not None else "needs_mlb_lookup"
        ),
    }


def _market_identity(market_name: Any) -> dict[str, str]:
    name = str(market_name or "")
    return {
        "name": name,
        "key": slug_key(name),
    }


def _fixture_teams(game: str) -> list[str]:
    if " - " not in game:
        return []
    return [part.strip() for part in game.split(" - ", 1) if part.strip()]


def _prop_id(
    fixture_slug: str,
    player_key: str,
    team_key: str,
    market_key: str,
) -> str:
    parts = [
        fixture_slug or "unknown-fixture",
        player_key or "unknown-player",
        team_key or "unknown-team",
        market_key or "unknown-market",
    ]
    return ":".join(parts)
