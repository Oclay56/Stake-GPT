import asyncio
from datetime import date

from app.slate import (
    build_mlb_primary_line_check,
    build_mlb_player_props_slate,
    flatten_player_prop_rows,
    select_primary_player_prop_outcome,
)


class PlayerPropsFakeStakeClient:
    def __init__(self):
        self.schedule_calls = []
        self.odds_calls = []

    async def get_tournament_schedule(
        self,
        sport: str,
        category: str,
        tournament: str,
    ):
        self.schedule_calls.append((sport, category, tournament))
        return {
            "sport": {"slug": sport, "name": "Baseball"},
            "schedule": [
                {
                    "date": 1778277600000,
                    "fixtures": [
                        {
                            "slug": "reds-astros",
                            "date": 1778277600000,
                            "status": "active",
                            "type": "match",
                        }
                    ],
                }
            ],
        }

    async def get_odds(self, fixture_slug: str):
        self.odds_calls.append(fixture_slug)
        return {
            "fixture": {
                "slug": fixture_slug,
                "name": "Cincinnati Reds - Houston Astros",
                "startTime": 1778277600000,
                "status": "active",
                "type": "match",
            },
            "groups": [],
            "swishMarkets": {
                "playerProps": [
                    {
                        "id": "prop-1",
                        "gameId": "game-1",
                        "competitorName": "Jose Altuve",
                        "teamName": "Houston Astros",
                        "marketName": "hits",
                        "sportStatType": "player",
                        "outcomes": [
                            {
                                "id": "outcome-1",
                                "line": 1.5,
                                "over": 3.1,
                                "under": 1.2,
                            },
                            {
                                "id": "outcome-main",
                                "line": 0.5,
                                "over": 1.51,
                                "under": 2.34,
                            },
                            {
                                "id": "outcome-zero",
                                "line": 0,
                                "over": 0,
                                "under": 0,
                            },
                        ],
                    },
                    {
                        "id": "prop-2",
                        "gameId": "game-1",
                        "competitorName": "Hunter Greene",
                        "teamName": "Cincinnati Reds",
                        "marketName": "strikeouts",
                        "sportStatType": "player",
                        "outcomes": [
                            {
                                "id": "outcome-2",
                                "line": 5.5,
                                "over": 1.91,
                                "under": 1.83,
                            }
                        ],
                    },
                ]
            },
        }


def test_flatten_player_prop_rows_returns_clean_over_under_rows():
    odds = asyncio.run(PlayerPropsFakeStakeClient().get_odds("reds-astros"))

    assert flatten_player_prop_rows(odds) == [
        {
            "player": "Jose Altuve",
            "team": "Houston Astros",
            "market": "hits",
            "sportStatType": "player",
            "line": 0.5,
            "over": 1.51,
            "under": 2.34,
        },
        {
            "player": "Hunter Greene",
            "team": "Cincinnati Reds",
            "market": "strikeouts",
            "sportStatType": "player",
            "line": 5.5,
            "over": 1.91,
            "under": 1.83,
        },
    ]


def test_select_primary_player_prop_outcome_chooses_balanced_valid_line():
    prop = {
        "outcomes": [
            {"line": 1.5, "over": 3.09, "under": 1.2},
            {"line": 0.5, "over": 1.51, "under": 2.34},
            {"line": 2.5, "over": 5.61, "under": 1.02},
            {"line": 3.5, "over": 7.4, "under": 0.98},
        ]
    }

    assert select_primary_player_prop_outcome(prop) == {
        "line": 0.5,
        "over": 1.51,
        "under": 2.34,
    }


def test_flatten_player_prop_rows_defaults_to_primary_lines_only():
    odds = asyncio.run(PlayerPropsFakeStakeClient().get_odds("reds-astros"))

    rows = flatten_player_prop_rows(odds)

    assert [row["line"] for row in rows] == [0.5, 5.5]


def test_flatten_player_prop_rows_can_include_all_lines_for_debugging():
    odds = asyncio.run(PlayerPropsFakeStakeClient().get_odds("reds-astros"))

    rows = flatten_player_prop_rows(odds, line_mode="all")

    assert [row["line"] for row in rows] == [1.5, 0.5, 5.5]


