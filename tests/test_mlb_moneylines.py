import asyncio
from datetime import date, timedelta

from app.mlb_moneylines import enrich_stake_ui_moneylines


SLATE_DATE = date(2026, 5, 31)


def _raw_board():
    return {
        "source": "stake_ui_mlb_moneylines_raw",
        "capturedAt": "2026-05-31T12:00:00+00:00",
        "url": "https://stake.com/sports/baseball/usa/mlb",
        "warnings": [],
        "games": [
            {
                "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                "matchup": "New York Yankees vs Toronto Blue Jays",
                "status": "pregame",
                "marketLabel": "Winner (incl. Extra Innings)",
                "selections": [
                    {"team": "New York Yankees", "odds": 1.72, "rowId": "mlb_ml_yankees"},
                    {"team": "Toronto Blue Jays", "odds": 2.08, "rowId": "mlb_ml_blue-jays"},
                ],
            },
            {
                "fixtureSlug": "456-los-angeles-dodgers-san-diego-padres",
                "matchup": "Los Angeles Dodgers vs San Diego Padres",
                "status": "pregame",
                "marketLabel": "Winner (incl. Extra Innings)",
                "selections": [
                    {"team": "Los Angeles Dodgers", "odds": 1.81, "rowId": "mlb_ml_dodgers"},
                    {"team": "San Diego Padres", "odds": 1.99, "rowId": "mlb_ml_padres"},
                ],
            },
        ],
    }


class FakeMoneylineMLBEngine:
    recent_limit = 15

    async def get_teams(self, season=None):
        return {
            "teams": [
                {"mlbId": 147, "name": "New York Yankees", "key": "new-york-yankees"},
                {"mlbId": 141, "name": "Toronto Blue Jays", "key": "toronto-blue-jays"},
                {"mlbId": 119, "name": "Los Angeles Dodgers", "key": "los-angeles-dodgers"},
                {"mlbId": 135, "name": "San Diego Padres", "key": "san-diego-padres"},
            ]
        }

    async def get_schedule(self, game_date: str):
        return {
            "games": [
                {
                    "gamePk": 900001,
                    "gameDate": f"{game_date}T17:05:00Z",
                    "status": "Scheduled",
                    "awayTeam": {
                        "mlbId": 147,
                        "name": "New York Yankees",
                        "key": "new-york-yankees",
                        "score": None,
                        "isWinner": None,
                        "probablePitcher": {"mlbId": 1001, "name": "Yankees Starter"},
                    },
                    "homeTeam": {
                        "mlbId": 141,
                        "name": "Toronto Blue Jays",
                        "key": "toronto-blue-jays",
                        "score": None,
                        "isWinner": None,
                        "probablePitcher": {"mlbId": 1002, "name": "Blue Jays Starter"},
                    },
                }
            ]
        }

    async def get_schedule_range(self, start_date: str, end_date: str):
        games = []
        for index in range(self.recent_limit):
            game_date = SLATE_DATE - timedelta(days=index + 1)
            yankees_home = index % 2 == 0
            yankees_score = 5 if index % 3 else 2
            blue_jays_score = 3 if index % 3 else 4
            yankees = {
                "mlbId": 147,
                "name": "New York Yankees",
                "key": "new-york-yankees",
                "score": yankees_score,
                "isWinner": yankees_score > blue_jays_score,
                "probablePitcher": None,
            }
            blue_jays = {
                "mlbId": 141,
                "name": "Toronto Blue Jays",
                "key": "toronto-blue-jays",
                "score": blue_jays_score,
                "isWinner": blue_jays_score > yankees_score,
                "probablePitcher": None,
            }
            games.append(
                {
                    "gamePk": 800000 + index,
                    "gameDate": f"{game_date.isoformat()}T17:05:00Z",
                    "status": "Final",
                    "awayTeam": blue_jays if yankees_home else yankees,
                    "homeTeam": yankees if yankees_home else blue_jays,
                }
            )
        return {"games": games}

    async def get_standings(self, season: int):
        return {
            "teamsById": {
                147: {
                    "mlbId": 147,
                    "name": "New York Yankees",
                    "key": "new-york-yankees",
                    "wins": 34,
                    "losses": 22,
                    "pct": ".607",
                },
                141: {
                    "mlbId": 141,
                    "name": "Toronto Blue Jays",
                    "key": "toronto-blue-jays",
                    "wins": 29,
                    "losses": 27,
                    "pct": ".518",
                },
            }
        }


