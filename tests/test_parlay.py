from app.parlay import build_parlay_candidates, build_pick_board
from app.correlation import analyze_parlay_correlation


def _row(
    prop_id,
    player,
    fixture,
    odds,
    score=84,
    bucket="watchlist",
    risk_flags=None,
    confidence="high",
    market_key="hits",
    line=0.5,
    game=None,
    recent_per_game=1.4,
    season_value=51,
    season_per_game=1.1,
    games_used=5,
    recent_games=None,
    lean="over",
    team_name="Houston Astros",
    under_odds=1.8,
    stat_key=None,
):
    return {
        "propId": f"{fixture}:{prop_id}",
        "fixtureSlug": fixture,
        "game": game or f"{fixture} matchup",
        "playerName": player,
        "teamName": team_name,
        "marketKey": market_key,
        "line": line,
        "bucket": bucket,
        "lean": lean,
        "score": score,
        "confidence": confidence,
        "overOdds": odds,
        "underOdds": under_odds,
        "riskFlags": risk_flags or [],
        "reasons": ["recent_per_game_above_line"],
        "statKey": stat_key or market_key,
        "recentPerGame": recent_per_game,
        "seasonValue": season_value,
        "seasonPerGame": season_per_game,
        "gamesUsed": games_used,
        "recentGames": recent_games or [
            {"date": "2026-05-07", "opponent": "Reds", "stats": {market_key: 2}},
            {"date": "2026-05-06", "opponent": "Rangers", "stats": {market_key: 1}},
        ],
    }


def test_build_parlay_candidates_uses_watchlist_and_avoids_duplicate_players():
    rows = [
        _row("one", "Player One", "game-a", 1.8, score=90),
        _row("two", "Player Two", "game-b", 1.7, score=88),
        _row("three", "Player Three", "game-c", 1.6, score=86),
        _row("dup", "Player One", "game-d", 2.4, score=99),
        _row("bad-risk", "Risk Player", "game-e", 2.2, risk_flags=["market_moved_against_over"]),
        _row("avoid", "Avoid Player", "game-f", 3.0, bucket="avoid"),
    ]

    result = build_parlay_candidates(
        rows,
        legs=3,
        odds_min=4.0,
        odds_max=6.0,
        count=3,
        mode="standard",
    )

    assert result["eligibleCount"] == 4
    assert result["candidateCount"] >= 1
    candidate = result["candidates"][0]
    assert candidate["withinRange"] is True
    assert candidate["legCount"] == 3
    assert 4.0 <= candidate["totalOdds"] <= 6.0
    assert len({leg["playerName"] for leg in candidate["legs"]}) == 3
    assert "Risk Player" not in {leg["playerName"] for leg in candidate["legs"]}
    assert "Avoid Player" not in {leg["playerName"] for leg in candidate["legs"]}


def test_build_parlay_candidates_enforces_same_game_minimum_two_legs():
    rows = [
        _row("a1", "Game A One", "game-a", 1.8),
        _row("a2", "Game A Two", "game-a", 1.7),
        _row("b1", "Game B One", "game-b", 1.6),
        _row("b2", "Game B Two", "game-b", 1.9),
        _row("c1", "Game C One", "game-c", 2.2),
    ]

    result = build_parlay_candidates(
        rows,
        legs=4,
        odds_min=1.0,
        odds_max=100.0,
        count=5,
        mode="sgp",
    )

    assert result["candidateCount"] >= 1
    for candidate in result["candidates"]:
        fixture_counts = {}
        for leg in candidate["legs"]:
            fixture_counts[leg["fixtureSlug"]] = fixture_counts.get(leg["fixtureSlug"], 0) + 1
        assert all(count >= 2 for count in fixture_counts.values())


def test_build_parlay_candidates_reports_closest_when_target_range_is_impossible():
    rows = [
        _row("one", "Player One", "game-a", 1.8),
        _row("two", "Player Two", "game-b", 1.7),
    ]

    result = build_parlay_candidates(
        rows,
        legs=2,
        odds_min=100000.0,
        odds_max=100000.0,
        count=3,
        mode="standard",
    )

    assert result["candidateCount"] == 1
    assert result["candidates"][0]["withinRange"] is False
    assert "no_candidates_in_requested_odds_range_showing_closest" in result["warnings"]


