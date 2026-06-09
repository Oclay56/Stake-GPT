from __future__ import annotations

from app.local_ai_operator import (
    build_ai_flow_context,
    fallback_summary,
    format_ai_flow_report,
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
        if args[0] == "update":
            return _command({"sync": {}, "enrich": {}, "dataset": {}, "analysis": {}})
        if args[0] == "analysis":
            return _command({"finalOutcome": {}})
        return _command({"metrics": {"holdout": {}}, "validation": {}})

    monkeypatch.setattr("app.local_ai_operator._run_json_command", fake_run_json_command)
    monkeypatch.setattr("app.local_ai_operator.summarize_with_ollama", lambda *args, **kwargs: "Ran: ok")

    report = run_ai_flow(root_dir=tmp_path)

    assert commands == [
        ["update", "--json"],
        ["analysis", "--json"],
        ["model", "train", "--json"],
    ]
    assert report["summary"] == "Ran: ok"
    assert report["localAiUsed"] is True
    assert "Stake-GPT Local AI Flow" in format_ai_flow_report(report)