def test_enrich_mlb_moneyline_board_maps_team_context():
    result = asyncio.run(
        enrich_stake_ui_moneylines(
            _raw_board(),
            FakeMoneylineMLBEngine(),
            slate_date=SLATE_DATE,
            fixture_slugs=["123-new-york-yankees-toronto-blue-jays"],
        )
    )

    game = result["games"][0]
    yankees = game["selections"][0]

    assert result["source"] == "stake_ui_mlb_moneylines"
    assert result["decisionOwner"] == "custom_gpt"
    assert result["builderRole"] == "read_only_moneyline_research_not_final_recommendation"
    assert result["market"] == "winner_including_extra_innings"
    assert result["pregameOnly"] is True
    assert yankees["teamContext"]["mlbTeamId"] == 147
    assert yankees["teamContext"]["seasonRecord"] == {
        "wins": 34,
        "losses": 22,
        "pct": ".607",
    }
    assert yankees["teamContext"]["last5"]["gamesUsed"] == 5
    assert yankees["teamContext"]["last10"]["gamesUsed"] == 10
    assert yankees["teamContext"]["last15"]["gamesUsed"] == 15
    assert yankees["teamContext"]["probablePitcher"]["name"] == "Yankees Starter"
    assert yankees["teamContext"]["opponent"]["name"] == "Toronto Blue Jays"
    assert yankees["teamContext"]["homeAwaySplit"]["scope"] == "recent_completed_games"


def test_enrich_mlb_moneyline_board_keeps_visible_row_with_partial_warning():
    engine = FakeMoneylineMLBEngine()
    engine.recent_limit = 3

    result = asyncio.run(
        enrich_stake_ui_moneylines(
            _raw_board(),
            engine,
            slate_date=SLATE_DATE,
            fixture_slugs=["123-new-york-yankees-toronto-blue-jays"],
        )
    )

    selection = result["games"][0]["selections"][0]
    assert selection["teamContext"]["last15"]["gamesUsed"] == 3
    assert "partial_recent_sample" in selection["warnings"]


def test_enrich_mlb_moneyline_board_excludes_non_final_scored_games():
    class LiveScoredGameEngine(FakeMoneylineMLBEngine):
        async def get_schedule_range(self, start_date: str, end_date: str):
            result = await super().get_schedule_range(start_date, end_date)
            result["games"].append(
                {
                    "gamePk": 999999,
                    "gameDate": "2026-05-31T16:00:00Z",
                    "status": "In Progress",
                    "awayTeam": {
                        "mlbId": 147,
                        "name": "New York Yankees",
                        "key": "new-york-yankees",
                        "score": 99,
                        "isWinner": True,
                        "probablePitcher": None,
                    },
                    "homeTeam": {
                        "mlbId": 141,
                        "name": "Toronto Blue Jays",
                        "key": "toronto-blue-jays",
                        "score": 0,
                        "isWinner": False,
                        "probablePitcher": None,
                    },
                }
            )
            return result

    result = asyncio.run(
        enrich_stake_ui_moneylines(
            _raw_board(),
            LiveScoredGameEngine(),
            slate_date=SLATE_DATE,
            fixture_slugs=["123-new-york-yankees-toronto-blue-jays"],
        )
    )

    yankees = result["games"][0]["selections"][0]["teamContext"]
    assert all(row["gamePk"] != 999999 for row in yankees["last15"]["results"])


def test_enrich_mlb_moneyline_board_warns_when_team_identity_is_unmatched():
    board = _raw_board()
    board["games"][0]["selections"][0]["team"] = "Unknown Expansion Team"

    result = asyncio.run(
        enrich_stake_ui_moneylines(
            board,
            FakeMoneylineMLBEngine(),
            slate_date=SLATE_DATE,
            fixture_slugs=["123-new-york-yankees-toronto-blue-jays"],
        )
    )

    selection = result["games"][0]["selections"][0]
    assert selection["teamContext"] is None
    assert "team_identity_unmatched" in selection["warnings"]


def test_enrich_mlb_moneyline_board_filters_fixture_and_matchup_requests():
    engine = FakeMoneylineMLBEngine()

    by_fixture = asyncio.run(
        enrich_stake_ui_moneylines(
            _raw_board(),
            engine,
            slate_date=SLATE_DATE,
            fixture_slugs=["456-los-angeles-dodgers-san-diego-padres"],
        )
    )
    by_matchup = asyncio.run(
        enrich_stake_ui_moneylines(
            _raw_board(),
            engine,
            slate_date=SLATE_DATE,
            matchups=["Yankees vs Blue Jays"],
        )
    )

    assert [game["fixtureSlug"] for game in by_fixture["games"]] == [
        "456-los-angeles-dodgers-san-diego-padres"
    ]
    assert [game["fixtureSlug"] for game in by_matchup["games"]] == [
        "123-new-york-yankees-toronto-blue-jays"
    ]
    dodgers = by_fixture["games"][0]["selections"][0]
    assert dodgers["teamContext"]["homeAwaySplit"] is None
    assert "scheduled_matchup_unavailable" in dodgers["warnings"]
