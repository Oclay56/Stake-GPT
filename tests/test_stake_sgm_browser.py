from __future__ import annotations

import pytest

import app.stake_sgm_browser as sgm_browser
from app.stake_sgm_browser import (
    _add_bet_confirmed,
    _batch_should_stop_after_group_result,
    _check_page_ready,
    _classify_moneyline_sidebar_state,
    _compact_preflight_result,
    _sidebar_clear_confirmed,
    _fixture_matchup_from_slug,
    _find_or_open_fixture_page,
    _normalize_mlb_game_link,
    _has_logged_out_warning,
    _market_display_aliases,
    _market_click_identity,
    _market_search_text,
    _moneyline_build_result,
    _normalize_mlb_moneyline_cards,
    _prepare_moneyline_build_selections,
    _preflight_sgm_review_selections,
    _preflight_result_is_buildable,
    _review_add_summary,
    _batch_resume_report,
    _sidebar_group_target,
    _sidebar_sgm_group_already_present,
    _sidebar_remove_confirmed,
    _transactional_selection_plan,
    fixture_url,
    make_mlb_moneyline_row_id,
)


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.navigated_to: list[str] = []

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.url = url
        self.navigated_to.append(url)


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages

    def new_page(self) -> FakePage:
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


class FakeLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, *, timeout: int) -> str:
        return self.text


class FakeReadyPage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.navigated_to: list[str] = []

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "body"
        return FakeLocator(self.text)

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.navigated_to.append(url)


def test_find_or_open_fixture_page_refreshes_restricted_region_tab():
    fixture_slug = "46575343-miami-marlins-atlanta-braves"
    page = FakePage(
        fixture_url(fixture_slug)
        + "?regionKey=US&country=US&region=GA&modal=restrictedRegion"
    )
    context = FakeContext([page])

    found = _find_or_open_fixture_page(context, fixture_slug)

    assert found is page
    assert page.navigated_to == [fixture_url(fixture_slug)]


def test_find_or_open_fixture_page_reuses_clean_fixture_tab():
    fixture_slug = "46575343-miami-marlins-atlanta-braves"
    page = FakePage(fixture_url(fixture_slug))
    context = FakeContext([page])

    found = _find_or_open_fixture_page(context, fixture_slug)

    assert found is page
    assert page.navigated_to == []


def test_expand_mlb_game_list_clicks_load_more_until_limit():
    class LoadMorePage:
        def __init__(self) -> None:
            self.visible_games = 4
            self.clicks = 0

        def evaluate(self, script, arg=None):
            if "querySelectorAll('a[href*=" in script:
                return [
                    {
                        "href": f"https://stake.com/sports/baseball/usa/mlb/{index}-team-a-team-b",
                        "text": "Team A Team B",
                    }
                    for index in range(self.visible_games)
                ]
            if "load more" in script.lower():
                if self.visible_games >= 10:
                    return {"status": "not_found", "visibleGameCount": self.visible_games}
                self.clicks += 1
                self.visible_games += 3
                return {"status": "clicked", "visibleGameCount": self.visible_games}
            raise AssertionError("unexpected script")

        def wait_for_timeout(self, ms):
            return None

    page = LoadMorePage()

    result = sgm_browser._expand_mlb_game_list(page, limit=10)

    assert result["status"] == "expanded"
    assert page.clicks == 2
    assert page.visible_games == 10


def test_check_page_ready_reports_cloudflare_verification():
    page = FakeReadyPage(
        "stake.com\nPerforming security verification\n"
        "This website uses a security service to protect against malicious bots."
    )

    with pytest.raises(RuntimeError, match="Cloudflare verification"):
        _check_page_ready(page)


def test_check_page_ready_accepts_hyphenated_same_game_multi_tab():
    page = FakeReadyPage("Wallet\nMain\nSame-Game Multi\nPlayer Props")

    assert _check_page_ready(page) == []


def test_check_page_ready_reloads_region_blocked_fixture_before_failing():
    fixture_slug = "46575343-miami-marlins-atlanta-braves"
    page = FakeReadyPage("Sorry, Stake.com is not available in your region.")

    with pytest.raises(RuntimeError, match="region-blocked"):
        _check_page_ready(page, fixture_slug=fixture_slug)

    assert page.navigated_to == [fixture_url(fixture_slug)]


def test_has_logged_out_warning_detects_account_action_blocker():
    assert _has_logged_out_warning(
        ["browser appears logged out; read-only SGM data may still load"]
    )
    assert not _has_logged_out_warning(["page did not reach networkidle before continuing"])


def test_normalize_mlb_game_link_accepts_localized_stake_urls():
    link = _normalize_mlb_game_link(
        "https://stake.com/de/sports/baseball/usa/mlb/46575562-washington-nationals-new-york-mets"
    )

    assert link == {
        "fixtureSlug": "46575562-washington-nationals-new-york-mets",
        "url": "https://stake.com/de/sports/baseball/usa/mlb/46575562-washington-nationals-new-york-mets",
        "matchup": "Washington Nationals vs New York Mets",
        "teams": ["Washington Nationals", "New York Mets"],
    }


