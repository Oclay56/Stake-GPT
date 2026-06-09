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
FLOW_ACTIONS: dict[str, dict[str, Any]] = {
    "sync": {
        "label": "Historic sync",
        "description": "Import new files from the historic import folder and skip duplicates.",
    },
    "enrich_missing": {
        "label": "Historic enrich missing",
        "description": "Fetch and store missing frozen historical MLB snapshots.",
    },
    "dataset_build": {
        "label": "Dataset build",
        "description": "Rebuild the derived historic M/L dataset.",
    },
    "analysis": {
        "label": "Historic analysis",
        "description": "Run ticket, leg, signal, calibration, and readiness analysis.",
    },
    "model_train": {
        "label": "M/L train",
        "description": "Train or refresh the offline baseline model.",
    },
    "review": {
        "label": "Historic review",
        "description": "Show rows that need manual review before training.",
    },
}
FLOW_ACTION_ORDER = ("sync", "enrich_missing", "dataset_build", "analysis", "model_train", "review")
FLOW_REQUIRED_ACTIONS = ("sync", "dataset_build", "analysis", "model_train")


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

    probe = _run_json_command(root_dir, ["report", "--json"])
    status_context = build_flow_status_context(
        probe=probe,
        from_date=from_date,
        skip_enrich=skip_enrich,
    )

    planner_error = "Local AI planner skipped by --skip-ai." if skip_ai else ""
    raw_plan: dict[str, Any] | None = None
    if not skip_ai:
        try:
            raw_plan = plan_flow_with_ollama(
                status_context,
                model=clean_model,
                base_url=clean_base_url,
            )
        except Exception as exc:  # pragma: no cover - local service availability varies.
            planner_error = str(exc)

    plan = normalize_flow_plan(
        raw_plan,
        status_context=status_context,
        skip_enrich=skip_enrich,
        planner_error=planner_error,
    )
    flow_results = execute_flow_plan(
        root_dir=root_dir,
        plan=plan,
        from_date=from_date,
    )
    context = build_ai_flow_context_from_results(
        probe=probe,
        flow_results=flow_results,
        plan=plan,
    )

    ai_summary = ""
    summary_error = ""
    if not skip_ai:
        try:
            ai_summary = summarize_with_ollama(
                context,
                model=clean_model,
                base_url=clean_base_url,
            )
        except Exception as exc:  # pragma: no cover - local service availability varies.
            summary_error = str(exc)
    if not ai_summary:
        ai_summary = fallback_summary(context)

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "provider": "ollama",
        "model": clean_model,
        "baseUrl": clean_base_url,
        "fromDate": from_date,
        "skipEnrich": skip_enrich,
        "flowMode": plan["mode"],
        "localAiUsed": bool(not skip_ai and (plan["plannerUsed"] or (ai_summary and not summary_error))),
        "localAiError": summary_error,
        "planner": {
            "used": bool(plan["plannerUsed"]),
            "source": plan["source"],
            "rationale": plan.get("rationale") or "",
            "error": planner_error,
            "adjustments": list(plan.get("adjustments") or []),
        },
        "plan": list(plan["steps"]),
        "commands": context["commands"],
        "historic": context["historic"],
        "analysis": context["analysis"],
        "ml": context["ml"],
        "summary": ai_summary,
        "guardrails": [
            "Local AI may plan maintenance steps only from a fixed allowlist.",
            "Python remains responsible for parsing, enrichment, analysis, and M/L validation.",
            "Local AI does not choose picks, override validation, or place bets.",
        ],
    }


