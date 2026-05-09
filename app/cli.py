from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .analyzer import analyze_stored_props
from .backtest import backtest_stored_props
from .mlb_bridge import build_match_audit, enrich_props_with_mlb_data
from .mlb_data import MLBDataEngine, MLBStatsClient, build_mlb_http_client
from .mlb_props import build_stable_props_payload
from .parlay import PROFILE_PRESETS, build_parlay_candidates, build_pick_board
from .settlement import settle_stored_props
from .slate import (
    DEFAULT_TIMEZONE,
    build_mlb_player_props_slate,
    build_mlb_primary_line_check,
)
from .stake_client import StakeClient, build_http_client
from .storage import SnapshotStore


MENU_OPTIONS = (
    ("1", "Stake MLB props"),
    ("2", "Enriched props"),
    ("3", "Match audit"),
    ("4", "MLB player search"),
    ("5", "MLB player profile + history"),
    ("6", "Primary-line check"),
    ("7", "Refresh stored props"),
    ("8", "Stored props"),
    ("9", "Prop movement"),
    ("10", "Latest props"),
    ("11", "Analyze stored props"),
    ("12", "Settle stored props"),
    ("13", "Backtest stored props"),
    ("14", "Run slate workflow"),
    ("15", "Build parlays"),
    ("16", "Pick board"),
    ("17", "Daily slate workflow"),
    ("0", "Exit"),
)

PRESET_MARKETS = {
    "mlb-core": ("hits", "runs", "rbi", "total-bases"),
    "power": ("total-bases", "home-runs", "rbi"),
    "pitchers": (
        "strikeouts",
        "pitcher-strikeouts",
        "earned-runs",
        "walks-allowed",
        "hits-allowed",
        "outs-recorded",
    ),
    "all-supported": (
        "hits",
        "runs",
        "rbi",
        "total-bases",
        "home-runs",
        "strikeouts",
        "pitcher-strikeouts",
        "earned-runs",
        "walks-allowed",
        "hits-allowed",
        "outs-recorded",
    ),
}

PROFILE_CHOICES = tuple(PROFILE_PRESETS)


def render_menu() -> str:
    lines = ["Stake + MLB Test CLI", ""]
    lines.extend(f"{key}. {label}" for key, label in MENU_OPTIONS)
    return "\n".join(lines)


def format_enriched_props(payload: dict[str, Any], max_rows: int = 10) -> str:
    lines = [
        f"Date: {payload.get('date')}",
        (
            f"Props: {payload.get('propCount', 0)} | "
            f"matched: {payload.get('matchedPropCount', 0)} | "
            f"unmatched: {payload.get('unmatchedPropCount', 0)}"
        ),
        "",
    ]
    for prop in (payload.get("props") or [])[:max_rows]:
        player = prop.get("player") or {}
        team = prop.get("team") or {}
        market = prop.get("market") or {}
        odds = prop.get("odds") or {}
        context = prop.get("statContext") or {}
        lines.append(
            f"{player.get('name')} | {team.get('name')} | "
            f"{market.get('name')} {prop.get('line')} | "
            f"O {odds.get('over')} / U {odds.get('under')}"
        )
        lines.append(
            f"  MLB {player.get('mlbId')} | {player.get('matchStatus')} | "
            f"{context.get('statKey')} recent/gm {context.get('recentPerGame')} | "
            f"season {context.get('seasonValue')}"
        )
    return "\n".join(lines).rstrip()


def format_match_audit(audit: dict[str, Any], max_rows: int = 15) -> str:
    issue_counts = audit.get("issueCounts") or {}
    issues = ", ".join(
        f"{key}={issue_counts[key]}" for key in sorted(issue_counts)
    ) or "none"
    lines = [
        f"Date: {audit.get('date')}",
        (
            f"Props: {audit.get('propCount', 0)} | "
            f"matched: {audit.get('matchedPropCount', 0)} | "
            f"unmatched: {audit.get('unmatchedPropCount', 0)}"
        ),
        f"Issues: {issues}",
        "",
    ]
    for row in (audit.get("rows") or [])[:max_rows]:
        row_issues = ", ".join(row.get("issues") or []) or "none"
        lines.append(
            f"{row.get('player')} | {row.get('team')} | {row.get('market')} | "
            f"{row.get('status')} | issues: {row_issues}"
        )
    return "\n".join(lines).rstrip()


def format_player_search(payload: dict[str, Any], max_rows: int = 10) -> str:
    lines = [
        f"Search: {payload.get('query')} | results: {payload.get('playerCount', 0)}",
        "",
    ]
    for player in (payload.get("players") or [])[:max_rows]:
        team = player.get("team") or {}
        lines.append(
            f"{player.get('mlbId')} | {player.get('name')} | "
            f"{player.get('position') or ''} | {team.get('name') or ''}"
        )
    return "\n".join(lines).rstrip()


def format_stake_props(payload: dict[str, Any], max_rows: int = 10) -> str:
    return format_enriched_props(
        {
            **payload,
            "matchedPropCount": 0,
            "unmatchedPropCount": payload.get("propCount", 0),
            "props": [
                {
                    **prop,
                    "statContext": {},
                    "player": {
                        **(prop.get("player") or {}),
                        "mlbId": None,
                        "matchStatus": "not_enriched",
                    },
                }
                for prop in payload.get("props") or []
            ],
        },
        max_rows=max_rows,
    )


def format_profile_history(profile: dict[str, Any], history: dict[str, Any]) -> str:
    player = profile.get("player") or {}
    lines = [
        f"{player.get('mlbId')} | {player.get('name')} | {player.get('position') or ''}",
        f"Season stats: {player.get('stats') or {}}",
        (
            f"Recent games used: {history.get('gamesUsed', 0)} | "
            f"per game: {history.get('perGame') or {}}"
        ),
    ]
    return "\n".join(lines)


def format_primary_line_check(report: dict[str, Any], max_rows: int = 10) -> str:
    lines = [
        f"Date: {report.get('date')}",
        (
            f"Checked props: {report.get('checkedPropCount', 0)} | "
            f"alternate-line props: {report.get('alternateLinePropCount', 0)}"
        ),
        "",
    ]
    for check in (report.get("checks") or [])[:max_rows]:
        lines.append(
            f"{check.get('player')} | {check.get('team')} | {check.get('market')} | "
            f"selected {check.get('selectedLine')} | valid lines {check.get('validLineCount')}"
        )
    return "\n".join(lines).rstrip()


