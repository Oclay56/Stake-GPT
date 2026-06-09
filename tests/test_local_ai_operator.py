from __future__ import annotations

from app.local_ai_operator import (
    build_ai_flow_context,
    fallback_summary,
    flow_action_args,
    format_ai_flow_report,
    normalize_flow_plan,
    run_ai_flow,
)


def _command(json_payload: dict, code: int = 0) -> dict:
    return {"args": ["fake"], "exitCode": code, "json": json_payload, "parseError": ""}


def test_local_ai_flow_context_keeps_compact_maintenance_fields():
    context = build_ai_flow_context(
        update=_command(
            {
                "sync": {
                    "filesConsidered": 9,
                    "filesImported": 2,
                    "filesSkippedDuplicate": 7,
                    "filesFailed": 0,
                    "history": {
                        "parsedLegs": 300,
                        "trainingEligible": 280,
                        "needsReview": 1,
                    },
                },
                "enrich": {"targets": 100, "legsEnriched": 80},
                "dataset": {
                    "rows": 300,
                    "trainingRows": 280,
                    "enrichedRows": 260,
                    "readiness": {"label": "ML dataset ready"},
                },
            }
        ),
        analysis=_command(
            {
                "enrichment": {"coverageRate": 0.8},
                "finalOutcome": {
                    "verdict": "Backtest-ready",
                    "historyQuality": {"label": "usable"},
                    "ticketSample": {
                        "tickets": 20,
                        "gradedTickets": 18,
                        "hitRate": 0.25,
                        "roi": -0.12,
                    },
                    "modelReadiness": {
                        "status": "ml_baseline_possible_not_validated",
                        "label": "ML baseline possible",
                        "reason": "Holdout still required.",
                    },
                    "nextAction": "Keep importing settled tickets.",
                },
            }
        ),
        model_report=_command(
            {
                "modelVersion": "historic_bucket_baseline_v1",
                "trainingRows": 500,
                "holdoutRows": 166,
                "globalPrior": 0.819,
                "metrics": {"holdout": {"brierImprovement": 0.003, "rankSpread": 0.122}},
                "validation": {
                    "label": "Baseline not validated",
                    "canInfluenceBuilds": False,
                    "reason": "Needs stronger holdout lift.",
                },
            }
        ),
    )

    assert context["historic"]["filesImported"] == 2
    assert context["historic"]["datasetRows"] == 300
    assert context["analysis"]["ticketSample"]["roi"] == -0.12
    assert context["ml"]["rankSpread"] == 0.122
    assert context["ml"]["canInfluenceBuilds"] is False
    assert "M/L:" in fallback_summary(context)


def test_run_ai_flow_uses_local_summary_when_available(monkeypatch, tmp_path):
    commands = []

    def fake_run_json_command(root_dir, args):
        commands.append(list(args))
        if args[0] == "report":
            return _command(
                {
                    "trainingEligible": 100,
                    "parsedLegs": 100,
                    "needsReview": 0,
                    "enrichment": {"legEnrichments": 100},
                }
            )
        if args[0] == "sync":
            return _command({"history": {"trainingEligible": 100, "parsedLegs": 100}})
        if args[0] == "dataset":
            return _command({"rows": 100, "trainingRows": 100, "readiness": {"label": "ready"}})
        if args[0] == "analysis":
            return _command({"finalOutcome": {}})
        return _command({"metrics": {"holdout": {}}, "validation": {}})

    monkeypatch.setattr("app.local_ai_operator._run_json_command", fake_run_json_command)
    monkeypatch.setattr(
        "app.local_ai_operator.plan_flow_with_ollama",
        lambda *args, **kwargs: {
            "rationale": "freshen the normal maintenance chain",
            "steps": [
                {"action": "sync", "reason": "new files may exist"},
                {"action": "dataset_build", "reason": "dataset should reflect sync"},
                {"action": "analysis", "reason": "show readiness"},
                {"action": "model_train", "reason": "refresh baseline"},
            ],
        },
    )
    monkeypatch.setattr("app.local_ai_operator.summarize_with_ollama", lambda *args, **kwargs: "Ran: ok")

    report = run_ai_flow(root_dir=tmp_path)

    assert commands == [
        ["report", "--json"],
        ["sync", "--json"],
        ["dataset", "build", "--json"],
        ["analysis", "--json"],
        ["model", "train", "--json"],
    ]
    assert report["summary"] == "Ran: ok"
    assert report["localAiUsed"] is True
    assert report["flowMode"] == "agentic"
    assert report["planner"]["source"] == "local_ai"
    assert "Stake-GPT Local AI Flow" in format_ai_flow_report(report)


def test_flow_plan_normalization_blocks_unknown_actions_and_adds_safe_dependencies():
    plan = normalize_flow_plan(
        {
            "rationale": "try extra stuff",
            "steps": [
                {"action": "shell", "reason": "bad"},
                {"action": "model_train", "reason": "wanted"},
            ],
        },
        status_context={
            "skipEnrich": False,
            "trainingEligible": 100,
            "enrichedLegs": 40,
            "enrichmentGap": 60,
            "enrichmentCoverage": 0.4,
        },
        skip_enrich=False,
    )

    actions = [step["action"] for step in plan["steps"]]

    assert actions == ["sync", "enrich_missing", "dataset_build", "analysis", "model_train"]
    assert all(action != "shell" for action in actions)
    assert plan["plannerUsed"] is True
    assert any("Added required step" in item for item in plan["adjustments"])


def test_flow_action_args_apply_from_date_only_to_supported_steps():
    assert flow_action_args("sync", from_date="2026-06-01") == ["sync", "--json"]
    assert flow_action_args("analysis", from_date="2026-06-01") == [
        "analysis",
        "--json",
        "--from-date",
        "2026-06-01",
    ]
    assert flow_action_args("enrich_missing", from_date="2026-06-01") == [
        "enrich",
        "--missing-only",
        "--json",
        "--from-date",
        "2026-06-01",
    ]