def test_normalize_mlb_game_link_rejects_non_fixture_links():
    assert _normalize_mlb_game_link("https://stake.com/sports/baseball/usa/mlb") is None
    assert _normalize_mlb_game_link("https://stake.com/sports/football/usa/nfl/123-test") is None


def test_normalize_mlb_moneyline_cards_returns_pregame_main_winner_rows():
    cards = [
        {
            "href": "https://stake.com/sports/baseball/usa/mlb/123-new-york-yankees-toronto-blue-jays",
            "text": "New York Yankees Toronto Blue Jays Winner (incl. Extra Innings)",
            "statusText": "NOT STARTED",
            "markets": [
                {
                    "label": "Winner (incl. Extra Innings)",
                    "outcomes": [
                        {"team": "New York Yankees", "oddsText": "1.72", "disabled": False},
                        {"team": "Toronto Blue Jays", "oddsText": "2.08", "disabled": False},
                    ],
                }
            ],
        }
    ]

    result = _normalize_mlb_moneyline_cards(cards, limit=50)

    assert result["games"][0]["marketLabel"] == "Winner (incl. Extra Innings)"
    assert result["games"][0]["status"] == "pregame"
    assert result["games"][0]["selections"][0]["odds"] == 1.72
    assert result["games"][0]["selections"][0]["rowId"].startswith("mlb_ml_")


def test_normalize_mlb_moneyline_cards_ignores_ambiguous_container_text():
    cards = [
        {
            "href": "https://stake.com/sports/baseball/usa/mlb/123-cincinnati-reds-atlanta-braves",
            "text": "Cincinnati Reds Atlanta Braves Winner (incl. Extra Innings)",
            "statusText": "NOT STARTED",
            "outcomeTexts": [
                "Cincinnati Reds 2.11 Atlanta Braves 1.74",
                "Cincinnati Reds 2.11",
                "Atlanta Braves 1.74",
            ],
        }
    ]

    result = _normalize_mlb_moneyline_cards(cards, limit=50)

    selections = result["games"][0]["selections"]
    assert selections == [
        {
            "team": "Cincinnati Reds",
            "odds": 2.11,
            "playable": True,
            "rowId": make_mlb_moneyline_row_id(
                "123-cincinnati-reds-atlanta-braves",
                "Cincinnati Reds",
            ),
        },
        {
            "team": "Atlanta Braves",
            "odds": 1.74,
            "playable": True,
            "rowId": make_mlb_moneyline_row_id(
                "123-cincinnati-reds-atlanta-braves",
                "Atlanta Braves",
            ),
        },
    ]


def test_normalize_mlb_moneyline_cards_rejects_ambiguous_only_fallback_text():
    cards = [
        {
            "href": "https://stake.com/sports/baseball/usa/mlb/123-new-york-yankees-toronto-blue-jays",
            "text": "New York Yankees Toronto Blue Jays Winner (incl. Extra Innings)",
            "statusText": "NOT STARTED",
            "outcomeTexts": [
                "New York Yankees 1.72 Toronto Blue Jays 2.08",
            ],
        }
    ]

    result = _normalize_mlb_moneyline_cards(cards, limit=50)

    assert result["games"] == []
    assert "moneyline_card_not_normalized" in result["warnings"]


def test_normalize_mlb_moneyline_cards_skips_live_cards_with_warning():
    cards = [
        {
            "href": "https://stake.com/sports/baseball/usa/mlb/123-new-york-yankees-toronto-blue-jays",
            "text": "LIVE New York Yankees Toronto Blue Jays Winner (incl. Extra Innings)",
            "statusText": "LIVE",
            "markets": [],
        }
    ]

    result = _normalize_mlb_moneyline_cards(cards, limit=50)

    assert result["games"] == []
    assert "live_fixture_skipped" in result["warnings"]


def test_normalize_mlb_moneyline_cards_requires_main_winner_label():
    cards = [
        {
            "href": "https://stake.com/sports/baseball/usa/mlb/123-new-york-yankees-toronto-blue-jays",
            "text": "New York Yankees Toronto Blue Jays First 5 Innings Winner",
            "statusText": "NOT STARTED",
            "outcomeTexts": ["New York Yankees 1.72", "Toronto Blue Jays 2.08"],
        }
    ]

    result = _normalize_mlb_moneyline_cards(cards, limit=50)

    assert result["games"] == []
    assert "moneyline_card_not_normalized" in result["warnings"]


def test_moneyline_row_id_is_stable_and_separate_from_sgm_ids():
    first = make_mlb_moneyline_row_id("123-yankees-blue-jays", "New York Yankees")
    second = make_mlb_moneyline_row_id("123-yankees-blue-jays", "New York Yankees")

    assert first == second
    assert first.startswith("mlb_ml_")
    assert not first.startswith("sgm_")