def format_refresh_result(result: dict[str, Any]) -> str:
    lines = [
        (
            "Refresh saved: "
            f"{result.get('propSnapshotsInserted', 0)} prop snapshots | "
            f"{result.get('playerStatSnapshotsInserted', 0)} MLB stat snapshots"
        ),
        f"Date: {result.get('date')}",
        f"Phase: {result.get('snapshotPhase') or 'manual'}",
    ]
    if result.get("snapshotLabel"):
        lines.append(f"Label: {result.get('snapshotLabel')}")
    lines.append(f"DB: {result.get('dbPath')}")
    return "\n".join(lines)


def format_stored_props(payload: dict[str, Any], max_rows: int = 20) -> str:
    lines = [
        f"Stored props: {payload.get('count', 0)}",
        f"DB: {payload.get('dbPath')}",
        "",
    ]
    for prop in (payload.get("props") or [])[:max_rows]:
        lines.append(
            f"{prop.get('playerName')} | {prop.get('teamName')} | "
            f"{prop.get('marketKey')} {prop.get('line')} | "
            f"O {prop.get('overOdds')} / U {prop.get('underOdds')}"
        )
        lines.append(
            f"  {prop.get('matchStatus')} | "
            f"{prop.get('snapshotPhase') or 'manual'} | "
            f"{prop.get('snapshotLabel') or '-'} | "
            f"{prop.get('capturedAt')} | "
            f"propId {prop.get('propId')}"
        )
    return "\n".join(lines).rstrip()


def format_latest_props(payload: dict[str, Any], max_rows: int = 20) -> str:
    lines = [
        f"Latest props: {payload.get('count', 0)}",
        f"DB: {payload.get('dbPath')}",
        "",
    ]
    for prop in (payload.get("props") or [])[:max_rows]:
        lines.append(_format_prop_row(prop))
    return "\n".join(lines).rstrip()