def build_flow_status_context(
    *,
    probe: dict[str, Any],
    from_date: str | None,
    skip_enrich: bool,
) -> dict[str, Any]:
    report = probe.get("json") or {}
    enrichment = report.get("enrichment") or {}
    parsed_legs = _int(report.get("parsedLegs"))
    enriched_legs = _int(enrichment.get("legEnrichments"))
    training_eligible = _int(report.get("trainingEligible"))
    import_files = list(report.get("importFiles") or [])
    enrichment_gap = max(training_eligible - enriched_legs, 0)
    coverage = (enriched_legs / training_eligible) if training_eligible else None
    return {
        "fromDate": from_date,
        "skipEnrich": bool(skip_enrich),
        "probeExitCode": _int(probe.get("exitCode")),
        "importFileCount": len(import_files),
        "rawRows": _int(report.get("rawRows")),
        "parsedLegs": parsed_legs,
        "trainingEligible": training_eligible,
        "needsReview": _int(report.get("needsReview")),
        "markets": dict(report.get("markets") or {}),
        "results": dict(report.get("results") or {}),
        "missingOrAmbiguous": dict(report.get("missingOrAmbiguous") or {}),
        "snapshotGames": _int(enrichment.get("snapshots")),
        "enrichedLegs": enriched_legs,
        "enrichmentGap": enrichment_gap,
        "enrichmentCoverage": coverage,
        "allowedActions": [
            {
                "action": action,
                "description": meta["description"],
            }
            for action, meta in FLOW_ACTIONS.items()
            if action != "enrich_missing" or not skip_enrich
        ],
        "policy": [
            "Choose only allowed actions.",
            "Never choose bets, builder actions, browser actions, shell commands, or destructive actions.",
            "Prefer sync first so new historic files are imported before downstream analysis.",
            "Use enrich_missing when enrichment is incomplete unless skipEnrich is true.",
            "Run dataset_build before model_train.",
            "Run analysis after dataset_build so the final summary uses updated history.",
        ],
    }


