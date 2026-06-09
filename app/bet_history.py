from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bet_history_fingerprint import FINGERPRINT_VERSION, history_fingerprint
from .bet_history_enrichment import enrich_bet_history, format_enrichment_report
from .market_normalization import SUPPORTED_MLB_PROP_MARKETS, normalize_mlb_prop_market_key
from .mlb_data import MLBDataEngine, MLBStatsClient, build_mlb_http_client
from .mlb_props import slug_key
from .storage import GptActionStore


SUPPORTED_FORMATS = {"auto", "csv", "json", "jsonl", "text"}
DEFAULT_IMPORT_DIR = Path("data") / "bet-history" / "imports"
PARSER_VERSION = "bet_history_parser_v2"
ELIGIBILITY_VERSION = "bet_history_eligibility_v2"
MAX_TEXT_TICKET_ODDS = 1_000_000.0
HIGH_CONFIDENCE = 0.85
MEDIUM_CONFIDENCE = 0.70

DATE_KEYS = (
    "date",
    "bet_date",
    "placed_at",
    "placed",
    "game_date",
    "event_date",
    "settled_at",
    "settled",
)
PLAYER_KEYS = ("player", "player_name", "participant", "athlete", "name")
TEAM_KEYS = ("team", "team_name", "club")
OPPONENT_KEYS = ("opponent", "opponent_name", "against")
MATCHUP_KEYS = ("matchup", "event", "game", "fixture")
MARKET_KEYS = ("market", "market_name", "bet_type", "prop", "prop_type", "selection_market")
SELECTION_KEYS = ("selection", "selection_name", "pick", "description", "leg", "wager")
SIDE_KEYS = ("side", "over_under", "direction")
LINE_KEYS = ("line", "handicap", "points", "total")
ODDS_KEYS = ("odds", "price", "decimal_odds", "decimalodds")
RESULT_KEYS = ("result", "status", "outcome", "settlement", "settlement_status")
ACTUAL_STAT_KEYS = ("actual_stat", "actualstat", "stat", "result_stat", "resultstat", "final_stat", "finalstat")
TICKET_KEYS = ("ticket_id", "bet_id", "wager_id", "slip_id", "ticket", "id")
STAKE_KEYS = ("stake", "stake_amount", "stakeamount", "wager_amount", "wageramount", "amount", "risk")
PAYOUT_KEYS = ("payout", "payout_amount", "payoutamount", "return", "profit", "win_amount", "winamount")

SUPPORTED_HISTORY_MARKETS = set(SUPPORTED_MLB_PROP_MARKETS)

CANONICAL_SOURCE_KEYS = {
    *DATE_KEYS,
    *PLAYER_KEYS,
    *TEAM_KEYS,
    *OPPONENT_KEYS,
    *MATCHUP_KEYS,
    *MARKET_KEYS,
    *SELECTION_KEYS,
    *SIDE_KEYS,
    *LINE_KEYS,
    *ODDS_KEYS,
    *RESULT_KEYS,
    *ACTUAL_STAT_KEYS,
    *TICKET_KEYS,
    *STAKE_KEYS,
    *PAYOUT_KEYS,
    "settled_at",
    "settled",
    "settlement_date",
    "sport",
    "league",
    "competition",
    "fixture_slug",
    "game_slug",
}
TECHNICAL_SOURCE_KEYS = {
    "source_line",
    "settlement_target",
    "live_at_import",
    "liveatimport",
    "sgm_multiplier",
    "sgmmultiplier",
    "ticket_odds",
    "ticketodds",
}
CANONICAL_SOURCE_KEYS_NORMALIZED = {
    slug_key(key).replace("-", "_")
    for key in [*CANONICAL_SOURCE_KEYS, *TECHNICAL_SOURCE_KEYS]
}

RESULT_ALIASES = {
    "win": "won",
    "won": "won",
    "cash": "won",
    "cashed": "won",
    "hit": "won",
    "loss": "lost",
    "lose": "lost",
    "lost": "lost",
    "miss": "lost",
    "missed": "lost",
    "push": "push",
    "tie": "push",
    "void": "void",
    "cancelled": "void",
    "canceled": "void",
    "refund": "void",
    "refunded": "void",
    "open": "unsettled",
    "pending": "unsettled",
    "unsettled": "unsettled",
}


def load_history_file(path: str | Path, source_format: str = "auto") -> dict[str, Any]:
    source_path = Path(path)
    clean_format = _detect_format(source_path, source_format)
    diagnostics: dict[str, Any] = {}
    if clean_format == "csv":
        rows = _load_csv(source_path)
    elif clean_format == "jsonl":
        rows = _load_jsonl(source_path)
    elif clean_format == "text":
        rows, diagnostics = _load_text(source_path)
    else:
        rows = _load_json(source_path)
    return {
        "sourcePath": str(source_path),
        "sourceFormat": clean_format,
        "rows": rows,
        "diagnostics": diagnostics,
    }


def parse_history_rows(
    rows: list[dict[str, Any]],
    *,
    source_format: str,
    source_path: str | None = None,
    review_limit: int = 25,
    source_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_rows: list[dict[str, Any]] = []
    legs: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=1):
        raw_rows.append(
            {
                "sourceRowNumber": row_number,
                "sourceFormat": source_format,
                "rawText": _raw_text(row),
                "rawJson": row,
            }
        )
        for leg_index, leg_record in enumerate(_leg_records(row), start=1):
            legs.append(_parse_leg(leg_record, source_row_number=row_number, leg_index=leg_index))

    source_fingerprint = history_fingerprint(legs)
    report = build_parse_report(raw_rows, legs, source_path=source_path, review_limit=review_limit)
    report["sourceFingerprint"] = source_fingerprint
    report["fingerprintVersion"] = FINGERPRINT_VERSION
    report["parserVersion"] = PARSER_VERSION
    report["eligibilityVersion"] = ELIGIBILITY_VERSION
    report["parseDiagnostics"] = source_diagnostics or {}
    return {
        "sourcePath": source_path,
        "sourceFormat": source_format,
        "sourceFingerprint": source_fingerprint,
        "fingerprintVersion": FINGERPRINT_VERSION,
        "parserVersion": PARSER_VERSION,
        "eligibilityVersion": ELIGIBILITY_VERSION,
        "parseDiagnostics": source_diagnostics or {},
        "rawRows": raw_rows,
        "legs": legs,
        "report": report,
    }


def parse_history_file(
    path: str | Path,
    *,
    source_format: str = "auto",
    review_limit: int = 25,
) -> dict[str, Any]:
    loaded = load_history_file(path, source_format=source_format)
    return parse_history_rows(
        loaded["rows"],
        source_format=loaded["sourceFormat"],
        source_path=loaded["sourcePath"],
        review_limit=review_limit,
        source_diagnostics=loaded.get("diagnostics") or {},
    )


def build_parse_report(
    raw_rows: list[dict[str, Any]],
    legs: list[dict[str, Any]],
    *,
    source_path: str | None = None,
    review_limit: int = 25,
) -> dict[str, Any]:
    confidence = Counter(leg["parseConfidenceLabel"] for leg in legs)
    markets = Counter(leg.get("marketKey") or "unknown" for leg in legs)
    results = Counter(leg.get("resultStatus") or "unknown" for leg in legs)
    missing = Counter(
        note
        for leg in legs
        for note in leg.get("parseNotes") or []
        if _reportable_note(str(note))
    )
    ignored = Counter(
        field
        for leg in legs
        for field in leg.get("ignoredFields") or []
    )
    review = [
        _review_leg_sample(leg)
        for leg in legs
        if leg.get("needsReview")
    ][: max(0, int(review_limit))]
    return {
        "sourcePath": source_path,
        "rawRows": len(raw_rows),
        "parsedLegs": len(legs),
        "needsReview": sum(1 for leg in legs if leg.get("needsReview")),
        "trainingEligible": sum(1 for leg in legs if leg.get("trainingEligible")),
        "confidence": dict(confidence),
        "markets": dict(markets),
        "results": dict(results),
        "missingOrAmbiguous": dict(missing),
        "ignoredFields": dict(ignored),
        "ignoredFieldCount": sum(ignored.values()),
        "reviewSamples": review,
    }


def _parse_leg(record: dict[str, Any], *, source_row_number: int, leg_index: int) -> dict[str, Any]:
    clean = _clean_record(record)
    ignored_fields = _ignored_source_fields(clean)
    text = " ".join(
        str(value)
        for value in (
            _first(clean, SELECTION_KEYS),
            _first(clean, MARKET_KEYS),
            _first(clean, SIDE_KEYS),
        )
        if value not in (None, "")
    )
    market_name = _first(clean, MARKET_KEYS) or _infer_market_text(text)
    market_key, market_notes = normalize_market(market_name or text)
    side = normalize_side(_first(clean, SIDE_KEYS), fallback_text=text)
    line = parse_number(_first(clean, LINE_KEYS))
    if line is None:
        line = _infer_line(text, side)
    odds = parse_odds(_first(clean, ODDS_KEYS))
    if odds is None:
        odds = _infer_odds(text, side=side, line=line)
    source_result = normalize_result(_first(clean, RESULT_KEYS)) or _infer_result(text)
    ticket_id = _first(clean, TICKET_KEYS)
    player_name = clean_name(_first(clean, PLAYER_KEYS))
    if player_name is None:
        player_name = _infer_player_name(text, market_key=market_key, side=side, line=line)
    bet_date = parse_date(_first(clean, DATE_KEYS)) or _infer_date(text)
    ticket_odds = parse_odds(_first(clean, ("ticket_odds", "ticketodds", "sgm_multiplier", "sgmmultiplier")))
    actual_stat = parse_number(_first(clean, ACTUAL_STAT_KEYS))
    settled_result = settle_result(side=side, line=line, actual_stat=actual_stat)
    result = source_result or settled_result
    parsed = {
        "sourceRowNumber": source_row_number,
        "legIndex": leg_index,
        "parserVersion": PARSER_VERSION,
        "eligibilityVersion": ELIGIBILITY_VERSION,
        "ticketId": str(ticket_id) if ticket_id not in (None, "") else None,
        "ticketOdds": ticket_odds,
        "betDate": bet_date,
        "settledDate": parse_date(_first(clean, ("settled_at", "settled", "settlement_date"))),
        "sport": slug_key(_first(clean, ("sport", "league", "competition")) or "mlb") or "mlb",
        "league": _first(clean, ("league", "competition")) or "MLB",
        "playerName": player_name,
        "teamName": clean_name(_first(clean, TEAM_KEYS)),
        "opponentName": clean_name(_first(clean, OPPONENT_KEYS)),
        "matchup": clean_name(_first(clean, MATCHUP_KEYS)),
        "fixtureSlug": slug_key(_first(clean, ("fixture_slug", "fixture", "game_slug"))),
        "marketKey": market_key,
        "marketName": clean_name(market_name),
        "side": side,
        "line": line,
        "odds": odds,
        "stakeAmount": parse_number(_first(clean, STAKE_KEYS)),
        "payoutAmount": parse_number(_first(clean, PAYOUT_KEYS)),
        "resultStatus": result,
        "sourceResultStatus": source_result,
        "settledResultStatus": settled_result,
        "resultSource": "source" if source_result else "actual_stat_math" if settled_result else None,
        "actualStat": actual_stat,
        "liveAtImport": False,
        "ignoredFields": ignored_fields,
        "rawJson": record,
    }
    notes = _parse_notes(parsed, market_notes)
    confidence = _parse_confidence(parsed, notes)
    parsed["parseConfidence"] = confidence
    parsed["parseConfidenceLabel"] = confidence_label(confidence)
    parsed["needsReview"] = confidence < MEDIUM_CONFIDENCE or _requires_review(parsed, notes)
    parsed["trainingEligible"] = _training_eligible(parsed, notes)
    parsed["parseNotes"] = notes
    return parsed


