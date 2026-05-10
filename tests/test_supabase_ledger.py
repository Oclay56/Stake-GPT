import asyncio
import json

import httpx

from app.supabase_ledger import (
    fetch_recommendation_performance_from_supabase,
    settlement_payloads,
)


def test_settlement_payloads_use_stable_ids_and_json_fields():
    rows = [
        {
            "requestId": "req-1",
            "rank": 2,
            "propId": "prop-1",
            "date": "2026-05-08",
            "marketKey": "hits",
            "side": "under",
            "actualValue": 0,
            "actualResult": "under",
            "overOutcome": "loss",
            "decisionOutcome": "correct",
            "reasons": [],
        }
    ]

    payloads = settlement_payloads(rows, settled_at="2026-05-09T05:00:00+00:00")

    assert payloads == [
        {
            "settlement_id": "req-1:2",
            "request_id": "req-1",
            "leg_id": "req-1:2",
            "leg_rank": 2,
            "prop_id": "prop-1",
            "slate_date": "2026-05-08",
            "market_key": "hits",
            "side": "under",
            "actual_value": 0.0,
            "actual_result": "under",
            "over_outcome": "loss",
            "decision_outcome": "correct",
            "reasons": [],
            "settled_at": "2026-05-09T05:00:00+00:00",
            "raw": rows[0],
        }
    ]


def test_fetch_recommendation_performance_from_supabase_summarizes_remote_rows(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/recommendation_legs"):
            return httpx.Response(
                200,
                json=[
                    {
                        "request_id": "req-1",
                        "captured_at": "2026-05-08T16:00:00+00:00",
                        "slate_date": "2026-05-08",
                        "matchup": "Blue Jays vs Angels",
                        "rank": 1,
                        "prop_id": "prop-1",
                        "fixture_slug": "blue-jays-angels",
                        "game": "Toronto Blue Jays - Los Angeles Angels",
                        "mlb_game_pk": 111,
                        "player_name": "George Springer",
                        "player_key": "george-springer",
                        "player_mlb_id": 543807,
                        "team_name": "Toronto Blue Jays",
                        "team_key": "toronto-blue-jays",
                        "team_mlb_id": 141,
                        "market_key": "hits",
                        "stat_key": "hits",
                        "line": 0.5,
                        "side": "under",
                        "lean": "under_or_avoid_over",
                        "odds": 2.9,
                        "over_odds": 1.34,
                        "under_odds": 2.9,
                        "edge": 0.5,
                        "score": 87,
                        "confidence": "high",
                        "selection": "George Springer under 0.5 hits",
                        "diversity_mode": "balanced",
                        "risk_flags": [],
                        "reasons": ["recent_per_game_below_line"],
                        "contextual_tags": ["hit_distribution_clustered_0_1"],
                        "deferred_layers": ["umpire_impact"],
                        "concentration_tags": ["same_side_cluster:under"],
                        "raw": {},
                    }
                ],
            )
        if request.url.path.endswith("/recommendation_settlements"):
            return httpx.Response(
                200,
                json=[
                    {
                        "request_id": "req-1",
                        "leg_rank": 1,
                        "prop_id": "prop-1",
                        "slate_date": "2026-05-08",
                        "market_key": "hits",
                        "side": "under",
                        "actual_value": 0,
                        "actual_result": "under",
                        "over_outcome": "loss",
                        "decision_outcome": "correct",
                        "reasons": [],
                        "settled_at": "2026-05-09T05:00:00+00:00",
                        "raw": {},
                    }
                ],
            )
        return httpx.Response(404, json={"error": "unexpected"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_recommendation_performance_from_supabase(
                date_text="2026-05-08",
                market="hits",
                side="under",
                diversity_mode="balanced",
                limit=100,
                client=client,
            )

    summary = asyncio.run(run())

    assert summary["source"] == "supabase"
    assert summary["counts"]["legs"] == 1
    assert summary["counts"]["correct"] == 1
    assert summary["byMarket"]["hits"]["accuracy"] == 1.0
    assert len(requests) == 2
    assert "market_key=eq.hits" in str(requests[0].url)
    assert "side=eq.under" in str(requests[0].url)
    assert "diversity_mode=eq.balanced" in str(requests[0].url)
