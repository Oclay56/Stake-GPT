from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.dashboard import build_mlb_dashboard
from app.main import app, get_snapshot_store
from app.storage import SnapshotStore


def _prop(
    prop_id="dodgers-padres:mookie-betts:los-angeles-dodgers:total-bases",
    fixture="dodgers-padres",
    player="Mookie Betts",
    team="Los Angeles Dodgers",
    market="total-bases",
    line=1.5,
    over=1.78,
    recent_per_game=2.1,
    season_value=88,
):
    return {
        "propId": prop_id,
        "fixtureSlug": fixture,
        "game": "Los Angeles Dodgers - San Diego Padres",
        "player": {
            "name": player,
            "key": player.lower().replace(" ", "-"),
            "mlbId": 605141,
            "matchStatus": "matched_exact_name_team",
        },
        "team": {"name": team, "key": "los-angeles-dodgers", "mlbId": 119},
        "market": {"name": market.replace("-", " "), "key": market},
        "line": line,
        "odds": {"over": over, "under": 1.95},
        "mlbGame": {
            "gamePk": 1,
            "gameDate": "2026-05-09T01:40:00Z",
            "status": "Preview",
        },
        "mlbProfile": {
            "player": {
                "mlbId": 605141,
                "name": player,
                "stats": {"gamesPlayed": 44, "totalBases": season_value},
            },
            "season": 2026,
            "group": "hitting",
        },
        "recentHistory": {
            "playerId": 605141,
            "season": 2026,
            "group": "hitting",
            "gamesUsed": 5,
            "games": [
                {"date": "2026-05-08", "opponent": "Padres", "stats": {"totalBases": 3}},
                {"date": "2026-05-07", "opponent": "Dbacks", "stats": {"totalBases": 2}},
            ],
            "totals": {"totalBases": 10.5},
            "perGame": {"totalBases": recent_per_game},
        },
        "statContext": {
            "statKey": "totalBases",
            "seasonValue": season_value,
            "recentTotal": 10.5,
            "recentPerGame": recent_per_game,
            "gamesUsed": 5,
        },
    }


def _payload(props):
    return {
        "date": "2026-05-09",
        "timezone": "America/New_York",
        "fixtureCount": 1,
        "propCount": len(props),
        "matchedPropCount": len(props),
        "unmatchedPropCount": 0,
        "props": props,
    }


def _analysis(prop):
    return {
        "date": "2026-05-09",
        "snapshotPhase": "pregame",
        "buckets": {
            "watchlist": [
                {
                    "propId": prop["propId"],
                    "fixtureSlug": prop["fixtureSlug"],
                    "game": prop["game"],
                    "playerName": prop["player"]["name"],
                    "teamName": prop["team"]["name"],
                    "marketKey": prop["market"]["key"],
                    "statKey": prop["statContext"]["statKey"],
                    "line": prop["line"],
                    "lean": "over",
                    "edge": 0.6,
                    "score": 91,
                    "confidence": "high",
                    "marketProfile": "power_count",
                    "marketThreshold": 0.4,
                    "overOdds": prop["odds"]["over"],
                    "underOdds": prop["odds"]["under"],
                    "recentPerGame": prop["statContext"]["recentPerGame"],
                    "seasonValue": prop["statContext"]["seasonValue"],
                    "seasonPerGame": 2.0,
                    "seasonEdge": 0.5,
                    "gamesUsed": prop["statContext"]["gamesUsed"],
                    "recentGames": prop["recentHistory"]["games"],
                    "seasonStats": prop["mlbProfile"]["player"]["stats"],
                    "riskFlags": [],
                    "reasons": ["recent_per_game_above_market_threshold"],
                }
            ],
            "neutral": [],
            "avoid": [],
        },
    }