def normalize_market(value: Any) -> tuple[str | None, list[str]]:
    text = str(value or "").strip()
    if not text:
        return None, ["missing_market"]
    key = slug_key(text)
    notes: list[str] = []
    if "run" in key and ("rbi" in key or "rbis" in key) and "hit" not in key:
        notes.append("unsupported_composite_market")
        return "runs_rbis", notes
    normalized = normalize_mlb_prop_market_key(text)
    if normalized == "strikeouts":
        notes.append("ambiguous_strikeouts_market")
        return normalized, notes
    if normalized in SUPPORTED_HISTORY_MARKETS:
        return normalized, notes
    notes.append("unknown_market")
    return None, notes


def normalize_side(value: Any, *, fallback_text: str = "") -> str | None:
    key = slug_key(value)
    if key in {"over", "o", "above", "more"}:
        return "over"
    if key in {"under", "u", "below", "less"}:
        return "under"
    text = f" {fallback_text} ".lower()
    if re.search(r"\b(over|o)\s*[0-9]", text):
        return "over"
    if re.search(r"\b(under|u)\s*[0-9]", text):
        return "under"
    if re.search(r"\b(over|under)\b", text):
        return "over" if "over" in text else "under"
    return None


def normalize_result(value: Any) -> str | None:
    key = slug_key(value)
    if not key:
        return None
    return RESULT_ALIASES.get(key, key)


def confidence_label(value: float) -> str:
    if value >= HIGH_CONFIDENCE:
        return "high"
    if value >= MEDIUM_CONFIDENCE:
        return "medium"
    return "low"


def bet_history_imports_dir(root_dir: str | Path = ".") -> Path:
    return Path(root_dir) / DEFAULT_IMPORT_DIR


def list_import_files(import_dir: str | Path) -> list[str]:
    directory = Path(import_dir)
    if not directory.exists():
        return []
    supported_suffixes = {".csv", ".json", ".jsonl", ".ndjson", ".txt", ".text"}
    return sorted(
        (
            path.name for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in supported_suffixes
        ),
        key=_natural_file_sort_key,
    )


def _natural_file_sort_key(file_name: str) -> list[int | str]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", file_name)
    ]


