from datetime import datetime, timezone

from app.line_movement import clear_line_movement_history, record_line_movements
from app.mlb_props import build_stable_props_payload, slug_key


def _slate(over: float = 1.51, under: float = 2.34):
    return {
        "league": "MLB",
        "date": "2026-05-08",
        "timezone": "America/New_York",
        "filters": {"markets": [], "excludeMarkets": []},
        "fixtureCount": 1,
        "playerPropRowCount": 1,
        "fixtures": [
            {
                "slug": "reds-astros",
                "name": "Cincinnati Reds - Houston Astros",
                "startTime": 1778277600000,
                "status": "active",
                "type": "match",
                "playerProps": [
                    {
                        "player": "José Altuve",
                        "team": "Houston Astros",
                        "market": "hits",
                        "sportStatType": "player",
                        "line": 0.5,
                        "over": over,
                        "under": under,
                    }
                ],
                "oddsError": None,
            }
        ],
    }


def test_slug_key_normalizes_names_for_matching():
    assert slug_key("José Altuve Jr.") == "jose-altuve-jr"
    assert slug_key("  Houston   Astros  ") == "houston-astros"


def test_build_stable_props_payload_adds_identity_fields():
    payload = build_stable_props_payload(_slate(), include_movement=False)

    assert payload["propCount"] == 1
    assert payload["props"][0] == {
        "propId": "reds-astros:jose-altuve:houston-astros:hits",
        "fixtureSlug": "reds-astros",
        "game": "Cincinnati Reds - Houston Astros",
        "startTime": 1778277600000,
        "status": "active",
        "type": "match",
        "player": {
            "name": "José Altuve",
            "key": "jose-altuve",
            "mlbId": None,
            "matchStatus": "needs_mlb_lookup",
        },
        "team": {
            "name": "Houston Astros",
            "key": "houston-astros",
            "fixtureTeamMatched": True,
            "fixtureTeamIndex": 1,
            "fixtureTeamName": "Houston Astros",
            "mlbId": None,
            "matchStatus": "fixture_team_match",
        },
        "market": {"name": "hits", "key": "hits"},
        "sportStatType": "player",
        "line": 0.5,
        "odds": {"over": 1.51, "under": 2.34},
    }


def test_record_line_movements_tracks_changed_odds():
    clear_line_movement_history()
    first = build_stable_props_payload(_slate(), include_movement=False)["props"]
    second = build_stable_props_payload(
        _slate(over=1.6, under=2.2),
        include_movement=False,
    )["props"]

    first_rows = record_line_movements(
        first,
        recorded_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
    )
    second_rows = record_line_movements(
        second,
        recorded_at=datetime(2026, 5, 8, 12, 5, tzinfo=timezone.utc),
    )

    assert first_rows[0]["movement"] == {
        "snapshotCount": 1,
        "previous": None,
        "change": None,
    }
    assert second_rows[0]["movement"] == {
        "snapshotCount": 2,
        "previous": {
            "recordedAt": "2026-05-08T12:00:00+00:00",
            "line": 0.5,
            "over": 1.51,
            "under": 2.34,
        },
        "change": {"line": 0.0, "over": 0.09, "under": -0.14},
    }
