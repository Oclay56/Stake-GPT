from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from .decision_profiles import evidence_check, evidence_windows, season_evidence, trend_labels
from .mlb_bridge import enrich_props_with_mlb_data, stat_mapping_for_market
from .mlb_props import slug_key
from .stake_sgm_browser import (
    SGM_BOARD_FRESH_SECONDS,
    make_sgm_selection_row_id,
    sgm_board_freshness,
    sgm_market_filter_matches,
)


DEFAULT_MAX_LEGS_PER_GAME_GROUP = 16
DEFAULT_MAX_SGM_GROUP_ODDS = 501.0
NORMAL_SLATE_GAME_CAP = 15
HARD_SLATE_GAME_CAP = 20
DEFAULT_MAX_CANDIDATES_PER_GAME = 8
DEFAULT_MAX_TOTAL_CANDIDATES = 75
COMPACT_REASON_TAG_LIMIT = 3
MARKET_CONTEST_ALTERNATIVE_LIMIT = 3
MARKET_CONTEST_REJECTED_LIMIT = 12
GAME_CONTEST_MIN_LEGS = 2
BLOCKING_RISK_FLAGS = {
    "lineup_not_starting",
    "game_postponed",
    "game_suspended",
    "game_cancelled",
    "stake_board_stale_refetch_required",
    "stake_bet_factor_zero_or_negative",
}
SUPPORTED_MODES = {
    "best_available",
    "safe",
    "balanced",
    "longshot",
    "per_game",
}


def compact_sgm_candidate_pool_response(pool: dict[str, Any]) -> dict[str, Any]:
    compact = dict(pool)
    compact["compact"] = True
    compact["rankedCandidates"] = [
        _compact_candidate_pool_row(row)
        for row in pool.get("rankedCandidates") or []
        if isinstance(row, dict)
    ]
    notes = list(compact.get("notes") or [])
    notes.append(
        "Compact mode returns lean candidate rows only; call with compact=false or fetch the SGM board for full per-row context."
    )
    compact["notes"] = notes
    return compact


def _compact_candidate_pool_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "fixtureSlug": row.get("fixtureSlug"),
        "matchup": row.get("matchup"),
        "rowId": row.get("rowId"),
        "player": row.get("player"),
        "team": row.get("team"),
        "market": row.get("market"),
        "side": row.get("side"),
        "line": row.get("line"),
        "odds": row.get("odds"),
        "contextQuality": row.get("contextQuality"),
        "score": row.get("score"),
        "marketContestRank": row.get("marketContestRank"),
        "gameContestRank": row.get("gameContestRank"),
        "selectedMarket": (row.get("selectionProof") or {}).get("selectedMarket"),
        "selectedScore": (row.get("selectionProof") or {}).get("selectedScore"),
        "marketsCompared": (row.get("selectionProof") or {}).get("marketsCompared"),
        "closestAlternativeMarket": (row.get("selectionProof") or {}).get("closestAlternativeMarket"),
        "closestAlternativeScore": (row.get("selectionProof") or {}).get("closestAlternativeScore"),
        "whySelectedBeatAlternative": (row.get("selectionProof") or {}).get("whySelectedBeatAlternative"),
        "availabilityRole": (row.get("selectionProof") or {}).get("availabilityRole")
        or row.get("availabilityRole"),
        "edgeStatus": (row.get("probabilityAssessment") or {}).get("edgeStatus"),
        "impliedProbability": (row.get("probabilityAssessment") or {}).get("impliedProbability"),
        "estimatedProbability": (row.get("probabilityAssessment") or {}).get("estimatedProbability"),
        "adjustedEstimatedProbability": (row.get("probabilityAssessment") or {}).get(
            "adjustedEstimatedProbability"
        ),
        "matchupFactor": ((row.get("probabilityAssessment") or {}).get("inputs") or {}).get(
            "matchupFactor"
        ),
        "reasonTags": list(row.get("reasonTags") or [])[:COMPACT_REASON_TAG_LIMIT],
        "riskFlags": list(row.get("riskFlags") or []),
    }


def normalize_sgm_market_key(market: Any, *, scope: str | None = None, position: str | None = None) -> str:
    key = slug_key(market)
    aliases = {
        "single": "singles",
        "player-singles": "singles",
        "one-base-hits": "singles",
        "one-baggers": "singles",
        "stolen-base": "stolen_bases",
        "stolen-bases": "stolen_bases",
        "player-stolen-bases": "stolen_bases",
        "steal": "stolen_bases",
        "steals": "stolen_bases",
        "sb": "stolen_bases",
        "bases-stolen": "stolen_bases",
        "bb": "batter_walks",
        "base-on-balls": "batter_walks",
        "bases-on-balls": "batter_walks",
        "walks-drawn": "batter_walks",
        "batter-walk": "batter_walks",
        "batter-walks": "batter_walks",
        "hitter-walks": "batter_walks",
        "batter-k": "batter_strikeouts",
        "batter-ks": "batter_strikeouts",
        "batter-strikeout": "batter_strikeouts",
        "batter-strikeouts": "batter_strikeouts",
        "hitter-strikeouts": "batter_strikeouts",
        "failed-attempts": "batter_strikeouts",
        "hits-runs-rbi": "hits_runs_rbis",
        "hits-runs-rbis": "hits_runs_rbis",
        "hit-runs-rbis": "hits_runs_rbis",
        "hits-runs-rbis-hrr": "hits_runs_rbis",
        "h-r-r": "hits_runs_rbis",
        "hrr": "hits_runs_rbis",
    }
    if key in aliases:
        return aliases[key]
    if key == "walks" and slug_key(scope) == "player" and slug_key(position) != "p":
        return "batter_walks"
    if key == "strikeouts" and slug_key(position) != "p":
        return "batter_strikeouts"
    return key.replace("-", "_")