def test_prepare_moneyline_build_selections_validates_identity_and_dedupes():
    row_id = make_mlb_moneyline_row_id(
        "123-new-york-yankees-toronto-blue-jays",
        "New York Yankees",
    )

    result = _prepare_moneyline_build_selections(
        [
            {
                "rowId": row_id,
                "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                "team": "New York Yankees",
                "odds": 1.72,
            },
            {
                "rowId": row_id,
                "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                "team": "New York Yankees",
                "odds": 1.74,
            },
        ]
    )

    assert result["status"] == "ready"
    assert len(result["selections"]) == 1
    assert result["selections"][0]["rowId"] == row_id
    assert result["selections"][0]["researchedOdds"] == 1.72
    assert result["errors"] == []


def test_prepare_moneyline_build_selections_blocks_sgm_row_ids():
    result = _prepare_moneyline_build_selections(
        [
            {
                "rowId": "sgm_abc123",
                "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                "team": "New York Yankees",
                "odds": 1.72,
            }
        ]
    )

    assert result["status"] == "blocked_invalid_row_id"
    assert result["errors"][0]["reason"] == "row_id_must_start_with_mlb_ml"


def test_prepare_moneyline_build_selections_blocks_row_id_identity_mismatch():
    wrong_row_id = make_mlb_moneyline_row_id(
        "123-new-york-yankees-toronto-blue-jays",
        "Toronto Blue Jays",
    )

    result = _prepare_moneyline_build_selections(
        [
            {
                "rowId": wrong_row_id,
                "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                "team": "New York Yankees",
                "odds": 1.72,
            }
        ]
    )

    assert result["status"] == "blocked_invalid_row_id"
    assert result["errors"][0]["reason"] == "row_id_does_not_match_fixture_team"


def test_fixture_matchup_from_slug_handles_multi_word_team_names():
    assert _fixture_matchup_from_slug(
        "46575351-new-york-yankees-toronto-blue-jays"
    ) == {
        "matchup": "New York Yankees vs Toronto Blue Jays",
        "teams": ["New York Yankees", "Toronto Blue Jays"],
    }


def test_market_aliases_cover_stake_sgm_team_and_translated_labels():
    assert _market_search_text("Team Hits") == "hits"
    assert _market_search_text("Team RBIs") == "rbi"
    assert _market_search_text("Failed Attempts") == "strikeouts"

    assert "Hits" in _market_display_aliases("Team Hits")
    assert "Team RBIs" in _market_display_aliases("Team RBIs")
    assert "RBIs" in _market_display_aliases("Team RBIs")
    assert "Failed Attempts" in _market_display_aliases("Strikeouts")
    assert "First Well Deserved Run" in _market_display_aliases("First ER")
    assert _market_search_text("First HR") == "first home run"
    assert "First Home Run" in _market_display_aliases("First HR")
    assert "First H." in _market_display_aliases("First Hit")
    assert "First Hit" in _market_display_aliases("First H.")


def test_market_click_identity_blocks_ambiguous_half_point_hitter_markets():
    runs_identity = _market_click_identity("Runs")
    assert "runs" in runs_identity["aliases"]
    assert "home runs" in runs_identity["blockedAliases"]
    assert "earned runs" in runs_identity["blockedAliases"]

    hits_identity = _market_click_identity("Hits")
    assert "hits" in hits_identity["aliases"]
    assert "hits allowed" in hits_identity["blockedAliases"]


def test_sgm_market_filter_matches_first_hit_and_first_home_run_aliases():
    assert sgm_browser.sgm_market_filter_matches({"market": "First HR"}, "first home run")
    assert sgm_browser.sgm_market_filter_matches({"market": "First Home Run"}, "first hr")
    assert sgm_browser.sgm_market_filter_matches({"market": "First H."}, "first hit")
    assert sgm_browser.sgm_market_filter_matches({"market": "First Hit"}, "first h.")


def test_add_bet_confirmation_requires_sidebar_change_when_existing_slip_present():
    before = {
        "rightPanelEmpty": False,
        "rightPanelTextDigest": "same",
        "rightPanelTextLength": 120,
        "rightPanelSelectionCount": 2,
    }
    unchanged_after = {
        "rightPanelEmpty": False,
        "rightPanelTextDigest": "same",
        "rightPanelTextLength": 120,
        "rightPanelSelectionCount": 2,
    }
    changed_after = {
        "rightPanelEmpty": False,
        "rightPanelTextDigest": "different",
        "rightPanelTextLength": 180,
        "rightPanelSelectionCount": 4,
    }

    assert not _add_bet_confirmed(before, unchanged_after)
    assert _add_bet_confirmed(before, changed_after)
    assert _add_bet_confirmed({"rightPanelEmpty": True}, changed_after)