def test_flatten_player_prop_rows_repairs_common_mojibake_names():
    odds = {
        "swishMarkets": {
            "playerProps": [
                {
                    "competitorName": "CÃ©sar Salazar",
                    "teamName": "Arizona Diamondbacks",
                    "marketName": "hits",
                    "sportStatType": "player",
                    "outcomes": [
                        {
                            "line": 0.5,
                            "over": 1.9,
                            "under": 1.81,
                        }
                    ],
                }
            ]
        }
    }

    rows = flatten_player_prop_rows(odds)

    assert rows[0]["player"] == "César Salazar"


def test_build_mlb_player_props_slate_uses_mlb_only_schedule():
    fake_client = PlayerPropsFakeStakeClient()

    slate = asyncio.run(
        build_mlb_player_props_slate(
            client=fake_client,
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=1,
        )
    )

    assert fake_client.schedule_calls == [("baseball", "usa", "mlb")]
    assert fake_client.odds_calls == ["reds-astros"]
    assert slate["league"] == "MLB"
    assert slate["date"] == "2026-05-08"
    assert slate["fixtureCount"] == 1
    assert slate["playerPropRowCount"] == 2
    assert slate["fixtures"][0] == {
        "slug": "reds-astros",
        "name": "Cincinnati Reds - Houston Astros",
        "startTime": 1778277600000,
        "status": "active",
        "type": "match",
        "playerPropCount": 2,
        "playerProps": [
            {
                "player": "Jose Altuve",
                "team": "Houston Astros",
                "market": "hits",
                "sportStatType": "player",
                "line": 0.5,
                "over": 1.51,
                "under": 2.34,
            },
            {
                "player": "Hunter Greene",
                "team": "Cincinnati Reds",
                "market": "strikeouts",
                "sportStatType": "player",
                "line": 5.5,
                "over": 1.91,
                "under": 1.83,
            },
        ],
        "oddsError": None,
    }


def test_build_mlb_player_props_slate_can_include_selected_markets():
    fake_client = PlayerPropsFakeStakeClient()

    slate = asyncio.run(
        build_mlb_player_props_slate(
            client=fake_client,
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=1,
            include_markets={"hits"},
        )
    )

    assert slate["filters"] == {"markets": ["hits"], "excludeMarkets": []}
    assert slate["playerPropRowCount"] == 1
    assert [row["market"] for row in slate["fixtures"][0]["playerProps"]] == ["hits"]


def test_build_mlb_player_props_slate_can_exclude_selected_markets():
    fake_client = PlayerPropsFakeStakeClient()

    slate = asyncio.run(
        build_mlb_player_props_slate(
            client=fake_client,
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=1,
            exclude_markets={"hits"},
        )
    )

    assert slate["filters"] == {"markets": [], "excludeMarkets": ["hits"]}
    assert slate["playerPropRowCount"] == 1
    assert [row["market"] for row in slate["fixtures"][0]["playerProps"]] == [
        "strikeouts"
    ]


def test_build_mlb_primary_line_check_reports_selected_primary_lines():
    fake_client = PlayerPropsFakeStakeClient()

    report = asyncio.run(
        build_mlb_primary_line_check(
            client=fake_client,
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=1,
        )
    )

    assert report["checkedPropCount"] == 2
    assert report["alternateLinePropCount"] == 1
    assert report["checks"][0] == {
        "fixtureSlug": "reds-astros",
        "game": "Cincinnati Reds - Houston Astros",
        "player": "Jose Altuve",
        "team": "Houston Astros",
        "market": "hits",
        "selectedLine": 0.5,
        "selectedOver": 1.51,
        "selectedUnder": 2.34,
        "validLineCount": 2,
        "alternateLineCount": 1,
        "method": "closest-over-under-balance",
        "allLines": [
            {"line": 1.5, "over": 3.1, "under": 1.2},
            {"line": 0.5, "over": 1.51, "under": 2.34},
        ],
    }
