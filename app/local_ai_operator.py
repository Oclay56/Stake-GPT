from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:8b"


def local_ai_base_url() -> str:
    return (
        os.getenv("STAKE_GPT_LOCAL_AI_URL")
        or os.getenv("AZP_LOCAL_AI_URL")
        or DEFAULT_OLLAMA_URL
    ).rstrip("/")


def local_ai_model() -> str:
    return (
        os.getenv("STAKE_GPT_LOCAL_AI_MODEL")
        or os.getenv("AZP_LOCAL_AI_MODEL")
        or DEFAULT_OLLAMA_MODEL
    ).strip()


def run_ai_flow(
    *,
    root_dir: Path = ROOT_DIR,
    model: str | None = None,
    base_url: str | None = None,
    from_date: str | None = None,
    skip_enrich: bool = False,
    skip_ai: bool = False,
) -> dict[str, Any]:
    clean_model = (model or local_ai_model()).strip() or DEFAULT_OLLAMA_MODEL
    clean_base_url = (base_url or local_ai_base_url()).rstrip("/")

    update_args = ["update", "--json"]
    if from_date:
        update_args.extend(["--from-date", from_date])
    if skip_enrich:
        update_args.append("--skip-enrich")

    analysis_args = ["analysis", "--json"]
    if from_date:
        analysis_args.extend(["--from-date", from_date])

    update = _run_json_command(root_dir, update_args)
    analysis = _run_json_command(root_dir, analysis_args)
    model_report = _run_json_command(root_dir, ["model", "train", "--json"])
    context = build_ai_flow_context(
        update=update,
        analysis=analysis,
        model_report=model_report,
    )

    ai_summary = ""
    ai_error = ""
    if not skip_ai:
        try:
            ai_summary = summarize_with_ollama(
                context,
                model=clean_model,
                base_url=clean_base_url,
            )
        except Exception as exc:  # pragma: no cover - local service availability varies.
            ai_error = str(exc)
    if not ai_summary:
        ai_summary = fallback_summary(context)

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "provider": "ollama",
        "model": clean_model,
        "baseUrl": clean_base_url,
        "fromDate": from_date,
        "skipEnrich": skip_enrich,
        "localAiUsed": bool(ai_summary and not ai_error and not skip_ai),
        "localAiError": ai_error,
        "commands": context["commands"],
        "historic": context["historic"],
        "analysis": context["analysis"],
        "ml": context["ml"],
        "summary": ai_summary,
        "guardrails": [
            "Local AI runs the maintenance workflow only.",
            "Python remains responsible for parsing, enrichment, analysis, and M/L validation.",
            "Local AI does not choose picks, override validation, or place bets.",
        ],
    }


