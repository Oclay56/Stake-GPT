import pytest
from fastapi.testclient import TestClient

from app.main import app, get_mlb_engine, get_stake_client
from app.stake_client import StakeAPIError


class FakeStakeClient:
    async def get_sports(self):
        return [{"slug": "baseball", "enabled": True}]

    async def get_sport_categories(self, sport: str):
        return {"sport": "Baseball", "categories": [{"slug": "usa"}]}

    async def get_fixture(self, fixture_slug: str):
        if fixture_slug == "missing":
            raise StakeAPIError(404, "notFound")
        return {
            "fixture": {"slug": fixture_slug},
            "groups": [],
            "swishMarkets": [],
        }

    async def get_odds(self, fixture_slug: str):
        fixture_name = (
            "Cincinnati Reds - Houston Astros"
            if fixture_slug == "reds-astros"
            else "Fixture 123"
        )
        return {
            "fixture": {"slug": fixture_slug, "name": fixture_name},
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
                                    {
                                        "odds": 1.5,
                                        "active": True,
                                        "name": "Fixture 123",
                                    }
                                ],
                            }
                        ]
                    ],
                }
            ],
            "swishMarkets": {
                "playerProps": [
                    {
                        "competitorName": "Jose Altuve",
                        "teamName": "Houston Astros",
                        "marketName": "hits",
                        "sportStatType": "player",
                        "outcomes": [
                            {
                                "line": 1.5,
                                "over": 2.1,
                                "under": 1.7,
                            }
                        ],
                    },
                    {
                        "competitorName": "Hunter Greene",
                        "teamName": "Cincinnati Reds",
                        "marketName": "strikeouts",
                        "sportStatType": "player",
                        "outcomes": [
                            {
                                "line": 5.5,
                                "over": 1.91,
                                "under": 1.83,
                            }
                        ],
                    },
                ]
            },
        }

    async def get_sport_schedule(self, sport: str):
        return {
            "sport": {"slug": sport},
            "schedule": [
                {
                    "date": 1778259600000,
                    "fixtures": [
                        {
                            "slug": "fixture-123",
                            "name": "Fixture 123",
                            "date": 1778259600000,
                            "status": "active",
                            "type": "match",
                        }
                    ],
                }
            ],
        }

    async def get_tournament_schedule(
        self,
        sport: str,
        category: str,
        tournament: str,
    ):
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


class FakeMLBEngine:
    async def get_teams(self, season=None):
        return {
            "season": season,
            "teamCount": 1,
            "teams": [
                {
                    "mlbId": 117,
                    "name": "Houston Astros",
                    "key": "houston-astros",
                    "abbreviation": "HOU",
                    "clubName": "Astros",
                    "league": "American League",
                    "division": "AL West",
                }
            ],
        }

    async def get_schedule(self, game_date: str):
        return {
            "date": game_date,
            "gameCount": 1,
            "games": [{"gamePk": 824522, "awayTeam": {}, "homeTeam": {}}],
        }

    async def get_team_roster(self, team_id: int, season=None):
        return {
            "teamId": team_id,
            "season": season,
            "playerCount": 1,
            "players": [{"mlbId": 592450, "name": "Aaron Judge"}],
        }

    async def search_players(self, query: str, limit: int = 10):
        if query == "Jose Altuve":
            return {
                "query": query,
                "playerCount": 1,
                "players": [
                    {
                        "mlbId": 514888,
                        "name": "Jose Altuve",
                        "key": "jose-altuve",
                        "team": {
                            "mlbId": 117,
                            "name": "Houston Astros",
                            "key": "houston-astros",
                        },
                    }
                ],
            }
        return {
            "query": query,
            "playerCount": 1,
            "players": [{"mlbId": 592450, "name": "Aaron Judge", "key": "aaron-judge"}],
        }

    async def get_player_profile(
        self,
        player_id: int,
        season=None,
        group: str = "hitting",
    ):
        return {
            "player": {
                "mlbId": player_id,
                "name": "Jose Altuve" if player_id == 514888 else "Aaron Judge",
                "key": "jose-altuve" if player_id == 514888 else "aaron-judge",
                "stats": {"hits": 51} if player_id == 514888 else {"homeRuns": 15},
            },
            "season": season,
            "group": group,
        }

    async def get_player_recent_history(
        self,
        player_id: int,
        group: str = "hitting",
        season=None,
        limit: int = 10,
    ):
        return {
            "playerId": player_id,
            "group": group,
            "season": season,
            "gamesUsed": 1,
            "games": [{"date": "2026-05-07", "stats": {"hits": 2}}],
            "totals": {"hits": 2.0},
            "perGame": {"hits": 2.0},
        }


@pytest.fixture(autouse=True)
def override_clients():
    app.dependency_overrides[get_stake_client] = lambda: FakeStakeClient()
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMLBEngine()
    yield
    app.dependency_overrides.clear()