def test_standard_mode_uses_only_one_leg_per_fixture():
    rows = [
        _row("a1", "Game A One", "game-a", 1.8, score=99),
        _row("a2", "Game A Two", "game-a", 1.7, score=98),
        _row("b1", "Game B One", "game-b", 1.6, score=80),
        _row("c1", "Game C One", "game-c", 1.9, score=79),
    ]

    result = build_parlay_candidates(
        rows,
        legs=3,
        odds_min=1.0,
        odds_max=100.0,
        count=5,
        mode="standard",
    )

    assert result["candidateCount"] >= 1
    for candidate in result["candidates"]:
        fixtures = [leg["fixtureSlug"] for leg in candidate["legs"]]
        assert len(fixtures) == len(set(fixtures))


def test_sgp_mode_allows_uneven_fixture_counts_as_long_as_each_has_two_plus():
    rows = [
        _row("a1", "Game A One", "game-a", 1.8, score=99),
        _row("a2", "Game A Two", "game-a", 1.7, score=98),
        _row("a3", "Game A Three", "game-a", 1.6, score=97),
        _row("b1", "Game B One", "game-b", 1.9, score=96),
        _row("b2", "Game B Two", "game-b", 1.5, score=95),
        _row("c1", "Game C One", "game-c", 2.2, score=94),
    ]

    result = build_parlay_candidates(
        rows,
        legs=5,
        odds_min=1.0,
        odds_max=100.0,
        count=1,
        mode="sgp",
    )

    assert result["candidateCount"] == 1
    assert result["candidates"][0]["fixtureCounts"] == {"game-a": 3, "game-b": 2}


def test_locked_players_are_kept_while_builder_fills_remaining_legs():
    rows = [
        _row("locked", "Locked Player", "game-a", 1.8, score=70),
        _row("b1", "Game B One", "game-b", 1.7, score=99),
        _row("c1", "Game C One", "game-c", 1.6, score=98),
        _row("d1", "Game D One", "game-d", 1.9, score=97),
    ]

    result = build_parlay_candidates(
        rows,
        legs=3,
        odds_min=1.0,
        odds_max=100.0,
        count=3,
        mode="standard",
        locked_players={"Locked Player"},
    )

    assert result["candidateCount"] >= 1
    for candidate in result["candidates"]:
        locked_legs = [leg for leg in candidate["legs"] if leg["playerName"] == "Locked Player"]
        assert len(locked_legs) == 1
        assert locked_legs[0]["locked"] is True


def test_build_pick_board_numbers_eligible_legs_for_selection():
    rows = [
        _row("one", "Player One", "game-a", 1.8, score=90),
        _row("two", "Player Two", "game-b", 1.7, score=88),
        _row("risk", "Risk Player", "game-c", 2.2, score=99, risk_flags=["market_moved_against_over"]),
        _row("avoid", "Avoid Player", "game-d", 3.0, bucket="avoid"),
    ]

    board = build_pick_board(rows, limit=10)

    assert board["eligibleCount"] == 2
    assert [row["pickNumber"] for row in board["picks"]] == [1, 2]
    assert board["picks"][0]["playerName"] == "Player One"
    assert board["picks"][0]["fixtureSlug"] == "game-a"
    assert board["picks"][0]["odds"] == 1.8
    assert "Risk Player" not in {row["playerName"] for row in board["picks"]}
    assert "Avoid Player" not in {row["playerName"] for row in board["picks"]}


def test_locked_pick_numbers_are_kept_while_builder_fills_remaining_legs():
    rows = [
        _row("top", "Top Player", "game-a", 1.8, score=99),
        _row("locked", "Locked Board Player", "game-b", 1.7, score=98),
        _row("third", "Third Player", "game-c", 1.6, score=97),
        _row("fourth", "Fourth Player", "game-d", 1.9, score=96),
    ]

    result = build_parlay_candidates(
        rows,
        legs=3,
        odds_min=1.0,
        odds_max=100.0,
        count=2,
        mode="standard",
        locked_pick_numbers={2},
    )

    assert result["lockedCount"] == 1
    assert result["requested"]["lockedPickNumbers"] == [2]
    for candidate in result["candidates"]:
        locked_legs = [
            leg for leg in candidate["legs"]
            if leg["playerName"] == "Locked Board Player"
        ]
        assert len(locked_legs) == 1
        assert locked_legs[0]["pickNumber"] == 2
        assert locked_legs[0]["locked"] is True