def test_review_add_summary_reports_sidebar_before_after_counts():
    selected_rows = [
        {"player": "Player One", "market": "Strikeouts", "side": "under", "line": 4.5},
        {"team": "Pittsburgh Pirates", "market": "Team RBIs", "side": "under", "line": 3.5},
    ]
    click_results = [{"status": "clicked"}, {"status": "clicked"}]
    add_bet_result = {
        "status": "clicked",
        "clickedBy": "playwright_locator",
        "beforeClick": {
            "rightPanelEmpty": False,
            "rightPanelSelectionCount": 2,
            "rightPanelTextLength": 120,
        },
        "postClick": {
            "rightPanelEmpty": False,
            "rightPanelSelectionCount": 4,
            "rightPanelTextLength": 220,
        },
        "addBetConfirmed": True,
    }

    summary = _review_add_summary(
        fixture_slug="465-test-fixture",
        matchup="Cardinals vs Pirates",
        selected_rows=selected_rows,
        click_results=click_results,
        add_bet_result=add_bet_result,
    )

    assert summary == {
        "fixtureSlug": "465-test-fixture",
        "matchup": "Cardinals vs Pirates",
        "gameAdded": True,
        "requestedLegs": 2,
        "clickedLegs": 2,
        "addBetClicked": True,
        "addBetConfirmed": True,
        "clickedBy": "playwright_locator",
        "sidebarBefore": {
            "empty": False,
            "selectionCount": 2,
            "textLength": 120,
        },
        "sidebarAfter": {
            "empty": False,
            "selectionCount": 4,
            "textLength": 220,
        },
        "sidebarSelectionDelta": 2,
        "sidebarChanged": True,
    }


def test_sidebar_group_target_uses_fixture_slug_matchup():
    target = _sidebar_group_target(
        fixture_slug="46575351-new-york-yankees-toronto-blue-jays",
        matchup=None,
    )

    assert target == {
        "fixtureSlug": "46575351-new-york-yankees-toronto-blue-jays",
        "matchup": "New York Yankees vs Toronto Blue Jays",
        "teams": ["New York Yankees", "Toronto Blue Jays"],
    }


def test_classify_moneyline_sidebar_allows_empty_sidebar():
    result = _classify_moneyline_sidebar_state(
        {
            "rightPanelEmpty": True,
            "rightPanelText": "",
            "rightPanelSelections": [],
        },
        requested=[],
    )

    assert result["mode"] == "empty"
    assert result["blockingReason"] is None


def test_classify_moneyline_sidebar_preserves_requested_moneyline():
    row_id = make_mlb_moneyline_row_id(
        "123-new-york-yankees-toronto-blue-jays",
        "New York Yankees",
    )
    requested = [
        {
            "rowId": row_id,
            "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
            "team": "New York Yankees",
        }
    ]

    result = _classify_moneyline_sidebar_state(
        {
            "rightPanelEmpty": False,
            "rightPanelText": "New York Yankees Winner (incl. Extra Innings) 1.72",
            "rightPanelSelections": [
                {
                    "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                    "team": "New York Yankees",
                    "market": "Winner (incl. Extra Innings)",
                    "rowId": row_id,
                    "odds": 1.72,
                }
            ],
        },
        requested=requested,
    )

    assert result["mode"] == "moneyline_only"
    assert result["alreadyPresentRowIds"] == [row_id]


def test_classify_moneyline_sidebar_blocks_sgm_group_text():
    result = _classify_moneyline_sidebar_state(
        {
            "rightPanelEmpty": False,
            "rightPanelText": "Same Game Multi New York Yankees Toronto Blue Jays",
            "rightPanelSelections": [],
        },
        requested=[],
    )

    assert result["mode"] == "blocked_mixed_or_unknown"
    assert result["blockingReason"] == "contains_sgm_or_custom_bet_group"


def test_moneyline_sidebar_removal_target_requires_row_and_team():
    row_id = make_mlb_moneyline_row_id(
        "123-new-york-yankees-toronto-blue-jays",
        "New York Yankees",
    )

    target = _sidebar_group_target(
        fixture_slug="123-new-york-yankees-toronto-blue-jays",
        matchup=None,
        row_id=row_id,
        team="New York Yankees",
    )

    assert target["type"] == "mlb_moneyline"
    assert target["rowId"] == row_id
    assert target["team"] == "New York Yankees"


def test_moneyline_build_status_reports_already_built():
    row_id = make_mlb_moneyline_row_id(
        "123-new-york-yankees-toronto-blue-jays",
        "New York Yankees",
    )
    result = _moneyline_build_result(
        status=None,
        requested=[
            {
                "rowId": row_id,
                "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                "team": "New York Yankees",
                "researchedOdds": 1.72,
            }
        ],
        added=[],
        already_present=[
            {
                "rowId": row_id,
                "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                "team": "New York Yankees",
                "researchedOdds": 1.72,
                "reason": "selection_already_present",
            }
        ],
        remaining=[],
        warnings=["selection_already_present"],
        sidebar={"mode": "moneyline_only"},
    )

    assert result["status"] == "already_built_for_review"
    assert result["requestedSelections"] == 1
    assert result["addedSelections"] == []
    assert result["alreadyPresentSelections"][0]["rowId"] == row_id
    assert result["remainingSelections"] == []
    assert result["safety"] == {
        "enteredStakeAmount": False,
        "clickedPlaceBet": False,
    }