def sync_import_folder(
    store: GptActionStore,
    import_dir: str | Path,
    *,
    review_limit: int = 25,
) -> dict[str, Any]:
    directory = Path(import_dir)
    directory.mkdir(parents=True, exist_ok=True)
    file_names = list_import_files(directory)
    rows: list[dict[str, Any]] = []
    imported_files = 0
    duplicate_files = 0
    failed_files = 0
    refreshed_legs = 0

    for file_name in file_names:
        file_path = directory / file_name
        try:
            parsed = parse_history_file(file_path, review_limit=review_limit)
            saved = store.save_bet_history_import(parsed)
        except Exception as exc:
            failed_files += 1
            rows.append(
                {
                    "file": file_name,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            continue

        duplicate = bool(saved.get("duplicateSkipped"))
        refreshed = int(saved.get("refreshedLegs") or 0)
        refreshed_legs += refreshed
        duplicate_files += 1 if duplicate else 0
        imported_files += 0 if duplicate else 1
        rows.append(
            {
                "file": file_name,
                "status": "refreshed_duplicate" if duplicate and refreshed else "skipped_duplicate" if duplicate else "imported",
                "importId": saved.get("importId"),
                "rawRows": parsed.get("report", {}).get("rawRows") or 0,
                "parsedLegs": parsed.get("report", {}).get("parsedLegs") or 0,
                "trainingEligible": parsed.get("report", {}).get("trainingEligible") or 0,
                "needsReview": parsed.get("report", {}).get("needsReview") or 0,
                "duplicateReason": saved.get("duplicateReason"),
                "refreshedLegs": refreshed,
            }
        )

    history_report = store.bet_history_report(review_limit=review_limit)
    history_report["importFiles"] = file_names
    persistence = store.sync_bet_history_to_supabase(
        table_names=("bet_history_imports", "bet_history_raw", "bet_history_legs")
    )
    return {
        "sourcePath": str(directory),
        "filesConsidered": len(file_names),
        "filesImported": imported_files,
        "filesSkippedDuplicate": duplicate_files,
        "filesFailed": failed_files,
        "refreshedLegs": refreshed_legs,
        "persistence": persistence,
        "rows": rows,
        "history": history_report,
    }


def parse_odds(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        match = re.search(r"([+-]?\d+(?:\.\d+)?)", text)
        if not match:
            return None
        numeric = float(match.group(1))
    if text.startswith("+") or text.startswith("-"):
        if numeric > 0:
            return round(1 + numeric / 100, 4)
        if numeric < 0:
            return round(1 + 100 / abs(numeric), 4)
    if numeric > 0:
        return round(numeric, 4)
    return None


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def settle_result(
    *,
    side: str | None,
    line: float | None,
    actual_stat: float | None,
) -> str | None:
    if side not in {"over", "under"} or line is None or actual_stat is None:
        return None
    if actual_stat == line:
        return "push"
    if side == "under":
        return "won" if actual_stat < line else "lost"
    return "won" if actual_stat > line else "lost"


def parse_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    current_year = datetime.now().year
    for fmt in ("%a, %b %d", "%b %d"):
        try:
            parsed = datetime.strptime(f"{current_year} {text[:11]}", f"%Y {fmt}")
            return parsed.date().isoformat()
        except ValueError:
            continue
    return None


def clean_name(value: Any) -> str | None:
    text = str(value or "").strip()
    return re.sub(r"\s+", " ", text) if text else None


def _detect_format(path: Path, source_format: str) -> str:
    clean = str(source_format or "auto").lower()
    if clean not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported source format: {source_format}")
    if clean != "auto":
        return clean
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in {".jsonl", ".ndjson"}:
        return "jsonl"
    if suffix in {".txt", ".text"}:
        return "text"
    return "json"


def _load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _load_json(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return [row if isinstance(row, dict) else {"value": row} for row in payload]
    if isinstance(payload, dict):
        for key in ("bets", "history", "rows", "records", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row if isinstance(row, dict) else {"value": row} for row in value]
        return [payload]
    return [{"value": payload}]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        rows.append(value if isinstance(value, dict) else {"value": value})
    return rows


def _load_text(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = []
    raw_lines = path.read_text(encoding="utf-8-sig").splitlines()
    lines = [line.strip() for line in raw_lines if line.strip()]
    grouped, parsed_indexes = _stake_ui_text_rows(lines)
    if grouped:
        skipped_indexes = set(range(len(lines))) - parsed_indexes
        skipped_prop_like = sum(
            1 for index in skipped_indexes
            if re.match(r"^\s*(under|over|u|o)\b", lines[index], flags=re.IGNORECASE)
        )
        return grouped, {
            "textMode": "stake_ui_blocks",
            "inputLines": len(lines),
            "parsedBlocks": len(grouped),
            "skippedLines": len(skipped_indexes),
            "skippedPropLikeLines": skipped_prop_like,
        }
    for line_number, line in enumerate(raw_lines, start=1):
        text = line.strip()
        if text:
            rows.append({"selection": text, "source_line": line_number})
    return rows, {
        "textMode": "line_fallback",
        "inputLines": len(lines),
        "parsedBlocks": 0,
        "skippedLines": 0,
        "skippedPropLikeLines": 0,
    }


def _clean_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        slug_key(key).replace("-", "_"): value
        for key, value in (record or {}).items()
    }


def _leg_records(row: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("legs", "selections", "bets", "outcomes"):
        value = row.get(key)
        if isinstance(value, list):
            parent = {k: v for k, v in row.items() if k != key}
            return [
                {**parent, **item} if isinstance(item, dict) else {**parent, "selection": item}
                for item in value
            ]
    return [row]


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        normalized = slug_key(key).replace("-", "_")
        value = record.get(normalized)
        if value not in (None, ""):
            return value
    return None


def _stake_ui_text_rows(lines: list[str]) -> tuple[list[dict[str, Any]], set[int]]:
    default_date = _first_text_date(lines)
    default_matchup = _first_text_matchup(lines)
    current_date = default_date
    current_matchup = default_matchup
    rows: list[dict[str, Any]] = []
    parsed_indexes: set[int] = set()
    index = 0

    # SGM / parlay grouping state for pastes that list "N Leg Same Game Multi".
    # Giving correlated legs the same ticketId lets later analysis separate
    # ticket-level behavior from independent single-leg behavior.
    current_ticket: str | None = None
    legs_in_current_ticket = 0
    expected_legs_in_ticket = 0
    sgm_multiplier: float | None = None

    def _try_start_sgm_ticket(start_idx: int) -> None:
        nonlocal current_date, current_matchup
        nonlocal current_ticket, legs_in_current_ticket, expected_legs_in_ticket, sgm_multiplier
        if current_ticket:
            return
        for lb in range(0, min(7, start_idx + 1)):
            header_index = start_idx - lb
            txt = lines[header_index].strip().lower()
            if _is_ticket_header_line(txt):
                header_end = _next_market_line_index(lines, header_index + 1)
                header_lines = lines[header_index:header_end]
                if header_end < len(lines):
                    parsed_indexes.update(range(header_index, header_end))
                current_date = _first_text_date(header_lines) or default_date
                current_matchup = _first_text_matchup(header_lines) or default_matchup
                leg_match = re.search(r"\b(\d+)\s*leg\b", txt)
                expected_legs_in_ticket = int(leg_match.group(1)) if leg_match else 2
                sgm_multiplier = None
                for fwd in range(1, 5):
                    if header_index + fwd >= len(lines):
                        break
                    candidate = lines[header_index + fwd]
                    mult = parse_number(candidate)
                    if mult and "." in candidate and 1.0 < mult < MAX_TEXT_TICKET_ODDS:
                        sgm_multiplier = mult
                        break
                current_ticket = f"sgm_{current_date or 'd'}_{len(rows):03d}"
                legs_in_current_ticket = 0
                return

    while index < len(lines):
        market_line = lines[index]

        # Prime SGM ticket when we see a header, even before hitting the first Under line
        if _is_ticket_header_line(market_line):
            _try_start_sgm_ticket(index)

        if not re.match(r"^\s*(under|over|u|o)\b", market_line, flags=re.IGNORECASE):
            index += 1
            continue
        market_key, market_notes = normalize_market(market_line)
        side = normalize_side(None, fallback_text=market_line)
        line = _infer_line(market_line, side)
        if not market_key or market_notes or side is None or line is None:
            index += 1
            continue

        player_index = _next_non_numeric_line(lines, index + 1)
        if player_index is None:
            index += 1
            continue
        player = lines[player_index]
        if _looks_like_header_line(player):
            index += 1
            continue

        next_market_index = _next_leg_boundary_index(lines, player_index + 1)
        post_player_lines = lines[player_index + 1 : next_market_index]
        stat_values = [
            parse_number(value)
            for value in post_player_lines
            if _is_plain_number(value)
        ]
        stat_values = [value for value in stat_values if value is not None]
        actual_stat = stat_values[0] if stat_values else None
        settlement_target = stat_values[-1] if len(stat_values) > 1 else None

        # Opportunistic monetary extraction for richer pastes or future settled views.
        # The core settled block format prioritizes actual_stat for settlement verification,
        # but some copies include odds/stake nearby. We scan the block lines without
        # mistaking stat numbers (typically small 0-N integers) for odds/stakes.
        block_lines = lines[index:next_market_index]
        block_text = " ".join(block_lines)
        inferred_odds = _infer_odds(block_text, side=side, line=line)
        inferred_stake = _infer_labeled_amount(block_text, ("stake", "wager", "risk", "amount"))

        row = {
            "date": current_date,
            "matchup": current_matchup,
            "player": player,
            "market": market_line,
            "side": side,
            "line": line,
            "actual_stat": actual_stat,
            "settlement_target": settlement_target,
            "selection": f"{player} {market_line}",
        }
        if inferred_odds is not None:
            row["odds"] = inferred_odds
        if inferred_stake is not None:
            row["stake"] = inferred_stake

        # React better to explicit "Void" tokens in settled UI pastes (p2.txt SGM style).
        # "Void" often appears in place of a stat number for that leg.
        block_has_void = any(re.search(r"(?i)\bvoid\b", ln) for ln in post_player_lines)
        if block_has_void:
            row["result"] = "void"

        # Attach ticketId so legs from the same SGM are correlated.
        if current_ticket:
            row["ticket_id"] = current_ticket
            if sgm_multiplier is not None:
                row["sgm_multiplier"] = sgm_multiplier
                row["ticket_odds"] = sgm_multiplier
            legs_in_current_ticket += 1
            if expected_legs_in_ticket and legs_in_current_ticket >= expected_legs_in_ticket:
                current_ticket = None
                legs_in_current_ticket = 0
                expected_legs_in_ticket = 0
                sgm_multiplier = None

        rows.append(row)

        parsed_indexes.update(range(index, next_market_index))
        index = next_market_index

    return rows, parsed_indexes



def _first_text_date(lines: list[str]) -> str | None:
    for line in lines:
        parsed = parse_date(line) or _infer_date(line)
        if parsed:
            return parsed
    return None


def _first_text_matchup(lines: list[str]) -> str | None:
    for line in lines:
        matchup = _explicit_matchup_line(line)
        if matchup:
            return matchup
    for index in range(0, max(0, len(lines) - 3)):
        first, second, first_score, second_score = lines[index : index + 4]
        if (
            not _looks_like_header_line(first)
            and not _looks_like_header_line(second)
            and not _is_plain_number(first)
            and not _is_plain_number(second)
            and _is_plain_number(first_score)
            and _is_plain_number(second_score)
        ):
            return f"{first} - {second}"
    return None


def _explicit_matchup_line(value: str) -> str | None:
    text = clean_name(value)
    if not text or " - " not in text:
        return None
    left, right = [part.strip() for part in text.split(" - ", 1)]
    if not left or not right:
        return None
    if _is_plain_number(left) or _is_plain_number(right):
        return None
    if _looks_like_header_line(left) or _looks_like_header_line(right):
        return None
    return f"{left} - {right}"


def _next_non_numeric_line(lines: list[str], start: int) -> int | None:
    for index in range(start, len(lines)):
        if not _is_plain_number(lines[index]):
            return index
    return None


def _next_market_line_index(lines: list[str], start: int) -> int:
    for index in range(start, len(lines)):
        market_key, market_notes = normalize_market(lines[index])
        side = normalize_side(None, fallback_text=lines[index])
        line = _infer_line(lines[index], side)
        if market_key and not market_notes and side is not None and line is not None:
            return index
    return len(lines)


def _next_leg_boundary_index(lines: list[str], start: int) -> int:
    for index in range(start, len(lines)):
        if _is_ticket_header_line(lines[index]):
            return index
        market_key, market_notes = normalize_market(lines[index])
        side = normalize_side(None, fallback_text=lines[index])
        line = _infer_line(lines[index], side)
        if market_key and not market_notes and side is not None and line is not None:
            return index
    return len(lines)


def _is_ticket_header_line(value: str) -> bool:
    text = str(value or "").strip()
    return bool(re.search(r"(?i)(same.?game|sgm)", text))


def _is_plain_number(value: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", str(value).strip()))


def _looks_like_header_line(value: str) -> bool:
    key = slug_key(value)
    return (
        not key
        or bool(re.search(r"\b\d{1,2}:\d{2}\s*(am|pm)\b", str(value), flags=re.IGNORECASE))
        or key in {"stake-sports", "live", "hide-legs", "show-legs"}
        or key.startswith("sun-jun")
        or key.startswith("mon-jun")
        or key.startswith("tue-jun")
        or key.startswith("wed-jun")
        or key.startswith("thu-jun")
        or key.startswith("fri-jun")
        or key.startswith("sat-jun")
    )


def _infer_market_text(text: str) -> str | None:
    cleaned = clean_name(text)
    return cleaned


def _infer_line(text: str, side: str | None) -> float | None:
    if not side:
        return None
    pattern = r"\b(over|o|under|u)\s*([-+]?\d+(?:\.\d+)?)"
    for match in re.finditer(pattern, text.lower()):
        prefix = match.group(1)
        if (side == "over" and prefix in {"over", "o"}) or (side == "under" and prefix in {"under", "u"}):
            return float(match.group(2))
    return None


def _infer_odds(text: str, *, side: str | None = None, line: float | None = None) -> float | None:
    if side and line is not None:
        decimal = _infer_decimal_odds_after_line(text, side=side, line=line)
        if decimal is not None:
            return decimal
    match = re.search(r"(?<![\w.])([+-]\d{2,5})(?![\w.])", text)
    if match:
        return parse_odds(match.group(1))
    match = re.search(r"\bodds[:\s]+(\d+(?:\.\d+)?)\b", text.lower())
    if match:
        return parse_odds(match.group(1))
    return None


def _infer_labeled_amount(text: str, labels: tuple[str, ...]) -> float | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"\b(?:{label_pattern})\b\s*[:=]?\s*\$?\s*(\d+(?:\.\d+)?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return parse_number(match.group(1))


def _infer_decimal_odds_after_line(
    text: str,
    *,
    side: str,
    line: float,
) -> float | None:
    line_pattern = _line_pattern(line)
    side_pattern = r"under|u" if side == "under" else r"over|o"
    pattern = rf"\b(?:{side_pattern})\s*{line_pattern}\s+(\d+(?:\.\d+)?)\b"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    odds = parse_odds(match.group(1))
    if odds is None:
        return None
    return odds if 1.0 < odds < 1000.0 else None


def _line_pattern(line: float) -> str:
    normalized = f"{float(line):g}"
    if "." in normalized:
        integer, decimal = normalized.split(".", 1)
        return rf"{re.escape(integer)}(?:\.{re.escape(decimal)})?"
    return rf"{re.escape(normalized)}(?:\.0+)?"


def _infer_result(text: str) -> str | None:
    tokens = re.findall(r"[A-Za-z]+", text.lower())
    if not tokens:
        return None
    for token in reversed(tokens[-4:]):
        result = normalize_result(token)
        if result in {"won", "lost", "push", "void", "unsettled"}:
            return result
    return None


def _infer_date(text: str) -> str | None:
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if iso:
        return parse_date(iso.group(0))
    slash = re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text)
    if slash:
        return parse_date(slash.group(0))
    return None


def _infer_player_name(
    text: str,
    *,
    market_key: str | None,
    side: str | None,
    line: float | None,
) -> str | None:
    if not text or not market_key:
        return None
    marker = _market_marker_pattern(market_key)
    if not marker:
        return None
    match = re.search(marker, text, flags=re.IGNORECASE)
    if not match:
        return None
    prefix = text[: match.start()]
    prefix = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", prefix)
    prefix = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", " ", prefix)
    prefix = re.sub(r"\b(mlb|baseball|player|prop|selection)\b", " ", prefix, flags=re.IGNORECASE)
    prefix = re.sub(r"[^A-Za-z.' -]+", " ", prefix)
    name = clean_name(prefix)
    if not name or len(name.split()) < 2:
        return None
    if side and side in slug_key(name).split("-"):
        return None
    if line is not None and str(line) in name:
        return None
    return name


def _market_marker_pattern(market_key: str) -> str | None:
    patterns = {
        "hits": r"\b(player\s+)?hits?\b",
        "singles": r"\b(player\s+)?singles?\b",
        "total_bases": r"\b(player\s+)?total\s+bases?\b",
        "home_runs": r"\b(player\s+)?home\s+runs?\b",
        "rbi": r"\b(player\s+)?rbi(?:s|s\b)?\b|runs\s+batted\s+in",
        "runs": r"\b(player\s+)?runs?\b",
        "hits_runs_rbis": r"\bhrr\b|hits?\s*\+?\s*runs?\s*\+?\s*rbi(?:s)?",
        "batter_walks": r"\b(batter\s+)?walks?\b|bases?\s+on\s+balls",
        "batter_strikeouts": r"\b(batter\s+)?(?:strikeouts?|ks?)\b",
        "strikeouts": r"\b(?:strikeouts?|ks?)\b",
        "stolen_bases": r"\bstolen\s+bases?\b|\bsteals?\b",
        "pitcher_strikeouts": r"\bpitcher\s+(?:strikeouts?|ks?)\b|\bpitching\s+(?:strikeouts?|ks?)\b",
        "outs_recorded": r"\bouts\s+recorded\b|\bpitcher\s+outs\b",
        "hits_allowed": r"\bhits\s+allowed\b",
        "earned_runs": r"\bearned\s+runs?(?:\s+allowed)?\b",
        "walks_allowed": r"\bwalks?\s+allowed\b|\bpitcher\s+walks?\b",
    }
    return patterns.get(market_key)


def _ignored_source_fields(record: dict[str, Any]) -> list[str]:
    return sorted(
        key for key in record
        if key not in CANONICAL_SOURCE_KEYS_NORMALIZED
    )


def _parse_notes(parsed: dict[str, Any], market_notes: list[str]) -> list[str]:
    notes = list(market_notes)
    if not parsed.get("betDate"):
        notes.append("missing_date")
    if not (parsed.get("playerName") or parsed.get("teamName")):
        notes.append("missing_player_or_team")
    if not parsed.get("marketKey"):
        notes.append("missing_market")
    if not parsed.get("side"):
        notes.append("missing_side")
    if parsed.get("line") is None:
        notes.append("missing_line")
    if parsed.get("odds") is None:
        notes.append("missing_odds")
    if not parsed.get("resultStatus"):
        notes.append("missing_result")
    if parsed.get("marketKey") and parsed["marketKey"] not in SUPPORTED_HISTORY_MARKETS:
        notes.append("unsupported_market_for_training")
    if parsed.get("settledResultStatus") and not parsed.get("sourceResultStatus"):
        notes.append("result_inferred_from_actual_stat")
    if (
        parsed.get("settledResultStatus")
        and parsed.get("sourceResultStatus")
        and parsed.get("sourceResultStatus") != "void"
        and parsed["settledResultStatus"] != parsed["sourceResultStatus"]
    ):
        notes.append("result_actual_mismatch")
    return sorted(set(notes))


def _parse_confidence(parsed: dict[str, Any], notes: list[str]) -> float:
    score = 0.0
    score += 0.12 if parsed.get("betDate") else 0
    score += 0.14 if parsed.get("playerName") or parsed.get("teamName") else 0
    score += 0.18 if parsed.get("marketKey") else 0
    score += 0.12 if parsed.get("side") else 0
    score += 0.10 if parsed.get("line") is not None else 0
    score += 0.12 if parsed.get("odds") is not None else 0
    score += 0.14 if parsed.get("resultStatus") else 0
    score += 0.08 if parsed.get("actualStat") is not None else 0
    score += 0.04 if parsed.get("stakeAmount") is not None else 0
    score += 0.04 if parsed.get("payoutAmount") is not None else 0
    if any(note.startswith("ambiguous_") for note in notes):
        score -= 0.10
    if "unknown_market" in notes:
        score -= 0.12
    if "unsupported_composite_market" in notes:
        score -= 0.06
    return round(max(0.0, min(1.0, score)), 4)


def _requires_review(parsed: dict[str, Any], notes: list[str]) -> bool:
    blockers = {
        "missing_player_or_team",
        "missing_market",
        "missing_side",
        "missing_line",
        "missing_result",
        "ambiguous_strikeouts_market",
        "unknown_market",
        "unsupported_market_for_training",
        "result_actual_mismatch",
    }
    return bool(set(notes) & blockers)


def _training_eligible(parsed: dict[str, Any], notes: list[str]) -> bool:
    if parsed.get("needsReview"):
        return False
    if parsed.get("marketKey") not in SUPPORTED_HISTORY_MARKETS:
        return False
    if parsed.get("side") not in {"over", "under"}:
        return False
    if parsed.get("line") is None:
        return False
    if not parsed.get("betDate"):
        return False
    if not (parsed.get("playerName") or parsed.get("teamName")):
        return False
    # Voids are acceptable historical outcomes (stake returned, no decision).
    return parsed.get("resultStatus") in {"won", "lost", "push", "void"}


def _reportable_note(note: str) -> bool:
    return (
        note.startswith("missing_")
        or note.startswith("ambiguous_")
        or note in {
            "unknown_market",
            "unsupported_composite_market",
            "unsupported_market_for_training",
        }
    )


def _review_leg_sample(leg: dict[str, Any]) -> dict[str, Any]:
    return {
        "sourceRowNumber": leg.get("sourceRowNumber"),
        "legIndex": leg.get("legIndex"),
        "playerName": leg.get("playerName"),
        "teamName": leg.get("teamName"),
        "marketName": leg.get("marketName"),
        "marketKey": leg.get("marketKey"),
        "side": leg.get("side"),
        "line": leg.get("line"),
        "odds": leg.get("odds"),
        "resultStatus": leg.get("resultStatus"),
        "sourceResultStatus": leg.get("sourceResultStatus"),
        "settledResultStatus": leg.get("settledResultStatus"),
        "resultSource": leg.get("resultSource"),
        "actualStat": leg.get("actualStat"),
        "trainingEligible": bool(leg.get("trainingEligible")),
        "ignoredFields": leg.get("ignoredFields") or [],
        "parseConfidence": leg.get("parseConfidence"),
        "parseNotes": leg.get("parseNotes") or [],
    }


def _raw_text(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=True, default=str)


def format_report(
    report: dict[str, Any],
    *,
    storage_path: str | Path | None = None,
    import_dir: str | Path | None = None,
) -> str:
    lines = [
        "Bet Historic",
        "-----------",
        f"Source: {report.get('sourcePath') or 'inline rows'}",
    ]
    if storage_path:
        lines.append(f"Database: {_display_path(storage_path)}")
    if import_dir:
        lines.append(f"Import folder: {_display_path(import_dir)}")
    lines.extend(
        [
            "",
            f"Raw source rows: {report.get('rawRows') or 0}",
            f"Parsed legs: {report.get('parsedLegs') or 0}",
            f"Needs review: {report.get('needsReview') or 0}",
            f"Training eligible: {report.get('trainingEligible') or 0}",
            f"Confidence: {report.get('confidence') or {}}",
            f"Markets: {report.get('markets') or {}}",
            f"Results: {report.get('results') or {}}",
        ]
    )
    if report.get("missingOrAmbiguous"):
        lines.append(f"Missing/ambiguous: {report['missingOrAmbiguous']}")
    if report.get("ignoredFields"):
        lines.append(f"Ignored UI/source fields: {report['ignoredFields']}")
    enrichment = report.get("enrichment") or {}
    if enrichment:
        lines.append(
            "Historical enrichment: "
            f"{enrichment.get('legEnrichments') or 0} legs, "
            f"{enrichment.get('snapshots') or 0} frozen MLB game snapshots, "
            f"{enrichment.get('gradedBySnapshot') or 0} snapshot grades"
        )
    diagnostics = report.get("parseDiagnostics") or {}
    if diagnostics:
        lines.extend(
            [
                f"Parser version: {report.get('parserVersion') or PARSER_VERSION}",
                (
                    "Text diagnostics: "
                    f"mode={diagnostics.get('textMode') or 'n/a'}, "
                    f"inputLines={diagnostics.get('inputLines') or 0}, "
                    f"parsedBlocks={diagnostics.get('parsedBlocks') or 0}, "
                    f"skippedLines={diagnostics.get('skippedLines') or 0}, "
                    f"skippedPropLikeLines={diagnostics.get('skippedPropLikeLines') or 0}"
                ),
            ]
        )
    import_files = list(report.get("importFiles") or [])
    if not report.get("rawRows") and import_dir:
        lines.extend(
            [
                "",
                "No bet historic has been imported yet.",
            ]
        )
        if not import_files:
            lines.extend(
                [
                    "Drop CSV, JSON, JSONL, or raw TXT exports into the import folder, then run:",
                    f"  historic {_display_path(import_dir)}\\your-file.txt --dry-run",
                    f"  historic {_display_path(import_dir)}\\your-file.txt",
                ]
            )
    if import_files and import_dir:
        lines.extend(["", "Pending import files:" if not report.get("rawRows") else "Import folder files:"])
        for file_name in import_files:
            lines.append(f"- {file_name}")
        if not report.get("rawRows"):
            first_file = import_files[0]
            lines.extend(
                [
                    "",
                    "Run:",
                    f"  historic {_display_path(import_dir)}\\{first_file} --dry-run",
                    f"  historic {_display_path(import_dir)}\\{first_file}",
                ]
            )
    if report.get("reviewSamples"):
        lines.extend(["", "Review samples:"])
        for sample in report["reviewSamples"]:
            lines.append(
                f"- row {sample['sourceRowNumber']} leg {sample['legIndex']}: "
                f"{sample.get('playerName') or sample.get('teamName') or 'unknown'} "
                f"{sample.get('marketKey') or 'unknown'} {sample.get('side') or '?'} "
                f"{sample.get('line') if sample.get('line') is not None else '?'} "
                f"eligible={bool(sample.get('trainingEligible'))} "
                f"notes={sample.get('parseNotes') or []}"
            )
    return "\n".join(lines)


def format_backtest_report(report: dict[str, Any]) -> str:
    filters = report.get("filters") or {}
    view = str(filters.get("view") or "dashboard")
    overall = report.get("overall") or {}
    tickets = report.get("tickets") or {}
    ticket_overall = tickets.get("overall") or {}
    lines = [
        "Bet Historic Analysis",
        "---------------------",
        *_format_backtest_filters(filters),
        "",
    ]

    if view == "tickets":
        lines.extend(_format_ticket_backtest_section(tickets, detailed=True))
        return "\n".join(lines)

    if view == "signals":
        lines.extend(_format_signal_section(report.get("signals") or {}, detailed=True))
        return "\n".join(lines)

    if view == "calibration":
        lines.extend(_format_calibration_section(report.get("calibration") or {}, detailed=True))
        return "\n".join(lines)

    if view == "legs":
        lines.append(_format_backtest_bucket("Leg performance", overall))
        by_market = list(report.get("byMarket") or [])
        if by_market:
            lines.extend(["", "By market:"])
            for row in by_market[:12]:
                lines.append(_format_backtest_bucket(str(row.get("label") or "unknown"), row))
        by_side = list(report.get("bySide") or [])
        if by_side:
            lines.extend(["", "By side:"])
            for row in by_side:
                lines.append(_format_backtest_bucket(str(row.get("label") or "unknown"), row))
        if report.get("notes"):
            lines.extend(["", "Notes:"])
            lines.extend(f"- {note}" for note in report["notes"])
        return "\n".join(lines)

    lines.extend(
        [
            "Automated flow:",
            *_format_automated_flow(report.get("flow") or []),
            "",
            _format_backtest_bucket("Leg performance", overall),
            _format_ticket_bucket("Ticket performance", ticket_overall),
            _format_enrichment_backtest_summary(report.get("enrichment") or {}),
            "",
            "Top leg signals:",
        ]
    )
    by_market = list(((report.get("signals") or {}).get("byMarket")) or report.get("byMarket") or [])
    if by_market:
        for row in by_market[:12]:
            lines.append(_format_signal_row(row))
    else:
        lines.append("- No eligible leg signals yet.")

    contributors = ((report.get("signals") or {}).get("ticketFailureContributors") or {}).get("byMarket") or []
    if contributors:
        lines.extend(["", "Ticket failure contributors:"])
        for row in contributors[:8]:
            lines.append(f"- {row.get('label')}: losing legs {row.get('losingLegs') or 0}")

    enriched_bucket_lines = _format_enriched_bucket_preview(report.get("enrichedBuckets") or {})
    if enriched_bucket_lines:
        lines.extend(["", "Enriched / ticket-structure buckets:", *enriched_bucket_lines])

    calibration_rows = ((report.get("calibration") or {}).get("marketSideLine") or [])
    if calibration_rows:
        lines.extend(["", "Calibration preview:"])
        for row in calibration_rows[:8]:
            lines.append(_format_calibration_row(row))

    lines.extend(["", *_format_final_outcome(report.get("finalOutcome") or {})])

    lines.extend(
        [
            "",
            "Commands:",
            "  analysis legs --market hits",
            "  analysis tickets --from-date 2026-06-01",
            "  analysis signals --player \"Max Muncy\"",
            "  analysis calibration --market singles",
            "  historic enrich --missing-only",
        ]
    )
    return "\n".join(lines)


def format_backtest_rich_report(report: dict[str, Any], *, width: int = 140) -> str:
    from io import StringIO

    from rich.console import Console

    buffer = StringIO()
    console = Console(file=buffer, width=width, force_terminal=False, color_system=None)
    print_backtest_rich_report(report, console=console)
    return buffer.getvalue()


def print_backtest_rich_report(report: dict[str, Any], *, console: Any | None = None) -> None:
    from rich.console import Console

    rich_console = console or Console(color_system="truecolor", width=_backtest_rich_console_width())
    rich_console.print(build_backtest_rich_renderable(report))


def build_backtest_rich_renderable(report: dict[str, Any]) -> Any:
    from rich import box
    from rich.columns import Columns
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    filters = report.get("filters") or {}
    outcome = report.get("finalOutcome") or {}
    history_quality = outcome.get("historyQuality") or {}
    readiness = outcome.get("modelReadiness") or {}
    leg_sample = outcome.get("legSample") or {}
    tickets = report.get("tickets") or {}
    ticket_sample = outcome.get("ticketSample") or {}
    ticket_overall = tickets.get("overall") or {}
    enrichment = report.get("enrichment") or {}
    enriched_buckets = report.get("enrichedBuckets") or {}
    signals = report.get("signals") or {}
    calibration = report.get("calibration") or {}
    contributors = (signals.get("ticketFailureContributors") or {}).get("byMarket") or []
    calibration_rows = calibration.get("marketSideLine") or []

    title = Table.grid(expand=True)
    title.add_column(ratio=1)
    title.add_column(justify="right")
    title.add_row(
        Text("Stake-GPT Historic Analysis", style="bold #F1EED0"),
        Text(f"View: {filters.get('view') or 'dashboard'}", style="#7F7F7F"),
    )
    title.add_row(
        Text("Historic performance dashboard", style="#7F7F7F"),
        Text("Generated from history storage", style="#7F7F7F"),
    )

    header = Panel(
        title,
        border_style="#5A5A5A",
        box=box.ROUNDED,
        padding=(1, 2),
    )

    column_width = 52
    outcome_panel = _rich_panel(
        "Final Outcome",
        _rich_kv_table(
            [
                ("Verdict", _compact_verdict(outcome.get("verdict"))),
                ("Quality", history_quality.get("label") or "n/a"),
                ("Readiness", readiness.get("label") or "n/a"),
            ]
        ),
        width=column_width,
    )
    legs_panel = _rich_panel(
        "Leg Sample",
        _rich_kv_table(
            [
                ("Graded", leg_sample.get("gradedLegs") or 0),
                ("Hit rate", _percent(leg_sample.get("hitRate"))),
                ("Per-leg odds", leg_sample.get("oddsLegs") or 0),
                ("Missing odds", leg_sample.get("missingOdds") or 0),
            ]
        ),
        width=column_width,
    )
    tickets_panel = _rich_panel(
        "Ticket Sample",
        _rich_kv_table(
            [
                ("Tickets", f"{ticket_sample.get('gradedTickets') or 0}/{ticket_sample.get('tickets') or 0}"),
                ("Hit rate", _percent(ticket_sample.get("hitRate"))),
                ("ROI", _percent(ticket_sample.get("roi"))),
                ("Profit/unit", ticket_overall.get("profitPerUnit")),
            ]
        ),
        width=column_width,
    )

    strongest_panel = _rich_panel(
        "Strongest Markets",
        _rich_market_table(outcome.get("strongestMarkets") or []),
        width=column_width,
    )
    failure_panel = _rich_panel(
        "Ticket Failures",
        _rich_failure_table(contributors),
        width=column_width,
    )
    calibration_panel = _rich_panel(
        "Calibration Preview",
        _rich_calibration_table(calibration_rows),
        width=column_width,
    )

    filters_panel = _rich_panel(
        "Filters",
        _rich_kv_table(
            [
                ("Market", filters.get("marketKey") or "all"),
                ("Side", filters.get("side") or "all"),
                ("Player", filters.get("playerName") or "all"),
                ("From date", filters.get("fromDate") or "all historic"),
                ("Ticket", filters.get("ticketId") or "all tickets"),
            ]
        ),
        width=column_width,
    )
    enrichment_panel = _rich_panel(
        "Historical Enrichment",
        _rich_kv_table(
            [
                ("Status", enrichment.get("status") or "unknown"),
                ("Coverage", _percent(enrichment.get("coverageRate"))),
                ("Snapshots", enrichment.get("snapshotGames") or 0),
                ("Mismatches", enrichment.get("resultMismatches") or 0),
            ]
        ),
        width=column_width,
    )
    warnings_panel = _rich_panel(
        "Warnings",
        _rich_lines(outcome.get("warnings") or ["No active warnings."]),
        width=column_width,
    )
    context_panel = _rich_panel(
        "Context Buckets",
        _rich_context_bucket_table(enriched_buckets),
        width=column_width,
    )
    next_action_panel = _rich_panel(
        "Next Action",
        _rich_lines([outcome.get("nextAction") or "No next action available."]),
    )

    return Group(
        header,
        "",
        Columns([outcome_panel, legs_panel, tickets_panel], equal=True, expand=True),
        "",
        Columns([strongest_panel, failure_panel, calibration_panel], equal=True, expand=True),
        "",
        Columns([filters_panel, enrichment_panel, context_panel], equal=True, expand=True),
        "",
        warnings_panel,
        "",
        next_action_panel,
    )


def _rich_panel(title: str, renderable: Any, *, width: int | None = None) -> Any:
    from rich import box
    from rich.panel import Panel

    return Panel(
        renderable,
        title=f"[#F1EED0]{title}[/]",
        border_style="#5A5A5A",
        box=box.ROUNDED,
        padding=(1, 2),
        width=width,
    )


def _rich_kv_table(rows: list[tuple[str, Any]]) -> Any:
    from rich.table import Table

    table = Table.grid(expand=True, padding=(0, 2))
    table.add_column(style="#7F7F7F", no_wrap=True)
    table.add_column(style="#B8B19C", ratio=2)
    for label, value in rows:
        table.add_row(str(label), str(value if value is not None else "n/a"))
    return table


def _backtest_rich_console_width() -> int | None:
    import os

    try:
        width = int(os.environ.get("STAKE_GPT_BACKTEST_RICH_WIDTH") or "0")
    except ValueError:
        return None
    return width if 100 <= width <= 240 else None


def _rich_market_table(rows: list[dict[str, Any]]) -> Any:
    from rich.table import Table

    table = Table(box=None, expand=True, show_edge=False, header_style="#A46214")
    table.add_column("Market", style="#B8B19C")
    table.add_column("Graded", justify="right", style="#7F7F7F")
    table.add_column("Hit", justify="right", style="#F1EED0")
    table.add_column("Signal", style="#7F7F7F")
    if not rows:
        table.add_row("No market signals yet.", "", "", "")
        return table
    for row in rows[:6]:
        table.add_row(
            _compact_market_label(row.get("market") or "unknown"),
            str(row.get("gradedLegs") or 0),
            _percent(row.get("hitRate")),
            str(row.get("signal") or "n/a"),
        )
    return table


def _rich_failure_table(rows: list[dict[str, Any]]) -> Any:
    from rich.table import Table

    table = Table(box=None, expand=True, show_edge=False, header_style="#A46214")
    table.add_column("Market", style="#B8B19C")
    table.add_column("Losing legs", justify="right", style="#F1EED0")
    if not rows:
        table.add_row("No failure contributors yet.", "")
        return table
    for row in rows[:6]:
        table.add_row(_compact_market_label(row.get("label") or "unknown"), str(row.get("losingLegs") or 0))
    return table


def _rich_calibration_table(rows: list[dict[str, Any]]) -> Any:
    from rich.table import Table

    table = Table(box=None, expand=True, show_edge=False, header_style="#A46214")
    table.add_column("Market", style="#B8B19C", no_wrap=True)
    table.add_column("Line", justify="right", style="#7F7F7F", no_wrap=True)
    table.add_column("Hit", justify="right", style="#F1EED0")
    table.add_column("Adj", justify="right", style="#7F7F7F")
    table.add_column("Status", style="#7F7F7F")
    if not rows:
        table.add_row("No buckets", "", "", "", "")
        return table
    for row in rows[:6]:
        adjustment = row.get("recommendedAdjustment")
        adjustment_text = f"{adjustment:+.4f}" if isinstance(adjustment, (int, float)) else "n/a"
        market, line = _compact_calibration_label(row.get("label"))
        table.add_row(
            market,
            line,
            _percent(row.get("hitRate")),
            adjustment_text,
            _compact_status_label(row.get("status")),
        )
    return table


def _rich_context_bucket_table(report: dict[str, Any]) -> Any:
    from rich.table import Table

    table = Table(box=None, expand=True, show_edge=False, header_style="#A46214")
    table.add_column("Bucket", style="#7F7F7F", no_wrap=True)
    table.add_column("Top", style="#B8B19C")
    table.add_column("Hit", justify="right", style="#F1EED0", no_wrap=True)
    rows = [
        ("Lineup", report.get("byLineupSpot") or []),
        ("Starter", report.get("byStarterRole") or []),
        ("Pitch", report.get("byPitchHand") or []),
        ("Venue", report.get("byVenue") or []),
        ("Longshot", report.get("byLongshotOdds") or []),
        ("Legs", report.get("byLegCount") or []),
    ]
    added = False
    for label, bucket_rows in rows:
        if not bucket_rows:
            continue
        top = bucket_rows[0]
        table.add_row(label, str(top.get("label") or "unknown"), _percent(top.get("hitRate")))
        added = True
    if not added:
        table.add_row("Coverage", f"{_percent(report.get('coverage'))} enriched", "")
    return table


def _rich_lines(lines: list[str]) -> Any:
    from rich.text import Text

    text = Text(style="#7F7F7F")
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        text.append(f"- {line}", style="#7F7F7F")
    return text


def _compact_verdict(verdict: Any) -> str:
    text = str(verdict or "n/a")
    marker = ": current sample shows "
    if marker in text:
        return text.split(marker, 1)[1]
    return text


def _compact_calibration_label(label: Any) -> tuple[str, str]:
    parts = [part.strip() for part in str(label or "unknown").split("|")]
    market = _compact_market_label(parts[0] if parts else "unknown")
    side = parts[1][:1].lower() if len(parts) > 1 and parts[1] else "?"
    line = parts[2].replace("line", "").strip() if len(parts) > 2 else "?"
    return market, f"{side}{line}"


def _compact_market_label(label: Any) -> str:
    text = str(label or "unknown").strip()
    aliases = {
        "batter_strikeouts": "batter Ks",
        "batter_walks": "walks",
        "total_bases": "total bases",
        "home_runs": "home runs",
        "hits_allowed": "hits allowed",
        "earned_runs": "earned runs",
        "rbi": "RBI",
    }
    return aliases.get(text, text.replace("_", " "))


def _compact_status_label(status: Any) -> str:
    text = str(status or "unknown").strip()
    aliases = {
        "missing_odds": "no odds",
        "low_sample": "low sample",
    }
    return aliases.get(text, text.replace("_", " "))


def format_imports_report(imports: list[dict[str, Any]]) -> str:
    lines = ["Bet Historic Imports", "--------------------"]
    if not imports:
        lines.append("No imports found.")
        return "\n".join(lines)
    for item in imports:
        lines.append(
            f"{item.get('importId')} | {item.get('importedAt')} | "
            f"legs={item.get('parsedLegs') or 0} review={item.get('needsReview') or 0} | "
            f"{item.get('sourcePath') or 'unknown source'}"
        )
    return "\n".join(lines)


def format_sync_report(report: dict[str, Any], *, storage_path: str | Path | None = None) -> str:
    file_names = list(report.get("history", {}).get("importFiles") or [])
    latest_files = file_names[-8:]
    latest_text = ", ".join(latest_files) if latest_files else "none"
    lines = [
        "Bet Historic Sync",
        "----------------",
        f"Import folder: {_display_path(report.get('sourcePath') or DEFAULT_IMPORT_DIR)}",
        f"Files checked: {report.get('filesConsidered') or 0}",
        f"Latest checked files: {latest_text}",
        f"Imported: {report.get('filesImported') or 0}",
        f"Skipped duplicates: {report.get('filesSkippedDuplicate') or 0}",
        f"Refreshed duplicate legs: {report.get('refreshedLegs') or 0}",
        f"Failed: {report.get('filesFailed') or 0}",
    ]
    lines.extend(_format_persistence_lines(report.get("persistence")))
    rows = list(report.get("rows") or [])
    if rows:
        lines.extend(["", "Files:"])
        for row in rows:
            status = row.get("status") or "unknown"
            if status == "imported":
                lines.append(
                    f"- imported {row.get('file')}: "
                    f"{row.get('parsedLegs') or 0} legs, "
                    f"{row.get('trainingEligible') or 0} training eligible"
                )
            elif status == "skipped_duplicate":
                lines.append(f"- skipped duplicate {row.get('file')}")
            elif status == "refreshed_duplicate":
                lines.append(
                    f"- refreshed duplicate {row.get('file')}: "
                    f"{row.get('refreshedLegs') or 0} legs updated"
                )
            else:
                lines.append(f"- failed {row.get('file')}: {row.get('error') or 'unknown error'}")

    lines.extend(
        [
            "",
            format_report(
                report.get("history") or {},
                storage_path=storage_path,
                import_dir=report.get("sourcePath") or DEFAULT_IMPORT_DIR,
            ),
        ]
    )
    return "\n".join(lines)


def format_update_report(report: dict[str, Any], *, rich_analysis: bool = False) -> str:
    sync = report.get("sync") or {}
    enrich = report.get("enrich") or {}
    analysis = report.get("analysis") or {}
    lines = [
        "Bet Historic Update",
        "------------------",
        "Flow: sync imports -> enrich missing MLB snapshots -> analyze updated history",
        f"Files checked: {sync.get('filesConsidered') or 0}",
        f"Imported: {sync.get('filesImported') or 0}",
        f"Skipped duplicates: {sync.get('filesSkippedDuplicate') or 0}",
        f"Refreshed duplicate legs: {sync.get('refreshedLegs') or 0}",
        f"Import failures: {sync.get('filesFailed') or 0}",
        f"Enrichment targets: {enrich.get('targets') or 0}",
        f"Legs enriched: {enrich.get('legsEnriched') or 0}",
        f"Legs skipped: {enrich.get('legsSkipped') or 0}",
        f"Snapshots created/reused: {enrich.get('snapshotsCreated') or 0}/{enrich.get('snapshotsReused') or 0}",
        f"Result mismatches: {enrich.get('resultMismatches') or 0}",
    ]
    lines.extend(_format_persistence_lines(enrich.get("persistence") or sync.get("persistence")))
    enrichment = analysis.get("enrichment") or {}
    outcome = analysis.get("finalOutcome") or {}
    ticket_sample = outcome.get("ticketSample") or {}
    lines.extend(
        [
            "",
            "Updated analysis:",
            f"Historical enrichment: {enrichment.get('status') or 'unknown'} "
            f"({_percent(enrichment.get('coverageRate'))} coverage)",
            f"Tickets: {ticket_sample.get('gradedTickets') or 0}/{ticket_sample.get('tickets') or 0} graded, "
            f"hit {_percent(ticket_sample.get('hitRate'))}, ROI {_percent(ticket_sample.get('roi'))}",
            f"Readiness: {(outcome.get('modelReadiness') or {}).get('label') or 'unknown'}",
            f"Next action: {outcome.get('nextAction') or 'none'}",
        ]
    )
    if rich_analysis:
        lines.extend(["", "Rich analysis:", format_backtest_rich_report(analysis)])
    else:
        lines.extend(["", "Analysis:", format_backtest_report(analysis)])
    return "\n".join(lines)


def format_storage_sync_report(report: dict[str, Any]) -> str:
    lines = [
        "Bet Historic Storage",
        "-------------------",
        f"Action: {report.get('action') or 'unknown'}",
    ]
    if report.get("pull"):
        lines.extend(_format_persistence_lines(report["pull"], label="Supabase pull"))
    if report.get("push"):
        lines.extend(_format_persistence_lines(report["push"], label="Supabase push"))
    if not report.get("pull") and not report.get("push"):
        lines.append("No storage operation ran.")
    return "\n".join(lines)


def _format_persistence_lines(report: Any, *, label: str = "Supabase history") -> list[str]:
    if not isinstance(report, dict):
        return []
    if report.get("enabled") is False:
        return [f"{label}: disabled ({report.get('reason') or 'not configured'})"]
    if report.get("error"):
        return [f"{label}: failed ({report.get('error')})"]
    if report.get("skipped"):
        return [f"{label}: skipped ({report.get('reason') or 'already current'})"]
    if report.get("direction") == "sqlite_to_supabase":
        return [f"{label}: pushed {report.get('rowsPushed') or 0} rows"]
    if report.get("direction") == "supabase_to_sqlite":
        return [f"{label}: pulled {report.get('rowsPulled') or 0} rows"]
    return []


def format_review_report(report: dict[str, Any]) -> str:
    lines = [
        "Bet Historic Review",
        "------------------",
        f"Import filter: {report.get('importId') or 'all imports'}",
        f"Needs review shown: {report.get('needsReview') or 0}",
        f"Reasons: {report.get('reasonCounts') or {}}",
    ]
    rows = list(report.get("reviewRows") or [])
    if not rows:
        lines.append("")
        lines.append("No review rows found.")
        return "\n".join(lines)
    lines.append("")
    for row in rows:
        subject = row.get("playerName") or row.get("teamName") or "unknown"
        line = row.get("line") if row.get("line") is not None else "?"
        lines.append(
            f"- row {row.get('sourceRowNumber') or '?'}: {subject} "
            f"{row.get('marketKey') or 'unknown'} {row.get('side') or '?'} {line} "
            f"status={row.get('status')}"
        )
        if row.get("matchup"):
            lines.append(f"  matchup: {row['matchup']}")
        lines.append(f"  reasons: {row.get('reasons') or []}")
        lines.append(f"  repair: {row.get('repairPolicy') or 'Review source text before training.'}")
    return "\n".join(lines)


def _format_backtest_bucket(label: str, row: dict[str, Any]) -> str:
    hit_rate = _percent(row.get("hitRate"))
    roi = _percent(row.get("roi"))
    profit = row.get("profitPerUnit")
    odds_text = (
        f"ROI {roi}, profit/unit {profit}"
        if row.get("roiEligible")
        else "ROI n/a (no odds in source)"
    )

    # Stake-aware / realized info when available from the import source.
    stake_text = ""
    if row.get("totalStaked"):
        rroi = _percent(row.get("realizedRoi"))
        tprofit = row.get("totalProfit")
        tstaked = row.get("totalStaked")
        stake_text = f", staked {tstaked}, profit {tprofit}, realizedRoi {rroi}"
    elif row.get("stakeAwareEligible"):
        stake_text = f", stakeAware {row.get('stakeAwareEligible')}"

    return (
        f"{label}: legs {row.get('legs') or 0}, graded {row.get('gradedLegs') or 0}, "
        f"W-L-P-V {row.get('won') or 0}-{row.get('lost') or 0}-"
        f"{row.get('push') or 0}-{row.get('void') or 0}, "
        f"hit {hit_rate}, per-leg odds {row.get('oddsLegs') or 0}, "
        f"ROI eligible {row.get('roiEligible') or 0}, "
        f"missing per-leg odds {row.get('roiExcludedMissingOdds') or 0}"
        f"{stake_text}, {odds_text}"
    )


def _format_backtest_filters(filters: dict[str, Any]) -> list[str]:
    return [
        f"View: {filters.get('view') or 'dashboard'}",
        f"Market filter: {filters.get('marketKey') or 'all'}",
        f"Side filter: {filters.get('side') or 'all'}",
        f"Player filter: {filters.get('playerName') or 'all'}",
        f"From date: {filters.get('fromDate') or 'all historic'}",
        f"Ticket filter: {filters.get('ticketId') or 'all tickets'}",
        f"Import filter: {filters.get('importId') or 'all imports (combined)'}",
    ]


def _format_enrichment_backtest_summary(enrichment: dict[str, Any]) -> str:
    if not enrichment:
        return "Historical enrichment: unavailable"
    status = enrichment.get("status") or "unknown"
    coverage = _percent(enrichment.get("coverageRate"))
    return (
        "Historical enrichment: "
        f"{status}, {enrichment.get('enrichedLegs') or 0}/{enrichment.get('legs') or 0} legs, "
        f"{enrichment.get('snapshotGames') or 0} frozen MLB game snapshots, "
        f"coverage {coverage}, result mismatches {enrichment.get('resultMismatches') or 0}"
    )


def _format_automated_flow(flow: list[dict[str, Any]]) -> list[str]:
    if not flow:
        return ["- No flow metadata available."]
    return [
        f"- {str(row.get('step') or 'step').replace('_', ' ')}: "
        f"{row.get('status') or 'unknown'} - {row.get('summary') or ''}"
        for row in flow
    ]


def _format_final_outcome(outcome: dict[str, Any]) -> list[str]:
    if not outcome:
        return ["Final Outcome", "-------------", "No final outcome available."]
    history_quality = outcome.get("historyQuality") or {}
    leg_sample = outcome.get("legSample") or {}
    ticket_sample = outcome.get("ticketSample") or {}
    calibration = outcome.get("calibrationStatus") or {}
    readiness = outcome.get("modelReadiness") or {}
    lines = [
        "Final Outcome",
        "-------------",
        f"Verdict: {outcome.get('verdict') or 'n/a'}",
        f"Historic quality: {history_quality.get('label') or 'n/a'} - {history_quality.get('reason') or 'n/a'}",
        (
            f"Leg sample: graded {leg_sample.get('gradedLegs') or 0}, "
            f"hit {_percent(leg_sample.get('hitRate'))}, "
            f"per-leg odds {leg_sample.get('oddsLegs') or 0}, "
            f"missing per-leg odds {leg_sample.get('missingOdds') or 0}"
        ),
        (
            f"Ticket sample: {ticket_sample.get('status') or 'n/a'}, "
            f"graded {ticket_sample.get('gradedTickets') or 0}/{ticket_sample.get('tickets') or 0}, "
            f"hit {_percent(ticket_sample.get('hitRate'))}, ROI {_percent(ticket_sample.get('roi'))}"
        ),
        f"Calibration: {calibration.get('label') or 'n/a'} - {calibration.get('reason') or 'n/a'}",
        f"Model readiness: {readiness.get('label') or 'n/a'} - {readiness.get('reason') or 'n/a'}",
    ]
    strongest = list(outcome.get("strongestMarkets") or [])
    weakest = list(outcome.get("weakestMarkets") or [])
    if strongest:
        lines.extend(["Strongest markets:", *_format_market_rank_rows(strongest)])
    if weakest:
        lines.extend(["Weakest markets:", *_format_market_rank_rows(weakest)])
    warnings = list(outcome.get("warnings") or [])
    if warnings:
        lines.extend(["Warnings:", *(f"- {warning}" for warning in warnings)])
    if outcome.get("nextAction"):
        lines.append(f"Next action: {outcome['nextAction']}")
    return lines


def _format_market_rank_rows(rows: list[dict[str, Any]]) -> list[str]:
    output: list[str] = []
    for row in rows[:5]:
        signal = row.get("signal") or "n/a"
        sample_warning = row.get("sampleWarning")
        sample = f", sample {sample_warning}" if sample_warning and sample_warning != signal else ""
        output.append(
            f"- {row.get('market') or 'unknown'}: hit {_percent(row.get('hitRate'))}, "
            f"graded {row.get('gradedLegs') or 0}, signal {signal}{sample}"
        )
    return output


def _format_ticket_backtest_section(report: dict[str, Any], *, detailed: bool = False) -> list[str]:
    lines = [_format_ticket_bucket("Ticket performance", report.get("overall") or {})]
    rows = list(report.get("ticketRows") or [])
    if not rows:
        lines.extend(["", "No ticketId-backed SGM/multi tickets found."])
        return lines
    lines.extend(["", "Tickets:"])
    for row in rows[:25 if detailed else 8]:
        lines.append(_format_ticket_row(row))
    contributors = (report.get("failureContributors") or {}).get("byMarket") or []
    if contributors:
        lines.extend(["", "Failure contributors by market:"])
        for row in contributors[:12 if detailed else 6]:
            lines.append(f"- {row.get('label')}: losing legs {row.get('losingLegs') or 0}")
    if detailed and report.get("notes"):
        lines.extend(["", "Notes:"])
        lines.extend(f"- {note}" for note in report["notes"])
    return lines


def _format_ticket_bucket(label: str, row: dict[str, Any]) -> str:
    return (
        f"{label}: tickets {row.get('tickets') or 0}, SGM/multi {row.get('sgmTickets') or 0}, "
        f"graded {row.get('gradedTickets') or 0}, W-L-P-V "
        f"{row.get('won') or 0}-{row.get('lost') or 0}-"
        f"{row.get('push') or 0}-{row.get('void') or 0}, "
        f"hit {_percent(row.get('hitRate'))}, ROI eligible {row.get('roiEligible') or 0}, "
        f"missing ticket odds {row.get('missingTicketOdds') or 0}, "
        f"adjusted-odds excluded {row.get('roiExcludedAdjustedOdds') or 0}, "
        f"ROI {_percent(row.get('roi'))}, profit/unit {row.get('profitPerUnit')}"
    )


def _format_ticket_row(row: dict[str, Any]) -> str:
    matchup = f", {row.get('matchup')}" if row.get("matchup") else ""
    odds = row.get("ticketOdds") if row.get("ticketOdds") is not None else "?"
    blocker = f", ROI blocker {row.get('roiBlocker')}" if row.get("roiBlocker") else ""
    losing = row.get("losingLegs") or []
    losing_text = ""
    if losing:
        losing_text = " | losing: " + "; ".join(
            f"{leg.get('subject')} {leg.get('marketKey')} {leg.get('side')} {leg.get('line')}"
            for leg in losing[:3]
        )
    return (
        f"- {row.get('ticketId')}: {row.get('resultStatus')} legs {row.get('legs')}, "
        f"W-L-P-V {row.get('won') or 0}-{row.get('lost') or 0}-"
        f"{row.get('push') or 0}-{row.get('void') or 0}, odds {odds}{blocker}"
        f"{matchup}{losing_text}"
    )


def _format_signal_section(report: dict[str, Any], *, detailed: bool = False) -> list[str]:
    lines = ["Signals by market:"]
    by_market = list(report.get("byMarket") or [])
    if by_market:
        lines.extend(_format_signal_row(row) for row in by_market[:20 if detailed else 8])
    else:
        lines.append("- No market signals yet.")

    lines.extend(["", "Signals by player-market:"])
    player_market = list(report.get("byPlayerMarket") or [])
    if player_market:
        lines.extend(_format_signal_row(row) for row in player_market[:25 if detailed else 8])
    else:
        lines.append("- No player-market signals yet.")

    lines.extend(["", "Signals by line bucket:"])
    line_bucket = list(report.get("byLineBucket") or [])
    if line_bucket:
        lines.extend(_format_signal_row(row) for row in line_bucket[:25 if detailed else 8])
    else:
        lines.append("- No line-bucket signals yet.")

    market_line = list(report.get("byMarketLine") or [])
    if market_line:
        lines.extend(["", "Signals by market + line:"])
        lines.extend(_format_signal_row(row) for row in market_line[:25 if detailed else 8])

    under_only = report.get("underOnly") or {}
    under_market = list(under_only.get("byMarket") or [])
    if under_market:
        lines.extend(["", "Under-only market signals:"])
        lines.extend(_format_signal_row(row) for row in under_market[:25 if detailed else 8])

    contributors = (report.get("ticketFailureContributors") or {}).get("byPlayerMarket") or []
    if contributors:
        lines.extend(["", "Ticket failure contributors by player-market:"])
        for row in contributors[:20 if detailed else 8]:
            lines.append(f"- {row.get('label')}: losing legs {row.get('losingLegs') or 0}")

    if report.get("warnings"):
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    return lines


def _format_signal_row(row: dict[str, Any]) -> str:
    sample = f", sample={row.get('sampleWarning')}" if row.get("sampleWarning") else ""
    return (
        f"- {row.get('label')}: graded {row.get('gradedLegs') or 0}, "
        f"W-L {row.get('won') or 0}-{row.get('lost') or 0}, "
        f"hit {_percent(row.get('hitRate'))}, BE {_percent(row.get('averageBreakEvenRate'))}, "
        f"signal {row.get('signal') or 'n/a'}{sample}"
    )


def _format_calibration_section(report: dict[str, Any], *, detailed: bool = False) -> list[str]:
    lines = ["Calibration by market-side-line:"]
    market_side_line = list(report.get("marketSideLine") or [])
    if market_side_line:
        lines.extend(_format_calibration_row(row) for row in market_side_line[:25 if detailed else 8])
    else:
        lines.append("- No calibration buckets yet.")

    lines.extend(["", "Calibration by market-side:"])
    market_side = list(report.get("marketSide") or [])
    if market_side:
        lines.extend(_format_calibration_row(row) for row in market_side[:20 if detailed else 8])
    else:
        lines.append("- No market-side calibration yet.")

    if detailed:
        lines.extend(["", "Calibration by player-market:"])
        player_market = list(report.get("playerMarket") or [])
        if player_market:
            lines.extend(_format_calibration_row(row) for row in player_market[:25])
        else:
            lines.append("- No player-market calibration yet.")

    if report.get("notes"):
        lines.extend(["", "Notes:"])
        lines.extend(f"- {note}" for note in report["notes"])
    return lines


def _format_calibration_row(row: dict[str, Any]) -> str:
    adjustment = row.get("recommendedAdjustment")
    adjustment_text = f"{adjustment:+.4f}" if isinstance(adjustment, (int, float)) else "n/a"
    return (
        f"- {row.get('label')}: graded {row.get('gradedLegs') or 0}, "
        f"hit {_percent(row.get('hitRate'))}, BE {_percent(row.get('averageBreakEvenRate'))}, "
        f"edge {_signed_percent(row.get('historicalEdge'))}, adjustment {adjustment_text}, "
        f"status {row.get('status') or 'unknown'}"
    )


def _format_enriched_bucket_preview(report: dict[str, Any]) -> list[str]:
    if not report:
        return []
    lines = [
        (
            f"- enrichment coverage {_percent(report.get('coverage'))}; "
            f"enriched legs {report.get('enrichedLegs') or 0}"
        )
    ]
    groups = [
        ("lineup", report.get("byLineupSpot") or []),
        ("starter", report.get("byStarterRole") or []),
        ("pitch hand", report.get("byPitchHand") or []),
        ("venue", report.get("byVenue") or []),
        ("longshot odds", report.get("byLongshotOdds") or []),
        ("leg count", report.get("byLegCount") or []),
    ]
    for label, rows in groups:
        if rows:
            top = rows[0]
            lines.append(
                f"- {label}: {top.get('label')} | graded {top.get('gradedLegs') or 0}, "
                f"hit {_percent(top.get('hitRate'))}, signal {top.get('signal') or 'n/a'}"
            )
    return lines


def _signed_percent(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:+.1f}%"


def _percent(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _print_report(
    report: dict[str, Any],
    *,
    storage_path: str | Path | None = None,
    import_dir: str | Path | None = None,
) -> None:
    print(format_report(report, storage_path=storage_path, import_dir=import_dir))


def _display_path(path: str | Path) -> str:
    value = Path(path)
    if value.is_absolute():
        return str(value)
    try:
        return str((Path.cwd() / value).resolve())
    except OSError:
        return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import and normalize historical MLB bet historic with decimal odds."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="Parse and import a CSV/JSON/JSONL/text historic file.")
    import_parser.add_argument("path", help="CSV, JSON, JSONL, or raw text bet historic file.")
    import_parser.add_argument("--format", choices=sorted(SUPPORTED_FORMATS), default="auto")
    import_parser.add_argument("--db-path", default=None)
    import_parser.add_argument("--dry-run", action="store_true")
    import_parser.add_argument("--force", action="store_true")
    import_parser.add_argument("--json", action="store_true", dest="as_json")
    import_parser.add_argument("--review-limit", type=int, default=25)

    report_parser = subparsers.add_parser("report", help="Show aggregate imported historic status.")
    report_parser.add_argument("--db-path", default=None)
    report_parser.add_argument("--review-limit", type=int, default=25)
    report_parser.add_argument("--json", action="store_true", dest="as_json")

    sync_parser = subparsers.add_parser(
        "sync",
        help="Import new files from the bet-history imports folder, skip duplicates, then show status.",
    )
    sync_parser.add_argument("--db-path", default=None)
    sync_parser.add_argument("--import-dir", dest="import_dir", default=None)
    sync_parser.add_argument("--review-limit", type=int, default=25)
    sync_parser.add_argument("--json", action="store_true", dest="as_json")

    update_parser = subparsers.add_parser(
        "update",
        aliases=["auto", "pipeline"],
        help="Sync new historic files, enrich missing MLB snapshots, then run updated analysis.",
    )
    update_parser.add_argument("--db-path", default=None)
    update_parser.add_argument("--import-dir", dest="import_dir", default=None)
    update_parser.add_argument("--from-date", dest="from_date", default=None)
    update_parser.add_argument("--review-limit", type=int, default=25)
    update_parser.add_argument("--enrich-limit", type=int, default=500)
    update_parser.add_argument("--skip-enrich", action="store_true")
    update_parser.add_argument("--rich", action="store_true", help="Include a Rich boxed analysis dashboard.")
    update_parser.add_argument("--json", action="store_true", dest="as_json")

    review_parser = subparsers.add_parser("review", help="Show rows that need manual review before training.")
    review_parser.add_argument("--db-path", default=None)
    review_parser.add_argument("--import-id", dest="import_id", default=None)
    review_parser.add_argument("--limit", type=int, default=50)
    review_parser.add_argument("--json", action="store_true", dest="as_json")

    enrich_parser = subparsers.add_parser(
        "enrich",
        help="Fetch and store frozen historical MLB context snapshots for imported bet historic.",
    )
    enrich_parser.add_argument("--db-path", default=None)
    enrich_parser.add_argument("--import-id", dest="import_id", default=None)
    enrich_parser.add_argument("--from-date", dest="from_date", default=None)
    enrich_parser.add_argument("--missing-only", action="store_true")
    enrich_parser.add_argument("--limit", type=int, default=500)
    enrich_parser.add_argument("--json", action="store_true", dest="as_json")

    backtest_parser = subparsers.add_parser(
        "analysis",
        aliases=["backtest"],
        help="Summarize historic hit-rate, ticket ROI, signals, and calibration.",
    )
    backtest_parser.add_argument(
        "view",
        nargs="?",
        choices=["dashboard", "legs", "tickets", "signals", "calibration"],
        default="dashboard",
        help="Historic analysis view to show. Default: dashboard.",
    )
    backtest_parser.add_argument("--db-path", default=None)
    backtest_parser.add_argument("--market", default=None)
    backtest_parser.add_argument("--side", choices=["over", "under"], default=None)
    backtest_parser.add_argument("--player", dest="player_name", default=None)
    backtest_parser.add_argument("--from-date", dest="from_date", default=None)
    backtest_parser.add_argument("--ticket", dest="ticket_id", default=None)
    backtest_parser.add_argument("--import-id", dest="import_id", default=None,
                                help="Limit analysis to legs from a specific import (see 'historic imports').")
    backtest_parser.add_argument("--limit", type=int, default=10000)
    backtest_parser.add_argument("--rich", action="store_true", help="Render a boxed Rich dashboard.")
    backtest_parser.add_argument("--json", action="store_true", dest="as_json")

    imports_parser = subparsers.add_parser("imports", help="List saved bet historic imports.")
    imports_parser.add_argument("--db-path", default=None)
    imports_parser.add_argument("--limit", type=int, default=50)
    imports_parser.add_argument("--json", action="store_true", dest="as_json")

    storage_parser = subparsers.add_parser(
        "storage",
        help="Sync Supabase-backed historic storage with the local SQLite backup/cache.",
    )
    storage_parser.add_argument("action", nargs="?", choices=["pull", "push", "sync"], default="sync")
    storage_parser.add_argument("--db-path", default=None)
    storage_parser.add_argument("--json", action="store_true", dest="as_json")

    delete_parser = subparsers.add_parser("delete-import", help="Delete one saved bet historic import.")
    delete_parser.add_argument("import_id")
    delete_parser.add_argument("--db-path", default=None)
    delete_parser.add_argument("--yes", action="store_true")
    delete_parser.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args()
    if args.command == "import":
        parsed = parse_history_file(args.path, source_format=args.format, review_limit=args.review_limit)
        result: dict[str, Any] = {"dryRun": bool(args.dry_run), "report": parsed["report"]}
        storage_path = None
        if not args.dry_run:
            store = GptActionStore(args.db_path)
            storage_path = store.db_path
            result["import"] = store.save_bet_history_import(parsed, force=args.force)
        if args.as_json:
            print(json.dumps(result, indent=2, ensure_ascii=True, default=str))
        else:
            _print_report(parsed["report"], storage_path=storage_path)
            if args.dry_run:
                print()
                print("Dry run: nothing was saved.")
            else:
                print()
                if result["import"].get("duplicateSkipped"):
                    print(f"Duplicate import skipped: {result['import']['importId']}")
                    print("No rows were added.")
                else:
                    print(f"Saved import: {result['import']['importId']}")
        return 0

    if args.command == "report":
        store = GptActionStore(args.db_path)
        import_dir = bet_history_imports_dir(Path.cwd())
        import_dir.mkdir(parents=True, exist_ok=True)
        report = store.bet_history_report(review_limit=args.review_limit)
        report["importFiles"] = list_import_files(import_dir)
        if args.as_json:
            print(json.dumps(report, indent=2, ensure_ascii=True, default=str))
        else:
            _print_report(report, storage_path=store.db_path, import_dir=import_dir)
        return 0

    if args.command == "sync":
        store = GptActionStore(args.db_path)
        import_dir = Path(args.import_dir) if args.import_dir else bet_history_imports_dir(Path.cwd())
        report = sync_import_folder(store, import_dir, review_limit=args.review_limit)
        if args.as_json:
            print(json.dumps(report, indent=2, ensure_ascii=True, default=str))
        else:
            print(format_sync_report(report, storage_path=store.db_path))
        return 0

    if args.command in {"update", "auto", "pipeline"}:
        store = GptActionStore(args.db_path)
        import_dir = Path(args.import_dir) if args.import_dir else bet_history_imports_dir(Path.cwd())
        sync_report = sync_import_folder(store, import_dir, review_limit=args.review_limit)
        enrich_report: dict[str, Any]
        if args.skip_enrich:
            enrich_report = {
                "targets": 0,
                "legsEnriched": 0,
                "legsSkipped": 0,
                "snapshotsCreated": 0,
                "snapshotsReused": 0,
                "resultMismatches": 0,
                "skipReasons": {},
                "notes": ["Historical enrichment was skipped by request."],
                "storeSummary": store.bet_history_enrichment_report(),
            }
        else:
            async def _run_update_enrichment() -> dict[str, Any]:
                async with build_mlb_http_client() as http_client:
                    engine = MLBDataEngine(MLBStatsClient(http_client))
                    return await enrich_bet_history(
                        store=store,
                        mlb_engine=engine,
                        from_date=args.from_date,
                        missing_only=True,
                        limit=args.enrich_limit,
                    )

            enrich_report = asyncio.run(_run_update_enrichment())
        analysis_report = store.bet_history_backtest(from_date=args.from_date)
        update_report = {
            "sync": sync_report,
            "enrich": enrich_report,
            "analysis": analysis_report,
            "notes": [
                "This deterministic update flow never uses live MLB context inside analysis; enrichment snapshots are stored first.",
                "Imported ticket results remain canonical. Enriched boxscore grades are verification/offline-learning fields.",
            ],
        }
        if args.as_json:
            print(json.dumps(update_report, indent=2, ensure_ascii=True, default=str))
        else:
            print(format_update_report(update_report, rich_analysis=args.rich))
        return 0

    if args.command in {"analysis", "backtest"}:
        store = GptActionStore(args.db_path)
        report = store.bet_history_backtest(
            market_key=args.market,
            side=args.side,
            player_name=args.player_name,
            from_date=args.from_date,
            ticket_id=args.ticket_id,
            import_id=args.import_id,
            limit=args.limit,
            view=args.view,
        )
        if args.as_json:
            print(json.dumps(report, indent=2, ensure_ascii=True, default=str))
        elif args.rich:
            print_backtest_rich_report(report)
        else:
            print(format_backtest_report(report))
        return 0

    if args.command == "enrich":
        store = GptActionStore(args.db_path)

        async def _run_enrichment() -> dict[str, Any]:
            async with build_mlb_http_client() as http_client:
                engine = MLBDataEngine(MLBStatsClient(http_client))
                return await enrich_bet_history(
                    store=store,
                    mlb_engine=engine,
                    import_id=args.import_id,
                    from_date=args.from_date,
                    missing_only=args.missing_only,
                    limit=args.limit,
                )

        report = asyncio.run(_run_enrichment())
        if args.as_json:
            print(json.dumps(report, indent=2, ensure_ascii=True, default=str))
        else:
            print(format_enrichment_report(report))
        return 0

    if args.command == "review":
        store = GptActionStore(args.db_path)
        report = store.bet_history_review(import_id=args.import_id, limit=args.limit)
        if args.as_json:
            print(json.dumps(report, indent=2, ensure_ascii=True, default=str))
        else:
            print(format_review_report(report))
        return 0

    if args.command == "imports":
        store = GptActionStore(args.db_path)
        imports = store.list_bet_history_imports(limit=args.limit)
        if args.as_json:
            print(json.dumps(imports, indent=2, ensure_ascii=True, default=str))
        else:
            print(format_imports_report(imports))
        return 0

    if args.command == "storage":
        store = GptActionStore(args.db_path)
        result: dict[str, Any] = {"action": args.action}
        if args.action in {"pull", "sync"}:
            result["pull"] = store.sync_bet_history_from_supabase(force=True)
        if args.action in {"push", "sync"}:
            result["push"] = store.sync_bet_history_to_supabase()
        if args.as_json:
            print(json.dumps(result, indent=2, ensure_ascii=True, default=str))
        else:
            print(format_storage_sync_report(result))
        return 0

    if args.command == "delete-import":
        if not args.yes:
            result = {
                "deleted": False,
                "importId": args.import_id,
                "reason": "confirmation_required",
                "nextCommand": f"historic delete-import {args.import_id} --yes",
            }
        else:
            store = GptActionStore(args.db_path)
            result = store.delete_bet_history_import(args.import_id)
        if args.as_json:
            print(json.dumps(result, indent=2, ensure_ascii=True, default=str))
        else:
            if result.get("deleted"):
                print(
                    f"Deleted import {result['importId']}: "
                    f"{result['legsDeleted']} legs, {result['rawRowsDeleted']} raw rows."
                )
            elif result.get("reason") == "confirmation_required":
                print("Delete not run. Add --yes to confirm:")
                print(f"  historic delete-import {args.import_id} --yes")
            else:
                print(f"Delete not run: {result.get('reason') or 'unknown'}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
