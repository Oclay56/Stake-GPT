import asyncio

from app.mlb_bridge import (
    build_match_audit,
    clear_mlb_bridge_cache,
    enrich_props_with_mlb_data,
    group_for_market,
    stat_value_from_stats,
    stat_mapping_for_market,
)


class BridgeFakeMLBEngine:
    def __init__(self):
        self.search_calls = []
        self.profile_calls = []
        self.history_calls = []
        self.schedule_calls = []
        self.roster_calls = []

    async def search_players(self, query: str, limit: int = 10):
        self.search_calls.append((query, limit))
        if query == "Jose Altuve":
            return {
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
                    },
                    {
                        "mlbId": 999999,
                        "name": "Jose Altuve",
                        "key": "jose-altuve",
                        "team": {
                            "mlbId": 120,
                            "name": "Washington Nationals",
                            "key": "washington-nationals",
                        },
                    },
                ]
            }
        if query == "Teamless Search":
            return {
                "players": [
                    {
                        "mlbId": 123456,
                        "name": "Teamless Search",
                        "key": "teamless-search",
                        "team": None,
                    }
                ]
            }
        if query == "Roster Search":
            return {
                "players": [
                    {
                        "mlbId": 234567,
                        "name": "Roster Search",
                        "key": "roster-search",
                        "team": None,
                    }
                ]
            }
        return {"players": []}

    async def get_player_profile(
        self,
        player_id: int,
        season=None,
        group: str = "hitting",
    ):
        self.profile_calls.append((player_id, season, group))
        if player_id == 123456:
            return {
                "player": {
                    "mlbId": player_id,
                    "name": "Teamless Search",
                    "key": "teamless-search",
                    "team": {
                        "mlbId": 117,
                        "name": "Houston Astros",
                        "key": "houston-astros",
                    },
                    "stats": {"hits": 12},
                },
                "season": season,
                "group": group,
            }
        if player_id == 234567:
            return {
                "player": {
                    "mlbId": player_id,
                    "name": "Roster Search",
                    "key": "roster-search",
                    "team": None,
                    "stats": {"hits": 8},
                },
                "season": season,
                "group": group,
            }
        return {
            "player": {
                "mlbId": player_id,
                "name": "Jose Altuve",
                "key": "jose-altuve",
                "stats": {"hits": 51, "rbi": 24},
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
        self.history_calls.append((player_id, group, season, limit))
        return {
            "playerId": player_id,
            "group": group,
            "season": season,
            "gamesUsed": 3,
            "games": [{"date": "2026-05-07", "stats": {"hits": 2}}],
            "totals": {"hits": 5.0},
            "perGame": {"hits": 1.6667},
        }

    async def get_schedule(self, game_date: str):
        self.schedule_calls.append(game_date)
        return {
            "date": game_date,
            "gameCount": 1,
            "games": [
                {
                    "gamePk": 824522,
            "awayTeam": {
                "mlbId": 113,
                "name": "Cincinnati Reds",
                "key": "cincinnati-reds",
                "probablePitcher": None,
            },
            "homeTeam": {
                "mlbId": 117,
                "name": "Houston Astros",
                "key": "houston-astros",
                "probablePitcher": None,
            },
        }
            ],
        }

    async def get_team_roster(self, team_id: int, season=None):
        self.roster_calls.append((team_id, season))
        if team_id == 117:
            return {
                "teamId": team_id,
                "season": season,
                "playerCount": 1,
                "players": [
                    {
                        "mlbId": 234567,
                        "name": "Roster Search",
                        "key": "roster-search",
                        "position": "2B",
                    }
                ],
            }
        return {"teamId": team_id, "season": season, "playerCount": 0, "players": []}


class ContextBridgeFakeMLBEngine(BridgeFakeMLBEngine):
    async def get_schedule(self, game_date: str):
        self.schedule_calls.append(game_date)
        return {
            "date": game_date,
            "gameCount": 1,
            "games": [
                {
                    "gamePk": 824522,
                    "gameDate": f"{game_date}T23:05:00Z",
                    "status": "Scheduled",
                    "awayTeam": {
                        "mlbId": 113,
                        "name": "Cincinnati Reds",
                        "key": "cincinnati-reds",
                        "probablePitcher": {
                            "mlbId": 1111,
                            "name": "Reds Starter",
                            "key": "reds-starter",
                        },
                    },
                    "homeTeam": {
                        "mlbId": 117,
                        "name": "Houston Astros",
                        "key": "houston-astros",
                        "probablePitcher": {
                            "mlbId": 2222,
                            "name": "Astros Starter",
                            "key": "astros-starter",
                        },
                    },
                }
            ],
        }

    async def get_game_context(self, game_pk: int):
        return {
            "gamePk": game_pk,
            "gameDate": "2026-05-08T23:05:00Z",
            "officialDate": "2026-05-08",
            "status": {"detailedState": "Scheduled"},
            "statusRiskFlags": [],
            "gameInfo": {"dayNight": "night", "doubleHeader": "N", "gameNumber": 1},
            "venue": {"name": "Test Park", "roofType": "Open"},
            "weather": {"condition": "Clear", "temp": "78", "wind": "5 mph"},
            "teams": {
                "away": {
                    "team": {"mlbId": 113, "name": "Cincinnati Reds", "key": "cincinnati-reds"},
                    "lineupConfirmed": False,
                    "battingOrder": [],
                    "lineup": [],
                    "playersById": {},
                },
                "home": {
                    "team": {"mlbId": 117, "name": "Houston Astros", "key": "houston-astros"},
                    "lineupConfirmed": True,
                    "battingOrder": [514888],
                    "lineup": [
                        {
                            "mlbId": 514888,
                            "name": "Jose Altuve",
                            "batSide": "R",
                            "position": "2B",
                            "battingOrder": 1,
                            "confirmedStarter": True,
                        }
                    ],
                    "playersById": {
                        "514888": {
                            "mlbId": 514888,
                            "name": "Jose Altuve",
                            "batSide": "R",
                            "position": "2B",
                            "battingOrder": 1,
                            "confirmedStarter": True,
                        }
                    },
                },
            },
        }

    async def get_player_profile(
        self,
        player_id: int,
        season=None,
        group: str = "hitting",
    ):
        if player_id == 1111:
            return {
                "player": {
                    "mlbId": 1111,
                    "name": "Reds Starter",
                    "key": "reds-starter",
                    "pitchHand": "R",
                    "stats": {
                        "gamesPlayed": 12,
                        "gamesStarted": 12,
                        "hits": 60,
                        "baseOnBalls": 18,
                        "strikeOuts": 70,
                        "homeRuns": 8,
                    },
                },
                "season": season,
                "group": group,
            }
        return await super().get_player_profile(player_id, season=season, group=group)

    async def get_player_splits(
        self,
        player_id: int,
        group: str = "hitting",
        season=None,
        sit_codes=None,
    ):
        return {
            "playerId": player_id,
            "group": group,
            "season": season,
            "sitCodes": sit_codes,
            "splitCount": 1,
            "splits": [
                {
                    "split": "Home",
                    "type": "home/away",
                    "stat": {"gamesPlayed": 10, "hits": 15},
                }
            ],
        }


def _props_payload():
    return {
        "league": "MLB",
        "date": "2026-05-08",
        "timezone": "America/New_York",
        "filters": {"markets": ["hits"], "excludeMarkets": []},
        "fixtureCount": 1,
        "propCount": 2,
        "props": [
            {
                "propId": "reds-astros:jose-altuve:houston-astros:hits",
                "fixtureSlug": "reds-astros",
                "game": "Cincinnati Reds - Houston Astros",
                "player": {
                    "name": "Jose Altuve",
                    "key": "jose-altuve",
                    "mlbId": None,
                    "matchStatus": "needs_mlb_lookup",
                },
                "team": {
                    "name": "Houston Astros",
                    "key": "houston-astros",
                    "mlbId": None,
                    "matchStatus": "fixture_team_match",
                },
                "market": {"name": "hits", "key": "hits"},
                "line": 0.5,
                "odds": {"over": 1.8, "under": 1.9},
            },
            {
                "propId": "reds-astros:unknown-player:houston-astros:hits",
                "fixtureSlug": "reds-astros",
                "game": "Cincinnati Reds - Houston Astros",
                "player": {
                    "name": "Unknown Player",
                    "key": "unknown-player",
                    "mlbId": None,
                    "matchStatus": "needs_mlb_lookup",
                },
                "team": {
                    "name": "Houston Astros",
                    "key": "houston-astros",
                    "mlbId": None,
                    "matchStatus": "fixture_team_match",
                },
                "market": {"name": "hits", "key": "hits"},
                "line": 0.5,
                "odds": {"over": 2.1, "under": 1.7},
            },
        ],
    }


def _roster_search_props_payload():
    payload = _teamless_search_props_payload()
    payload["props"][0]["propId"] = "reds-astros:roster-search:houston-astros:hits"
    payload["props"][0]["player"] = {
        "name": "Roster Search",
        "key": "roster-search",
        "mlbId": None,
        "matchStatus": "needs_mlb_lookup",
    }
    return payload


def _teamless_search_props_payload():
    return {
        "league": "MLB",
        "date": "2026-05-08",
        "timezone": "America/New_York",
        "filters": {"markets": ["hits"], "excludeMarkets": []},
        "fixtureCount": 1,
        "propCount": 1,
        "props": [
            {
                "propId": "reds-astros:teamless-search:houston-astros:hits",
                "fixtureSlug": "reds-astros",
                "game": "Cincinnati Reds - Houston Astros",
                "player": {
                    "name": "Teamless Search",
                    "key": "teamless-search",
                    "mlbId": None,
                    "matchStatus": "needs_mlb_lookup",
                },
                "team": {
                    "name": "Houston Astros",
                    "key": "houston-astros",
                    "mlbId": None,
                    "matchStatus": "fixture_team_match",
                },
                "market": {"name": "hits", "key": "hits"},
                "line": 0.5,
                "odds": {"over": 1.8, "under": 1.9},
            }
        ],
    }


def test_market_mapping_selects_group_and_stat_key():
    assert group_for_market("hits") == "hitting"
    assert group_for_market("total-bases") == "hitting"
    assert group_for_market("strikeouts") == "pitching"
    assert stat_mapping_for_market("hits") == {
        "marketKey": "hits",
        "group": "hitting",
        "statKey": "hits",
        "label": "Hits",
        "supported": True,
        "contextQuality": "strong",
    }
    assert stat_mapping_for_market("total-bases") == {
        "marketKey": "total-bases",
        "group": "hitting",
        "statKey": "totalBases",
        "label": "Total Bases",
        "supported": True,
        "contextQuality": "strong",
    }
    assert stat_mapping_for_market("mystery-market") == {
        "marketKey": "mystery-market",
        "group": "hitting",
        "statKey": None,
        "label": "mystery-market",
        "supported": False,
        "contextQuality": "unsupported",
    }
    assert stat_mapping_for_market("batter-strikeouts") == {
        "marketKey": "batter-strikeouts",
        "group": "hitting",
        "statKey": "strikeOuts",
        "label": "Batter Strikeouts",
        "supported": True,
        "contextQuality": "partial",
    }


def test_sgm_only_market_mapping_supports_derived_and_hitting_stats():
    singles = stat_mapping_for_market("singles")
    assert singles == {
        "marketKey": "singles",
        "group": "hitting",
        "statKey": None,
        "statFormula": "singles",
        "label": "Singles",
        "supported": True,
        "contextQuality": "partial",
    }
    assert stat_value_from_stats(
        singles,
        {"hits": 10, "doubles": 2, "triples": 1, "homeRuns": 3},
    ) == 4.0

    assert stat_mapping_for_market("stolen bases") == {
        "marketKey": "stolen-bases",
        "group": "hitting",
        "statKey": "stolenBases",
        "label": "Stolen Bases",
        "supported": True,
        "contextQuality": "partial",
    }
    assert stat_mapping_for_market("batter walks") == {
        "marketKey": "batter-walks",
        "group": "hitting",
        "statKey": "baseOnBalls",
        "label": "Batter Walks",
        "supported": True,
        "contextQuality": "partial",
    }
    assert stat_mapping_for_market("pitcher strikeouts")["group"] == "pitching"


def test_enrich_props_matches_by_player_and_team_then_attaches_stats():
    clear_mlb_bridge_cache()
    engine = BridgeFakeMLBEngine()

    payload = asyncio.run(
        enrich_props_with_mlb_data(
            _props_payload(),
            engine,
            season=2026,
            group_mode="auto",
            history_limit=3,
        )
    )

    assert payload["matchedPropCount"] == 1
    assert payload["unmatchedPropCount"] == 1
    assert payload["props"][0]["player"] == {
        "name": "Jose Altuve",
        "key": "jose-altuve",
        "mlbId": 514888,
        "matchStatus": "matched_exact_name_team",
    }
    assert payload["props"][0]["mlbMatch"] == {
        "status": "matched_exact_name_team",
        "confidence": 1.0,
        "candidateCount": 2,
        "matchedPlayer": {
            "mlbId": 514888,
            "name": "Jose Altuve",
            "key": "jose-altuve",
            "team": {
                "mlbId": 117,
                "name": "Houston Astros",
                "key": "houston-astros",
            },
        },
    }
    assert payload["props"][0]["mlbProfile"]["player"]["stats"] == {
        "hits": 51,
        "rbi": 24,
    }
    assert payload["props"][0]["recentHistory"]["perGame"] == {"hits": 1.6667}
    assert payload["props"][0]["mlbGame"] == {
        "gamePk": 824522,
        "gameDate": None,
        "status": None,
        "awayTeam": {
            "mlbId": 113,
            "name": "Cincinnati Reds",
            "key": "cincinnati-reds",
            "probablePitcher": None,
        },
        "homeTeam": {
            "mlbId": 117,
            "name": "Houston Astros",
            "key": "houston-astros",
            "probablePitcher": None,
        },
    }
    assert payload["props"][0]["statContext"] == {
        "marketKey": "hits",
        "group": "hitting",
            "statKey": "hits",
            "label": "Hits",
            "supported": True,
            "contextQuality": "strong",
            "line": 0.5,
        "seasonValue": 51,
        "recentTotal": 5.0,
        "recentPerGame": 1.6667,
        "gamesUsed": 3,
    }
    assert payload["props"][1]["player"]["matchStatus"] == "unmatched"
    assert payload["props"][1]["mlbMatch"]["candidateCount"] == 0
    assert engine.profile_calls == [(514888, 2026, "hitting")]
    assert engine.history_calls == [(514888, "hitting", 2026, 3)]
    assert engine.schedule_calls == ["2026-05-08"]


def test_enrich_props_attaches_lineup_game_and_opponent_pitcher_context():
    clear_mlb_bridge_cache()

    payload = asyncio.run(
        enrich_props_with_mlb_data(
            _props_payload(),
            ContextBridgeFakeMLBEngine(),
            season=2026,
            group_mode="auto",
            history_limit=3,
        )
    )

    prop = payload["props"][0]
    assert prop["gameContext"]["venue"]["name"] == "Test Park"
    assert prop["lineupContext"]["status"] == "confirmed_starter"
    assert prop["lineupContext"]["battingOrder"] == 1
    assert prop["opponentPitcherContext"]["pitcher"]["pitchHand"] == "R"
    assert prop["opponentPitcherContext"]["season"]["strikeOuts"] == 70
    assert prop["playerSplits"]["seasonSplits"][0]["split"] == "Home"


def test_enrich_props_upgrades_name_match_when_profile_confirms_team():
    clear_mlb_bridge_cache()
    engine = BridgeFakeMLBEngine()

    payload = asyncio.run(
        enrich_props_with_mlb_data(
            _teamless_search_props_payload(),
            engine,
            season=2026,
            group_mode="auto",
            history_limit=3,
        )
    )
    audit = build_match_audit(payload)

    assert payload["matchedPropCount"] == 1
    assert payload["unmatchedPropCount"] == 0
    assert payload["props"][0]["player"]["matchStatus"] == "matched_exact_name_team"
    assert payload["props"][0]["team"]["mlbId"] == 117
    assert payload["props"][0]["mlbMatch"] == {
        "status": "matched_exact_name_team",
        "confidence": 1.0,
        "candidateCount": 1,
        "matchedPlayer": {
            "mlbId": 123456,
            "name": "Teamless Search",
            "key": "teamless-search",
            "team": {
                "mlbId": 117,
                "name": "Houston Astros",
                "key": "houston-astros",
            },
        },
    }
    assert audit["issueCounts"] == {}


def test_enrich_props_upgrades_name_match_when_roster_confirms_team():
    clear_mlb_bridge_cache()
    engine = BridgeFakeMLBEngine()

    payload = asyncio.run(
        enrich_props_with_mlb_data(
            _roster_search_props_payload(),
            engine,
            season=2026,
            group_mode="auto",
            history_limit=3,
        )
    )
    audit = build_match_audit(payload)

    assert payload["props"][0]["player"]["matchStatus"] == "matched_exact_name_team"
    assert payload["props"][0]["team"]["mlbId"] == 117
    assert payload["props"][0]["mlbMatch"] == {
        "status": "matched_exact_name_team",
        "confidence": 1.0,
        "candidateCount": 1,
        "matchedPlayer": {
            "mlbId": 234567,
            "name": "Roster Search",
            "key": "roster-search",
            "team": {
                "mlbId": 117,
                "name": "Houston Astros",
                "key": "houston-astros",
            },
            "position": "2B",
        },
    }
    assert audit["issueCounts"] == {}
    assert engine.schedule_calls == ["2026-05-08"]
    assert engine.roster_calls == [(117, 2026)]


def test_enrich_props_reuses_cached_mlb_lookups_for_duplicate_players():
    clear_mlb_bridge_cache()
    engine = BridgeFakeMLBEngine()
    payload = _props_payload()
    payload["props"] = [payload["props"][0], payload["props"][0].copy()]

    asyncio.run(
        enrich_props_with_mlb_data(
            payload,
            engine,
            season=2026,
            group_mode="auto",
            history_limit=3,
        )
    )
    asyncio.run(
        enrich_props_with_mlb_data(
            payload,
            engine,
            season=2026,
            group_mode="auto",
            history_limit=3,
        )
    )

    assert engine.search_calls == [("Jose Altuve", 5)]
    assert engine.profile_calls == [(514888, 2026, "hitting")]
    assert engine.history_calls == [(514888, "hitting", 2026, 3)]


def test_build_match_audit_summarizes_match_quality():
    enriched = {
        "date": "2026-05-08",
        "propCount": 3,
        "matchedPropCount": 2,
        "unmatchedPropCount": 1,
        "props": [
            {
                "propId": "one",
                "player": {"name": "Jose Altuve"},
                "team": {"name": "Houston Astros"},
                "market": {"name": "hits"},
                "mlbMatch": {
                    "status": "matched_exact_name_team",
                    "confidence": 1.0,
                    "candidateCount": 2,
                    "matchedPlayer": {"name": "Jose Altuve"},
                },
            },
            {
                "propId": "two",
                "player": {"name": "Same Name"},
                "team": {"name": "Wrong Team"},
                "market": {"name": "hits"},
                "mlbMatch": {
                    "status": "matched_exact_name",
                    "confidence": 0.85,
                    "candidateCount": 2,
                    "matchedPlayer": {"name": "Same Name"},
                },
            },
            {
                "propId": "three",
                "player": {"name": "Unknown"},
                "team": {"name": "Houston Astros"},
                "market": {"name": "hits"},
                "mlbMatch": {
                    "status": "unmatched",
                    "confidence": 0.0,
                    "candidateCount": 0,
                    "matchedPlayer": None,
                },
            },
        ],
    }

    audit = build_match_audit(enriched)

    assert audit == {
        "date": "2026-05-08",
        "propCount": 3,
        "matchedPropCount": 2,
        "unmatchedPropCount": 1,
        "statusCounts": {
            "matched_exact_name_team": 1,
            "matched_exact_name": 1,
            "unmatched": 1,
        },
            "issueCounts": {
                "unmatched": 1,
                "multiple_candidates": 2,
                "team_not_confirmed": 1,
                "low_confidence": 2,
            },
        "rows": [
            {
                "propId": "one",
                "player": "Jose Altuve",
                "team": "Houston Astros",
                "market": "hits",
                "status": "matched_exact_name_team",
                "confidence": 1.0,
                "candidateCount": 2,
                "matchedPlayer": "Jose Altuve",
                "issues": ["multiple_candidates"],
            },
            {
                "propId": "two",
                "player": "Same Name",
                "team": "Wrong Team",
                "market": "hits",
                "status": "matched_exact_name",
                "confidence": 0.85,
                "candidateCount": 2,
                "matchedPlayer": "Same Name",
                "issues": [
                    "multiple_candidates",
                    "team_not_confirmed",
                    "low_confidence",
                ],
            },
            {
                "propId": "three",
                "player": "Unknown",
                "team": "Houston Astros",
                "market": "hits",
                "status": "unmatched",
                "confidence": 0.0,
                "candidateCount": 0,
                "matchedPlayer": None,
                "issues": ["unmatched", "low_confidence"],
            },
        ],
    }