def test_moneyline_build_status_reports_partial_with_remaining_rows():
    result = _moneyline_build_result(
        status=None,
        requested=[
            {
                "rowId": "mlb_ml_a",
                "fixtureSlug": "fixture-a",
                "team": "Team A",
                "researchedOdds": 1.72,
            },
            {
                "rowId": "mlb_ml_b",
                "fixtureSlug": "fixture-b",
                "team": "Team B",
                "researchedOdds": 1.95,
            },
        ],
        added=[
            {
                "rowId": "mlb_ml_a",
                "fixtureSlug": "fixture-a",
                "team": "Team A",
                "researchedOdds": 1.72,
                "clickedOdds": 1.68,
                "oddsMoved": True,
            }
        ],
        already_present=[],
        remaining=[
            {
                "rowId": "mlb_ml_b",
                "fixtureSlug": "fixture-b",
                "team": "Team B",
                "researchedOdds": 1.95,
                "reason": "visible_moneyline_selection_not_found_after_retry",
            }
        ],
        warnings=["odds_moved"],
        sidebar={"mode": "moneyline_only"},
    )

    assert result["status"] == "partial_review_slip"
    assert result["addedSelections"][0]["oddsMoved"] is True
    assert result["remainingSelections"][0]["reason"] == "visible_moneyline_selection_not_found_after_retry"


def test_sidebar_remove_confirmed_accepts_disappeared_target_or_sidebar_shrink():
    before = {
        "rightPanelTextDigest": "abc",
        "rightPanelTextLength": 220,
        "rightPanelSelectionCount": 4,
    }
    after = {
        "rightPanelTextDigest": "def",
        "rightPanelTextLength": 140,
        "rightPanelSelectionCount": 2,
    }

    assert _sidebar_remove_confirmed(
        remove_result={"status": "clicked", "targetStillVisible": False},
        before_state=before,
        after_state=before,
    )
    assert _sidebar_remove_confirmed(
        remove_result={"status": "clicked", "targetStillVisible": True},
        before_state=before,
        after_state=after,
    )
    assert not _sidebar_remove_confirmed(
        remove_result={"status": "not_removed"},
        before_state=before,
        after_state=after,
    )


def test_sidebar_clear_confirmed_requires_empty_or_selection_drop_to_zero():
    before = {
        "rightPanelTextDigest": "abc",
        "rightPanelTextLength": 220,
        "rightPanelSelectionCount": 4,
        "rightPanelEmpty": False,
    }
    cleared_after = {
        "rightPanelTextDigest": "def",
        "rightPanelTextLength": 80,
        "rightPanelSelectionCount": 0,
        "rightPanelEmpty": True,
    }
    unchanged_after = {
        "rightPanelTextDigest": "abc",
        "rightPanelTextLength": 220,
        "rightPanelSelectionCount": 4,
        "rightPanelEmpty": False,
    }

    assert _sidebar_clear_confirmed(
        clear_result={"status": "clicked"},
        before_state=before,
        after_state=cleared_after,
    )
    assert not _sidebar_clear_confirmed(
        clear_result={"status": "clicked"},
        before_state=before,
        after_state=unchanged_after,
    )
    assert not _sidebar_clear_confirmed(
        clear_result={"status": "not_cleared"},
        before_state=before,
        after_state=cleared_after,
    )


def test_sidebar_sgm_group_detects_existing_requested_legs():
    result = _sidebar_sgm_group_already_present(
        {
            "rightPanelEmpty": False,
            "rightPanelText": (
                "bet slip same game multi new york yankees toronto blue jays "
                "austin wells hits under 0.5 george springer total bases under 1.5"
            ),
        },
        fixture_slug="46575351-new-york-yankees-toronto-blue-jays",
        matchup="New York Yankees vs Toronto Blue Jays",
        selected_rows=[
            {
                "rowId": "sgm_a",
                "player": "Austin Wells",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
            },
            {
                "rowId": "sgm_b",
                "player": "George Springer",
                "market": "Total Bases",
                "side": "under",
                "line": 1.5,
            },
        ],
        required_legs=2,
    )

    assert result["status"] == "present"
    assert result["matchedLegs"] == 2


def test_sidebar_sgm_group_does_not_skip_ambiguous_sidebar_text():
    result = _sidebar_sgm_group_already_present(
        {
            "rightPanelEmpty": False,
            "rightPanelText": "bet slip same game multi new york yankees toronto blue jays hits under 0.5",
        },
        fixture_slug="46575351-new-york-yankees-toronto-blue-jays",
        matchup="New York Yankees vs Toronto Blue Jays",
        selected_rows=[
            {
                "rowId": "sgm_a",
                "player": "Austin Wells",
                "market": "Hits",
                "side": "under",
                "line": 0.5,
            },
            {
                "rowId": "sgm_b",
                "player": "George Springer",
                "market": "Total Bases",
                "side": "under",
                "line": 1.5,
            },
        ],
        required_legs=2,
    )

    assert result["status"] == "not_present"
    assert result["matchedLegs"] == 0


