import asyncio

import httpx

from app.mlb_data.client import MLBStatsClient
from app.mlb_data.engine import MLBDataEngine


def test_mlb_stats_client_uses_official_endpoint_paths():
    asyncio.run(_run_mlb_stats_client_uses_official_endpoint_paths())


async def _run_mlb_stats_client_uses_official_endpoint_paths():
    seen_requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.path.endswith("/teams/117/roster"):
            return httpx.Response(200, json={"roster": []})
        if request.url.path.endswith("/people/search"):
            return httpx.Response(200, json={"people": []})
        if request.url.path.endswith("/people/592450/stats"):
            return httpx.Response(200, json={"stats": [{"splits": []}]})
        if request.url.path.endswith("/people/592450"):
            return httpx.Response(200, json={"people": []})
        if request.url.path.endswith("/schedule"):
            return httpx.Response(200, json={"dates": []})
        return httpx.Response(200, json={"teams": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://statsapi.mlb.com/api/v1",
    ) as http_client:
        client = MLBStatsClient(http_client)
        await client.get_teams(season=2026)
        await client.get_schedule("2026-05-08")
        await client.get_team_roster(117, season=2026)
        await client.search_players("Aaron Judge")
        await client.get_player(592450)
        await client.get_player_stats(592450, group="hitting", season=2026)
        await client.get_player_game_log(592450, group="hitting", season=2026)

    assert seen_requests[0].url.path == "/api/v1/teams"
    assert dict(seen_requests[0].url.params) == {"sportId": "1", "season": "2026"}
    assert seen_requests[1].url.path == "/api/v1/schedule"
    assert dict(seen_requests[1].url.params) == {
        "sportId": "1",
        "date": "2026-05-08",
        "hydrate": "probablePitcher",
    }
    assert seen_requests[2].url.path == "/api/v1/teams/117/roster"
    assert dict(seen_requests[2].url.params) == {
        "rosterType": "active",
        "season": "2026",
    }
    assert seen_requests[3].url.path == "/api/v1/people/search"
    assert dict(seen_requests[3].url.params) == {
        "names": "Aaron Judge",
        "sportId": "1",
    }
    assert seen_requests[4].url.path == "/api/v1/people/592450"
    assert seen_requests[5].url.path == "/api/v1/people/592450/stats"
    assert dict(seen_requests[5].url.params) == {
        "stats": "season",
        "group": "hitting",
        "season": "2026",
    }
    assert dict(seen_requests[6].url.params) == {
        "stats": "gameLog",
        "group": "hitting",
        "season": "2026",
    }


class FakeMLBStatsClient:
    async def get_teams(self, season=None):
        return {
            "teams": [
                {
                    "id": 117,
                    "name": "Houston Astros",
                    "abbreviation": "HOU",
                    "clubName": "Astros",
                    "league": {"name": "American League"},
                    "division": {"name": "AL West"},
                }
            ]
        }

    async def get_schedule(self, game_date: str):
        return {
            "totalGames": 1,
            "dates": [
                {
                    "date": game_date,
                    "games": [
                        {
                            "gamePk": 824522,
                            "gameDate": f"{game_date}T23:05:00Z",
                            "status": {"detailedState": "Scheduled"},
                            "teams": {
                                "away": {
                                    "team": {"id": 113, "name": "Cincinnati Reds"},
                                    "probablePitcher": {
                                        "id": 668881,
                                        "fullName": "Hunter Greene",
                                    },
                                },
                                "home": {
                                    "team": {"id": 117, "name": "Houston Astros"},
                                    "probablePitcher": {
                                        "id": 805123,
                                        "fullName": "AJ Blubaugh",
                                    },
                                },
                            },
                        }
                    ],
                }
            ],
        }

    async def get_team_roster(self, team_id: int, season=None, roster_type="active"):
        return {
            "roster": [
                {
                    "person": {"id": 592450, "fullName": "Aaron Judge"},
                    "position": {"abbreviation": "RF", "name": "Outfielder"},
                    "status": {"description": "Active"},
                }
            ]
        }

    async def search_players(self, query: str):
        return {
            "people": [
                {
                    "id": 592450,
                    "fullName": "Aaron Judge",
                    "primaryPosition": {"abbreviation": "RF"},
                    "currentTeam": {"id": 147, "name": "New York Yankees"},
                    "active": True,
                }
            ]
        }

    async def get_player(self, player_id: int):
        return {
            "people": [
                {
                    "id": player_id,
                    "fullName": "Aaron Judge",
                    "primaryPosition": {"abbreviation": "RF"},
                    "currentTeam": {"id": 147, "name": "New York Yankees"},
                    "batSide": {"code": "R"},
                    "pitchHand": {"code": "R"},
                    "active": True,
                }
            ]
        }

    async def get_player_stats(self, player_id: int, group: str, season=None):
        return {
            "stats": [
                {
                    "splits": [
                        {
                            "season": "2026",
                            "stat": {"avg": ".270", "homeRuns": 15, "rbi": 38},
                        }
                    ]
                }
            ]
        }

    async def get_player_game_log(self, player_id: int, group: str, season=None):
        return {
            "stats": [
                {
                    "splits": [
                        {
                            "game": {"gamePk": 824520},
                            "date": "2026-05-06",
                            "opponent": {"name": "Boston Red Sox"},
                            "isHome": True,
                            "stat": {"hits": 2, "rbi": 1, "homeRuns": 1},
                        },
                        {
                            "game": {"gamePk": 824522},
                            "date": "2026-05-07",
                            "opponent": {"name": "Tampa Bay Rays"},
                            "isHome": False,
                            "stat": {"hits": 1, "rbi": 0, "homeRuns": 0},
                        },
                    ]
                }
            ]
        }


def test_mlb_data_engine_normalizes_core_shapes():
    engine = MLBDataEngine(FakeMLBStatsClient())

    teams = asyncio.run(engine.get_teams(season=2026))
    schedule = asyncio.run(engine.get_schedule("2026-05-08"))
    roster = asyncio.run(engine.get_team_roster(117, season=2026))
    players = asyncio.run(engine.search_players("Aaron Judge"))
    player = asyncio.run(engine.get_player_profile(592450, season=2026, group="hitting"))
    history = asyncio.run(
        engine.get_player_recent_history(
            592450,
            group="hitting",
            season=2026,
            limit=2,
        )
    )

    assert teams["teams"][0] == {
        "mlbId": 117,
        "name": "Houston Astros",
        "key": "houston-astros",
        "abbreviation": "HOU",
        "clubName": "Astros",
        "league": "American League",
        "division": "AL West",
    }
    assert schedule["games"][0]["homeTeam"] == {
        "mlbId": 117,
        "name": "Houston Astros",
        "key": "houston-astros",
        "probablePitcher": {
            "mlbId": 805123,
            "name": "AJ Blubaugh",
            "key": "aj-blubaugh",
        },
    }
    assert roster["players"][0]["name"] == "Aaron Judge"
    assert players["players"][0]["key"] == "aaron-judge"
    assert player["player"]["stats"] == {"avg": ".270", "homeRuns": 15, "rbi": 38}
    assert history["games"][0]["gamePk"] == 824522
    assert history["games"][0]["date"] == "2026-05-07"
    assert history["totals"] == {"hits": 3.0, "rbi": 1.0, "homeRuns": 1.0}
    assert history["perGame"] == {"hits": 1.5, "rbi": 0.5, "homeRuns": 0.5}
