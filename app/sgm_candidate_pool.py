from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from .decision_profiles import evidence_check, evidence_windows, season_evidence, trend_labels
from .mlb_bridge import enrich_props_with_mlb_data, stat_mapping_for_market
from .mlb_props import slug_key
from .stake_sgm_browser import make_sgm_selection_row_id, sgm_market_filter_matches


DEFAULT_MAX_LEGS_PER_GAME_GROUP = 16
DEFAULT_MAX_SGM_GROUP_ODDS = 501.0
NORMAL_SLATE_GAME_CAP = 15
HARD_SLATE_GAME_CAP = 20
DEFAULT_MAX_CANDIDATES_PER_GAME = 8
DEFAULT_MAX_TOTAL_CANDIDATES = 75
COMPACT_REASON_TAG_LIMIT = 3
SUPPORTED_MODES = {
    "best_available",
    "safe",
    "balanced",
    "longshot",
    "per_game",
    "strict_diversity",
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
    market_exposure_penalty = 0.0
    risk_flags: list[str] = []
    reason_tags: list[str] = []

    if odds is not None and odds < 1.15:
        quota_filler_penalty = 18.0
        risk_flags.append("short_odds_quota_filler")
    if odds is not None and odds >= 4.0 and evidence_score < 55:
        odds_trap_penalty = 26.0 if clean_mode == "longshot" else 34.0
        risk_flags.append("high_odds_no_stat_support")
    if max_market_repeats is not None and market_exposure_count >= max_market_repeats:
        market_exposure_penalty = 25.0
        risk_flags.append("market_repeat_cap_reached")
    elif market_exposure_count >= 2:
        market_exposure_penalty = min(18.0, market_exposure_count * 6.0)
        reason_tags.append("market_exposure_soft_penalty")
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

    score = (
        evidence_score * 0.48
        + value_score * 0.22
        + mode_fit_score * 0.20
        + 10.0
        - odds_trap_penalty
        - quota_filler_penalty
        - volatility_penalty
        - stake_metadata_penalty
        - correlation_penalty
        - market_exposure_penalty
    )
    return {
        "evidenceScore": round(evidence_score, 2),
        "valueScore": round(value_score, 2),
        "modeFitScore": round(mode_fit_score, 2),
        "oddsTrapPenalty": round(odds_trap_penalty, 2),
        "quotaFillerPenalty": round(quota_filler_penalty, 2),
        "volatilityPenalty": round(volatility_penalty, 2),
        "stakeMetadataPenalty": round(stake_metadata_penalty, 2),
        "correlationPenalty": round(correlation_penalty, 2),
        "marketExposurePenalty": round(market_exposure_penalty, 2),
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
) -> dict[str, Any]:
    clean_mode = _clean_mode(mode)
    clean_side = _clean_side(side)
    wanted_markets = _market_filter_set(markets)
    max_games = max(1, min(int(max_games or NORMAL_SLATE_GAME_CAP), HARD_SLATE_GAME_CAP))
    max_candidates_per_game = max(1, min(int(max_candidates_per_game), DEFAULT_MAX_LEGS_PER_GAME_GROUP))
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
    exposure_counter: Counter[str] = Counter()

    for row in flat_rows:
        enriched_prop = enriched_by_id.get(str(row.get("propId") or "")) or {}
        candidate = _candidate_from_enriched_row(row, enriched_prop)
        score = score_sgm_candidate(
            candidate,
            mode=clean_mode,
            market_exposure_count=exposure_counter[candidate["normalizedMarketKey"]],
            max_market_repeats=1 if clean_mode == "strict_diversity" else None,
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
            exposure_counter[candidate["normalizedMarketKey"]] += 1

    ranked = _select_ranked_candidates(
        scored_rows,
        mode=clean_mode,
        legs_per_game=legs_per_game,
        max_total_legs=max_total_legs,
        max_candidates_per_game=max_candidates_per_game,
        max_total_candidates=max_total_candidates,
    )
    per_game = _per_game_summary(flat_rows, ranked, scored_rows, rejected)
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
        },
        "candidateCounts": {
            "scannedRows": len(flat_rows),
            "acceptedRows": len(scored_rows),
            "returnedRows": len(ranked),
            "rejectedRows": sum(rejected.values()),
        },
        "rankedCandidates": ranked,
        "perGame": per_game,
        "rejectedSummary": dict(sorted(rejected.items())),
        "marketExposure": dict(Counter(row["normalizedMarketKey"] for row in ranked)),
        "contextCoverage": _context_coverage(ranked),
        "notes": [
            "Candidate pool is support data only; the Custom GPT owns final selections.",
            "This endpoint never clicks Stake UI selections or builds a review slip.",
        ],
    }


def _flatten_board_rows(
    boards: list[dict[str, Any]],
    *,
    side: str,
    market_filter: set[str],
    min_odds: float | None,
    max_odds: float | None,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    wanted_sides = ("over", "under") if side == "any" else (side,)
    for board in boards:
        fixture_slug = str(board.get("fixtureSlug") or "")
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
        "context": context,
        "gameContext": enriched_prop.get("gameContext"),
        "lineupContext": enriched_prop.get("lineupContext"),
        "opponentPitcherContext": enriched_prop.get("opponentPitcherContext"),
        "opponentTeamContext": enriched_prop.get("opponentTeamContext"),
        "playerSplits": enriched_prop.get("playerSplits"),
        "capturedAt": datetime.now(timezone.utc).isoformat(),
    }


def _select_ranked_candidates(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    legs_per_game: int | None,
    max_total_legs: int | None,
    max_candidates_per_game: int,
    max_total_candidates: int,
) -> list[dict[str, Any]]:
    rows = sorted(rows, key=lambda row: row.get("score") or 0, reverse=True)
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
        "strict_diversity": 57.0,
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