def build_ai_flow_context(
    *,
    update: dict[str, Any],
    analysis: dict[str, Any],
    model_report: dict[str, Any],
) -> dict[str, Any]:
    update_json = update.get("json") or {}
    analysis_json = analysis.get("json") or {}
    model_json = model_report.get("json") or {}

    sync = update_json.get("sync") or {}
    history = sync.get("history") or {}
    enrich = update_json.get("enrich") or {}
    dataset = update_json.get("dataset") or {}
    dataset_readiness = dataset.get("readiness") or {}
    analysis_enrichment = analysis_json.get("enrichment") or {}
    final_outcome = analysis_json.get("finalOutcome") or {}
    ticket_sample = final_outcome.get("ticketSample") or {}
    model_readiness = final_outcome.get("modelReadiness") or {}
    model_validation = model_json.get("validation") or {}
    model_holdout = ((model_json.get("metrics") or {}).get("holdout") or {})

    return {
        "commands": [
            _command_summary("historic update", update),
            _command_summary("analysis", analysis),
            _command_summary("m/l train", model_report),
        ],
        "historic": {
            "filesConsidered": _int(sync.get("filesConsidered")),
            "filesImported": _int(sync.get("filesImported")),
            "filesSkippedDuplicate": _int(sync.get("filesSkippedDuplicate")),
            "filesFailed": _int(sync.get("filesFailed")),
            "refreshedLegs": _int(sync.get("refreshedLegs")),
            "parsedLegs": _int(history.get("parsedLegs")),
            "trainingEligible": _int(history.get("trainingEligible")),
            "needsReview": _int(history.get("needsReview")),
            "enrichTargets": _int(enrich.get("targets")),
            "legsEnriched": _int(enrich.get("legsEnriched")),
            "enrichmentCoverage": _float(analysis_enrichment.get("coverageRate")),
            "datasetRows": _int(dataset.get("rows")),
            "datasetTrainingRows": _int(dataset.get("trainingRows")),
            "datasetEnrichedRows": _int(dataset.get("enrichedRows")),
            "datasetReadiness": dataset_readiness.get("label") or "unknown",
        },
        "analysis": {
            "verdict": final_outcome.get("verdict") or "unknown",
            "historicQuality": (final_outcome.get("historyQuality") or {}).get("label") or "unknown",
            "ticketSample": {
                "tickets": _int(ticket_sample.get("tickets")),
                "gradedTickets": _int(ticket_sample.get("gradedTickets")),
                "hitRate": _float(ticket_sample.get("hitRate")),
                "roi": _float(ticket_sample.get("roi")),
            },
            "modelReadiness": {
                "status": model_readiness.get("status") or "unknown",
                "label": model_readiness.get("label") or "unknown",
                "reason": model_readiness.get("reason") or "",
            },
            "warnings": list(final_outcome.get("warnings") or [])[:6],
            "nextAction": final_outcome.get("nextAction") or "",
        },
        "ml": {
            "modelVersion": model_json.get("modelVersion") or "unknown",
            "trainingRows": _int(model_json.get("trainingRows")),
            "holdoutRows": _int(model_json.get("holdoutRows")),
            "globalPrior": _float(model_json.get("globalPrior")),
            "holdoutBrierLift": _float(model_holdout.get("brierImprovement")),
            "rankSpread": _float(model_holdout.get("rankSpread")),
            "validation": model_validation.get("label") or "unknown",
            "canInfluenceBuilds": bool(model_validation.get("canInfluenceBuilds")),
            "reason": model_validation.get("reason") or "",
        },
    }