def test_candidate_includes_conflict_warnings_and_leg_explanations():
    rows = [
        _row("a1", "Game A One", "game-a", 1.8, score=90),
        _row("a2", "Game A Two", "game-a", 1.7, score=88),
        _row("a3", "Game A Three", "game-a", 1.6, score=86),
        _row("a4", "Game A Four", "game-a", 1.9, score=84),
    ]

    result = build_parlay_candidates(
        rows,
        legs=4,
        odds_min=1.0,
        odds_max=100.0,
        count=1,
        mode="sgp",
    )

    candidate = result["candidates"][0]
    assert "same_game_correlation:game-a:4" in candidate["conflictWarnings"]
    assert "repeated_market:hits:4" in candidate["conflictWarnings"]
    assert "recent form clears market line" in candidate["legs"][0]["whyIncluded"]
    assert candidate["legs"][0]["whyNotStronger"] == []


def test_leg_explanations_include_season_support_and_new_risk_flags():
    rows = [
        _row(
            "season-risk",
            "Season Risk Player",
            "game-a",
            1.9,
            risk_flags=["high_line", "season_baseline_below_line", "recent_form_spike"],
        )
    ]
    rows[0]["reasons"] = [
        "recent_per_game_above_line",
        "season_baseline_supports_over",
        "recent_and_season_agree",
    ]

    board = build_pick_board(rows, allow_risk=True, limit=1)

    pick = board["picks"][0]
    assert "season baseline supports the lean" in pick["whyIncluded"]
    assert "recent and season form agree" in pick["whyIncluded"]
    assert "line is above the normal market tier" in pick["whyNotStronger"]
    assert "season baseline does not clear the line" in pick["whyNotStronger"]
    assert "recent form is much hotter than season baseline" in pick["whyNotStronger"]


def test_locked_standard_fixture_conflict_reports_warning():
    rows = [
        _row("a1", "Game A One", "game-a", 1.8, score=99),
        _row("a2", "Game A Two", "game-a", 1.7, score=98),
        _row("b1", "Game B One", "game-b", 1.6, score=97),
    ]

    result = build_parlay_candidates(
        rows,
        legs=3,
        odds_min=1.0,
        odds_max=100.0,
        count=3,
        mode="standard",
        locked_pick_numbers={1, 2},
    )

    assert result["candidateCount"] == 0
    assert "locked_legs_conflict_with_standard_mode" in result["warnings"]


def test_locked_sgp_singleton_without_available_partner_reports_warning():
    rows = [
        _row("a1", "Game A One", "game-a", 1.8, score=99),
        _row("b1", "Game B One", "game-b", 1.7, score=98),
        _row("b2", "Game B Two", "game-b", 1.6, score=97),
    ]

    result = build_parlay_candidates(
        rows,
        legs=3,
        odds_min=1.0,
        odds_max=100.0,
        count=3,
        mode="sgp",
        locked_pick_numbers={1},
    )

    assert result["candidateCount"] == 0
    assert "locked_sgp_fixture_needs_second_leg:game-a" in result["warnings"]


def test_safe_profile_requires_high_confidence_no_risk_and_lower_leg_odds():
    rows = [
        _row("safe", "Safe Player", "game-a", 1.75, score=90),
        _row("medium", "Medium Player", "game-b", 1.7, confidence="medium"),
        _row("risky", "Risky Player", "game-c", 1.8, risk_flags=["small_recent_sample"]),
        _row("expensive", "Expensive Player", "game-d", 2.4),
    ]

    board = build_pick_board(rows, profile="safe-ish", limit=10)

    assert board["requested"]["profile"] == "safe-ish"
    assert board["eligibleCount"] == 1
    assert board["picks"][0]["playerName"] == "Safe Player"


def test_longshot_profile_allows_risk_and_requires_higher_leg_odds():
    rows = [
        _row("low", "Low Odds Player", "game-a", 1.7, risk_flags=["small_recent_sample"]),
        _row("long", "Longshot Player", "game-b", 2.6, risk_flags=["small_recent_sample"]),
    ]

    board = build_pick_board(rows, profile="longshot", limit=10)

    assert board["requested"]["profile"] == "longshot"
    assert board["eligibleCount"] == 1
    assert board["picks"][0]["playerName"] == "Longshot Player"
    assert board["picks"][0]["riskFlags"] == ["small_recent_sample"]


def test_pick_board_keeps_player_detail_for_display():
    board = build_pick_board(
        [
            _row(
                "detail",
                "Detail Player",
                "game-a",
                1.85,
                game="Cincinnati Reds - Houston Astros",
                recent_per_game=1.6,
                season_value=51,
            )
        ],
        limit=1,
    )

    pick = board["picks"][0]
    assert pick["game"] == "Cincinnati Reds - Houston Astros"
    assert pick["recentPerGame"] == 1.6
    assert pick["seasonValue"] == 51
    assert pick["seasonPerGame"] == 1.1
    assert pick["gamesUsed"] == 5
    assert pick["recentGames"][0]["stats"]["hits"] == 2