def score_sgm_candidate(
    candidate: dict[str, Any],
    *,
    mode: str = "best_available",
    market_exposure_count: int = 0,
    max_market_repeats: int | None = None,
) -> dict[str, Any]:
    clean_mode = _clean_mode(mode)
    odds = _float_or_none(candidate.get("odds"))
    context = candidate.get("context") or {}
    market_key = str(candidate.get("normalizedMarketKey") or "").replace("-", "_")
    evidence_score = _evidence_score(context, str(candidate.get("side") or "under"))
    value_score = _value_score(odds, clean_mode)
    mode_fit_score = _mode_fit_score(evidence_score, odds, clean_mode)

    odds_trap_penalty = 0.0
    quota_filler_penalty = 0.0
    volatility_penalty = _volatility_penalty(market_key, clean_mode)
    stake_metadata_penalty = 0.0
    correlation_penalty = _float_or_none(candidate.get("correlationPenalty")) or 0.0
    risk_flags: list[str] = []
    reason_tags: list[str] = []

    if odds is not None and odds < 1.15:
        quota_filler_penalty = 18.0
        risk_flags.append("short_odds_quota_filler")
    if odds is not None and odds >= 4.0 and evidence_score < 55:
        odds_trap_penalty = 26.0 if clean_mode == "longshot" else 34.0
        risk_flags.append("high_odds_no_stat_support")
    if candidate.get("balanced") is False:
        stake_metadata_penalty += 4.0
        risk_flags.append("stake_line_unbalanced")
    bet_factor = _float_or_none(candidate.get("betFactor"))
    if bet_factor is not None and bet_factor <= 0:
        stake_metadata_penalty += 8.0
        risk_flags.append("stake_bet_factor_zero_or_negative")
    if candidate.get("push") not in (None, False):
        reason_tags.append("push_available")
    lineup_status = str((candidate.get("lineupContext") or {}).get("status") or "")
    if lineup_status == "not_in_confirmed_lineup":
        stake_metadata_penalty += 100.0
        risk_flags.append("lineup_not_starting")
    elif lineup_status == "lineup_unconfirmed":
        stake_metadata_penalty += 6.0
        risk_flags.append("lineup_unconfirmed")
    for flag in (candidate.get("gameContext") or {}).get("statusRiskFlags") or []:
        risk_flags.append(str(flag))
        if flag in {"game_postponed", "game_suspended", "game_cancelled"}:
            stake_metadata_penalty += 100.0
        elif flag in {"game_delay_risk", "start_time_tbd"}:
            stake_metadata_penalty += 8.0
    board_freshness = candidate.get("boardFreshness") or {}
    if board_freshness.get("refetchRequired"):
        stake_metadata_penalty += 100.0
        risk_flags.append("stake_board_stale_refetch_required")
    elif board_freshness.get("status") == "unknown":
        reason_tags.append("stake_board_freshness_unknown")

    if evidence_score >= 70:
        reason_tags.append("broader_context_supports_side")
    elif evidence_score >= 55:
        reason_tags.append("usable_context_support")
    else:
        reason_tags.append("weak_or_conflicting_context")

    if clean_mode == "longshot" and odds is not None and odds >= 2.75 and evidence_score >= 55:
        reason_tags.append("longshot_with_context_support")
    if clean_mode == "safe" and evidence_score < 70:
        risk_flags.append("safe_mode_evidence_below_strict_floor")

    probability_assessment = _probability_assessment(candidate, risk_flags)
    probability_adjustment = _probability_score_adjustment(probability_assessment)
    risk_flags.extend(probability_assessment.get("riskFlags") or [])
    reason_tags.extend(probability_assessment.get("reasonTags") or [])

    score = (
        evidence_score * 0.48
        + value_score * 0.22
        + mode_fit_score * 0.20
        + 10.0
        + probability_adjustment
        - odds_trap_penalty
        - quota_filler_penalty
        - volatility_penalty
        - stake_metadata_penalty
        - correlation_penalty
    )
    return {
        "availabilityRole": "eligibility_only",
        "dataDepthRole": "confidence_cap_not_direct_merit_bonus",
        "evidenceScore": round(evidence_score, 2),
        "valueScore": round(value_score, 2),
        "modeFitScore": round(mode_fit_score, 2),
        "oddsTrapPenalty": round(odds_trap_penalty, 2),
        "quotaFillerPenalty": round(quota_filler_penalty, 2),
        "volatilityPenalty": round(volatility_penalty, 2),
        "stakeMetadataPenalty": round(stake_metadata_penalty, 2),
        "correlationPenalty": round(correlation_penalty, 2),
        "probabilityScoreAdjustment": round(probability_adjustment, 2),
        "probabilityAssessment": probability_assessment,
        "marketExposurePenalty": 0.0,
        "score": round(max(0.0, min(100.0, score)), 2),
        "reasonTags": sorted(set(reason_tags)),
        "riskFlags": sorted(set(risk_flags)),
    }


async def build_sgm_candidate_pool_from_boards(
    boards: list[dict[str, Any]],
    engine: Any,
    *,
    date: str | None = None,
    matchups: list[str] | None = None,
    markets: Any = None,
    side: str = "any",
    mode: str = "best_available",
    legs_per_game: int | None = None,
    max_total_legs: int | None = None,
    max_candidates_per_game: int = DEFAULT_MAX_CANDIDATES_PER_GAME,
    max_total_candidates: int = DEFAULT_MAX_TOTAL_CANDIDATES,
    quality_floor: float | None = None,
    history_limit: int = 15,
    target_odds_min: float | None = None,
    target_odds_max: float | None = None,
    min_individual_odds: float | None = None,
    max_individual_odds: float | None = None,
    max_legs_per_game_group: int = DEFAULT_MAX_LEGS_PER_GAME_GROUP,
    max_sgm_group_odds: float = DEFAULT_MAX_SGM_GROUP_ODDS,
    max_games: int = NORMAL_SLATE_GAME_CAP,
    max_board_age_seconds: int = SGM_BOARD_FRESH_SECONDS,
) -> dict[str, Any]:
    clean_mode = _clean_mode(mode)
    clean_side = _clean_side(side)
    wanted_markets = _market_filter_set(markets)
    max_games = max(1, min(int(max_games or NORMAL_SLATE_GAME_CAP), HARD_SLATE_GAME_CAP))
    max_candidates_per_game = max(
        GAME_CONTEST_MIN_LEGS,
        min(int(max_candidates_per_game), DEFAULT_MAX_LEGS_PER_GAME_GROUP),
    )
    max_total_candidates = max(1, min(int(max_total_candidates), 300))
    max_legs_per_game_group = max(1, min(int(max_legs_per_game_group), DEFAULT_MAX_LEGS_PER_GAME_GROUP))
    max_sgm_group_odds = min(float(max_sgm_group_odds or DEFAULT_MAX_SGM_GROUP_ODDS), DEFAULT_MAX_SGM_GROUP_ODDS)
    clean_quality_floor = (
        _mode_quality_floor(clean_mode) if quality_floor is None else float(quality_floor)
    )

    flat_rows, initial_rejections = _flatten_board_rows(
        boards[:max_games],
        side=clean_side,
        market_filter=wanted_markets,
        min_odds=min_individual_odds or target_odds_min,
        max_odds=max_individual_odds or target_odds_max,
        max_board_age_seconds=max_board_age_seconds,
    )
    prop_payload = _props_payload_from_rows(flat_rows, date)
    enriched = await enrich_props_with_mlb_data(
        prop_payload,
        engine,
        season=_season_from_date(date),
        group_mode="auto",
        history_limit=max(1, min(int(history_limit or 15), 15)),
        search_limit=5,
    )

    enriched_by_id = {str(prop.get("propId") or ""): prop for prop in enriched.get("props") or []}
    scored_rows = []
    rejected = Counter(initial_rejections)

    for row in flat_rows:
        enriched_prop = enriched_by_id.get(str(row.get("propId") or "")) or {}
        candidate = _candidate_from_enriched_row(row, enriched_prop)
        score = score_sgm_candidate(
            candidate,
            mode=clean_mode,
        )
        candidate.update(score)
        if candidate["score"] < clean_quality_floor:
            candidate["rejectionReason"] = "score_below_quality_floor"
            rejected["score_below_quality_floor"] += 1
        elif not candidate.get("rowId"):
            candidate["rejectionReason"] = "missing_row_id"
            rejected["missing_row_id"] += 1
        elif candidate["identityResolution"] == "unmatched":
            candidate["rejectionReason"] = "mlb_identity_unmatched"
            rejected["mlb_identity_unmatched"] += 1
        else:
            scored_rows.append(candidate)

    market_contest = _apply_within_player_market_contest(scored_rows)
    game_contest = _apply_game_level_contest(
        scored_rows,
        legs_per_game=legs_per_game,
        max_candidates_per_game=max_candidates_per_game,
    )
    ranked = _select_ranked_candidates(
        scored_rows,
        mode=clean_mode,
        legs_per_game=legs_per_game,
        max_total_legs=max_total_legs,
        max_candidates_per_game=max_candidates_per_game,
        max_total_candidates=max_total_candidates,
    )
    per_game = _per_game_summary(flat_rows, ranked, scored_rows, rejected)
    market_concentration = _market_concentration_diagnostics(ranked)
    return {
        "source": "stake_ui_sgm_candidate_pool",
        "decisionOwner": "custom_gpt",
        "builderRole": "candidate_support_not_final_recommendation",
        "mode": clean_mode,
        "date": date,
        "slateSummary": {
            "normalSlateGameExpectation": NORMAL_SLATE_GAME_CAP,
            "hardGameCap": HARD_SLATE_GAME_CAP,
            "requestedGames": len(boards),
            "processedGames": min(len(boards), max_games),
            "boardSuccessCount": sum(1 for board in boards if board.get("source")),
        },
        "filters": {
            "matchups": matchups or [],
            "markets": sorted(wanted_markets),
            "side": clean_side,
            "qualityFloor": clean_quality_floor,
            "targetOddsMin": target_odds_min,
            "targetOddsMax": target_odds_max,
            "minIndividualOdds": min_individual_odds,
            "maxIndividualOdds": max_individual_odds,
            "historyLimit": max(1, min(int(history_limit or 15), 15)),
        },
        "guardrails": {
            "maxLegsPerGameGroup": max_legs_per_game_group,
            "maxSgmGroupOdds": max_sgm_group_odds,
            "maxGames": max_games,
            "noStaleRowIds": True,
            "noUnsupportedMarketPretendingResearched": True,
            "noForcedWeakPicks": True,
            "withinPlayerMarketContest": True,
            "playerMarketWinnerFirst": True,
            "gameLevelContest": True,
            "gameContestMinLegs": GAME_CONTEST_MIN_LEGS,
            "marketConcentrationDiagnosticOnly": True,
            "maxBoardAgeSeconds": max_board_age_seconds,
            "refetchOnStaleBoard": True,
            "refetchAfterLineupOrPitcherChange": True,
        },
        "candidateCounts": {
            "scannedRows": len(flat_rows),
            "acceptedRows": len(scored_rows),
            "returnedRows": len(ranked),
            "rejectedRows": sum(rejected.values()),
            "marketContestGroups": market_contest["playerGroups"],
            "contestedPlayerGroups": market_contest["contestedPlayerGroups"],
        },
        "rankedCandidates": ranked,
        "perGame": per_game,
        "rejectedSummary": dict(sorted(rejected.items())),
        "marketExposure": dict(Counter(row["normalizedMarketKey"] for row in ranked)),
        "marketConcentration": market_concentration,
        "marketContest": market_contest,
        "gameContest": game_contest,
        "contextCoverage": _context_coverage(ranked),
        "notes": [
            "Candidate pool is support data only; the Custom GPT owns final selections.",
            "This endpoint never clicks Stake UI selections or builds a review slip.",
            "Within-player market contest ranks each player's available markets first; rank-1 rows are winner-first in the slate ranking.",
            "Market concentration is diagnostic only and does not replace higher-merit rows.",
        ],
    }