def test_transactional_selection_plan_replaces_failed_primary_before_clicking():
    primary_rows = [
        {"rowId": "sgm_a", "player": "Player A", "market": "Outs"},
        {"rowId": "sgm_b", "player": "Player B", "market": "Outs"},
        {"rowId": "sgm_c", "player": "Player C", "market": "Walks"},
    ]
    primary_preflight = [
        {"status": "buildable"},
        {"status": "not_clicked", "reason": "row_not_visible"},
        {"status": "buildable"},
    ]
    fallback_rows = [
        {"rowId": "sgm_d", "player": "Player D", "market": "Strikeouts"},
        {"rowId": "sgm_e", "player": "Player E", "market": "Runs"},
    ]
    fallback_preflight = [
        {"status": "buildable"},
        {"status": "not_clicked", "reason": "market_mismatch"},
    ]

    plan = _transactional_selection_plan(
        primary_rows=primary_rows,
        primary_preflight=primary_preflight,
        fallback_rows=fallback_rows,
        fallback_preflight=fallback_preflight,
        required_legs=3,
    )

    assert plan["status"] == "ready"
    assert [row["rowId"] for row in plan["selectedRows"]] == ["sgm_a", "sgm_c", "sgm_d"]
    assert plan["replacements"] == [
        {
            "reason": "primary_not_buildable",
            "replacement": {
                "rowId": "sgm_d",
                "player": "Player D",
                "team": None,
                "market": "Strikeouts",
                "side": None,
                "line": None,
                "odds": None,
                "scope": None,
                "playerId": None,
                "marketId": None,
                "lineId": None,
            },
        }
    ]
    assert plan["preflightFailures"][0]["reason"] == "row_not_visible"
    assert (
        plan["preflightFailures"][0]["diagnosticStatus"]
        == "market_parsed_with_row_id_but_click_preflight_failed"
    )


def test_transactional_selection_plan_blocks_when_replacement_cannot_fill_group():
    plan = _transactional_selection_plan(
        primary_rows=[
            {"rowId": "sgm_a", "player": "Player A", "market": "Outs"},
            {"rowId": "sgm_b", "player": "Player B", "market": "Outs"},
        ],
        primary_preflight=[
            {"status": "buildable"},
            {"status": "not_clicked", "reason": "row_not_visible"},
        ],
        fallback_rows=[{"rowId": "sgm_c", "player": "Player C", "market": "Runs"}],
        fallback_preflight=[{"status": "not_clicked", "reason": "market_mismatch"}],
        required_legs=2,
    )

    assert plan["status"] == "blocked_preflight_failed"
    assert plan["selectedRows"] == []
    assert plan["buildableRows"][0]["rowId"] == "sgm_a"
    assert plan["missingLegs"] == 1


def test_transactional_selection_plan_enforces_two_leg_sgm_minimum():
    plan = _transactional_selection_plan(
        primary_rows=[{"rowId": "sgm_a", "player": "Player A", "market": "Hits"}],
        primary_preflight=[{"status": "buildable"}],
        fallback_rows=[],
        fallback_preflight=[],
        required_legs=1,
    )

    assert plan["status"] == "blocked_preflight_failed"
    assert plan["requiredLegs"] == 2
    assert plan["missingLegs"] == 1


def test_preflight_returns_structured_timeout_when_local_budget_is_exhausted():
    rows = [{"rowId": "sgm_a", "player": "Player A", "market": "Hits"}]

    results = _preflight_sgm_review_selections(page=object(), rows=rows, deadline=0)

    assert results == [
        {
            "status": "timeout",
            "phase": "preflight",
            "reason": "local_helper_execution_timeout",
            "lastAction": "preflight_row_match",
            "lastAttemptedRowId": "sgm_a",
        }
    ]

    plan = _transactional_selection_plan(
        primary_rows=rows,
        primary_preflight=results,
        fallback_rows=[],
        fallback_preflight=[],
        required_legs=1,
    )
    assert plan["status"] == "timeout"
    assert plan["preflightFailures"][0]["phase"] == "primary_preflight"


def test_batch_resume_report_lists_completed_skipped_and_remaining_groups():
    report = _batch_resume_report(
        requested_groups=[
            {
                "fixtureSlug": "fixture-a",
                "matchup": "A vs B",
                "rowIds": ["sgm_a1", "sgm_a2"],
            },
            {
                "fixtureSlug": "fixture-b",
                "matchup": "C vs D",
                "rowIds": ["sgm_b1", "sgm_b2"],
            },
            {
                "fixtureSlug": "fixture-c",
                "matchup": "E vs F",
                "rowIds": ["sgm_c1", "sgm_c2"],
            },
        ],
        results=[
            {
                "fixtureSlug": "fixture-a",
                "matchup": "A vs B",
                "status": "built_for_review",
                "clickedLegs": 2,
                "selectedRows": [{"rowId": "sgm_a1"}, {"rowId": "sgm_a2"}],
            },
            {
                "fixtureSlug": "fixture-b",
                "matchup": "C vs D",
                "status": "skipped_existing",
                "existingLegs": 2,
                "selectedRows": [{"rowId": "sgm_b1"}, {"rowId": "sgm_b2"}],
            },
            {
                "fixtureSlug": "fixture-c",
                "matchup": "E vs F",
                "status": "timeout",
                "clickedLegs": 1,
                "selectedRows": [{"rowId": "sgm_c1"}, {"rowId": "sgm_c2"}],
            },
        ],
        stop_reason="local_helper_execution_timeout",
    )

    assert [group["fixtureSlug"] for group in report["completedGroups"]] == [
        "fixture-a",
        "fixture-b",
    ]
    assert report["skippedExistingGroupDetails"][0]["fixtureSlug"] == "fixture-b"
    assert report["remainingGroups"][0]["fixtureSlug"] == "fixture-c"
    assert report["lastAttemptedGroup"]["fixtureSlug"] == "fixture-c"
    assert report["resumeSafe"] is False