def plan_flow_with_ollama(
    status_context: dict[str, Any],
    *,
    model: str,
    base_url: str,
) -> dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": (
                "You are the local Stake-GPT maintenance planner. Return JSON only. "
                "You may plan Historic, Analysis, and M/L maintenance steps from the "
                "provided allowlist. Do not choose bets, builder actions, browser "
                "actions, shell commands, deletes, or anything outside the allowlist."
            ),
        },
        {
            "role": "user",
            "content": (
                "Given this current state, choose the maintenance flow. "
                "Return exactly this JSON shape with no markdown: "
                "{\"rationale\":\"short reason\","
                "\"steps\":[{\"action\":\"sync\",\"reason\":\"why\"}]}.\n\n"
                + json.dumps(status_context, ensure_ascii=True, indent=2)
            ),
        },
    ]
    content = ollama_chat(
        messages,
        model=model,
        base_url=base_url,
        timeout_seconds=240,
        num_predict=900,
        json_mode=True,
    )
    parsed = _extract_json_object(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("Local AI planner did not return a JSON object.")
    return parsed


def normalize_flow_plan(
    raw_plan: dict[str, Any] | None,
    *,
    status_context: dict[str, Any],
    skip_enrich: bool,
    planner_error: str = "",
) -> dict[str, Any]:
    reasons: dict[str, str] = {}
    requested: list[str] = []
    if raw_plan:
        for step in raw_plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            action = str(step.get("action") or "").strip().lower()
            if action in FLOW_ACTIONS and action not in requested:
                requested.append(action)
                reasons[action] = str(step.get("reason") or "").strip()

    source = "local_ai" if requested and not planner_error else "deterministic_fallback"
    adjustments: list[str] = []
    if not requested:
        requested = default_flow_action_ids(status_context, skip_enrich=skip_enrich)
        if "skipped" in planner_error.lower():
            adjustments.append("Local AI planner skipped; used deterministic fallback.")
        elif planner_error:
            adjustments.append("Local AI planner failed; used deterministic fallback.")
        else:
            adjustments.append("Local AI planner returned no valid steps; used deterministic fallback.")

    if skip_enrich and "enrich_missing" in requested:
        requested = [action for action in requested if action != "enrich_missing"]
        adjustments.append("Removed enrich_missing because --skip-enrich is active.")

    for required in FLOW_REQUIRED_ACTIONS:
        if required not in requested:
            requested.append(required)
            reasons[required] = "Required final flow step."
            adjustments.append(f"Added required step: {required}.")

    if (
        not skip_enrich
        and "enrich_missing" not in requested
        and should_enrich_missing(status_context)
    ):
        requested.append("enrich_missing")
        reasons["enrich_missing"] = "Enrichment coverage is incomplete."
        adjustments.append("Added enrich_missing because historical enrichment is incomplete.")
    if _int(status_context.get("needsReview")) and "review" not in requested:
        requested.append("review")
        reasons["review"] = "Historic rows needing review are present."
        adjustments.append("Added review because at least one historic row needs attention.")

    ordered = [action for action in FLOW_ACTION_ORDER if action in requested]
    if ordered != requested:
        adjustments.append("Reordered steps into the safe maintenance dependency order.")

    steps = [
        {
            "action": action,
            "label": FLOW_ACTIONS[action]["label"],
            "reason": reasons.get(action) or default_flow_step_reason(action, status_context),
        }
        for action in ordered
    ]
    return {
        "mode": "agentic" if source == "local_ai" else "deterministic_fallback",
        "source": source,
        "plannerUsed": source == "local_ai",
        "rationale": str((raw_plan or {}).get("rationale") or "").strip(),
        "adjustments": adjustments,
        "steps": steps,
    }


def default_flow_action_ids(status_context: dict[str, Any], *, skip_enrich: bool) -> list[str]:
    actions = ["sync"]
    if not skip_enrich and should_enrich_missing(status_context):
        actions.append("enrich_missing")
    actions.extend(["dataset_build", "analysis", "model_train"])
    if _int(status_context.get("needsReview")):
        actions.append("review")
    return actions


def should_enrich_missing(status_context: dict[str, Any]) -> bool:
    if bool(status_context.get("skipEnrich")):
        return False
    coverage = _float(status_context.get("enrichmentCoverage"))
    gap = _int(status_context.get("enrichmentGap"))
    return gap > 0 or coverage is None or coverage < 0.98


def default_flow_step_reason(action: str, status_context: dict[str, Any]) -> str:
    if action == "sync":
        return "Import folder may contain new settled tickets."
    if action == "enrich_missing":
        return f"{_int(status_context.get('enrichmentGap'))} eligible legs still lack frozen MLB enrichment."
    if action == "dataset_build":
        return "Downstream analysis and M/L need the latest derived dataset."
    if action == "analysis":
        return "Final readiness should be computed from the updated history."
    if action == "model_train":
        return "Refresh the offline baseline after dataset changes."
    if action == "review":
        return "Rows needing review should be surfaced."
    return FLOW_ACTIONS.get(action, {}).get("description", "")


def execute_flow_plan(
    *,
    root_dir: Path,
    plan: dict[str, Any],
    from_date: str | None,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for step in plan.get("steps") or []:
        action = str(step.get("action") or "")
        args = flow_action_args(action, from_date=from_date)
        command = _run_json_command(root_dir, args)
        command["action"] = action
        command["planReason"] = step.get("reason") or ""
        results[action] = command
    return results


def flow_action_args(action: str, *, from_date: str | None = None) -> list[str]:
    if action == "sync":
        return ["sync", "--json"]
    if action == "enrich_missing":
        args = ["enrich", "--missing-only", "--json"]
        if from_date:
            args.extend(["--from-date", from_date])
        return args
    if action == "dataset_build":
        args = ["dataset", "build", "--json"]
        if from_date:
            args.extend(["--from-date", from_date])
        return args
    if action == "analysis":
        args = ["analysis", "--json"]
        if from_date:
            args.extend(["--from-date", from_date])
        return args
    if action == "model_train":
        return ["model", "train", "--json"]
    if action == "review":
        return ["review", "--limit", "25", "--json"]
    raise ValueError(f"Unsupported flow action: {action}")


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


def build_ai_flow_context_from_results(
    *,
    probe: dict[str, Any],
    flow_results: dict[str, dict[str, Any]],
    plan: dict[str, Any],
) -> dict[str, Any]:
    probe_json = probe.get("json") or {}
    sync = flow_results.get("sync") or {}
    sync_json = sync.get("json") or {}
    history = sync_json.get("history") or probe_json
    enrich = (flow_results.get("enrich_missing") or {}).get("json") or {}
    dataset = (flow_results.get("dataset_build") or {}).get("json") or {}
    analysis = flow_results.get("analysis") or {}
    analysis_json = analysis.get("json") or {}
    model_report = flow_results.get("model_train") or {}
    model_json = model_report.get("json") or {}

    dataset_readiness = dataset.get("readiness") or {}
    analysis_enrichment = analysis_json.get("enrichment") or {}
    final_outcome = analysis_json.get("finalOutcome") or {}
    ticket_sample = final_outcome.get("ticketSample") or {}
    model_readiness = final_outcome.get("modelReadiness") or {}
    model_validation = model_json.get("validation") or {}
    model_holdout = ((model_json.get("metrics") or {}).get("holdout") or {})
    enrichment_summary = history.get("enrichment") or {}

    command_summaries = [
        _command_summary("historic report probe", probe),
        *[
            _command_summary(str(step.get("label") or step.get("action")), flow_results.get(str(step.get("action"))) or {})
            | {"reason": step.get("reason") or ""}
            for step in plan.get("steps") or []
        ],
    ]

    return {
        "planner": {
            "mode": plan.get("mode"),
            "source": plan.get("source"),
            "rationale": plan.get("rationale") or "",
            "adjustments": list(plan.get("adjustments") or []),
            "steps": list(plan.get("steps") or []),
        },
        "commands": command_summaries,
        "historic": {
            "filesConsidered": _int(sync_json.get("filesConsidered") or len(probe_json.get("importFiles") or [])),
            "filesImported": _int(sync_json.get("filesImported")),
            "filesSkippedDuplicate": _int(sync_json.get("filesSkippedDuplicate")),
            "filesFailed": _int(sync_json.get("filesFailed")),
            "refreshedLegs": _int(sync_json.get("refreshedLegs")),
            "parsedLegs": _int(history.get("parsedLegs")),
            "trainingEligible": _int(history.get("trainingEligible")),
            "needsReview": _int(history.get("needsReview")),
            "enrichTargets": _int(enrich.get("targets")),
            "legsEnriched": _int(enrich.get("legsEnriched") or enrichment_summary.get("legEnrichments")),
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
    num_predict: int = 700,
    json_mode: bool = False,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": int(num_predict),
        },
    }
    if json_mode:
        payload["format"] = "json"
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
    planner = context.get("planner") or {}
    historic = context.get("historic") or {}
    analysis = context.get("analysis") or {}
    tickets = analysis.get("ticketSample") or {}
    ml = context.get("ml") or {}
    blocker = ml.get("reason") or (analysis.get("modelReadiness") or {}).get("reason") or "none"
    return "\n".join(
        [
            "Ran: Historic update, Analysis, and M/L baseline training completed.",
            f"Flow: {planner.get('mode') or 'unknown'} planner, steps {_plan_step_labels(planner)}.",
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
            "Planner: "
            f"{(report.get('planner') or {}).get('source') or 'unknown'} | "
            f"Mode: {report.get('flowMode') or 'unknown'}"
        ),
        "Plan: " + ", ".join(step.get("action", "?") for step in report.get("plan") or []),
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
    adjustments = list(((report.get("planner") or {}).get("adjustments") or []))
    if adjustments:
        lines.extend(["", "Planner adjustments:", *[f"- {item}" for item in adjustments]])
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


def _extract_json_object(text: str) -> dict[str, Any]:
    clean = _clean_model_text(text)
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.lower().startswith("json"):
            clean = clean[4:].strip()
    try:
        parsed = json.loads(clean)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    start = clean.find("{")
    end = clean.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(clean[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _plan_step_labels(planner: dict[str, Any]) -> str:
    steps = [
        str(step.get("action") or step.get("label") or "").strip()
        for step in planner.get("steps") or []
        if isinstance(step, dict)
    ]
    return ", ".join(step for step in steps if step) or "none"


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