def test_health_route():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sports_route():
    with TestClient(app) as client:
        response = client.get("/sports")

    assert response.status_code == 200
    assert response.json() == [{"slug": "baseball", "enabled": True}]


def test_categories_route():
    with TestClient(app) as client:
        response = client.get("/sports/baseball/categories")

    assert response.status_code == 200
    assert response.json()["categories"] == [{"slug": "usa"}]


def test_schedule_route():
    with TestClient(app) as client:
        response = client.get("/schedule/baseball")

    assert response.status_code == 200
    assert response.json()["sport"] == {"slug": "baseball"}
    assert response.json()["schedule"][0]["fixtures"][0]["slug"] == "fixture-123"


def test_fixture_route_maps_missing_fixture_to_http_status():
    with TestClient(app) as client:
        response = client.get("/fixtures/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "notFound"}


def test_odds_route_returns_normalized_odds_payload():
    with TestClient(app) as client:
        response = client.get("/odds/fixture-123")

    assert response.status_code == 200
    assert response.json()["fixture"] == {
        "slug": "fixture-123",
        "name": "Fixture 123",
    }
    assert response.json()["groups"][0]["name"] == "main"


def test_slate_route_returns_fixture_odds_for_requested_date():
    with TestClient(app) as client:
        response = client.get("/slate/baseball?date=2026-05-08")

    assert response.status_code == 200
    assert response.json() == {
        "sport": {"slug": "baseball"},
        "date": "2026-05-08",
        "timezone": "America/New_York",
        "fixtureCount": 1,
        "fixtures": [
            {
                "slug": "fixture-123",
                "name": "Fixture 123",
                "startTime": 1778259600000,
                "status": "active",
                "type": "match",
                "odds": {
                    "fixture": {"slug": "fixture-123", "name": "Fixture 123"},
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
                                            {
                                                "odds": 1.5,
                                                "active": True,
                                                "name": "Fixture 123",
                                            }
                                        ],
                                    }
                                ]
                            ],
                        }
                    ],
                    "swishMarkets": {
                        "playerProps": [
                            {
                                "competitorName": "Jose Altuve",
                                "teamName": "Houston Astros",
                                "marketName": "hits",
                                "sportStatType": "player",
                                "outcomes": [
                                    {
                                        "line": 1.5,
                                        "over": 2.1,
                                        "under": 1.7,
                                    }
                                ],
                            },
                            {
                                "competitorName": "Hunter Greene",
                                "teamName": "Cincinnati Reds",
                                "marketName": "strikeouts",
                                "sportStatType": "player",
                                "outcomes": [
                                    {
                                        "line": 5.5,
                                        "over": 1.91,
                                        "under": 1.83,
                                    }
                                ],
                            },
                        ]
                    },
                },
                "oddsError": None,
            }
        ],
    }


def test_slate_markets_route_returns_clean_market_rows():
    with TestClient(app) as client:
        response = client.get("/slate/baseball/markets?date=2026-05-08")

    assert response.status_code == 200
    body = response.json()
    assert body["marketRowCount"] == 1
    assert "odds" not in body["fixtures"][0]
    assert body["fixtures"][0]["marketRows"] == [
        {
            "group": "main",
            "market": "Winner",
            "marketStatus": "active",
            "specifiers": "",
            "selection": "Fixture 123",
            "odds": 1.5,
            "active": True,
            "updatedAt": 1778231452177,
        }
    ]


def test_slate_view_route_returns_html_table():
    with TestClient(app) as client:
        response = client.get("/slate/baseball/view?date=2026-05-08")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Baseball Slate" in response.text
    assert "Fixture 123" in response.text
    assert "Winner" in response.text