def test_preflight_blocks_visible_button_with_wrong_odds():
    result = {
        "status": "buildable",
        "clickedOdds": 1.93,
        "requestedOdds": 2.8465,
        "oddsChanged": True,
    }

    assert not _preflight_result_is_buildable(result)


def test_click_selection_downgrades_odds_mismatch_before_add_bet(monkeypatch):
    rows = [
        {
            "rowId": "sgm_xavier",
            "player": "Xavier Edwards",
            "market": "Hits",
            "side": "under",
            "line": 0.5,
            "odds": 2.8465,
        }
    ]

    def fake_click_one_sgm_selection(page, row):
        return {
            "selection": sgm_browser._compact_click_row(row),
            "status": "clicked",
            "clickedOdds": 1.93,
            "requestedOdds": 2.8465,
            "oddsChanged": True,
            "clickedLeafText": "Under\n1.93",
        }

    monkeypatch.setattr(
        sgm_browser,
        "_click_one_sgm_selection",
        fake_click_one_sgm_selection,
    )

    results = sgm_browser._click_sgm_review_selections(object(), rows)

    assert results[0]["status"] == "clicked_but_odds_mismatch_unverified"
    assert results[0]["reason"] == "clicked_odds_mismatch"


def test_click_selection_downgrades_unverified_click_before_add_bet(monkeypatch):
    rows = [
        {
            "rowId": "sgm_xavier",
            "player": "Xavier Edwards",
            "market": "Hits",
            "side": "under",
            "line": 0.5,
            "odds": 2.8465,
        }
    ]

    def fake_click_one_sgm_selection(page, row):
        return {
            "selection": sgm_browser._compact_click_row(row),
            "status": "clicked",
            "clickedOdds": 2.8465,
            "requestedOdds": 2.8465,
            "oddsChanged": False,
            "clickedLeafText": "Under\n2.85",
            "selectedAfterClick": False,
            "selectionEvidence": [],
        }

    monkeypatch.setattr(
        sgm_browser,
        "_click_one_sgm_selection",
        fake_click_one_sgm_selection,
    )

    results = sgm_browser._click_sgm_review_selections(object(), rows)

    assert results[0]["status"] == "clicked_but_selection_unverified"
    assert results[0]["reason"] == "clicked_selection_not_verified"


def test_selected_outcome_audit_rejects_unexpected_count():
    audit = {
        "expectedLegs": 5,
        "selectedOutcomeCount": 22,
        "selectedOutcomes": [],
    }

    assert not sgm_browser._selected_outcome_audit_is_valid(audit, expected_legs=5)


def test_selected_outcome_audit_rejects_background_only_evidence():
    audit = {
        "expectedLegs": 5,
        "selectedOutcomeCount": 5,
        "selectedOutcomes": [
            {"text": "Under\n2.85", "selectionEvidence": ["background_color"]}
            for _ in range(5)
        ],
    }

    assert not sgm_browser._selected_outcome_audit_is_valid(audit, expected_legs=5)


def test_selected_outcome_audit_accepts_reliable_ancestor_evidence():
    audit = {
        "expectedLegs": 5,
        "selectedOutcomeCount": 5,
        "selectedOutcomes": [
            {"text": "Under\n2.85", "selectionEvidence": ["ancestor_class_selected"]}
            for _ in range(5)
        ],
    }

    assert sgm_browser._selected_outcome_audit_is_valid(audit, expected_legs=5)


def test_sgm_row_id_does_not_change_when_provider_line_id_changes():
    base_row = {
        "team": "Miami Marlins",
        "player": "Xavier Edwards",
        "playerId": "player-xavier",
        "scope": "player",
        "market": "Hits",
        "marketId": "market-hits",
        "swishStatId": "stat-hits",
        "line": 0.5,
        "lineId": "line-old",
    }
    refreshed_row = {**base_row, "lineId": "line-new"}

    assert sgm_browser.make_sgm_selection_row_id(
        "46459719-miami-marlins-new-york-mets",
        base_row,
        "under",
    ) == sgm_browser.make_sgm_selection_row_id(
        "46459719-miami-marlins-new-york-mets",
        refreshed_row,
        "under",
    )


