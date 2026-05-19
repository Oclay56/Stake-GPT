from __future__ import annotations

import pytest

from app.stake_ui_builder import (
    StakeUiBuildConfig,
    choose_unique_click_candidate,
    evaluate_candidate_text,
    selection_to_ui_leg,
)


def test_selection_to_ui_leg_keeps_exact_stake_identity():
    leg = selection_to_ui_leg(
        {
            "selectionId": "springer-hits:under",
            "propId": "springer-hits",
            "fixtureSlug": "blue-jays-angels",
            "player": {"name": "George Springer"},
            "team": {"name": "Toronto Blue Jays"},
            "market": {"key": "hits", "name": "Hits"},
            "side": "under",
            "line": 0.5,
            "odds": 2.9,
        },
        index=1,
    )

    assert leg.index == 1
    assert leg.player == "George Springer"
    assert leg.market_name == "Hits"
    assert leg.side == "under"
    assert leg.line == 0.5
    assert leg.odds == 2.9
    assert leg.required_terms == ["george springer", "hits", "under", "0.5"]


def test_evaluate_candidate_text_blocks_wrong_line():
    leg = selection_to_ui_leg(
        {
            "player": {"name": "George Springer"},
            "market": {"name": "Hits"},
            "side": "under",
            "line": 0.5,
            "odds": 2.9,
        },
        index=1,
    )

    match = evaluate_candidate_text(
        leg,
        "George Springer Hits Under 1.5 1.35",
        StakeUiBuildConfig(odds_policy="warn"),
    )

    assert match.matched is False
    assert "line:0.5" in match.missing


def test_choose_unique_click_candidate_blocks_ambiguous_matches():
    leg = selection_to_ui_leg(
        {
            "player": {"name": "George Springer"},
            "market": {"name": "Hits"},
            "side": "under",
            "line": 0.5,
            "odds": 2.9,
        },
        index=1,
    )

    choice = choose_unique_click_candidate(
        leg,
        [
            {"domIndex": 1, "text": "George Springer Hits Under 0.5 2.90"},
            {"domIndex": 2, "text": "George Springer Hits Under 0.5 2.88"},
        ],
        StakeUiBuildConfig(odds_policy="warn"),
    )

    assert choice["status"] == "blocked"
    assert choice["reason"] == "ambiguous_ui_matches"
    assert choice["candidateCount"] == 2


def test_choose_unique_click_candidate_allows_one_exact_candidate():
    leg = selection_to_ui_leg(
        {
            "player": {"name": "Mike Trout"},
            "market": {"name": "Total Bases"},
            "side": "under",
            "line": 1.5,
            "odds": 1.83,
        },
        index=1,
    )

    choice = choose_unique_click_candidate(
        leg,
        [
            {"domIndex": 4, "text": "Mike Trout Total Bases Over 1.5 1.91"},
            {"domIndex": 5, "text": "Mike Trout Total Bases Under 1.5 1.83"},
        ],
        StakeUiBuildConfig(odds_policy="exact"),
    )

    assert choice["status"] == "matched"
    assert choice["domIndex"] == 5


def test_exact_odds_policy_blocks_moved_odds():
    leg = selection_to_ui_leg(
        {
            "player": {"name": "Mike Trout"},
            "market": {"name": "Total Bases"},
            "side": "under",
            "line": 1.5,
            "odds": 1.83,
        },
        index=1,
    )

    choice = choose_unique_click_candidate(
        leg,
        [{"domIndex": 5, "text": "Mike Trout Total Bases Under 1.5 1.70"}],
        StakeUiBuildConfig(odds_policy="exact", odds_tolerance=0.01),
    )

    assert choice["status"] == "blocked"
    assert choice["reason"] == "no_exact_ui_match"
    assert "odds:1.83" in choice["missing"][0]


@pytest.mark.parametrize("side", ["", "yes", None])
def test_selection_to_ui_leg_rejects_invalid_side(side):
    with pytest.raises(ValueError):
        selection_to_ui_leg(
            {
                "player": {"name": "Mike Trout"},
                "market": {"name": "Hits"},
                "side": side,
                "line": 0.5,
            },
            index=1,
        )