def _flatten_board_rows(
    boards: list[dict[str, Any]],
    *,
    side: str,
    market_filter: set[str],
    min_odds: float | None,
    max_odds: float | None,
    max_board_age_seconds: int,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    wanted_sides = ("over", "under") if side == "any" else (side,)
    for board in boards:
        fixture_slug = str(board.get("fixtureSlug") or "")
        board_freshness = board.get("boardFreshness") or sgm_board_freshness(
            board.get("capturedAt"),
            max_age_seconds=max_board_age_seconds,
        )
        source_rows = list(board.get("playerProps") or []) + list(board.get("teamMarkets") or [])
        for source_row in source_rows:
            if not source_row.get("playable"):
                rejected["not_playable"] += 1
                for reason in source_row.get("nonPlayableReasons") or []:
                    rejected[f"not_playable:{reason}"] += 1
                continue
            normalized = normalize_sgm_market_key(
                source_row.get("market"),
                scope=str(source_row.get("scope") or ""),
                position=str(source_row.get("position") or ""),
            )
            stat_key = normalized.replace("_", "-")
            if market_filter and stat_key not in market_filter and normalized not in market_filter:
                continue
            for row_side in wanted_sides:
                odds = _float_or_none(source_row.get(row_side))
                if odds is None:
                    continue
                if min_odds is not None and odds < float(min_odds):
                    rejected["below_min_odds"] += 1
                    continue
                if max_odds is not None and odds > float(max_odds):
                    rejected["above_max_odds"] += 1
                    continue
                row = dict(source_row)
                row.update(
                    {
                        "fixtureSlug": fixture_slug,
                        "normalizedMarketKey": normalized,
                        "side": row_side,
                        "odds": odds,
                        "balanced": source_row.get("balanced"),
                        "push": source_row.get("push"),
                        "betFactor": source_row.get("betFactor"),
                        "rowId": make_sgm_selection_row_id(fixture_slug, source_row, row_side),
                        "selectionId": f"{source_row.get('lineId') or ''}:{row_side}",
                        "propId": f"{fixture_slug}:{source_row.get('lineId') or source_row.get('marketId')}:{row_side}",
                        "boardCapturedAt": board.get("capturedAt"),
                        "boardFreshness": board_freshness,
                    }
                )
                rows.append(row)
    return rows, rejected


def _props_payload_from_rows(rows: list[dict[str, Any]], date: str | None) -> dict[str, Any]:
    return {
        "league": "MLB",
        "date": date,
        "timezone": "America/New_York",
        "filters": {"markets": sorted({row["normalizedMarketKey"] for row in rows})},
        "fixtureCount": len({row.get("fixtureSlug") for row in rows}),
        "propCount": len(rows),
        "props": [_prop_from_row(row) for row in rows],
    }


def _prop_from_row(row: dict[str, Any]) -> dict[str, Any]:
    market_key = str(row.get("normalizedMarketKey") or "").replace("_", "-")
    return {
        "propId": row.get("propId"),
        "fixtureSlug": row.get("fixtureSlug"),
        "game": row.get("matchup") or row.get("fixtureSlug"),
        "startTime": row.get("startTime"),
        "player": {
            "name": row.get("player"),
            "key": slug_key(row.get("player")),
            "mlbId": None,
            "matchStatus": "needs_mlb_lookup",
        },
        "team": {
            "name": row.get("team"),
            "key": slug_key(row.get("team")),
            "mlbId": None,
            "matchStatus": "fixture_team_match",
        },
        "market": {"name": row.get("market"), "key": market_key},
        "line": row.get("line"),
        "odds": {row.get("side"): row.get("odds")},
        "stakeMetadata": {
            "balanced": row.get("balanced"),
            "push": row.get("push"),
            "betFactor": row.get("betFactor"),
        },
    }


def _candidate_from_enriched_row(row: dict[str, Any], enriched_prop: dict[str, Any]) -> dict[str, Any]:
    side = str(row.get("side") or "under")
    stat_context = enriched_prop.get("statContext") or stat_mapping_for_market(
        str(row.get("normalizedMarketKey") or "").replace("_", "-")
    )
    stat_ref: Any = stat_context if stat_context.get("statFormula") else stat_context.get("statKey")
    recent = enriched_prop.get("recentHistory") or {}
    profile = enriched_prop.get("mlbProfile") or {}
    line = _float_or_none(row.get("line"))
    windows = evidence_windows(recent, stat_ref, line, side)
    season = season_evidence(profile, stat_ref, line, side)
    season_sample = _season_sample_summary(profile, stat_context)
    guard = evidence_check(windows, season, side)
    labels = trend_labels(windows, season, side)
    mlb_match = enriched_prop.get("mlbMatch") or {}
    matched_player = mlb_match.get("matchedPlayer") if isinstance(mlb_match, dict) else None
    matched_player = matched_player if isinstance(matched_player, dict) else {}
    matched_team = matched_player.get("team") if isinstance(matched_player.get("team"), dict) else {}
    context = {
        "last5": windows.get("5") or {},
        "last10": windows.get("10") or {},
        "last15": windows.get("15") or {},
        "season": season,
        "evidenceCheck": guard,
        "trendLabels": labels,
    }
    identity_status = str(mlb_match.get("status") or "unmatched")
    mapping_warnings = []
    if not stat_context.get("supported"):
        mapping_warnings.append("market_context_unsupported")
    if identity_status == "unmatched":
        mapping_warnings.append("mlb_player_unmatched")

    return {
        "fixtureSlug": row.get("fixtureSlug"),
        "matchup": row.get("matchup"),
        "rowId": row.get("rowId"),
        "selectionId": row.get("selectionId"),
        "propId": row.get("lineId"),
        "player": row.get("player"),
        "team": row.get("team"),
        "position": row.get("position"),
        "scope": row.get("scope"),
        "market": row.get("market"),
        "normalizedMarketKey": row.get("normalizedMarketKey"),
        "side": side,
        "line": row.get("line"),
        "odds": row.get("odds"),
        "balanced": row.get("balanced"),
        "push": row.get("push"),
        "betFactor": row.get("betFactor"),
        "swishStatId": row.get("swishStatId"),
        "marketId": row.get("marketId"),
        "lineId": row.get("lineId"),
        "stakePlayerId": row.get("playerId"),
        "mlbPersonId": matched_player.get("mlbId"),
        "mlbTeamId": matched_team.get("mlbId"),
        "mlbStatKey": stat_context.get("statKey") or stat_context.get("statFormula"),
        "statContext": stat_context,
        "identityResolution": identity_status,
        "contextQuality": stat_context.get("contextQuality") or "unsupported",
        "mappingWarnings": mapping_warnings,
        "last5": context["last5"],
        "last10": context["last10"],
        "last15": context["last15"],
        "season": season,
        "seasonSample": season_sample,
        "context": context,
        "gameContext": enriched_prop.get("gameContext"),
        "lineupContext": enriched_prop.get("lineupContext"),
        "opponentPitcherContext": enriched_prop.get("opponentPitcherContext"),
        "opponentTeamContext": enriched_prop.get("opponentTeamContext"),
        "playerSplits": enriched_prop.get("playerSplits"),
        "boardCapturedAt": row.get("boardCapturedAt"),
        "boardFreshness": row.get("boardFreshness"),
        "capturedAt": datetime.now(timezone.utc).isoformat(),
    }


def _season_sample_summary(profile: dict[str, Any], stat_context: dict[str, Any]) -> dict[str, Any]:
    stats = (((profile or {}).get("player") or {}).get("stats") or {})
    group = str((stat_context or {}).get("group") or "hitting")
    games = (
        _float_or_none(stats.get("gamesPlayed"))
        or _float_or_none(stats.get("gamesPitched"))
        or _float_or_none(stats.get("gamesStarted"))
    )
    plate_appearances = _float_or_none(stats.get("plateAppearances"))
    innings_pitched = _innings_float(stats.get("inningsPitched"))
    if group == "pitching":
        sample_value = innings_pitched
        sample_metric = "inningsPitched"
        status = "robust" if sample_value is not None and sample_value >= 20 else "low_confidence"
    else:
        sample_value = plate_appearances
        sample_metric = "plateAppearances"
        if sample_value is None:
            sample_value = games
            sample_metric = "gamesPlayed"
            status = "robust" if sample_value is not None and sample_value >= 20 else "unknown"
        else:
            status = "robust" if sample_value >= 50 else "low_confidence"
    return {
        "group": group,
        "games": games,
        "plateAppearances": plate_appearances,
        "inningsPitched": innings_pitched,
        "sampleMetric": sample_metric,
        "sampleValue": sample_value,
        "status": status,
    }


def _probability_assessment(
    candidate: dict[str, Any],
    scoring_risk_flags: list[str],
) -> dict[str, Any]:
    side = str(candidate.get("side") or "under").lower()
    odds = _float_or_none(candidate.get("odds"))
    implied = round(1 / odds, 4) if odds and odds > 0 else None
    context = candidate.get("context") or {}
    season = context.get("season") or candidate.get("season") or {}
    season_rate, season_source = _season_probability_rate(season, candidate.get("line"), side)
    recent_rate, recent_source, recent_games = _recent_probability_rate(context, side)
    matchup = _matchup_factor(candidate, side)
    penalties = _probability_penalties(candidate, scoring_risk_flags, matchup)
    reliability_flags = _probability_reliability_flags(
        candidate,
        season_rate=season_rate,
        season_source=season_source,
        recent_rate=recent_rate,
        recent_source=recent_source,
        recent_games=recent_games,
        matchup=matchup,
        implied=implied,
    )
    data_quality = _probability_data_quality(
        candidate,
        reliability_flags=reliability_flags,
        scoring_risk_flags=scoring_risk_flags,
    )

    estimated = None
    adjusted = None
    edge = None
    if season_rate is not None and recent_rate is not None and implied is not None:
        estimated = round(
            season_rate * 0.50
            + recent_rate * 0.30
            + (_float_or_none(matchup.get("factor")) or 0.50) * 0.20,
            4,
        )
        adjusted = round(
            max(0.0, min(1.0, estimated - sum(item["amount"] for item in penalties))),
            4,
        )
        edge = round(adjusted - implied, 4)

    edge_status = _edge_status(edge, data_quality)
    reason_tags = []
    if edge_status in {"clear_possible_edge", "thin_edge"}:
        reason_tags.append(f"probability_{edge_status}")
    elif edge_status == "negative_edge":
        reason_tags.append("probability_negative_edge")

    return {
        "formula": "estimatedProbability = (seasonRate * 0.50) + (last15Rate * 0.30) + (matchupFactor * 0.20)",
        "impliedProbability": implied,
        "estimatedProbability": estimated,
        "adjustedEstimatedProbability": adjusted,
        "edge": edge,
        "edgeStatus": edge_status,
        "dataQuality": data_quality,
        "inputs": {
            "seasonRate": season_rate,
            "seasonRateSource": season_source,
            "last15Rate": recent_rate,
            "last15RateSource": recent_source,
            "recentGamesUsed": recent_games,
            "matchupFactor": matchup.get("factor"),
            "matchupFactorSource": matchup.get("source"),
        },
        "penalties": penalties,
        "matchupFactor": matchup,
        "reliabilityFlags": reliability_flags,
        "riskFlags": [],
        "reasonTags": reason_tags,
    }


def _season_probability_rate(
    season: dict[str, Any],
    line: Any,
    side: str,
) -> tuple[float | None, str | None]:
    margin = _float_or_none(season.get("sideMargin"))
    if margin is None:
        return None, None
    numeric_line = _float_or_none(line)
    denominator = max(1.0, abs(numeric_line or 0.0) + 1.0)
    proxy = 0.50 + max(-0.30, min(0.30, margin / denominator * 0.35))
    return round(max(0.05, min(0.95, proxy)), 4), "season_average_proxy"


def _recent_probability_rate(
    context: dict[str, Any],
    side: str,
) -> tuple[float | None, str | None, int | None]:
    last15 = context.get("last15") or {}
    last15_rate = _float_or_none(last15.get("sideHitRate"))
    last15_games = _int_or_none(last15.get("gamesUsed"))
    if last15_rate is not None and (last15_games or 0) >= 10:
        return round(last15_rate, 4), "last15_exact", last15_games

    last10 = context.get("last10") or {}
    last10_rate = _float_or_none(last10.get("sideHitRate"))
    last10_games = _int_or_none(last10.get("gamesUsed"))
    if last10_rate is not None and (last10_games or 0) >= 10:
        return round(last10_rate, 4), "last10_proxy", last10_games
    if last15_rate is not None:
        return round(last15_rate, 4), "last15_thin_sample", last15_games
    return None, None, last15_games


def _matchup_factor(candidate: dict[str, Any], side: str) -> dict[str, Any]:
    market_key = str(candidate.get("normalizedMarketKey") or "").replace("-", "_")
    factor = 0.50
    adjustments: list[dict[str, Any]] = []
    actual_sources: set[str] = set()

    def add_over_delta(delta: float, source: str, reason: str) -> None:
        nonlocal factor
        side_delta = delta if side == "over" else -delta
        factor = max(0.25, min(0.75, factor + side_delta))
        adjustments.append(
            {
                "source": source,
                "reason": reason,
                "overDelta": round(delta, 3),
                "sideDelta": round(side_delta, 3),
            }
        )
        actual_sources.add(source)

    lineup = candidate.get("lineupContext") or {}
    batting_order = _int_or_none(lineup.get("battingOrder"))
    if batting_order is not None:
        if market_key in {"runs", "hits_runs_rbis"}:
            if batting_order <= 2:
                add_over_delta(0.04, "lineupContext", "top order supports run/HRR volume")
            elif batting_order >= 7:
                add_over_delta(-0.05, "lineupContext", "bottom order suppresses run/HRR volume")
        elif market_key == "rbi":
            if 3 <= batting_order <= 5:
                add_over_delta(0.04, "lineupContext", "middle order supports RBI volume")
            elif batting_order in {1, 8, 9}:
                add_over_delta(-0.03, "lineupContext", "lineup spot suppresses RBI volume")
        elif market_key in {"hits", "singles", "total_bases", "home_runs"}:
            if batting_order <= 3:
                add_over_delta(0.03, "lineupContext", "top order adds plate appearance volume")
            elif batting_order >= 8:
                add_over_delta(-0.04, "lineupContext", "bottom order cuts plate appearance volume")

    pitcher_context = candidate.get("opponentPitcherContext") or {}
    if pitcher_context.get("status") == "available":
        actual_sources.add("opponentPitcherContext")
        pitcher_season = pitcher_context.get("season") or {}
        pitcher_recent = pitcher_context.get("recent") or {}
        pitcher_games = (
            _float_or_none(pitcher_season.get("gamesStarted"))
            or _float_or_none(pitcher_season.get("gamesPitched"))
        )
        hits_pg = _per_game_from_total(pitcher_season.get("hitsAllowed"), pitcher_games)
        walks_pg = _per_game_from_total(pitcher_season.get("walksAllowed"), pitcher_games)
        strikeouts_pg = _per_game_from_total(pitcher_season.get("strikeOuts"), pitcher_games)
        homers_pg = _per_game_from_total(pitcher_season.get("homeRunsAllowed"), pitcher_games)
        recent_per_game = pitcher_recent.get("perGame") or {}

        if market_key in {"hits", "singles", "total_bases", "hits_runs_rbis", "runs", "rbi"}:
            _apply_threshold_adjustment(
                hits_pg,
                low=6.5,
                high=8.5,
                delta=0.04,
                source="opponentPitcherContext",
                low_reason="opposing pitcher suppresses hits allowed",
                high_reason="opposing pitcher allows elevated hits",
                add_over_delta=add_over_delta,
            )
        if market_key in {"total_bases", "home_runs", "hits_runs_rbis"}:
            _apply_threshold_adjustment(
                homers_pg,
                low=0.7,
                high=1.2,
                delta=0.04,
                source="opponentPitcherContext",
                low_reason="opposing pitcher suppresses home run damage",
                high_reason="opposing pitcher allows elevated home run damage",
                add_over_delta=add_over_delta,
            )
        if market_key == "batter_walks":
            _apply_threshold_adjustment(
                walks_pg,
                low=2.0,
                high=3.2,
                delta=0.05,
                source="opponentPitcherContext",
                low_reason="opposing pitcher limits walks",
                high_reason="opposing pitcher walk rate is elevated",
                add_over_delta=add_over_delta,
            )
        if market_key == "batter_strikeouts":
            _apply_threshold_adjustment(
                strikeouts_pg,
                low=4.0,
                high=6.5,
                delta=0.05,
                source="opponentPitcherContext",
                low_reason="opposing pitcher has lower strikeout volume",
                high_reason="opposing pitcher has elevated strikeout volume",
                add_over_delta=add_over_delta,
            )
        recent_hits_pg = _float_or_none(recent_per_game.get("hits"))
        if recent_hits_pg is not None and market_key in {"hits", "singles", "total_bases"}:
            _apply_threshold_adjustment(
                recent_hits_pg,
                low=5.5,
                high=8.5,
                delta=0.025,
                source="opponentPitcherContext.recent",
                low_reason="recent pitcher form suppresses contact",
                high_reason="recent pitcher form allows contact",
                add_over_delta=add_over_delta,
            )

    team_context = candidate.get("opponentTeamContext") or {}
    if team_context.get("status") == "available":
        actual_sources.add("opponentTeamContext")
        season_hitting = team_context.get("seasonHitting") or {}
        if market_key in {"strikeouts", "pitcher_strikeouts"}:
            _apply_threshold_adjustment(
                _float_or_none(season_hitting.get("strikeoutRate")),
                low=0.19,
                high=0.24,
                delta=0.05,
                source="opponentTeamContext",
                low_reason="opponent lineup has lower strikeout tendency",
                high_reason="opponent lineup has elevated strikeout tendency",
                add_over_delta=add_over_delta,
            )
        if market_key == "walks_allowed":
            _apply_threshold_adjustment(
                _float_or_none(season_hitting.get("walkRate")),
                low=0.07,
                high=0.09,
                delta=0.05,
                source="opponentTeamContext",
                low_reason="opponent lineup has lower walk tendency",
                high_reason="opponent lineup has elevated walk tendency",
                add_over_delta=add_over_delta,
            )
        if market_key in {"hits_allowed", "earned_runs"}:
            _apply_threshold_adjustment(
                _float_or_none(season_hitting.get("hitsPerGame")),
                low=7.0,
                high=9.0,
                delta=0.04,
                source="opponentTeamContext",
                low_reason="opponent lineup has lower contact output",
                high_reason="opponent lineup has elevated contact output",
                add_over_delta=add_over_delta,
            )
            _apply_threshold_adjustment(
                _float_or_none(season_hitting.get("runsPerGame")),
                low=3.8,
                high=5.0,
                delta=0.035,
                source="opponentTeamContext",
                low_reason="opponent lineup has lower run output",
                high_reason="opponent lineup has elevated run output",
                add_over_delta=add_over_delta,
            )

    if not actual_sources:
        return {
            "factor": 0.50,
            "source": "neutral_no_matchup_context",
            "adjustments": [],
            "reliability": "limited",
        }
    return {
        "factor": round(factor, 4),
        "source": "actual_context_adjusted",
        "adjustments": adjustments,
        "reliability": "medium" if adjustments else "neutral_with_context",
    }


def _apply_threshold_adjustment(
    value: float | None,
    *,
    low: float,
    high: float,
    delta: float,
    source: str,
    low_reason: str,
    high_reason: str,
    add_over_delta: Any,
) -> None:
    if value is None:
        return
    if value <= low:
        add_over_delta(-delta, source, low_reason)
    elif value >= high:
        add_over_delta(delta, source, high_reason)


def _probability_penalties(
    candidate: dict[str, Any],
    scoring_risk_flags: list[str],
    matchup: dict[str, Any],
) -> list[dict[str, Any]]:
    penalties: list[dict[str, Any]] = []
    flags = {str(flag) for flag in scoring_risk_flags}
    evidence = ((candidate.get("context") or {}).get("evidenceCheck") or {})
    trend = set(((candidate.get("context") or {}).get("trendLabels") or []))
    if evidence.get("last5OverreactionRisk") or "last5_overreaction_risk" in trend or "recencyTrap" in flags:
        penalties.append({"flag": "last5_overreaction_risk", "amount": 0.15})
    if str(candidate.get("contextQuality") or "").lower() == "partial":
        penalties.append({"flag": "context_quality_partial", "amount": 0.05})
    for flag, amount in (
        ("lineup_unconfirmed", 0.05),
        ("probable_pitcher_unavailable", 0.05),
        ("opponent_lineup_unconfirmed", 0.05),
        ("game_delay_risk", 0.08),
        ("start_time_tbd", 0.08),
    ):
        if flag in flags:
            penalties.append({"flag": flag, "amount": amount})
    if (matchup or {}).get("source") == "neutral_no_matchup_context":
        penalties.append({"flag": "matchup_factor_neutral_missing_context", "amount": 0.03})
    market_key = str(candidate.get("normalizedMarketKey") or "").replace("-", "_")
    if market_key in {"home_runs", "stolen_bases"}:
        penalties.append({"flag": "extreme_volatility_market", "amount": 0.07})
    elif market_key in {"runs", "rbi", "total_bases", "batter_walks", "hits_runs_rbis"}:
        penalties.append({"flag": "volatile_market", "amount": 0.04})
    return _dedupe_penalties(penalties)


def _probability_reliability_flags(
    candidate: dict[str, Any],
    *,
    season_rate: float | None,
    season_source: str | None,
    recent_rate: float | None,
    recent_source: str | None,
    recent_games: int | None,
    matchup: dict[str, Any],
    implied: float | None,
) -> list[str]:
    flags = []
    if implied is None:
        flags.append("implied_probability_unavailable")
    if season_rate is None:
        flags.append("season_rate_unavailable")
    if season_source and season_source.endswith("_proxy"):
        flags.append("season_rate_proxy")
    if recent_rate is None:
        flags.append("recent_rate_unavailable")
    if (recent_games or 0) < 10:
        flags.append("recent_sample_under_10")
    if recent_source and recent_source.endswith("_proxy"):
        flags.append("recent_rate_proxy")
    if recent_source == "last15_thin_sample":
        flags.append("recent_rate_thin_sample")
    if (matchup or {}).get("source") == "neutral_no_matchup_context":
        flags.append("matchup_factor_neutral_no_context")
    sample_status = str((candidate.get("seasonSample") or {}).get("status") or "")
    if sample_status in {"low_confidence", "unknown"}:
        flags.append(f"season_sample_{sample_status}")
    elif sample_status == "robust":
        flags.append("season_sample_robust")
    return sorted(set(flags))


def _probability_data_quality(
    candidate: dict[str, Any],
    *,
    reliability_flags: list[str],
    scoring_risk_flags: list[str],
) -> str:
    flags = set(scoring_risk_flags or [])
    if flags & BLOCKING_RISK_FLAGS:
        return "low"
    context_quality = str(candidate.get("contextQuality") or "").lower()
    if context_quality == "unsupported":
        return "low"
    required_missing = {
        "implied_probability_unavailable",
        "season_rate_unavailable",
        "recent_rate_unavailable",
    }
    reliability = set(reliability_flags)
    if reliability & required_missing:
        return "low"
    if "recent_sample_under_10" in reliability and "season_sample_robust" not in reliability:
        return "low"
    medium_flags = {
        "season_rate_proxy",
        "recent_rate_proxy",
        "recent_rate_thin_sample",
        "matchup_factor_neutral_no_context",
        "season_sample_low_confidence",
        "season_sample_unknown",
    }
    if context_quality == "partial" or reliability & medium_flags:
        return "medium"
    minor_risks = {
        "lineup_unconfirmed",
        "probable_pitcher_unavailable",
        "opponent_lineup_unconfirmed",
        "game_delay_risk",
        "start_time_tbd",
    }
    if flags & minor_risks:
        return "medium"
    return "high"


def _edge_status(edge: float | None, data_quality: str) -> str:
    if edge is None or data_quality == "low":
        return "unknown_edge"
    if edge >= 0.05:
        return "clear_possible_edge"
    if edge >= 0.02:
        return "thin_edge"
    if edge <= -0.02:
        return "negative_edge"
    return "no_clear_edge"


def _probability_score_adjustment(probability: dict[str, Any]) -> float:
    if probability.get("dataQuality") == "low":
        return 0.0
    return {
        "clear_possible_edge": 6.0,
        "thin_edge": 2.5,
        "no_clear_edge": -1.5,
        "negative_edge": -6.0,
    }.get(str(probability.get("edgeStatus") or ""), 0.0)


def _dedupe_penalties(penalties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for penalty in penalties:
        flag = str(penalty.get("flag") or "")
        if not flag or flag in deduped:
            continue
        deduped[flag] = {"flag": flag, "amount": round(float(penalty.get("amount") or 0.0), 4)}
    return list(deduped.values())


def _per_game_from_total(total: Any, games: Any) -> float | None:
    total_float = _float_or_none(total)
    games_float = _float_or_none(games)
    if total_float is None or not games_float:
        return None
    return round(total_float / games_float, 4)


def _innings_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if "." in text:
        whole, fraction = text.split(".", 1)
        if fraction in {"1", "2"}:
            try:
                return int(whole) + int(fraction) / 3
            except ValueError:
                return _float_or_none(value)
    return _float_or_none(value)


def _select_ranked_candidates(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    legs_per_game: int | None,
    max_total_legs: int | None,
    max_candidates_per_game: int,
    max_total_candidates: int,
) -> list[dict[str, Any]]:
    rows = sorted(rows, key=_ranked_candidate_sort_key)
    if mode == "per_game":
        per_game_limit = max(1, min(int(legs_per_game or 2), max_candidates_per_game))
    else:
        per_game_limit = max_candidates_per_game
    total_limit = max_total_legs or max_total_candidates
    total_limit = max(1, min(int(total_limit), max_total_candidates))

    selected: list[dict[str, Any]] = []
    per_game_counts: Counter[str] = Counter()
    group_odds_by_fixture: defaultdict[str, float] = defaultdict(lambda: 1.0)
    for row in rows:
        fixture = str(row.get("fixtureSlug") or "unknown")
        odds = _float_or_none(row.get("odds")) or 1.0
        if per_game_counts[fixture] >= per_game_limit:
            continue
        if group_odds_by_fixture[fixture] * odds > DEFAULT_MAX_SGM_GROUP_ODDS:
            row.setdefault("riskFlags", []).append("sgm_group_odds_cap_would_be_exceeded")
            continue
        selected.append(row)
        per_game_counts[fixture] += 1
        group_odds_by_fixture[fixture] *= odds
        if len(selected) >= total_limit:
            break
    return selected


def _ranked_candidate_sort_key(row: dict[str, Any]) -> tuple[int, int, float, str]:
    game_selected = bool(row.get("gameContestSelected"))
    contest_rank = _clean_contest_rank(row.get("marketContestRank"))
    score = _float_or_none(row.get("score")) or 0.0
    row_id = str(row.get("rowId") or "")
    return (0 if game_selected else 1, 0 if contest_rank <= 1 else 1, -score, row_id)


def _apply_within_player_market_contest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _entity_market_contest_key(row)
        if key:
            groups[key].append(row)

    contested_groups = 0
    winner_rows = 0
    alternative_rows = 0
    max_contenders = 0
    for key, group in groups.items():
        ranked = sorted(
            group,
            key=lambda row: (-(row.get("score") or 0), str(row.get("rowId") or "")),
        )
        winner = ranked[0]
        winner_score = _float_or_none(winner.get("score")) or 0.0
        winner_rows += 1
        alternative_rows += max(0, len(ranked) - 1)
        max_contenders = max(max_contenders, len(ranked))
        if len(ranked) > 1:
            contested_groups += 1
        markets_compared = [_market_comparison_row(row) for row in ranked]
        alternatives = [
            _compact_market_contest_alternative(winner, row)
            for row in ranked[1 : MARKET_CONTEST_ALTERNATIVE_LIMIT + 1]
        ]
        rejected_alternatives = [
            _rejected_market_alternative(winner, row)
            for row in ranked[1 : MARKET_CONTEST_REJECTED_LIMIT + 1]
        ]
        market_count = len(
            {str(row.get("normalizedMarketKey") or row.get("market") or "") for row in ranked}
        )
        for rank, row in enumerate(ranked, start=1):
            score = _float_or_none(row.get("score")) or 0.0
            is_winner = rank == 1
            closest_alternative = ranked[rank] if rank < len(ranked) else None
            closest_for_winner = ranked[1] if is_winner and len(ranked) > 1 else closest_alternative
            closest_proof = _closest_alternative_summary(closest_for_winner)
            why_selected = _why_selected_beat_alternative(row, closest_for_winner)
            loss_reason = None if is_winner else _why_alternative_lost(winner, row)
            row["marketContestRank"] = rank
            row["marketContestWinner"] = is_winner
            row["marketContestScoreGap"] = round(max(0.0, winner_score - score), 2)
            row["marketContest"] = {
                "scope": _entity_market_contest_scope(row),
                "entity": row.get("player") or row.get("team"),
                "entityKey": key,
                "rank": rank,
                "winner": is_winner,
                "contenderCount": len(ranked),
                "marketCount": market_count,
                "marketsCompared": markets_compared,
                "winnerRowId": winner.get("rowId"),
                "winnerMarket": winner.get("market"),
                "winnerSide": winner.get("side"),
                "scoreGapToWinner": round(max(0.0, winner_score - score), 2),
                "availabilityRole": "eligibility_only",
                "dataDepthRole": "confidence_cap_not_direct_merit_bonus",
            }
            if is_winner and alternatives:
                row["marketContest"]["topAlternatives"] = alternatives
                row["marketContest"]["rejectedAlternatives"] = rejected_alternatives
            if not is_winner:
                row["marketContest"]["lostTo"] = {
                    "rowId": winner.get("rowId"),
                    "market": winner.get("market"),
                    "side": winner.get("side"),
                    "line": winner.get("line"),
                    "odds": winner.get("odds"),
                    "score": winner.get("score"),
                    "reason": loss_reason,
                }
            row["selectionProof"] = {
                "selectedMarket": row.get("market"),
                "selectedScore": row.get("score"),
                "probabilityAssessment": _compact_probability_assessment(
                    row.get("probabilityAssessment") or {}
                ),
                "marketsCompared": markets_compared,
                "closestAlternativeMarket": closest_proof.get("market"),
                "closestAlternativeScore": closest_proof.get("score"),
                "whySelectedBeatAlternative": why_selected,
                "availabilityRole": "eligibility_only",
                "dataDepthRole": "confidence_cap_not_direct_merit_bonus",
                "riskFlags": list(row.get("riskFlags") or []),
                "contextQuality": row.get("contextQuality"),
                "marketContestRank": rank,
                "marketContestWinner": is_winner,
                "rejectedAlternatives": rejected_alternatives if is_winner else [],
            }
            reason_tags = set(row.get("reasonTags") or [])
            reason_tags.add(
                "player_market_fit_winner"
                if is_winner
                else "player_market_fit_alternative"
            )
            row["reasonTags"] = sorted(reason_tags)

    return {
        "enabled": True,
        "policy": "within_player_winner_first",
        "playerGroups": len(groups),
        "contestedPlayerGroups": contested_groups,
        "winnerRows": winner_rows,
        "alternativeRows": alternative_rows,
        "maxContendersForOnePlayer": max_contenders,
        "alternativeLimitPerWinner": MARKET_CONTEST_ALTERNATIVE_LIMIT,
        "availabilityRole": "eligibility_only",
        "dataDepthRole": "confidence_cap_not_direct_merit_bonus",
    }


def _entity_market_contest_key(row: dict[str, Any]) -> str | None:
    player_key = slug_key(row.get("player"))
    fixture = slug_key(row.get("fixtureSlug") or row.get("matchup") or "unknown")
    team = slug_key(row.get("team") or "unknown")
    if player_key:
        return f"{fixture}:player:{team}:{player_key}"
    if team and team != "unknown":
        return f"{fixture}:team:{team}"
    return None


def _entity_market_contest_scope(row: dict[str, Any]) -> str:
    return "player" if slug_key(row.get("player")) else "team"


def _compact_market_contest_alternative(winner: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    winner_score = _float_or_none(winner.get("score")) or 0.0
    score = _float_or_none(row.get("score")) or 0.0
    return {
        "rowId": row.get("rowId"),
        "market": row.get("market"),
        "side": row.get("side"),
        "line": row.get("line"),
        "odds": row.get("odds"),
        "score": row.get("score"),
        "edgeStatus": (row.get("probabilityAssessment") or {}).get("edgeStatus"),
        "adjustedEstimatedProbability": (row.get("probabilityAssessment") or {}).get(
            "adjustedEstimatedProbability"
        ),
        "scoreGapToWinner": round(max(0.0, winner_score - score), 2),
        "reasonLost": _why_alternative_lost(winner, row),
        "riskFlags": list(row.get("riskFlags") or []),
        "contextQuality": row.get("contextQuality"),
    }


def _market_comparison_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market"),
        "side": row.get("side"),
        "line": row.get("line"),
        "odds": row.get("odds"),
        "score": row.get("score"),
        "edgeStatus": (row.get("probabilityAssessment") or {}).get("edgeStatus"),
        "adjustedEstimatedProbability": (row.get("probabilityAssessment") or {}).get(
            "adjustedEstimatedProbability"
        ),
        "contextQuality": row.get("contextQuality"),
        "riskFlags": list(row.get("riskFlags") or []),
        "availabilityRole": "eligibility_only",
    }


def _rejected_market_alternative(winner: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    reason = _why_alternative_lost(winner, row)
    loss_type = _market_loss_type(row)
    return {
        "market": row.get("market"),
        "side": row.get("side"),
        "line": row.get("line"),
        "odds": row.get("odds"),
        "score": row.get("score"),
        "edgeStatus": (row.get("probabilityAssessment") or {}).get("edgeStatus"),
        "reasonLost": reason,
        "blocker": reason if loss_type == "blocker" else None,
        "lowerMeritReason": reason if loss_type == "lower_merit" else None,
        "lossType": loss_type,
        "riskFlags": list(row.get("riskFlags") or []),
        "contextQuality": row.get("contextQuality"),
    }


def _closest_alternative_summary(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"market": None, "score": None}
    return {"market": row.get("market"), "score": row.get("score")}


def _compact_probability_assessment(probability: dict[str, Any]) -> dict[str, Any]:
    if not probability:
        return {}
    inputs = probability.get("inputs") or {}
    return {
        "impliedProbability": probability.get("impliedProbability"),
        "estimatedProbability": probability.get("estimatedProbability"),
        "adjustedEstimatedProbability": probability.get("adjustedEstimatedProbability"),
        "edge": probability.get("edge"),
        "edgeStatus": probability.get("edgeStatus"),
        "dataQuality": probability.get("dataQuality"),
        "seasonRate": inputs.get("seasonRate"),
        "last15Rate": inputs.get("last15Rate"),
        "matchupFactor": inputs.get("matchupFactor"),
        "penalties": probability.get("penalties") or [],
        "reliabilityFlags": probability.get("reliabilityFlags") or [],
    }


def _why_selected_beat_alternative(
    selected: dict[str, Any],
    alternative: dict[str, Any] | None,
) -> str:
    if not alternative:
        return "Only eligible researched market-side row for this entity."
    return _comparison_reason(selected, alternative, selected_label="Selected", alternative_label="closest alternative")


def _why_alternative_lost(winner: dict[str, Any], alternative: dict[str, Any]) -> str:
    if _market_loss_type(alternative) == "blocker":
        flags = ", ".join(flag for flag in alternative.get("riskFlags") or [] if flag in BLOCKING_RISK_FLAGS)
        return f"Blocked by material risk flag: {flags or 'blocking risk'}."
    return _comparison_reason(winner, alternative, selected_label="Winner", alternative_label="alternative")


def _comparison_reason(
    selected: dict[str, Any],
    alternative: dict[str, Any],
    *,
    selected_label: str,
    alternative_label: str,
) -> str:
    selected_score = _float_or_none(selected.get("score")) or 0.0
    alternative_score = _float_or_none(alternative.get("score")) or 0.0
    score_gap = round(max(0.0, selected_score - alternative_score), 2)
    reasons: list[str] = []
    for key, label in (
        ("evidenceScore", "stronger evidence"),
        ("valueScore", "better price/value score"),
        ("modeFitScore", "better build-mode fit"),
        ("probabilityScoreAdjustment", "better implied-vs-estimated probability edge"),
    ):
        selected_value = _float_or_none(selected.get(key)) or 0.0
        alternative_value = _float_or_none(alternative.get(key)) or 0.0
        if selected_value >= alternative_value + 3:
            reasons.append(label)
    for key, label in (
        ("volatilityPenalty", "lower volatility penalty"),
        ("stakeMetadataPenalty", "cleaner Stake metadata"),
        ("oddsTrapPenalty", "less odds-trap risk"),
        ("quotaFillerPenalty", "less filler risk"),
        ("correlationPenalty", "lower correlation penalty"),
    ):
        selected_value = _float_or_none(selected.get(key)) or 0.0
        alternative_value = _float_or_none(alternative.get(key)) or 0.0
        if selected_value + 3 <= alternative_value:
            reasons.append(label)
    if len(selected.get("riskFlags") or []) < len(alternative.get("riskFlags") or []):
        reasons.append("fewer risk flags")
    if _quality_rank(selected.get("contextQuality")) > _quality_rank(alternative.get("contextQuality")):
        reasons.append("better context quality")
    detail = ", ".join(reasons[:3]) if reasons else "higher total merit score after line, odds, evidence, volatility, and risk"
    return f"{selected_label} beat {alternative_label} by {score_gap} points: {detail}."


def _market_loss_type(row: dict[str, Any]) -> str:
    risk_flags = set(row.get("riskFlags") or [])
    return "blocker" if risk_flags & BLOCKING_RISK_FLAGS else "lower_merit"


def _quality_rank(value: Any) -> int:
    key = str(value or "").strip().lower()
    return {
        "full": 4,
        "high": 4,
        "strong": 4,
        "supported": 3,
        "medium": 3,
        "partial": 2,
        "low": 1,
        "unsupported": 0,
    }.get(key, 0)


def _apply_game_level_contest(
    rows: list[dict[str, Any]],
    *,
    legs_per_game: int | None,
    max_candidates_per_game: int,
) -> dict[str, Any]:
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("fixtureSlug") or "unknown")].append(row)

    target_legs = max(
        GAME_CONTEST_MIN_LEGS,
        min(int(legs_per_game or GAME_CONTEST_MIN_LEGS), max_candidates_per_game),
    )
    fixture_summaries: dict[str, dict[str, Any]] = {}
    ready_groups = 0
    insufficient_groups = 0

    for fixture, group in groups.items():
        entity_winners = [row for row in group if row.get("marketContestWinner") is True]
        primary = sorted(entity_winners or group, key=lambda row: (-(row.get("score") or 0), str(row.get("rowId") or "")))
        fallback = sorted(
            [row for row in group if row not in primary],
            key=lambda row: (-(row.get("score") or 0), str(row.get("rowId") or "")),
        )
        ranked = primary + fallback
        selected_rows = ranked[:target_legs]
        status = "ready_for_two_plus_leg_sgm" if len(selected_rows) >= GAME_CONTEST_MIN_LEGS else "insufficient_two_leg_candidates"
        if status == "ready_for_two_plus_leg_sgm":
            ready_groups += 1
        else:
            insufficient_groups += 1
        fixture_summaries[fixture] = {
            "status": status,
            "targetLegs": target_legs,
            "candidateRows": len(group),
            "entityWinnerRows": len(entity_winners),
            "selectedRows": len(selected_rows),
            "selectedRowIds": [row.get("rowId") for row in selected_rows],
            "selectedMarkets": [row.get("market") for row in selected_rows],
            "selectionBasis": "entity_market_winners_first_then_score",
        }
        selected_ids = {row.get("rowId") for row in selected_rows}
        for rank, row in enumerate(ranked, start=1):
            selected = row.get("rowId") in selected_ids
            row["gameContestRank"] = rank
            row["gameContestSelected"] = selected
            row["gameContestWinner"] = selected
            row["gameContest"] = {
                "fixtureSlug": fixture,
                "rank": rank,
                "selected": selected,
                "targetLegs": target_legs,
                "requiredLegs": GAME_CONTEST_MIN_LEGS,
                "candidateRows": len(group),
                "entityWinnerRows": len(entity_winners),
                "selectionBasis": "entity_market_winners_first_then_score",
                "status": status,
            }
            proof = row.setdefault("selectionProof", {})
            proof["gameContestRank"] = rank
            proof["gameContestSelected"] = selected
            proof["gameContestStatus"] = status

    return {
        "enabled": True,
        "policy": "per_fixture_entity_winners_first",
        "minLegsPerGame": GAME_CONTEST_MIN_LEGS,
        "targetLegsPerGame": target_legs,
        "fixtureGroups": len(groups),
        "readyFixtureGroups": ready_groups,
        "insufficientFixtureGroups": insufficient_groups,
        "fixtures": fixture_summaries,
    }


def _market_concentration_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    exposure = Counter(str(row.get("normalizedMarketKey") or "unknown") for row in rows)
    total = len(rows)
    if not total:
        return {
            "diagnosticOnly": True,
            "totalRows": 0,
            "marketExposure": {},
            "warnings": [],
        }
    dominant_market, dominant_count = exposure.most_common(1)[0]
    dominant_share = dominant_count / total
    warnings = []
    if dominant_share >= 0.5 and total >= 4:
        warnings.append(
            {
                "type": "market_concentration",
                "market": dominant_market,
                "share": round(dominant_share, 3),
                "message": (
                    f"{dominant_market} makes up {dominant_count}/{total} returned rows; "
                    "this is diagnostic only and did not change selection order."
                ),
            }
        )
    return {
        "diagnosticOnly": True,
        "totalRows": total,
        "dominantMarket": dominant_market,
        "dominantCount": dominant_count,
        "dominantShare": round(dominant_share, 3),
        "marketExposure": dict(exposure),
        "warnings": warnings,
    }


def _clean_contest_rank(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _per_game_summary(
    flat_rows: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    rejected: Counter[str],
) -> dict[str, Any]:
    fixtures = sorted({str(row.get("fixtureSlug") or "unknown") for row in flat_rows})
    selected_by_fixture = Counter(str(row.get("fixtureSlug") or "unknown") for row in ranked)
    accepted_by_fixture = Counter(str(row.get("fixtureSlug") or "unknown") for row in accepted)
    scanned_by_fixture = Counter(str(row.get("fixtureSlug") or "unknown") for row in flat_rows)
    return {
        fixture: {
            "scannedRows": scanned_by_fixture[fixture],
            "acceptedRows": accepted_by_fixture[fixture],
            "returnedRows": selected_by_fixture[fixture],
            "skipped": selected_by_fixture[fixture] == 0,
        }
        for fixture in fixtures
    }


def _context_coverage(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter()
    for row in rows:
        counter[str(row.get("contextQuality") or "unsupported")] += 1
        counter[str(row.get("identityResolution") or "unmatched")] += 1
    return dict(counter)


def _market_filter_set(markets: Any) -> set[str]:
    if not markets:
        return set()
    if isinstance(markets, str):
        values = markets.split(",")
    else:
        values = list(markets)
    cleaned = set()
    for value in values:
        normalized = normalize_sgm_market_key(value)
        cleaned.add(normalized)
        cleaned.add(normalized.replace("_", "-"))
    return cleaned


def _evidence_score(context: dict[str, Any], side: str) -> float:
    rates = []
    for key, weight in (("last10", 0.35), ("last15", 0.35)):
        rate = _float_or_none((context.get(key) or {}).get("sideHitRate"))
        if rate is not None:
            rates.append(rate * 100 * weight)
    season = context.get("season") or {}
    season_supported = season.get("sideSupported")
    if season_supported is True:
        rates.append(70 * 0.30)
    elif season_supported is False:
        rates.append(25 * 0.30)
    if not rates:
        return 0.0
    total_weight = 0.35 * sum(1 for key in ("last10", "last15") if _float_or_none((context.get(key) or {}).get("sideHitRate")) is not None)
    if season_supported is not None:
        total_weight += 0.30
    return sum(rates) / max(total_weight, 0.01)


def _value_score(odds: float | None, mode: str) -> float:
    if odds is None:
        return 40.0
    if odds < 1.15:
        return 8.0
    if odds < 1.45:
        return 35.0
    if odds < 2.75:
        return 76.0
    if odds < 4.5:
        return 70.0 if mode == "longshot" else 54.0
    return 58.0 if mode == "longshot" else 30.0


def _mode_fit_score(evidence_score: float, odds: float | None, mode: str) -> float:
    odds = odds or 0.0
    if mode == "safe":
        return evidence_score if evidence_score >= 70 and odds <= 3.0 else max(20.0, evidence_score - 20.0)
    if mode == "longshot":
        odds_bonus = 15.0 if odds >= 2.75 else 0.0
        return min(100.0, evidence_score + odds_bonus)
    if mode == "balanced":
        return evidence_score * 0.8 + _value_score(odds, mode) * 0.2
    return evidence_score


def _volatility_penalty(market_key: str, mode: str) -> float:
    high = {"home_runs", "stolen_bases"}
    medium = {"runs", "rbi", "total_bases", "batter_walks"}
    if market_key in high:
        return 6.0 if mode == "longshot" else 14.0
    if market_key in medium:
        return 4.0 if mode == "longshot" else 8.0
    return 2.0


def _mode_quality_floor(mode: str) -> float:
    return {
        "safe": 68.0,
        "balanced": 58.0,
        "longshot": 50.0,
        "per_game": 55.0,
    }.get(mode, 60.0)


def _clean_mode(mode: str | None) -> str:
    normalized = slug_key(mode or "best_available").replace("-", "_")
    if normalized in SUPPORTED_MODES:
        return normalized
    if normalized == "best":
        return "best_available"
    return "best_available"


def _clean_side(side: str | None) -> str:
    normalized = str(side or "any").strip().lower()
    return normalized if normalized in {"any", "over", "under"} else "any"


def _season_from_date(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(str(value)[:4])
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