def format_analyze_stored(payload: dict[str, Any], max_rows: int = 10) -> str:
    lines = [
        (
            "Stored Analyzer | "
            f"date {payload.get('date') or 'all'} | "
            f"market {payload.get('market') or 'all'} | "
            f"phase {payload.get('snapshotPhase') or 'all'}"
        ),
        (
            f"Counts: watchlist={payload.get('counts', {}).get('watchlist', 0)} | "
            f"neutral={payload.get('counts', {}).get('neutral', 0)} | "
            f"avoid={payload.get('counts', {}).get('avoid', 0)}"
        ),
        "",
    ]
    bucket_labels = (
        ("watchlist", "Watchlist"),
        ("neutral", "Neutral"),
        ("avoid", "Avoid / Needs Review"),
    )
    buckets = payload.get("buckets") or {}
    for key, label in bucket_labels:
        rows = buckets.get(key) or []
        lines.append(f"{label} ({len(rows)})")
        for row in rows[:max_rows]:
            lines.append(
                f"  {row.get('playerName')} | {row.get('teamName')} | "
                f"{row.get('marketKey')} {row.get('line')} | lean {row.get('lean')} | "
                f"score {row.get('score')} | confidence {row.get('confidence')} | "
                f"recent/gm {row.get('recentPerGame')} | season {row.get('seasonValue')} | "
                f"O {row.get('overOdds')} / U {row.get('underOdds')}"
            )
            lines.append(f"    reasons: {', '.join(row.get('reasons') or []) or 'none'}")
            risk_flags = row.get("riskFlags") or []
            if risk_flags:
                lines.append(f"    risk: {', '.join(risk_flags)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_settlement(payload: dict[str, Any], max_rows: int = 20) -> str:
    counts = payload.get("counts") or {}
    lines = [
        (
            "Settlement | "
            f"date {payload.get('date') or 'all'} | "
            f"market {payload.get('market') or 'all'} | "
            f"phase {payload.get('snapshotPhase') or 'all'}"
        ),
        (
            f"Counts: settled={counts.get('settled', 0)} | "
            f"pending={counts.get('pending', 0)} | "
            f"ungraded={counts.get('ungraded', 0)} | "
            f"over={counts.get('over', 0)} | "
            f"under={counts.get('under', 0)} | "
            f"push={counts.get('push', 0)}"
        ),
        "",
    ]
    for row in (payload.get("rows") or [])[:max_rows]:
        actual = row.get("actualValue")
        actual_text = "-" if actual is None else actual
        lines.append(
            f"{row.get('playerName')} | {row.get('teamName')} | "
            f"{row.get('marketKey')} {row.get('line')} | "
            f"actual {actual_text} | result {row.get('actualResult')} | "
            f"over {row.get('overOutcome')}"
        )
        reasons = row.get("reasons") or []
        if reasons:
            lines.append(f"  reasons: {', '.join(reasons)}")
    return "\n".join(lines).rstrip()


def format_backtest(payload: dict[str, Any], max_rows: int = 20) -> str:
    counts = payload.get("counts") or {}
    lines = [
        (
            "Backtest | "
            f"date {payload.get('date') or 'all'} | "
            f"market {payload.get('market') or 'all'} | "
            f"phase {payload.get('snapshotPhase') or 'all'}"
        ),
        (
            f"Counts: rows={counts.get('rows', 0)} | "
            f"settled={counts.get('settled', 0)} | "
            f"decisions={counts.get('decisions', 0)} | "
            f"correct={counts.get('correct', 0)} | "
            f"incorrect={counts.get('incorrect', 0)} | "
            f"accuracy={_format_accuracy(_accuracy(counts))}"
        ),
        "",
        "By bucket:",
    ]
    for bucket in ("watchlist", "neutral", "avoid"):
        values = (payload.get("byBucket") or {}).get(bucket) or {}
        lines.append(f"  {bucket}: {_format_backtest_group(values)}")

    lines.append("")
    lines.append("Rows:")
    for row in (payload.get("rows") or [])[:max_rows]:
        actual = row.get("actualValue")
        actual_text = "-" if actual is None else actual
        lines.append(
            f"{row.get('playerName')} | {row.get('teamName')} | "
            f"{row.get('marketKey')} {row.get('line')} | "
            f"{row.get('bucket')}/{row.get('lean')} | "
            f"score {row.get('score')} | actual {actual_text} | "
            f"over {row.get('overOutcome')} | "
            f"decision {row.get('decisionOutcome')}"
        )
    return "\n".join(lines).rstrip()


def format_slate_run(payload: dict[str, Any], max_rows: int = 10) -> str:
    refresh = payload.get("refresh") or {}
    analysis_saved = payload.get("analysisSaved") or {}
    analysis = payload.get("analysis") or {}
    counts = analysis.get("counts") or {}
    lines = [
        (
            "Slate Run | "
            f"date {payload.get('date')} | "
            f"preset {payload.get('preset')} | "
            f"phase {payload.get('snapshotPhase') or 'manual'}"
        ),
        f"Markets: {', '.join(payload.get('markets') or []) or 'all'}",
        (
            f"Saved: {refresh.get('propSnapshotsInserted', 0)} props | "
            f"{refresh.get('playerStatSnapshotsInserted', 0)} MLB stat snapshots | "
            f"{analysis_saved.get('analysisSnapshotsInserted', 0)} analyzer decisions"
        ),
        (
            f"Analyzer: watchlist={counts.get('watchlist', 0)} | "
            f"neutral={counts.get('neutral', 0)} | "
            f"avoid={counts.get('avoid', 0)}"
        ),
        "",
        "Top watchlist:",
    ]
    rows = ((analysis.get("buckets") or {}).get("watchlist") or [])[:max_rows]
    if not rows:
        lines.append("  none")
    for row in rows:
        lines.append(
            f"  {row.get('playerName')} | {row.get('teamName')} | "
            f"{row.get('marketKey')} {row.get('line')} | "
            f"lean {row.get('lean')} | score {row.get('score')} | "
            f"confidence {row.get('confidence')} | "
            f"O {row.get('overOdds')} / U {row.get('underOdds')}"
        )
        lines.append(f"    reasons: {', '.join(row.get('reasons') or []) or 'none'}")
    return "\n".join(lines).rstrip()


def format_daily_slate(
    payload: dict[str, Any],
    max_picks: int = 10,
    max_parlays: int = 5,
) -> str:
    lines = ["Daily Slate Workflow", ""]
    lines.append(format_slate_run(payload.get("slateRun") or {}, max_rows=max_picks))
    lines.extend(["", format_pick_board(payload.get("pickBoard") or {}, max_rows=max_picks)])
    lines.extend(
        [
            "",
            format_parlay_candidates(
                payload.get("parlays") or {},
                max_rows=max_parlays,
            ),
        ]
    )
    if payload.get("reportPath"):
        lines.extend(["", f"Report: {payload.get('reportPath')}"])
    return "\n".join(lines).rstrip()


def format_parlay_candidates(payload: dict[str, Any], max_rows: int = 5) -> str:
    requested = payload.get("requested") or {}
    lines = [
        (
            "Parlay Builder | "
            f"legs {requested.get('legs')} | "
            f"odds {requested.get('oddsMin')}-{requested.get('oddsMax')} | "
            f"mode {requested.get('mode')}"
        ),
        (
            f"Eligible legs: {payload.get('eligibleCount', 0)} | "
            f"candidates: {payload.get('candidateCount', 0)}"
        ),
    ]
    warnings = payload.get("warnings") or []
    if warnings:
        lines.append(f"Warnings: {', '.join(warnings)}")
    lines.append("")

    candidates = (payload.get("candidates") or [])[:max_rows]
    if not candidates:
        lines.append("No candidates.")
        return "\n".join(lines).rstrip()

    for candidate in candidates:
        range_text = "in range" if candidate.get("withinRange") else "closest"
        lines.append(
            f"#{candidate.get('rank')} | "
            f"total odds {_format_number(candidate.get('totalOdds'))} | "
            f"score {candidate.get('score')} | {range_text}"
        )
        correlation_risk = candidate.get("correlationRisk") or {}
        if correlation_risk:
            quote_text = "quote needed" if correlation_risk.get("quoteNeeded") else str(
                correlation_risk.get("repriceSignal") or "quote compared"
            )
            lines.append(
                f"  raw product {_format_number(candidate.get('rawProductOdds'))} | "
                f"correlation {correlation_risk.get('riskLevel') or 'unknown'} | "
                f"{quote_text}"
            )
            if correlation_risk.get("warning"):
                lines.append(f"  {correlation_risk.get('warning')}")
            if correlation_risk.get("tags"):
                lines.append(f"  correlation tags: {', '.join(correlation_risk.get('tags') or [])}")
            if correlation_risk.get("gameScriptTags"):
                lines.append(f"  game script: {', '.join(correlation_risk.get('gameScriptTags') or [])}")
        conflict_warnings = candidate.get("conflictWarnings") or []
        if conflict_warnings:
            lines.append(f"  conflicts: {', '.join(conflict_warnings)}")
        for leg in candidate.get("legs") or []:
            locked_text = " | locked" if leg.get("locked") else ""
            lines.append(
                f"  - {leg.get('playerName')} | {leg.get('teamName')} | "
                f"{leg.get('fixtureSlug')} | {leg.get('marketKey')} {leg.get('line')} | "
                f"{leg.get('lean')} @ {_format_number(leg.get('odds'))} | "
                f"score {leg.get('score')} | {leg.get('confidence')}{locked_text}"
            )
            why_included = leg.get("whyIncluded") or []
            if why_included:
                lines.append(f"    why: {', '.join(why_included)}")
            why_not_stronger = leg.get("whyNotStronger") or []
            if why_not_stronger:
                lines.append(f"    watch: {', '.join(why_not_stronger)}")
    return "\n".join(lines).rstrip()


def format_pick_board(payload: dict[str, Any], max_rows: int = 50) -> str:
    requested = payload.get("requested") or {}
    markets = ", ".join(requested.get("markets") or []) or "all"
    lines = [
        (
            "Pick Board | "
            f"date {requested.get('date') or 'all'} | "
            f"phase {requested.get('snapshotPhase') or 'all'} | "
            f"markets {markets} | "
            f"profile {requested.get('profile') or 'custom'}"
        ),
        f"Eligible legs: {payload.get('eligibleCount', 0)}",
        "",
    ]
    picks = (payload.get("picks") or [])[:max_rows]
    if not picks:
        lines.append("No eligible picks.")
        return "\n".join(lines).rstrip()

    for pick in picks:
        risk_flags = pick.get("riskFlags") or []
        risk_text = f" | risk {', '.join(risk_flags)}" if risk_flags else ""
        matchup = pick.get("game") or pick.get("fixtureSlug")
        lines.append(
            f"{pick.get('pickNumber')}. "
            f"{pick.get('playerName')} | {pick.get('teamName')} | "
            f"{matchup} | {pick.get('marketKey')} {pick.get('line')} | "
            f"{pick.get('lean')} @ {_format_number(pick.get('odds'))} | "
            f"score {pick.get('score')} | {pick.get('confidence')}{risk_text}"
        )
        lines.append(
            "   detail: "
            f"recent5/gm {_format_number(pick.get('recentPerGame'))} | "
            f"season {_format_number(pick.get('seasonValue'))} | "
            f"season/gm {_format_number(pick.get('seasonPerGame'))} | "
            f"Stake O {_format_number(pick.get('overOdds'))} / "
            f"U {_format_number(pick.get('underOdds'))}"
        )
        recent_games = _format_recent_games(pick)
        if recent_games:
            lines.append(f"   recent games: {recent_games}")
        why_included = pick.get("whyIncluded") or []
        if why_included:
            lines.append(f"   why: {', '.join(why_included)}")
        lines.append(f"   risk: {', '.join(risk_flags) if risk_flags else 'none'}")
    return "\n".join(lines).rstrip()


def format_movement(payload: dict[str, Any]) -> str:
    lines = [
        f"Movement for {payload.get('propId')}",
        f"Snapshots: {payload.get('snapshotCount', 0)}",
        "",
    ]
    snapshots = payload.get("snapshots") or []
    if snapshots:
        latest = snapshots[-1]
        lines.append(
            "Latest: "
            f"{latest.get('capturedAt')} | "
            f"{latest.get('snapshotPhase') or 'manual'} | "
            f"{latest.get('snapshotLabel') or '-'} | "
            f"line {latest.get('line')} | "
            f"O {latest.get('overOdds')} / U {latest.get('underOdds')}"
        )
        lines.append("")
    for change in payload.get("changes") or []:
        lines.append(
            f"{change.get('fromCapturedAt')} -> {change.get('toCapturedAt')} | "
            f"line {change.get('line')} | "
            f"over {change.get('overOdds')} | under {change.get('underOdds')}"
        )
    return "\n".join(lines).rstrip()


def _format_prop_row(prop: dict[str, Any]) -> str:
    return "\n".join(
        [
            (
                f"{prop.get('playerName')} | {prop.get('teamName')} | "
                f"{prop.get('marketKey')} {prop.get('line')} | "
                f"O {prop.get('overOdds')} / U {prop.get('underOdds')}"
            ),
            (
                f"  {prop.get('matchStatus')} | "
                f"{prop.get('snapshotPhase') or 'manual'} | "
                f"{prop.get('snapshotLabel') or '-'} | "
                f"{prop.get('capturedAt')} | "
                f"propId {prop.get('propId')}"
            ),
        ]
    )


def _format_backtest_group(values: dict[str, Any]) -> str:
    return (
        f"total {values.get('total', 0)} | "
        f"decisions {values.get('decisions', 0)} | "
        f"correct {values.get('correct', 0)} | "
        f"incorrect {values.get('incorrect', 0)} | "
        f"accuracy {_format_accuracy(values.get('accuracy'))}"
    )


def _accuracy(counts: dict[str, Any]) -> float | None:
    decisions = int(counts.get("decisions") or 0)
    if decisions <= 0:
        return None
    return float(counts.get("correct") or 0) / decisions


def _format_accuracy(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def _format_number(value: Any, places: int = 2) -> str:
    if value is None:
        return "-"
    try:
        formatted = f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return str(value)
    if "." not in formatted:
        return formatted
    return formatted.rstrip("0").rstrip(".")


def _format_recent_games(pick: dict[str, Any]) -> str:
    stat_key = pick.get("statKey") or pick.get("marketKey")
    parts = []
    for game in (pick.get("recentGames") or [])[:5]:
        stats = game.get("stats") or {}
        value = stats.get(stat_key)
        if value is None:
            continue
        opponent = game.get("opponent")
        opponent_text = f" vs {opponent}" if opponent else ""
        parts.append(
            f"{game.get('date')} {_format_number(value)}{opponent_text}"
        )
    return ", ".join(parts)


async def run_command(args: argparse.Namespace) -> str:
    if args.command == "search":
        async with _mlb_engine() as engine:
            return format_player_search(
                await engine.search_players(args.query, limit=args.limit),
                max_rows=args.limit,
            )

    if args.command == "profile":
        async with _mlb_engine() as engine:
            profile = await engine.get_player_profile(
                args.player_id,
                season=args.season,
                group=args.group,
            )
            history = await engine.get_player_recent_history(
                args.player_id,
                group=args.group,
                season=args.season,
                limit=args.history_limit,
            )
            return format_profile_history(profile, history)

    if args.command == "props":
        async with _stake_client() as stake:
            props = await _props_payload(
                stake,
                args.date,
                args.limit,
                args.markets,
                args.exclude_markets,
            )
            return format_stake_props(props, max_rows=args.limit)

    if args.command == "enriched":
        async with _stake_client() as stake, _mlb_engine() as engine:
            enriched = await _enriched_payload(stake, engine, args)
            return format_enriched_props(enriched, max_rows=args.limit)

    if args.command == "audit":
        async with _stake_client() as stake, _mlb_engine() as engine:
            enriched = await _enriched_payload(stake, engine, args)
            return format_match_audit(build_match_audit(enriched), max_rows=args.limit)

    if args.command == "lines":
        async with _stake_client() as stake:
            timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
            report = await build_mlb_primary_line_check(
                stake,
                _parse_date(args.date),
                timezone_name,
                args.limit,
                _csv_filter(args.markets),
                _csv_filter(args.exclude_markets),
            )
            return format_primary_line_check(report, max_rows=args.limit)

    if args.command == "refresh":
        async with _stake_client() as stake, _mlb_engine() as engine:
            enriched = await _enriched_payload(stake, engine, args)
        store = _snapshot_store(args)
        result = store.save_enriched_props(
            enriched,
            snapshot_phase=args.snapshot_phase,
            snapshot_label=args.snapshot_label,
        )
        result.update(
            {
                "date": enriched.get("date"),
                "snapshotPhase": args.snapshot_phase,
                "snapshotLabel": args.snapshot_label,
                "dbPath": str(store.db_path),
            }
        )
        return format_refresh_result(result)

    if args.command == "run-slate":
        args.markets = _run_slate_markets(args.preset, args.markets)
        async with _stake_client() as stake, _mlb_engine() as engine:
            enriched = await _enriched_payload(stake, engine, args)
        store = _snapshot_store(args)
        refresh_result = store.save_enriched_props(
            enriched,
            snapshot_phase=args.snapshot_phase,
            snapshot_label=args.snapshot_label,
        )
        analysis_limit = max(args.limit, len(enriched.get("props") or []))
        analysis = analyze_stored_props(
            store,
            date_text=args.date,
            snapshot_phase=args.snapshot_phase,
            min_edge=args.min_edge,
            limit=analysis_limit,
        )
        analysis_saved = store.save_analysis_result(
            analysis,
            snapshot_label=args.snapshot_label,
        )
        return format_slate_run(
            {
                "date": args.date,
                "preset": args.preset,
                "markets": sorted(_csv_filter(args.markets)),
                "snapshotPhase": args.snapshot_phase,
                "snapshotLabel": args.snapshot_label,
                "refresh": refresh_result,
                "analysisSaved": analysis_saved,
                "analysis": analysis,
            },
            max_rows=args.limit,
        )

    if args.command == "daily-slate":
        args.markets = _run_slate_markets(args.preset, args.markets)
        async with _stake_client() as stake, _mlb_engine() as engine:
            enriched = await _enriched_payload(stake, engine, args)

        store = _snapshot_store(args)
        refresh_result = store.save_enriched_props(
            enriched,
            snapshot_phase=args.snapshot_phase,
            snapshot_label=args.snapshot_label,
        )
        analysis_limit = max(args.limit, len(enriched.get("props") or []))
        analysis = analyze_stored_props(
            store,
            date_text=args.date,
            snapshot_phase=args.snapshot_phase,
            min_edge=args.min_edge,
            limit=analysis_limit,
        )
        analysis_saved = store.save_analysis_result(
            analysis,
            snapshot_label=args.snapshot_label,
        )
        decision_limit = max(args.pick_limit, args.limit, 100)
        decisions = store.list_latest_analysis_snapshots(
            date_text=args.date,
            snapshot_phase=args.snapshot_phase,
            limit=decision_limit,
        )
        markets = _csv_filter(_resolve_markets(args.preset, args.markets))
        pick_board = build_pick_board(
            decisions,
            markets=markets,
            allow_risk=args.allow_risk,
            profile=args.profile,
            limit=args.pick_limit,
        )
        pick_board["requested"].update(
            {
                "date": args.date,
                "snapshotPhase": args.snapshot_phase,
                "dbPath": str(store.db_path),
            }
        )
        odds_min, odds_max = _profile_odds(args.profile, args.odds_min, args.odds_max)
        parlays = build_parlay_candidates(
            decisions,
            legs=args.legs,
            odds_min=odds_min,
            odds_max=odds_max,
            count=args.count,
            mode=args.mode,
            markets=markets,
            allow_risk=args.allow_risk,
            profile=args.profile,
        )
        parlays.update(
            {
                "date": args.date,
                "snapshotPhase": args.snapshot_phase,
                "dbPath": str(store.db_path),
            }
        )
        payload = {
            "slateRun": {
                "date": args.date,
                "preset": args.preset,
                "markets": sorted(markets),
                "snapshotPhase": args.snapshot_phase,
                "snapshotLabel": args.snapshot_label,
                "refresh": refresh_result,
                "analysisSaved": analysis_saved,
                "analysis": analysis,
            },
            "pickBoard": pick_board,
            "parlays": parlays,
            "reportPath": args.report_path,
        }
        output = format_daily_slate(
            payload,
            max_picks=args.pick_limit,
            max_parlays=args.count,
        )
        _write_report(args.report_path, output)
        return output

    if args.command == "build-parlay":
        store = _snapshot_store(args)
        decisions = store.list_latest_analysis_snapshots(
            date_text=args.date,
            snapshot_phase=args.snapshot_phase,
            limit=args.limit,
        )
        odds_min, odds_max = _profile_odds(args.profile, args.odds_min, args.odds_max)
        result = build_parlay_candidates(
            decisions,
            legs=args.legs,
            odds_min=odds_min,
            odds_max=odds_max,
            count=args.count,
            mode=args.mode,
            markets=_csv_filter(_resolve_markets(args.preset, args.markets)),
            allow_risk=args.allow_risk,
            locked_prop_ids=_csv_filter(args.lock_prop_ids),
            locked_players=_csv_filter(args.lock_players),
            locked_pick_numbers=_csv_int_filter(args.lock_picks),
            profile=args.profile,
        )
        result.update(
            {
                "date": args.date,
                "snapshotPhase": args.snapshot_phase,
                "dbPath": str(store.db_path),
            }
        )
        return format_parlay_candidates(result, max_rows=args.count)

    if args.command == "pick-board":
        store = _snapshot_store(args)
        decisions = store.list_latest_analysis_snapshots(
            date_text=args.date,
            snapshot_phase=args.snapshot_phase,
            limit=max(args.limit, 100),
        )
        result = build_pick_board(
            decisions,
            markets=_csv_filter(_resolve_markets(args.preset, args.markets)),
            allow_risk=args.allow_risk,
            profile=args.profile,
            limit=args.limit,
        )
        result["requested"].update(
            {
                "date": args.date,
                "snapshotPhase": args.snapshot_phase,
                "dbPath": str(store.db_path),
            }
        )
        return format_pick_board(result, max_rows=args.limit)

    if args.command == "stored-props":
        store = _snapshot_store(args)
        props = store.list_prop_snapshots(
            date_text=args.date,
            player=args.player,
            market=args.market,
            snapshot_phase=args.snapshot_phase,
            limit=args.limit,
        )
        return format_stored_props(
            {"dbPath": str(store.db_path), "count": len(props), "props": props},
            max_rows=args.limit,
        )

    if args.command == "movement":
        store = _snapshot_store(args)
        return format_movement(store.get_prop_movement(args.prop_id, limit=args.limit))

    if args.command == "latest-props":
        store = _snapshot_store(args)
        props = store.list_latest_prop_snapshots(
            date_text=args.date,
            player=args.player,
            market=args.market,
            snapshot_phase=args.snapshot_phase,
            limit=args.limit,
        )
        return format_latest_props(
            {"dbPath": str(store.db_path), "count": len(props), "props": props},
            max_rows=args.limit,
        )

    if args.command == "analyze-stored":
        store = _snapshot_store(args)
        result = analyze_stored_props(
            store,
            date_text=args.date,
            market=args.market,
            snapshot_phase=args.snapshot_phase,
            min_edge=args.min_edge,
            limit=args.limit,
        )
        return format_analyze_stored(result, max_rows=args.limit)

    if args.command == "settle-stored":
        store = _snapshot_store(args)
        async with _mlb_engine() as engine:
            result = await settle_stored_props(
                store,
                engine,
                date_text=args.date,
                market=args.market,
                snapshot_phase=args.snapshot_phase,
                season=args.season,
                limit=args.limit,
        )
        return format_settlement(result, max_rows=args.limit)

    if args.command == "backtest-stored":
        store = _snapshot_store(args)
        async with _mlb_engine() as engine:
            result = await backtest_stored_props(
                store,
                engine,
                date_text=args.date,
                market=args.market,
                snapshot_phase=args.snapshot_phase,
                season=args.season,
                min_edge=args.min_edge,
                limit=args.limit,
            )
        return format_backtest(result, max_rows=args.limit)

    raise ValueError(f"Unknown command: {args.command}")


async def interactive_main() -> None:
    while True:
        print(render_menu())
        choice = input("\nChoose: ").strip()
        if choice == "0":
            print("Exiting.")
            return
        try:
            output = await _run_interactive_choice(choice)
        except Exception as exc:
            output = f"Error: {exc}"
        print(f"\n{output}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mini CLI for testing Stake odds + MLB data workflows.",
    )
    subparsers = parser.add_subparsers(dest="command")

    _add_slate_args(subparsers.add_parser("props", help="Show flat Stake MLB props"))
    _add_slate_args(subparsers.add_parser("enriched", help="Show props enriched with MLB data"))
    _add_slate_args(subparsers.add_parser("audit", help="Show Stake-to-MLB match audit"))
    _add_slate_args(subparsers.add_parser("lines", help="Show primary-line diagnostics"))
    refresh = subparsers.add_parser("refresh", help="Fetch enriched props and save snapshots")
    _add_slate_args(refresh)
    _add_snapshot_args(refresh)
    _add_db_arg(refresh)

    run_slate = subparsers.add_parser(
        "run-slate",
        help="Refresh, analyze, save analyzer decisions, and show top candidates",
    )
    _add_slate_args(run_slate)
    run_slate.add_argument("--preset", choices=tuple(PRESET_MARKETS), default="mlb-core")
    run_slate.add_argument("--min-edge", type=float, default=0.25)
    _add_snapshot_args(run_slate, default_phase="pregame")
    _add_db_arg(run_slate)

    daily_slate = subparsers.add_parser(
        "daily-slate",
        help="Refresh, analyze, show pick board, and build parlays in one run",
    )
    _add_slate_args(daily_slate)
    daily_slate.add_argument("--preset", choices=tuple(PRESET_MARKETS), default="mlb-core")
    daily_slate.add_argument("--profile", choices=PROFILE_CHOICES, default="safe-ish")
    daily_slate.add_argument("--min-edge", type=float, default=0.25)
    daily_slate.add_argument("--legs", type=int, default=3)
    daily_slate.add_argument("--odds-min", type=float, default=None)
    daily_slate.add_argument("--odds-max", type=float, default=None)
    daily_slate.add_argument("--count", type=int, default=5)
    daily_slate.add_argument("--mode", choices=("standard", "sgp"), default="standard")
    daily_slate.add_argument("--allow-risk", action="store_true")
    daily_slate.add_argument("--pick-limit", type=int, default=10)
    daily_slate.add_argument("--report-path", default=None)
    _add_snapshot_args(daily_slate, default_phase="pregame")
    _add_db_arg(daily_slate)

    parlay = subparsers.add_parser(
        "build-parlay",
        help="Build parlay candidates from saved analyzer decisions",
    )
    parlay.add_argument("--date", default=None)
    parlay.add_argument("--snapshot-phase", default="pregame")
    parlay.add_argument("--preset", choices=tuple(PRESET_MARKETS), default=None)
    parlay.add_argument("--profile", choices=PROFILE_CHOICES, default="custom")
    parlay.add_argument("--markets", default="")
    parlay.add_argument("--legs", type=int, default=3)
    parlay.add_argument("--odds-min", type=float, default=None)
    parlay.add_argument("--odds-max", type=float, default=None)
    parlay.add_argument("--count", type=int, default=5)
    parlay.add_argument("--mode", choices=("standard", "sgp"), default="standard")
    parlay.add_argument("--lock-players", default="")
    parlay.add_argument("--lock-prop-ids", default="")
    parlay.add_argument("--lock-picks", default="")
    parlay.add_argument("--allow-risk", action="store_true")
    parlay.add_argument("--limit", type=int, default=100)
    _add_db_arg(parlay)

    pick_board = subparsers.add_parser(
        "pick-board",
        help="Show numbered saved analyzer legs for parlay locking",
    )
    pick_board.add_argument("--date", default=None)
    pick_board.add_argument("--snapshot-phase", default="pregame")
    pick_board.add_argument("--preset", choices=tuple(PRESET_MARKETS), default=None)
    pick_board.add_argument("--profile", choices=PROFILE_CHOICES, default="custom")
    pick_board.add_argument("--markets", default="")
    pick_board.add_argument("--allow-risk", action="store_true")
    pick_board.add_argument("--limit", type=int, default=50)
    _add_db_arg(pick_board)

    stored_props = subparsers.add_parser("stored-props", help="List saved prop snapshots")
    stored_props.add_argument("--date", default=None)
    stored_props.add_argument("--player", default=None)
    stored_props.add_argument("--market", default=None)
    stored_props.add_argument("--snapshot-phase", default=None)
    stored_props.add_argument("--limit", type=int, default=20)
    _add_db_arg(stored_props)

    movement = subparsers.add_parser("movement", help="Show saved movement for one prop ID")
    movement.add_argument("prop_id")
    movement.add_argument("--limit", type=int, default=50)
    _add_db_arg(movement)

    latest = subparsers.add_parser("latest-props", help="List newest saved row per prop")
    latest.add_argument("--date", default=None)
    latest.add_argument("--player", default=None)
    latest.add_argument("--market", default=None)
    latest.add_argument("--snapshot-phase", default=None)
    latest.add_argument("--limit", type=int, default=20)
    _add_db_arg(latest)

    analyze = subparsers.add_parser("analyze-stored", help="Analyze latest saved props")
    analyze.add_argument("--date", default=None)
    analyze.add_argument("--market", default=None)
    analyze.add_argument("--snapshot-phase", default=None)
    analyze.add_argument("--min-edge", type=float, default=0.25)
    analyze.add_argument("--limit", type=int, default=20)
    _add_db_arg(analyze)

    settle = subparsers.add_parser("settle-stored", help="Grade saved props against MLB game logs")
    settle.add_argument("--date", default=None)
    settle.add_argument("--market", default=None)
    settle.add_argument("--snapshot-phase", default=None)
    settle.add_argument("--season", type=int, default=None)
    settle.add_argument("--limit", type=int, default=20)
    _add_db_arg(settle)

    backtest = subparsers.add_parser(
        "backtest-stored",
        help="Compare analyzer decisions against settled MLB results",
    )
    backtest.add_argument("--date", default=None)
    backtest.add_argument("--market", default=None)
    backtest.add_argument("--snapshot-phase", default=None)
    backtest.add_argument("--season", type=int, default=None)
    backtest.add_argument("--min-edge", type=float, default=0.25)
    backtest.add_argument("--limit", type=int, default=20)
    _add_db_arg(backtest)

    search = subparsers.add_parser("search", help="Search MLB players")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)

    profile = subparsers.add_parser("profile", help="Show MLB profile and recent history")
    profile.add_argument("player_id", type=int)
    profile.add_argument("--season", type=int, default=None)
    profile.add_argument("--group", choices=("hitting", "pitching"), default="hitting")
    profile.add_argument("--history-limit", type=int, default=5)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command:
        print(asyncio.run(run_command(args)))
        return
    asyncio.run(interactive_main())


async def _run_interactive_choice(choice: str) -> str:
    if choice == "1":
        args = _interactive_slate_args("props")
    elif choice == "2":
        args = _interactive_slate_args("enriched")
    elif choice == "3":
        args = _interactive_slate_args("audit")
    elif choice == "4":
        query = _prompt("Player search", "Aaron Judge")
        args = argparse.Namespace(command="search", query=query, limit=10)
    elif choice == "5":
        player_id = int(_prompt("MLB player ID", "592450"))
        season = _optional_int(_prompt("Season blank for current/default", "2026"))
        group = _prompt("Group hitting/pitching", "hitting")
        args = argparse.Namespace(
            command="profile",
            player_id=player_id,
            season=season,
            group=group,
            history_limit=5,
        )
    elif choice == "6":
        args = _interactive_slate_args("lines")
    elif choice == "7":
        args = _interactive_slate_args("refresh")
    elif choice == "8":
        args = argparse.Namespace(
            command="stored-props",
            date=_prompt("Date blank for all", ""),
            player=_prompt("Player contains blank for all", ""),
            market=_prompt("Market key blank for all", ""),
            snapshot_phase=_prompt("Snapshot phase blank for all", ""),
            limit=int(_prompt("Limit", "20")),
            db_path=_prompt("DB path blank for default", ""),
        )
    elif choice == "9":
        args = argparse.Namespace(
            command="movement",
            prop_id=_prompt("Prop ID", ""),
            limit=int(_prompt("Limit", "50")),
            db_path=_prompt("DB path blank for default", ""),
        )
    elif choice == "10":
        args = argparse.Namespace(
            command="latest-props",
            date=_prompt("Date blank for all", ""),
            player=_prompt("Player contains blank for all", ""),
            market=_prompt("Market key blank for all", ""),
            snapshot_phase=_prompt("Snapshot phase blank for all", ""),
            limit=int(_prompt("Limit", "20")),
            db_path=_prompt("DB path blank for default", ""),
        )
    elif choice == "11":
        args = argparse.Namespace(
            command="analyze-stored",
            date=_prompt("Date blank for all", ""),
            market=_prompt("Market key blank for all", ""),
            snapshot_phase=_prompt("Snapshot phase blank for all", ""),
            min_edge=float(_prompt("Min edge", "0.25")),
            limit=int(_prompt("Limit per bucket", "20")),
            db_path=_prompt("DB path blank for default", ""),
        )
    elif choice == "12":
        args = argparse.Namespace(
            command="settle-stored",
            date=_prompt("Date blank for all", ""),
            market=_prompt("Market key blank for all", ""),
            snapshot_phase=_prompt("Snapshot phase blank for all", ""),
            season=_optional_int(_prompt("Season blank for date year", "")),
            limit=int(_prompt("Limit", "20")),
            db_path=_prompt("DB path blank for default", ""),
        )
    elif choice == "13":
        args = argparse.Namespace(
            command="backtest-stored",
            date=_prompt("Date blank for all", ""),
            market=_prompt("Market key blank for all", ""),
            snapshot_phase=_prompt("Snapshot phase blank for all", ""),
            season=_optional_int(_prompt("Season blank for date year", "")),
            min_edge=float(_prompt("Min edge", "0.25")),
            limit=int(_prompt("Limit", "20")),
            db_path=_prompt("DB path blank for default", ""),
        )
    elif choice == "14":
        args = _interactive_slate_args("run-slate")
        args.preset = _prompt("Preset", "mlb-core")
        args.min_edge = float(_prompt("Min edge", "0.25"))
        if args.snapshot_phase == "manual":
            args.snapshot_phase = "pregame"
    elif choice == "15":
        date_text = _prompt("Date blank for all", "")
        snapshot_phase = _prompt("Snapshot phase", "pregame")
        preset = _prompt("Market preset blank for none", "")
        profile = _prompt("Profile custom/safe-ish/longshot", "custom")
        markets = _prompt("Markets comma-list blank for all", "")
        allow_risk = _prompt("Allow risk flags y/N", "N").strip().lower() == "y"
        limit = int(_prompt("Saved decisions to scan", "100"))
        db_path = _prompt("DB path blank for default", "")
        store = SnapshotStore(db_path or None)
        decisions = store.list_latest_analysis_snapshots(
            date_text=date_text,
            snapshot_phase=snapshot_phase,
            limit=limit,
        )
        board = build_pick_board(
            decisions,
            markets=_csv_filter(_resolve_markets(preset, markets)),
            allow_risk=allow_risk,
            profile=profile,
            limit=limit,
        )
        board["requested"].update(
            {
                "date": date_text,
                "snapshotPhase": snapshot_phase,
                "dbPath": str(store.db_path),
            }
        )
        print(f"\n{format_pick_board(board, max_rows=limit)}\n")
        args = argparse.Namespace(
            command="build-parlay",
            date=date_text,
            snapshot_phase=snapshot_phase,
            preset=preset or None,
            profile=profile,
            markets=markets,
            legs=int(_prompt("Legs", "3")),
            odds_min=float(_prompt("Minimum total odds", "3.0")),
            odds_max=float(_prompt("Maximum total odds", "8.0")),
            count=int(_prompt("Candidates to show", "5")),
            mode=_prompt("Mode standard/sgp", "standard"),
            lock_picks=_prompt("Lock pick numbers comma-list blank for none", ""),
            lock_players=_prompt("Lock players comma-list blank for none", ""),
            lock_prop_ids=_prompt("Lock prop IDs comma-list blank for none", ""),
            allow_risk=allow_risk,
            limit=limit,
            db_path=db_path,
        )
    elif choice == "16":
        args = argparse.Namespace(
            command="pick-board",
            date=_prompt("Date blank for all", ""),
            snapshot_phase=_prompt("Snapshot phase", "pregame"),
            preset=_prompt("Market preset blank for none", "") or None,
            profile=_prompt("Profile custom/safe-ish/longshot", "custom"),
            markets=_prompt("Markets comma-list blank for all", ""),
            allow_risk=_prompt("Allow risk flags y/N", "N").strip().lower() == "y",
            limit=int(_prompt("Saved decisions to scan", "50")),
            db_path=_prompt("DB path blank for default", ""),
        )
    elif choice == "17":
        args = _interactive_slate_args("daily-slate")
        args.preset = _prompt("Preset", "mlb-core")
        args.profile = _prompt("Profile custom/safe-ish/longshot", "safe-ish")
        args.min_edge = float(_prompt("Min edge", "0.25"))
        args.legs = int(_prompt("Parlay legs", "3"))
        args.odds_min = _optional_float(_prompt("Minimum total odds blank for profile default", ""))
        args.odds_max = _optional_float(_prompt("Maximum total odds blank for profile default", ""))
        args.count = int(_prompt("Parlay candidates to show", "5"))
        args.mode = _prompt("Mode standard/sgp", "standard")
        args.allow_risk = _prompt("Allow risk flags y/N", "N").strip().lower() == "y"
        args.pick_limit = int(_prompt("Pick board rows", "10"))
        args.report_path = _prompt("Report path blank for none", "") or None
        if args.snapshot_phase == "manual":
            args.snapshot_phase = "pregame"
    else:
        return "Unknown menu option."
    return await run_command(args)


def _interactive_slate_args(command: str) -> argparse.Namespace:
    return argparse.Namespace(
        command=command,
        date=_prompt("Date", date.today().isoformat()),
        limit=int(_prompt("Limit", "5")),
        markets=_prompt("Markets comma-list blank for all", ""),
        exclude_markets=_prompt("Exclude markets comma-list blank for none", ""),
        season=_optional_int(_prompt("Season blank for current/default", str(date.today().year))),
        group="auto",
        history_limit=int(_prompt("History limit", "5")),
        snapshot_phase=_prompt("Snapshot phase", "manual"),
        snapshot_label=_prompt("Snapshot label blank for none", ""),
        db_path=_prompt("DB path blank for default", ""),
    )


def _add_slate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--markets", default="")
    parser.add_argument("--exclude-markets", default="")
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--group", choices=("auto", "hitting", "pitching"), default="auto")
    parser.add_argument("--history-limit", type=int, default=5)


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db-path", default=None)


def _add_snapshot_args(
    parser: argparse.ArgumentParser,
    default_phase: str = "manual",
) -> None:
    parser.add_argument(
        "--snapshot-phase",
        default=default_phase,
        help="Snapshot phase, for example pregame, live, postgame, final, or manual.",
    )
    parser.add_argument("--snapshot-label", default=None)


async def _enriched_payload(
    stake: StakeClient,
    engine: MLBDataEngine,
    args: argparse.Namespace,
) -> dict[str, Any]:
    props = await _props_payload(
        stake,
        args.date,
        args.limit,
        args.markets,
        args.exclude_markets,
    )
    return await enrich_props_with_mlb_data(
        props,
        engine,
        season=args.season,
        group_mode=args.group,
        history_limit=args.history_limit,
    )


async def _props_payload(
    stake: StakeClient,
    slate_date: str,
    limit: int,
    markets: str,
    exclude_markets: str,
) -> dict[str, Any]:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    slate = await build_mlb_player_props_slate(
        stake,
        _parse_date(slate_date),
        timezone_name,
        limit,
        "primary",
        _csv_filter(markets),
        _csv_filter(exclude_markets),
    )
    return build_stable_props_payload(slate)


@asynccontextmanager
async def _stake_client():
    api_key = os.getenv("STAKE_API_KEY") or None
    async with build_http_client() as http_client:
        yield StakeClient(http_client=http_client, api_key=api_key)


@asynccontextmanager
async def _mlb_engine():
    async with build_mlb_http_client() as http_client:
        yield MLBDataEngine(MLBStatsClient(http_client))


def _csv_filter(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


def _csv_int_filter(value: str | None) -> set[int]:
    if not value:
        return set()
    picks = set()
    for part in value.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        picks.add(int(cleaned))
    return picks


def _resolve_markets(preset: str | None, markets: str | None) -> str:
    if markets:
        return markets
    if not preset:
        return ""
    preset_markets = PRESET_MARKETS.get(preset)
    if not preset_markets:
        raise ValueError(f"Unknown preset: {preset}")
    return ",".join(preset_markets)


def _profile_odds(
    profile: str,
    odds_min: float | None,
    odds_max: float | None,
) -> tuple[float | None, float | None]:
    settings = PROFILE_PRESETS.get(profile, PROFILE_PRESETS["custom"])
    return (
        odds_min if odds_min is not None else settings["defaultOddsMin"],
        odds_max if odds_max is not None else settings["defaultOddsMax"],
    )


def _run_slate_markets(preset: str, markets: str | None) -> str:
    if markets:
        return markets
    preset_markets = PRESET_MARKETS.get(preset)
    if not preset_markets:
        raise ValueError(f"Unknown preset: {preset}")
    return ",".join(preset_markets)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _prompt(label: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _optional_int(value: str) -> int | None:
    return int(value) if value.strip() else None


def _optional_float(value: str) -> float | None:
    return float(value) if value.strip() else None


def _snapshot_store(args: argparse.Namespace) -> SnapshotStore:
    db_path = getattr(args, "db_path", None) or None
    return SnapshotStore(db_path)


def _write_report(report_path: str | None, output: str) -> None:
    if not report_path:
        return
    path = Path(report_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