def test_mlb_player_props_route_returns_clean_player_rows():
    with TestClient(app) as client:
        response = client.get("/slate/mlb/player-props?date=2026-05-08")

    assert response.status_code == 200
    body = response.json()
    assert body["league"] == "MLB"
    assert body["playerPropRowCount"] == 2
    assert body["fixtures"][0]["slug"] == "reds-astros"
    assert body["fixtures"][0]["playerProps"] == [
        {
            "player": "Jose Altuve",
            "team": "Houston Astros",
            "market": "hits",
            "sportStatType": "player",
            "line": 1.5,
            "over": 2.1,
            "under": 1.7,
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


def test_mlb_player_props_route_filters_markets():
    with TestClient(app) as client:
        response = client.get("/slate/mlb/player-props?date=2026-05-08&markets=hits")

    assert response.status_code == 200
    body = response.json()
    assert body["filters"] == {"markets": ["hits"], "excludeMarkets": []}
    assert body["playerPropRowCount"] == 1
    assert body["fixtures"][0]["playerProps"][0]["market"] == "hits"


def test_mlb_player_props_alias_keeps_old_route_working():
    with TestClient(app) as client:
        old_response = client.get(
            "/slate/mlb/player-props?date=2026-05-08&markets=hits"
        )
        alias_response = client.get("/mlb/player-props?date=2026-05-08&markets=hits")

    assert alias_response.status_code == 200
    assert alias_response.json() == old_response.json()


def test_mlb_player_props_view_route_returns_html_table():
    with TestClient(app) as client:
        response = client.get(
            "/slate/mlb/player-props/view?date=2026-05-08"
            "&limit=1&lineMode=primary&refreshSeconds=15"
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "MLB Player Props" in response.text
    assert "Jose Altuve" in response.text
    assert 'id="searchBox"' in response.text
    assert 'id="marketFilter"' in response.text
    assert 'id="lastUpdated"' in response.text
    assert 'data-url="/slate/mlb/player-props?date=2026-05-08&amp;limit=1&amp;lineMode=primary"' in response.text
    assert "setInterval(refreshBoard, 15000)" in response.text
    assert "window.location.reload" not in response.text
    assert "location.href" not in response.text


def test_mlb_player_props_view_alias_uses_alias_data_url_and_filters():
    with TestClient(app) as client:
        response = client.get(
            "/mlb/player-props/view?date=2026-05-08"
            "&limit=1&lineMode=primary&markets=hits&excludeMarkets=first+h"
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'data-url="/mlb/player-props?' in response.text
    assert "markets=hits" in response.text
    assert "excludeMarkets=first+h" in response.text


def test_mlb_props_route_returns_stable_analyzer_rows():
    with TestClient(app) as client:
        response = client.get("/mlb/props?date=2026-05-08&markets=hits")
        movement_response = client.get("/mlb/line-movement")

    assert response.status_code == 200
    body = response.json()
    assert body["propCount"] == 1
    assert body["props"][0]["propId"] == "reds-astros:jose-altuve:houston-astros:hits"
    assert body["props"][0]["player"] == {
        "name": "Jose Altuve",
        "key": "jose-altuve",
        "mlbId": None,
        "matchStatus": "needs_mlb_lookup",
    }
    assert body["props"][0]["team"]["fixtureTeamMatched"] is True
    assert body["props"][0]["market"] == {"name": "hits", "key": "hits"}
    assert body["props"][0]["odds"] == {"over": 2.1, "under": 1.7}
    assert body["props"][0]["movement"]["snapshotCount"] >= 1
    assert movement_response.status_code == 200
    assert movement_response.json()["trackedPropCount"] >= 1


def test_mlb_props_enriched_route_matches_stake_props_to_mlb_players():
    with TestClient(app) as client:
        response = client.get(
            "/mlb/props/enriched?date=2026-05-08"
            "&markets=hits&season=2026&historyLimit=5"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["matchedPropCount"] == 1
    assert body["unmatchedPropCount"] == 0
    assert body["props"][0]["player"]["mlbId"] == 514888
    assert body["props"][0]["player"]["matchStatus"] == "matched_exact_name_team"
    assert body["props"][0]["mlbProfile"]["player"]["stats"] == {"hits": 51}
    assert body["props"][0]["recentHistory"]["perGame"] == {"hits": 2.0}
    assert body["props"][0]["statContext"]["statKey"] == "hits"


def test_mlb_props_match_audit_route_returns_match_quality_summary():
    with TestClient(app) as client:
        response = client.get(
            "/mlb/props/match-audit?date=2026-05-08"
            "&markets=hits&season=2026&historyLimit=5"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["matchedPropCount"] == 1
    assert body["unmatchedPropCount"] == 0
    assert body["statusCounts"] == {"matched_exact_name_team": 1}
    assert body["rows"][0]["player"] == "Jose Altuve"
    assert body["rows"][0]["issues"] == []


def test_mlb_primary_line_check_route_returns_diagnostic_report():
    with TestClient(app) as client:
        response = client.get("/mlb/primary-line-check?date=2026-05-08&limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["league"] == "MLB"
    assert body["checkedPropCount"] == 2
    assert body["checks"][0]["method"] == "closest-over-under-balance"
    assert body["checks"][0]["validLineCount"] == 1


def test_mlb_data_routes_expose_engine_only_shapes():
    with TestClient(app) as client:
        teams = client.get("/mlb-data/teams?season=2026")
        schedule = client.get("/mlb-data/schedule?date=2026-05-08")
        roster = client.get("/mlb-data/teams/117/roster?season=2026")
        search = client.get("/mlb-data/players/search?query=Aaron+Judge")
        player = client.get("/mlb-data/players/592450?season=2026")
        history = client.get("/mlb-data/players/592450/history?season=2026&limit=5")

    assert teams.status_code == 200
    assert teams.json()["teams"][0]["key"] == "houston-astros"
    assert schedule.status_code == 200
    assert schedule.json()["date"] == "2026-05-08"
    assert roster.status_code == 200
    assert roster.json()["players"][0]["name"] == "Aaron Judge"
    assert search.status_code == 200
    assert search.json()["players"][0]["mlbId"] == 592450
    assert player.status_code == 200
    assert player.json()["player"]["stats"] == {"homeRuns": 15}
    assert history.status_code == 200
    assert history.json()["perGame"] == {"hits": 2.0}
