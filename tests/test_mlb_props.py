from __future__ import annotations

from app.mlb_props import build_stable_props_payload, slug_key


def test_slug_key_normalizes_player_and_market_names():
    assert slug_key("Vladimir Guerrero Jr.") == "vladimir-guerrero-jr"
    assert slug_key("Hits Allowed") == "hits-allowed"


def test_build_stable_props_payload_adds_line_specific_prop_ids():
    payload = build_stable_props_payload(
        {
            "league": "MLB",
            "date": "2026-05-08",
            "timezone": "America/New_York",
            "fixtureCount": 1,
            "fixtures": [
                {
                    "slug": "reds-astros",
                    "name": "Cincinnati Reds - Houston Astros",
                    "startTime": 1778277600000,
                    "status": "active",
                    "type": "match",
                    "oddsError": None,
                    "playerProps": [
                        {
                            "player": "Jose Altuve",
                            "team": "Houston Astros",
                            "market": "hits",
                            "sportStatType": "player",
                            "line": 0.5,
                            "over": 1.55,
                            "under": 2.25,
                        }
                    ],
                }
            ],
        }
    )

    prop = payload["props"][0]
    assert payload["propCount"] == 1
    assert prop["propId"] == "reds-astros:jose-altuve:houston-astros:hits:line-0p5"
    assert prop["player"]["key"] == "jose-altuve"
    assert prop["team"]["fixtureTeamMatched"] is True
    assert prop["market"] == {"name": "hits", "key": "hits"}
    assert prop["odds"] == {"over": 1.55, "under": 2.25}
