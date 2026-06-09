from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import re
from typing import Any

from .mlb_bridge import stat_mapping_for_market, stat_value_from_stats
from .mlb_props import slug_key
from .storage import GptActionStore


ENRICHMENT_VERSION = "bet_history_enrichment_v1"


async def enrich_bet_history(
    *,
    store: GptActionStore,
    mlb_engine: Any,
    import_id: str | None = None,
    from_date: str | None = None,
    missing_only: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    targets = store.bet_history_enrichment_targets(
        import_id=import_id,
        from_date=from_date,
        missing_only=missing_only,
        limit=limit,
    )
    schedule_cache: dict[str, dict[str, Any]] = {}
    summary = {
        "source": "mlb_stats_api_to_local_sqlite_snapshots",
        "version": ENRICHMENT_VERSION,
        "filters": {
            "importId": import_id,
            "fromDate": from_date,
            "missingOnly": bool(missing_only),
            "limit": limit,
        },
        "targets": len(targets),
        "snapshotsCreated": 0,
        "snapshotsReused": 0,
        "legsEnriched": 0,
        "legsSkipped": 0,
        "skipReasons": {},
        "resultMismatches": 0,
        "rows": [],
        "storeSummary": {},
        "notes": [
            "Snapshots are immutable inserts keyed by MLB gamePk; existing snapshots are reused.",
            "Pregame/context fields are separated from grading fields to avoid postgame boxscore leakage.",
            "Historic analysis reads stored SQLite snapshots; it does not call live MLB APIs.",
        ],
    }

    for leg in targets:
        row = await _enrich_leg(
            leg,
            store=store,
            mlb_engine=mlb_engine,
            schedule_cache=schedule_cache,
        )
        summary["rows"].append(row)
        if row.get("status") == "enriched":
            summary["legsEnriched"] += 1
            summary["snapshotsCreated"] += 1 if row.get("snapshotAction") == "created" else 0
            summary["snapshotsReused"] += 1 if row.get("snapshotAction") == "reused" else 0
            if row.get("resultMismatch"):
                summary["resultMismatches"] += 1
        else:
            summary["legsSkipped"] += 1

    summary["skipReasons"] = dict(Counter(row.get("reason") for row in summary["rows"] if row.get("reason")))
    summary["storeSummary"] = store.bet_history_enrichment_report()
    summary["persistence"] = store.sync_bet_history_to_supabase(
        table_names=("bet_history_game_snapshots", "bet_history_leg_enrichments")
    )
    return summary


def format_enrichment_report(report: dict[str, Any]) -> str:
    lines = [
        "Bet Historic Enrichment",
        "----------------------",
        f"Targets: {report.get('targets') or 0}",
        f"Legs enriched: {report.get('legsEnriched') or 0}",
        f"Legs skipped: {report.get('legsSkipped') or 0}",
        f"Snapshots created: {report.get('snapshotsCreated') or 0}",
        f"Snapshots reused: {report.get('snapshotsReused') or 0}",
        f"Result mismatches: {report.get('resultMismatches') or 0}",
        f"Skip reasons: {report.get('skipReasons') or {}}",
    ]
    store_summary = report.get("storeSummary") or {}
    lines.extend(
        [
            "",
            "Stored snapshot layer:",
            f"  Game snapshots: {store_summary.get('snapshots') or 0}",
            f"  Leg enrichments: {store_summary.get('legEnrichments') or 0}",
            f"  Snapshot stat values: {store_summary.get('statValues') or 0}",
            f"  Snapshot grades: {store_summary.get('gradedBySnapshot') or 0}",
        ]
    )
    rows = list(report.get("rows") or [])[:8]
    if rows:
        lines.extend(["", "Recent rows:"])
        for row in rows:
            if row.get("status") == "enriched":
                result = row.get("enrichedResultStatus") or "ungraded"
                lines.append(
                    f"- {row.get('playerName') or 'unknown'} | {row.get('marketKey') or 'unknown'} "
                    f"{row.get('side') or '?'} {row.get('line') if row.get('line') is not None else '?'} | "
                    f"gamePk {row.get('gamePk')} | stat {row.get('statValue')} | {result}"
                )
            else:
                lines.append(
                    f"- skipped {row.get('playerName') or 'unknown'} | "
                    f"{row.get('reason') or 'unknown_reason'}"
                )
    if report.get("notes"):
        lines.extend(["", "Notes:"])
        lines.extend(f"- {note}" for note in report["notes"])
    return "\n".join(lines)


async def _enrich_leg(
    leg: dict[str, Any],
    *,
    store: GptActionStore,
    mlb_engine: Any,
    schedule_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    bet_date = str(leg.get("betDate") or "").strip()
    if not bet_date:
        return _skip_row(leg, "missing_bet_date")
    schedule = schedule_cache.get(bet_date)
    if schedule is None:
        schedule = await mlb_engine.get_schedule(bet_date)
        schedule_cache[bet_date] = schedule

    game = _match_schedule_game(leg, schedule)
    if not game:
        return _skip_row(leg, "mlb_game_not_matched")
    game_pk = _int_or_none(game.get("gamePk"))
    if game_pk is None:
        return _skip_row(leg, "matched_game_missing_game_pk")

    snapshot = store.get_bet_history_game_snapshot(game_pk)
    snapshot_action = "reused"
    if not snapshot:
        context = await mlb_engine.get_game_context(game_pk)
        snapshot = _build_game_snapshot(game, context)
        store.save_bet_history_game_snapshot(snapshot)
        snapshot_action = "created"
    context = snapshot.get("rawContext") or {}
    enrichment = _build_leg_enrichment(leg, game=game, context=context)
    enrichment["gamePk"] = game_pk
    saved = store.save_bet_history_leg_enrichment(enrichment)
    result_mismatch = bool(
        saved.get("resultStatus")
        and leg.get("resultStatus")
        and saved.get("resultStatus") != leg.get("resultStatus")
    )
    return {
        "status": "enriched",
        "historyLegId": leg.get("historyLegId"),
        "playerName": leg.get("playerName"),
        "marketKey": leg.get("marketKey"),
        "side": leg.get("side"),
        "line": leg.get("line"),
        "gamePk": game_pk,
        "snapshotAction": snapshot_action,
        "playerMlbId": saved.get("playerMlbId"),
        "statKey": saved.get("statKey"),
        "statValue": saved.get("statValue"),
        "enrichedResultStatus": saved.get("resultStatus"),
        "importedResultStatus": leg.get("resultStatus"),
        "contextQuality": saved.get("contextQuality"),
        "resultMismatch": result_mismatch,
        "notes": saved.get("notes") or [],
    }


def _build_game_snapshot(game: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    away_team = ((context.get("teams") or {}).get("away") or {}).get("team") or game.get("awayTeam") or {}
    home_team = ((context.get("teams") or {}).get("home") or {}).get("team") or game.get("homeTeam") or {}
    away_name = away_team.get("name")
    home_name = home_team.get("name")
    probable_pitchers = {
        "away": (game.get("awayTeam") or {}).get("probablePitcher"),
        "home": (game.get("homeTeam") or {}).get("probablePitcher"),
    }
    status = context.get("status") or {}
    return {
        "gamePk": game.get("gamePk") or context.get("gamePk"),
        "officialDate": context.get("officialDate") or _date_prefix(context.get("gameDate") or game.get("gameDate")),
        "gameDate": context.get("gameDate") or game.get("gameDate"),
        "matchupKey": _team_set_key([away_name, home_name]),
        "awayTeamName": away_name,
        "homeTeamName": home_name,
        "finalStatus": status.get("detailedState") or game.get("status"),
        "venue": context.get("venue") or {},
        "probablePitchers": probable_pitchers,
        "pregameContext": {
            "gamePk": game.get("gamePk") or context.get("gamePk"),
            "officialDate": context.get("officialDate"),
            "venue": context.get("venue"),
            "weather": context.get("weather"),
            "gameInfo": context.get("gameInfo"),
            "probablePitchers": probable_pitchers,
            "lineups": _snapshot_lineups(context),
            "leakPolicy": "No boxscore player stat values are stored in pregameContext.",
        },
        "gradingContext": {
            "status": context.get("status"),
            "statusRiskFlags": context.get("statusRiskFlags") or [],
            "teams": context.get("teams") or {},
            "leakPolicy": "Boxscore/player stats live here only and are used for grading or offline model-backtest features.",
        },
        "rawContext": context,
        "source": "mlb_stats_api",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }


def _build_leg_enrichment(
    leg: dict[str, Any],
    *,
    game: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    mapping = stat_mapping_for_market(str(leg.get("marketKey") or ""))
    player_match = _find_player_for_leg(leg, context)
    player = player_match.get("player") or {}
    notes = ["game_matched"]
    if player:
        notes.append("player_matched")
    else:
        notes.append("player_not_found_in_boxscore")
    if not mapping.get("supported"):
        notes.append("market_stat_mapping_unsupported")

    group = str(mapping.get("group") or "hitting")
    stat_source = _player_game_stats(player, group)
    stat_value = stat_value_from_stats(mapping, stat_source) if player else None
    if stat_value is None:
        notes.append("stat_unavailable")
    enriched_result = _settle_result(
        side=str(leg.get("side") or "").lower(),
        line=_float_or_none(leg.get("line")),
        actual_stat=stat_value,
    )
    if enriched_result and leg.get("resultStatus") and enriched_result != leg.get("resultStatus"):
        notes.append("result_mismatch_with_imported_history")

    team_context = player_match.get("teamContext") or {}
    team = team_context.get("team") or {}
    context_quality = "full" if player and stat_value is not None else "partial" if player else "low"
    return {
        "historyLegId": leg.get("historyLegId"),
        "playerMlbId": player.get("mlbId"),
        "playerTeamSide": player_match.get("side"),
        "playerTeamName": team.get("name"),
        "lineupConfirmed": bool(team_context.get("lineupConfirmed")),
        "confirmedStarter": bool(player.get("confirmedStarter")),
        "battingOrder": player.get("battingOrder"),
        "batSide": player.get("batSide"),
        "pitchHand": player.get("pitchHand"),
        "position": player.get("position"),
        "statKey": mapping.get("statKey") or mapping.get("statFormula"),
        "statValue": stat_value,
        "enrichedResultStatus": enriched_result,
        "contextQuality": context_quality,
        "pregameContext": {
            "gamePk": game.get("gamePk") or context.get("gamePk"),
            "lineupConfirmed": bool(team_context.get("lineupConfirmed")),
            "confirmedStarter": bool(player.get("confirmedStarter")),
            "battingOrder": player.get("battingOrder"),
            "batSide": player.get("batSide"),
            "pitchHand": player.get("pitchHand"),
            "position": player.get("position"),
            "venue": context.get("venue"),
            "weather": context.get("weather"),
            "gameInfo": context.get("gameInfo"),
        },
        "gradingContext": {
            "statKey": mapping.get("statKey"),
            "statFormula": mapping.get("statFormula"),
            "statValue": stat_value,
            "enrichedResultStatus": enriched_result,
            "importedResultStatus": leg.get("resultStatus"),
            "source": "stored_mlb_game_snapshot",
        },
        "notes": sorted(set(notes)),
        "source": "mlb_stats_api_snapshot",
        "enrichedAt": datetime.now(timezone.utc).isoformat(),
    }


def _find_player_for_leg(leg: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    player_key = slug_key(leg.get("playerName"))
    if not player_key:
        return {}
    teams = context.get("teams") or {}
    preferred_side = _preferred_team_side(leg, context)
    sides = [preferred_side] if preferred_side else []
    sides.extend(side for side in ("away", "home") if side not in sides)
    for side in sides:
        team_context = teams.get(side) or {}
        for player in _team_players(team_context):
            if slug_key(player.get("name")) == player_key:
                return {"side": side, "teamContext": team_context, "player": player}
    return {}


def _preferred_team_side(leg: dict[str, Any], context: dict[str, Any]) -> str | None:
    team_key = slug_key(leg.get("teamName"))
    if not team_key:
        return None
    teams = context.get("teams") or {}
    for side in ("away", "home"):
        team = ((teams.get(side) or {}).get("team") or {})
        if slug_key(team.get("name")) == team_key:
            return side
    return None


def _team_players(team_context: dict[str, Any]) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    seen: set[int] = set()
    for player in team_context.get("lineup") or []:
        player_id = _int_or_none(player.get("mlbId"))
        if player_id is not None:
            seen.add(player_id)
        players.append(player)
    for player in (team_context.get("playersById") or {}).values():
        player_id = _int_or_none(player.get("mlbId"))
        if player_id is not None and player_id in seen:
            continue
        players.append(player)
    return players


def _player_game_stats(player: dict[str, Any], group: str) -> dict[str, Any]:
    stats = player.get("stats") or {}
    if not isinstance(stats, dict):
        return {}
    if group in stats and isinstance(stats[group], dict):
        return stats[group]
    if group == "hitting" and isinstance(stats.get("batting"), dict):
        return stats["batting"]
    if group == "pitching" and isinstance(stats.get("pitching"), dict):
        return stats["pitching"]
    return stats


def _match_schedule_game(leg: dict[str, Any], schedule: dict[str, Any]) -> dict[str, Any] | None:
    target_keys = _leg_team_keys(leg)
    if not target_keys:
        return None
    best_partial: dict[str, Any] | None = None
    for game in schedule.get("games") or []:
        game_keys = _game_team_keys(game)
        if len(target_keys) >= 2 and target_keys == game_keys:
            return game
        if target_keys and target_keys.issubset(game_keys):
            if best_partial is not None:
                return None
            best_partial = game
    return best_partial


def _leg_team_keys(leg: dict[str, Any]) -> set[str]:
    values = [leg.get("teamName"), leg.get("opponentName")]
    matchup = str(leg.get("matchup") or "")
    if matchup:
        values.extend(_split_matchup(matchup))
    return {slug_key(value) for value in values if slug_key(value)}


def _split_matchup(value: str) -> list[str]:
    parts = re.split(r"\s+(?:-|vs\.?|at|@)\s+", value, flags=re.IGNORECASE)
    return [part.strip() for part in parts if part.strip()]


def _game_team_keys(game: dict[str, Any]) -> set[str]:
    return {
        slug_key((game.get("awayTeam") or {}).get("name")),
        slug_key((game.get("homeTeam") or {}).get("name")),
    } - {""}


def _team_set_key(names: list[Any]) -> str:
    return "|".join(sorted(slug_key(name) for name in names if slug_key(name)))


def _snapshot_lineups(context: dict[str, Any]) -> dict[str, Any]:
    teams = context.get("teams") or {}
    result: dict[str, Any] = {}
    for side in ("away", "home"):
        team_context = teams.get(side) or {}
        result[side] = {
            "team": team_context.get("team"),
            "lineupConfirmed": bool(team_context.get("lineupConfirmed")),
            "lineup": [
                {
                    "mlbId": player.get("mlbId"),
                    "name": player.get("name"),
                    "battingOrder": player.get("battingOrder"),
                    "position": player.get("position"),
                    "batSide": player.get("batSide"),
                    "pitchHand": player.get("pitchHand"),
                }
                for player in team_context.get("lineup") or []
            ],
        }
    return result


def _skip_row(leg: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "reason": reason,
        "historyLegId": leg.get("historyLegId"),
        "playerName": leg.get("playerName"),
        "marketKey": leg.get("marketKey"),
        "side": leg.get("side"),
        "line": leg.get("line"),
    }


def _settle_result(*, side: str | None, line: float | None, actual_stat: float | None) -> str | None:
    if side not in {"over", "under"} or line is None or actual_stat is None:
        return None
    if actual_stat == line:
        return "push"
    if side == "under":
        return "won" if actual_stat < line else "lost"
    return "won" if actual_stat > line else "lost"


def _date_prefix(value: Any) -> str | None:
    text = str(value or "")
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


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