def test_correlation_engine_flags_same_game_pitcher_under_reprice_needs_quote():
    legs = [
        _row(
            "jays-pitcher",
            "Toronto Pitcher",
            "blue-jays-angels",
            2.05,
            lean="under_or_avoid_over",
            under_odds=1.94,
            market_key="first-earned-run",
            stat_key="earnedRuns",
            team_name="Toronto Blue Jays",
        ),
        _row(
            "angels-pitcher",
            "Angels Pitcher",
            "blue-jays-angels",
            2.2,
            lean="under_or_avoid_over",
            under_odds=1.67,
            market_key="first-earned-run",
            stat_key="earnedRuns",
            team_name="Los Angeles Angels",
        ),
    ]

    risk = analyze_parlay_correlation(legs)

    assert risk["rawProductOdds"] == 3.2398
    assert risk["quoteNeeded"] is True
    assert risk["repriceSignal"] == "unconfirmed"
    assert risk["sgpRepricingLikely"] is True
    assert risk["riskLevel"] == "high"
    assert "same_fixture" in risk["tags"]
    assert "opposing_pitchers" in risk["tags"]
    assert "multiple_unders_same_game" in risk["tags"]
    assert "low_scoring_game_script" in risk["gameScriptTags"]
    assert "mutual_starter_suppression" in risk["gameScriptTags"]
    assert "Stake quote needed" in risk["warning"]


def test_correlation_engine_confirms_stake_reprice_when_quote_is_supplied():
    legs = [
        _row(
            "jays-pitcher",
            "Toronto Pitcher",
            "blue-jays-angels",
            2.05,
            lean="under_or_avoid_over",
            under_odds=1.94,
            market_key="first-earned-run",
            stat_key="earnedRuns",
            team_name="Toronto Blue Jays",
        ),
        _row(
            "angels-pitcher",
            "Angels Pitcher",
            "blue-jays-angels",
            2.2,
            lean="under_or_avoid_over",
            under_odds=1.67,
            market_key="first-earned-run",
            stat_key="earnedRuns",
            team_name="Los Angeles Angels",
        ),
    ]

    risk = analyze_parlay_correlation(legs, stake_quoted_odds=30.0)

    assert risk["rawProductOdds"] == 3.2398
    assert risk["stakeQuotedOdds"] == 30.0
    assert risk["repriceFactor"] == 9.26
    assert risk["repricePercent"] == 825.98
    assert risk["repriceSignal"] == "confirmed_extreme_stake_reprice"
    assert risk["quoteNeeded"] is False
    assert risk["riskLevel"] == "extreme"
    assert "high_variance_rare_outcome" in risk["gameScriptTags"]


def test_correlation_engine_tags_offense_stack_against_pitcher_over():
    legs = [
        _row(
            "hitter-one",
            "Hitter One",
            "dodgers-padres",
            1.8,
            market_key="total-bases",
            team_name="Los Angeles Dodgers",
        ),
        _row(
            "hitter-two",
            "Hitter Two",
            "dodgers-padres",
            1.9,
            market_key="hits",
            team_name="Los Angeles Dodgers",
        ),
        _row(
            "padres-pitcher",
            "Padres Pitcher",
            "dodgers-padres",
            2.1,
            market_key="earned-runs",
            stat_key="earnedRuns",
            team_name="San Diego Padres",
        ),
    ]

    risk = analyze_parlay_correlation(legs)

    assert risk["sgpRepricingLikely"] is True
    assert "same_team" in risk["tags"]
    assert "pitcher_vs_batter" in risk["tags"]
    assert "offense_stack" in risk["gameScriptTags"]
    assert "opposing_pitcher_earned_runs_over" in risk["tags"]


def test_parlay_candidates_include_raw_product_and_unconfirmed_correlation_warning():
    rows = [
        _row("a1", "Game A One", "game-a", 1.8, score=99),
        _row("a2", "Game A Two", "game-a", 1.7, score=98),
    ]

    result = build_parlay_candidates(
        rows,
        legs=2,
        odds_min=1.0,
        odds_max=100.0,
        count=1,
        mode="sgp",
    )

    candidate = result["candidates"][0]
    assert candidate["rawProductOdds"] == candidate["totalOdds"]
    assert candidate["correlationRisk"]["sgpRepricingLikely"] is True
    assert candidate["correlationRisk"]["quoteNeeded"] is True
    assert "same_fixture" in candidate["correlationRisk"]["tags"]