def summarize_with_ollama(
    context: dict[str, Any],
    *,
    model: str,
    base_url: str,
    timeout_seconds: int = 240,
) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "You are the local Stake-GPT maintenance operator. Summarize the "
                "Historic, Analysis, and M/L maintenance run. Do not choose picks, "
                "recommend bets, override validation, or invent data. Be concise."
            ),
        },
        {
            "role": "user",
            "content": (
                "Summarize this maintenance context in 5 to 7 short lines. "
                "Use these labels exactly: Ran, Data, Analysis, M/L, Blocker, Next. "
                "If no blocker exists, write Blocker: none.\n\n"
                + json.dumps(context, ensure_ascii=True, indent=2)
            ),
        },
    ]
    content = ollama_chat(
        messages,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    return _clean_model_text(content)


def ollama_chat(
    messages: list[dict[str, str]],
    *,
    model: str,
    base_url: str,
    timeout_seconds: int = 240,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 700,
        },
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach local AI at {base_url}: {exc}") from exc
    message = data.get("message") or {}
    return str(message.get("content") or "").strip()


def fallback_summary(context: dict[str, Any]) -> str:
    historic = context.get("historic") or {}
    analysis = context.get("analysis") or {}
    tickets = analysis.get("ticketSample") or {}
    ml = context.get("ml") or {}
    blocker = ml.get("reason") or (analysis.get("modelReadiness") or {}).get("reason") or "none"
    return "\n".join(
        [
            "Ran: Historic update, Analysis, and M/L baseline training completed.",
            (
                f"Data: {historic.get('datasetRows', 0)} dataset rows, "
                f"{historic.get('datasetTrainingRows', 0)} training rows, "
                f"{_percent(historic.get('enrichmentCoverage'))} enrichment coverage."
            ),
            (
                f"Analysis: {tickets.get('gradedTickets', 0)}/{tickets.get('tickets', 0)} "
                f"graded tickets, ROI {_percent(tickets.get('roi'))}."
            ),
            (
                f"M/L: {ml.get('trainingRows', 0)}/{ml.get('holdoutRows', 0)} train/holdout, "
                f"Brier lift {ml.get('holdoutBrierLift')}, rank spread {_percent(ml.get('rankSpread'))}."
            ),
            f"Blocker: {blocker}",
            f"Next: {analysis.get('nextAction') or 'Keep importing settled, strategy-consistent tickets.'}",
        ]
    )


def format_ai_flow_report(report: dict[str, Any]) -> str:
    historic = report.get("historic") or {}
    analysis = report.get("analysis") or {}
    ml = report.get("ml") or {}
    lines = [
        "Stake-GPT Local AI Flow",
        "-----------------------",
        f"Provider: {report.get('provider')} | Model: {report.get('model')}",
        (
            "Historic: "
            f"files {historic.get('filesConsidered', 0)}, "
            f"imported {historic.get('filesImported', 0)}, "
            f"dataset {historic.get('datasetRows', 0)} rows"
        ),
        (
            "Analysis: "
            f"{analysis.get('historicQuality')}, "
            f"tickets {(analysis.get('ticketSample') or {}).get('gradedTickets', 0)}/"
            f"{(analysis.get('ticketSample') or {}).get('tickets', 0)}, "
            f"ROI {_percent((analysis.get('ticketSample') or {}).get('roi'))}"
        ),
        (
            "M/L: "
            f"{ml.get('validation')} | "
            f"canInfluenceBuilds={bool(ml.get('canInfluenceBuilds'))}"
        ),
    ]
    if report.get("localAiError"):
        lines.append(f"Local AI: fallback summary used ({report['localAiError']})")
    else:
        lines.append(f"Local AI: {'used' if report.get('localAiUsed') else 'fallback summary used'}")
    lines.extend(["", "Summary:", str(report.get("summary") or "").strip()])
    return "\n".join(lines).rstrip()


def _run_json_command(root_dir: Path, args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "app.bet_history", *args],
        cwd=root_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = (completed.stdout or "").strip()
    parsed: dict[str, Any] | None = None
    parse_error = ""
    if output:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            parse_error = str(exc)
    return {
        "args": args,
        "exitCode": int(completed.returncode),
        "json": parsed or {},
        "parseError": parse_error,
        "outputPreview": output[:3000],
    }


def _command_summary(name: str, command: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "args": list(command.get("args") or []),
        "exitCode": _int(command.get("exitCode")),
        "parseError": command.get("parseError") or "",
    }


def _clean_model_text(text: str) -> str:
    clean = str(text or "").strip()
    if "<think>" in clean and "</think>" in clean:
        before, _, tail = clean.partition("</think>")
        clean = tail.strip() if before else clean
    return "\n".join(line.rstrip() for line in clean.splitlines() if line.strip())


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _percent(value: Any) -> str:
    numeric = _float(value)
    if numeric is None:
        return "n/a"
    return f"{numeric * 100:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the local AI operator over Historic, Analysis, and M/L maintenance."
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--from-date", default=None)
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    report = run_ai_flow(
        root_dir=ROOT_DIR,
        model=args.model,
        base_url=args.base_url,
        from_date=args.from_date,
        skip_enrich=args.skip_enrich,
        skip_ai=args.skip_ai,
    )
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=True, default=str))
    else:
        print(format_ai_flow_report(report))
    command_failures = [row for row in report.get("commands", []) if row.get("exitCode")]
    return 1 if command_failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