def test_match_sgm_review_selections_accepts_legacy_line_id_based_row_id():
    sgm_browser._SGM_ROW_ID_CACHE.clear()
    old_row = {
        "team": "Miami Marlins",
        "player": "Xavier Edwards",
        "playerId": "player-xavier",
        "scope": "player",
        "market": "Hits",
        "marketId": "market-hits",
        "swishStatId": "stat-hits",
        "line": 0.5,
        "lineId": "line-old",
        "under": 2.84,
        "over": 1.35,
        "playable": True,
    }
    old_row_id = sgm_browser._make_sgm_selection_row_id(
        "46459719-miami-marlins-new-york-mets",
        old_row,
        "under",
        include_provider_line_id=True,
    )
    sgm_browser._remember_sgm_board_rows(
        {
            "fixtureSlug": "46459719-miami-marlins-new-york-mets",
            "playerProps": [old_row],
            "teamMarkets": [],
        }
    )
    board = {
        "fixtureSlug": "46459719-miami-marlins-new-york-mets",
        "playerProps": [
            {
                "team": "Miami Marlins",
                "player": "Xavier Edwards",
                "playerId": "player-xavier",
                "scope": "player",
                "market": "Hits",
                "marketId": "market-hits",
                "swishStatId": "stat-hits",
                "line": 0.5,
                "lineId": "line-new",
                "under": 2.84,
                "over": 1.35,
                "playable": True,
            }
        ],
        "teamMarkets": [],
    }

    result = sgm_browser.match_sgm_review_selections(
        board,
        [{"rowId": old_row_id}],
    )

    assert result["missingSelections"] == []
    assert result["matchedRows"][0]["player"] == "Xavier Edwards"
    assert result["matchedRows"][0]["side"] == "under"


def test_add_bet_clicks_enabled_sticky_button_before_blocking_on_audit(monkeypatch):
    class StickyButton:
        def __init__(self, page):
            self.page = page

        @property
        def first(self):
            return self

        def count(self):
            return 1

        def scroll_into_view_if_needed(self, *, timeout):
            return None

        def click(self, *, timeout):
            self.page.clicked_sticky = True

    class AddBetPage:
        def __init__(self):
            self.clicked_sticky = False

        def locator(self, selector):
            assert selector == "#custom-bet-sticky-add"
            return StickyButton(self)

        def wait_for_timeout(self, ms):
            return None

    page = AddBetPage()

    def fake_read_bet_slip_state(current_page):
        return {
            "rightPanelEmpty": not current_page.clicked_sticky,
            "rightPanelSelectionCount": 5 if current_page.clicked_sticky else 0,
            "rightPanelTextDigest": "after" if current_page.clicked_sticky else "before",
            "rightPanelTextLength": 100 if current_page.clicked_sticky else 10,
        }

    def fail_if_called(*args, **kwargs):
        raise AssertionError("audit should not block an enabled Add Bet control")

    monkeypatch.setattr(sgm_browser, "_read_bet_slip_state", fake_read_bet_slip_state)
    monkeypatch.setattr(sgm_browser, "_wait_for_selected_outcome_audit", fail_if_called)

    result = sgm_browser._click_sgm_add_bet_button(page, expected_legs=5)

    assert result["status"] == "clicked"
    assert result["clickedBy"] == "playwright_locator"
    assert result["addBetConfirmed"] is True


def test_sgm_click_matcher_does_not_allow_generic_body_elements():
    class EmptyLocator:
        def count(self):
            return 0

    class InteractionPage:
        def __init__(self):
            self.scripts: list[str] = []

        def get_by_placeholder(self, value):
            return EmptyLocator()

        def locator(self, value):
            return EmptyLocator()

        def evaluate(self, script, arg=None):
            self.scripts.append(script)
            if "const wanted = norm(value)" in script:
                return True
            return {"status": "not_clicked", "reason": "test_stop"}

    page = InteractionPage()
    sgm_browser._interact_one_sgm_selection(
        page,
        {
            "rowId": "sgm_xavier",
            "player": "Xavier Edwards",
            "market": "Hits",
            "side": "under",
            "line": 0.5,
            "odds": 2.8465,
        },
        click=True,
    )

    interaction_script = page.scripts[-1]
    assert "body *" not in interaction_script


def test_compact_preflight_result_keeps_row_context_diagnostics():
    compact = _compact_preflight_result(
        {
            "status": "not_clicked",
            "reason": "no visible exact clickable selection button found",
            "candidateSamples": ["Under\n2.87"],
            "rowCandidateSamples": [
                {
                    "buttonText": "Under\n2.87",
                    "rowTextSample": "Aaron Judge Hits 0.5 Over 1.35 Under 2.87",
                    "ownerMatched": True,
                    "marketMatched": True,
                    "lineMatched": True,
                }
            ],
            "visibleRowSamples": ["Aaron Judge Hits 0.5 Over 1.35 Under 2.87"],
        }
    )

    assert compact["rowCandidateSamples"][0]["rowTextSample"].startswith("Aaron Judge")
    assert compact["visibleRowSamples"] == [
        "Aaron Judge Hits 0.5 Over 1.35 Under 2.87"
    ]


def test_batch_should_continue_after_failed_group_when_partial_mode_enabled():
    assert not _batch_should_stop_after_group_result(
        {"status": "blocked_preflight_failed"},
        continue_on_group_failure=True,
    )
    assert _batch_should_stop_after_group_result(
        {"status": "blocked_preflight_failed"},
        continue_on_group_failure=False,
    )
    assert not _batch_should_stop_after_group_result(
        {"status": "built_for_review"},
        continue_on_group_failure=False,
    )