def test_build_mlb_dashboard_uses_saved_analysis_and_real_assets(tmp_path):
    asset_dir = tmp_path / "assets" / "players"
    asset_dir.mkdir(parents=True)
    (asset_dir / "mookie-betts.png").write_bytes(b"png")
    store = SnapshotStore(tmp_path / "azp.sqlite")
    prop = _prop()
    store.save_enriched_props(
        _payload([prop]),
        captured_at=datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc),
        snapshot_phase="pregame",
        snapshot_label="morning",
    )
    store.save_analysis_result(
        _analysis(prop),
        captured_at=datetime(2026, 5, 9, 14, 1, tzinfo=timezone.utc),
        snapshot_label="morning analyzer",
    )

    dashboard = build_mlb_dashboard(
        store,
        date_text="2026-05-09",
        limit=5,
        asset_dir=asset_dir,
        asset_base_url="/app/assets/players",
    )

    assert dashboard["status"]["source"] == "saved_analysis"
    assert dashboard["summary"]["games"] == 1
    assert dashboard["summary"]["playerProps"] == 1
    assert dashboard["summary"]["topScore"] == 91
    assert dashboard["topPlayerProps"][0]["playerName"] == "Mookie Betts"
    assert dashboard["topPlayerProps"][0]["playerImage"] == "/app/assets/players/mookie-betts.png"
    assert dashboard["topPlayerProps"][0]["recentGames"][0]["stats"]["totalBases"] == 3
    assert dashboard["slip"]["legs"][0]["playerName"] == "Mookie Betts"
    assert dashboard["slip"]["combinedOdds"] == 1.78


def test_build_mlb_dashboard_falls_back_to_current_analyzer_when_no_saved_decisions(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    prop = _prop(player="Juan Soto", prop_id="yankees-red-sox:juan-soto:new-york-yankees:hits", market="hits", line=0.5, recent_per_game=1.4)
    store.save_enriched_props(
        _payload([prop]),
        captured_at=datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )

    dashboard = build_mlb_dashboard(store, date_text="2026-05-09", limit=5)

    assert dashboard["status"]["source"] == "latest_props_analyzed"
    assert dashboard["topPlayerProps"][0]["playerName"] == "Juan Soto"
    assert dashboard["topPlayerProps"][0]["lean"] == "over"
    assert dashboard["summary"]["playerProps"] == 1


def test_dashboard_api_route_uses_snapshot_store_dependency(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    prop = _prop()
    store.save_enriched_props(
        _payload([prop]),
        captured_at=datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    app.dependency_overrides[get_snapshot_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get("/dashboard/mlb?date=2026-05-09&limit=3")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["sport"] == "mlb"
    assert body["date"] == "2026-05-09"
    assert body["summary"]["playerProps"] == 1


def test_dashboard_fallback_slip_still_includes_correlation_risk(tmp_path):
    store = SnapshotStore(tmp_path / "azp.sqlite")
    first = _prop()
    second = _prop(
        prop_id="dodgers-padres:freddie-freeman:los-angeles-dodgers:hits",
        player="Freddie Freeman",
        team="Los Angeles Dodgers",
        market="hits",
        line=0.5,
        over=1.6,
        recent_per_game=1.2,
    )
    analysis = _analysis(first)
    analysis["buckets"]["watchlist"].append(
        {
            **analysis["buckets"]["watchlist"][0],
            "propId": second["propId"],
            "playerName": second["player"]["name"],
            "marketKey": second["market"]["key"],
            "line": second["line"],
            "overOdds": second["odds"]["over"],
            "score": 89,
        }
    )
    store.save_enriched_props(
        _payload([first, second]),
        captured_at=datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc),
        snapshot_phase="pregame",
    )
    store.save_analysis_result(
        analysis,
        captured_at=datetime(2026, 5, 9, 14, 1, tzinfo=timezone.utc),
    )

    dashboard = build_mlb_dashboard(store, date_text="2026-05-09", limit=2)

    assert dashboard["slip"]["legCount"] == 2
    assert dashboard["slip"]["correlationRisk"]["sgpRepricingLikely"] is True
    assert dashboard["slip"]["correlationRisk"]["quoteNeeded"] is True
    assert "same_fixture" in dashboard["slip"]["correlationRisk"]["tags"]


def test_static_app_route_serves_desktop_concept():
    with TestClient(app) as client:
        response = client.get("/app/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "AZP Suite" in response.text
