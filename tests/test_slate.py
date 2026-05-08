import asyncio
from datetime import date

from app.slate import build_market_slate, build_slate, flatten_market_rows
from app.stake_client import StakeAPIError


class SlateFakeStakeClient:
    def __init__(self):
        self.odds_calls = []

    async def get_sport_schedule(self, sport: str):
        return {
            "sport": {"slug": sport},
            "schedule": [
                {
                    "date": 1778259600000,
                    "fixtures": [
                        {
                            "slug": "today-one",
                            "name": "Today One",
                            "date": 1778259600000,
                            "status": "active",
                            "type": "match",
                        },
                        {
                            "slug": "today-two",
                            "name": "Today Two",
                            "startTime": 1778263200000,
                            "status": "active",
                            "type": "match",
                        },
                    ],
                },
                {
                    "date": 1778346000000,
                    "fixtures": [
                        {
                            "slug": "tomorrow-one",
                            "name": "Tomorrow One",
                            "date": 1778346000000,
                            "status": "active",
                            "type": "match",
                        }
                    ],
                },
            ],
        }

    async def get_odds(self, fixture_slug: str):
        self.odds_calls.append(fixture_slug)
        if fixture_slug == "today-two":
            raise StakeAPIError(404, "notFound")
        return {
            "fixture": {
                "slug": fixture_slug,
                "name": "Odds Name",
                "startTime": 1778259600000,
            },
            "groups": [
                {
                    "name": "main",
                    "markets": [
                        [
                            {
                                "status": "active",
                                "specifiers": "",
                                "updatedAt": 1778231452177,
                                "name": "Winner (Incl. Extra Innings)",
                                "outcomes": [
                                    {
                                        "odds": 1.56,
                                        "active": True,
                                        "name": "Home",
                                    },
                                    {
                                        "odds": 2.28,
                                        "active": True,
                                        "name": "Away",
                                    },
                                ],
                            }
                        ],
                        [
                            {
                                "status": "active",
                                "specifiers": "total=10.5",
                                "updatedAt": 1778231452177,
                                "name": "Total (Incl. Extra Innings)",
                                "outcomes": [
                                    {
                                        "odds": 1.85,
                                        "active": True,
                                        "name": "Over 10.5",
                                    }
                                ],
                            }
                        ],
                    ],
                }
            ],
            "swishMarkets": [],
        }


def test_build_slate_filters_by_date_and_attaches_odds():
    fake_client = SlateFakeStakeClient()

    slate = asyncio.run(
        build_slate(
            client=fake_client,
            sport="baseball",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=10,
        )
    )

    assert slate["sport"] == {"slug": "baseball"}
    assert slate["date"] == "2026-05-08"
    assert slate["timezone"] == "America/New_York"
    assert slate["fixtureCount"] == 2
    assert [fixture["slug"] for fixture in slate["fixtures"]] == [
        "today-one",
        "today-two",
    ]
    assert slate["fixtures"][0]["odds"]["groups"][0]["name"] == "main"
    assert slate["fixtures"][1]["odds"] is None
    assert slate["fixtures"][1]["oddsError"] == {
        "statusCode": 404,
        "message": "notFound",
    }
    assert fake_client.odds_calls == ["today-one", "today-two"]


def test_build_slate_applies_fixture_limit_before_fetching_odds():
    fake_client = SlateFakeStakeClient()

    slate = asyncio.run(
        build_slate(
            client=fake_client,
            sport="baseball",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=1,
        )
    )

    assert slate["fixtureCount"] == 1
    assert [fixture["slug"] for fixture in slate["fixtures"]] == ["today-one"]
    assert fake_client.odds_calls == ["today-one"]


def test_flatten_market_rows_handles_nested_stake_markets_and_dedupes():
    odds = {
        "groups": [
            {
                "name": "main",
                "markets": [
                    [
                        {
                            "status": "active",
                            "specifiers": "",
                            "updatedAt": 1778231452177,
                            "name": "Winner",
                            "outcomes": [
                                {"odds": 1.5, "active": True, "name": "Home"},
                                {"odds": 2.4, "active": True, "name": "Away"},
                            ],
                        }
                    ]
                ],
            },
            {
                "name": "winner",
                "markets": [
                    [
                        {
                            "status": "active",
                            "specifiers": "",
                            "updatedAt": 1778231452177,
                            "name": "Winner",
                            "outcomes": [
                                {"odds": 1.5, "active": True, "name": "Home"}
                            ],
                        }
                    ]
                ],
            },
        ]
    }

    assert flatten_market_rows(odds) == [
        {
            "group": "main",
            "market": "Winner",
            "marketStatus": "active",
            "specifiers": "",
            "selection": "Home",
            "odds": 1.5,
            "active": True,
            "updatedAt": 1778231452177,
        },
        {
            "group": "main",
            "market": "Winner",
            "marketStatus": "active",
            "specifiers": "",
            "selection": "Away",
            "odds": 2.4,
            "active": True,
            "updatedAt": 1778231452177,
        },
    ]


def test_build_market_slate_returns_clean_rows_without_raw_odds():
    fake_client = SlateFakeStakeClient()

    slate = asyncio.run(
        build_market_slate(
            client=fake_client,
            sport="baseball",
            slate_date=date(2026, 5, 8),
            timezone_name="America/New_York",
            limit=1,
        )
    )

    assert slate["fixtureCount"] == 1
    assert slate["marketRowCount"] == 3
    fixture = slate["fixtures"][0]
    assert fixture["name"] == "Odds Name"
    assert fixture["marketCount"] == 3
    assert "odds" not in fixture
    assert fixture["marketRows"][0] == {
        "group": "main",
        "market": "Winner (Incl. Extra Innings)",
        "marketStatus": "active",
        "specifiers": "",
        "selection": "Home",
        "odds": 1.56,
        "active": True,
        "updatedAt": 1778231452177,
    }
