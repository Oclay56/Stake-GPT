# MLB Moneyline Review Slip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a review-only MLB main-winner moneyline builder that appends exact `mlb_ml_` selections to the visible Stake slip while preserving existing requested moneylines and blocking mixed/unknown sidebar states.

**Architecture:** Keep this separate from the SGM builder. Reuse the existing Stake MLB index scanner for row identity and visible button discovery, add one local-helper job type, add one GPT action, and extend the existing sidebar-removal action for a single MLB moneyline leg. The builder never enters stake and never clicks Place Bet.

**Tech Stack:** Python 3.13, FastAPI, Playwright sync API, Supabase local UI jobs, pytest, existing Custom GPT OpenAPI generator.

---

## File Structure

- Modify `app/stake_sgm_browser.py`
  - Add pure moneyline build request validation helpers.
  - Add sidebar moneyline-state detection helpers.
  - Add the Playwright moneyline click builder.
  - Extend `remove_stake_sidebar_group` and lower-level sidebar removal helpers for `mlb_ml_` row removal.
- Modify `app/local_ui_bridge.py`
  - Add `STAKE_MLB_MONEYLINE_BUILD_SLIP_JOB_TYPE = "stake_ui_mlb_moneyline_build_slip"`.
- Modify `app/local_stake_helper.py`
  - Import and route the new builder job.
  - Let review/build/all helper modes claim the job.
  - Pass `rowId`, `fixtureSlug`, and `team` through to sidebar removal.
- Modify `app/main.py`
  - Add `POST /mlb/stake-ui/moneyline-review-slip`.
  - Validate `reviewOnly`, normalize selections, create the local helper job, and return the compact result.
- Modify `app/gpt_action.py`
  - Add one GPT-visible action: `buildStakeUiMoneylineReviewSlip`.
  - Extend `removeStakeUiSidebarGroup` request schema with optional moneyline fields.
  - Keep OpenAPI operation count at 30.
- Modify `custom_gpt_action/custom-gpt-instructions.md`
  - Add the moneyline build workflow and sidebar boundary.
- Modify tests:
  - `tests/test_stake_sgm_browser.py`
  - `tests/test_local_stake_helper.py`
  - `tests/test_stake_ui_bridge.py`
  - `tests/test_gpt_action.py`

---

### Task 1: Pure Moneyline Selection Validation

**Files:**
- Modify: `app/stake_sgm_browser.py`
- Test: `tests/test_stake_sgm_browser.py`

- [ ] **Step 1: Write failing validation tests**

Append these tests near the existing moneyline row ID tests in `tests/test_stake_sgm_browser.py`:

```python
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
```

- [ ] **Step 2: Run validation tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_sgm_browser.py::test_prepare_moneyline_build_selections_validates_identity_and_dedupes tests\test_stake_sgm_browser.py::test_prepare_moneyline_build_selections_blocks_sgm_row_ids tests\test_stake_sgm_browser.py::test_prepare_moneyline_build_selections_blocks_row_id_identity_mismatch -q
```

Expected: FAIL because `_prepare_moneyline_build_selections` is not defined.

- [ ] **Step 3: Implement validation helpers**

Add this near `make_mlb_moneyline_row_id` in `app/stake_sgm_browser.py`:

```python
def _prepare_moneyline_build_selections(selections: list[dict[str, Any]]) -> dict[str, Any]:
    prepared: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for raw in selections or []:
        row_id = str((raw or {}).get("rowId") or "").strip()
        fixture_slug = str((raw or {}).get("fixtureSlug") or "").strip()
        team = str((raw or {}).get("team") or "").strip()
        researched_odds = _float_or_none((raw or {}).get("odds"))

        if not row_id or not fixture_slug or not team:
            errors.append({"selection": raw, "reason": "missing_row_id_fixture_or_team"})
            continue
        if not row_id.startswith("mlb_ml_"):
            errors.append({"selection": raw, "reason": "row_id_must_start_with_mlb_ml"})
            continue
        expected_row_id = make_mlb_moneyline_row_id(fixture_slug, team)
        if row_id != expected_row_id:
            errors.append({"selection": raw, "reason": "row_id_does_not_match_fixture_team"})
            continue

        identity = (fixture_slug, _text_key(team), row_id)
        if identity in seen:
            continue
        seen.add(identity)
        prepared.append(
            {
                "rowId": row_id,
                "fixtureSlug": fixture_slug,
                "team": team,
                "market": MONEYLINE_MARKET_LABEL,
                "marketKey": MONEYLINE_MARKET_KEY,
                "researchedOdds": researched_odds,
            }
        )

    if errors:
        return {
            "status": "blocked_invalid_row_id",
            "selections": prepared,
            "errors": errors,
        }
    if not prepared:
        return {
            "status": "blocked_missing_selection_identity",
            "selections": [],
            "errors": [{"reason": "no_valid_moneyline_selections"}],
        }
    return {"status": "ready", "selections": prepared, "errors": []}
```

- [ ] **Step 4: Run validation tests and confirm pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_sgm_browser.py::test_prepare_moneyline_build_selections_validates_identity_and_dedupes tests\test_stake_sgm_browser.py::test_prepare_moneyline_build_selections_blocks_sgm_row_ids tests\test_stake_sgm_browser.py::test_prepare_moneyline_build_selections_blocks_row_id_identity_mismatch -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app\stake_sgm_browser.py tests\test_stake_sgm_browser.py
git commit -m "Add moneyline build selection validation"
```

---

### Task 2: Sidebar State Classification and Moneyline Removal Target

**Files:**
- Modify: `app/stake_sgm_browser.py`
- Test: `tests/test_stake_sgm_browser.py`

- [ ] **Step 1: Write failing sidebar tests**

Append these tests near `test_sidebar_group_target_uses_fixture_slug_matchup` in `tests/test_stake_sgm_browser.py`:

```python
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
                    "market": MONEYLINE_MARKET_LABEL,
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
```

- [ ] **Step 2: Run sidebar tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_sgm_browser.py::test_classify_moneyline_sidebar_allows_empty_sidebar tests\test_stake_sgm_browser.py::test_classify_moneyline_sidebar_preserves_requested_moneyline tests\test_stake_sgm_browser.py::test_classify_moneyline_sidebar_blocks_sgm_group_text tests\test_stake_sgm_browser.py::test_moneyline_sidebar_removal_target_requires_row_and_team -q
```

Expected: FAIL because `_classify_moneyline_sidebar_state` does not exist and `_sidebar_group_target` does not accept `row_id` or `team`.

- [ ] **Step 3: Implement sidebar classification and extend target shape**

Add this helper in `app/stake_sgm_browser.py` near the sidebar helper functions:

```python
def _classify_moneyline_sidebar_state(
    slip: dict[str, Any],
    *,
    requested: list[dict[str, Any]],
) -> dict[str, Any]:
    text = str((slip or {}).get("rightPanelText") or "").strip()
    if bool((slip or {}).get("rightPanelEmpty")) or not text:
        return {
            "mode": "empty",
            "blockingReason": None,
            "alreadyPresentRowIds": [],
            "moneylineSelections": [],
        }

    lowered = text.lower()
    if "same game multi" in lowered or "custom bet" in lowered:
        return {
            "mode": "blocked_mixed_or_unknown",
            "blockingReason": "contains_sgm_or_custom_bet_group",
            "alreadyPresentRowIds": [],
            "moneylineSelections": [],
        }

    sidebar_selections = [
        item
        for item in (slip or {}).get("rightPanelSelections") or []
        if _text_key((item or {}).get("market")) == _text_key(MONEYLINE_MARKET_LABEL)
        and str((item or {}).get("rowId") or "").startswith("mlb_ml_")
    ]
    if not sidebar_selections:
        return {
            "mode": "blocked_mixed_or_unknown",
            "blockingReason": "unknown_sidebar_selection",
            "alreadyPresentRowIds": [],
            "moneylineSelections": [],
        }

    requested_keys = {
        (
            str(item.get("fixtureSlug") or "").strip(),
            _text_key(item.get("team")),
            str(item.get("rowId") or "").strip(),
        )
        for item in requested or []
    }
    already_present = []
    for item in sidebar_selections:
        key = (
            str(item.get("fixtureSlug") or "").strip(),
            _text_key(item.get("team")),
            str(item.get("rowId") or "").strip(),
        )
        if key in requested_keys:
            already_present.append(str(item.get("rowId") or "").strip())

    return {
        "mode": "moneyline_only",
        "blockingReason": None,
        "alreadyPresentRowIds": already_present,
        "moneylineSelections": sidebar_selections,
    }
```

Update `_sidebar_group_target` signature and first branch:

```python
def _sidebar_group_target(
    *,
    fixture_slug: str | None,
    matchup: str | None,
    row_id: str | None = None,
    team: str | None = None,
) -> dict[str, Any]:
    if row_id and str(row_id).startswith("mlb_ml_"):
        clean_fixture_slug = str(fixture_slug or "").strip()
        clean_team = str(team or "").strip()
        if not clean_fixture_slug or not clean_team:
            raise RuntimeError("fixtureSlug and team are required to remove an MLB moneyline.")
        return {
            "type": "mlb_moneyline",
            "fixtureSlug": clean_fixture_slug,
            "rowId": str(row_id).strip(),
            "team": clean_team,
            "teams": [clean_team],
            "matchup": matchup,
        }
```

Keep the existing SGM fixture/matchup branch after this new branch.

- [ ] **Step 4: Run sidebar tests and confirm pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_sgm_browser.py::test_classify_moneyline_sidebar_allows_empty_sidebar tests\test_stake_sgm_browser.py::test_classify_moneyline_sidebar_preserves_requested_moneyline tests\test_stake_sgm_browser.py::test_classify_moneyline_sidebar_blocks_sgm_group_text tests\test_stake_sgm_browser.py::test_moneyline_sidebar_removal_target_requires_row_and_team -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app\stake_sgm_browser.py tests\test_stake_sgm_browser.py
git commit -m "Classify MLB moneyline sidebar state"
```

---

### Task 3: Browser Moneyline Builder

**Files:**
- Modify: `app/stake_sgm_browser.py`
- Test: `tests/test_stake_sgm_browser.py`

- [ ] **Step 1: Write failing pure result-shaping tests**

Append these tests in `tests/test_stake_sgm_browser.py`:

```python
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
```

- [ ] **Step 2: Run result-shaping tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_sgm_browser.py::test_moneyline_build_status_reports_already_built tests\test_stake_sgm_browser.py::test_moneyline_build_status_reports_partial_with_remaining_rows -q
```

Expected: FAIL because `_moneyline_build_result` is not defined.

- [ ] **Step 3: Implement result shaping**

Add this helper in `app/stake_sgm_browser.py`:

```python
def _moneyline_build_result(
    *,
    status: str | None,
    requested: list[dict[str, Any]],
    added: list[dict[str, Any]],
    already_present: list[dict[str, Any]],
    remaining: list[dict[str, Any]],
    warnings: list[str],
    sidebar: dict[str, Any],
) -> dict[str, Any]:
    if status is None:
        if remaining and (added or already_present):
            status = "partial_review_slip"
        elif remaining:
            status = "blocked"
        elif added:
            status = "built_for_review"
        elif already_present:
            status = "already_built_for_review"
        else:
            status = "blocked_missing_selection_identity"

    return {
        "source": "stake_ui_mlb_moneyline_review_slip",
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reviewOnly": True,
        "requestedSelections": len(requested),
        "addedSelections": added,
        "alreadyPresentSelections": already_present,
        "remainingSelections": remaining,
        "warnings": list(dict.fromkeys(warnings)),
        "sidebar": sidebar,
        "safety": {
            "enteredStakeAmount": False,
            "clickedPlaceBet": False,
        },
    }
```

- [ ] **Step 4: Add browser builder skeleton**

Add this public function in `app/stake_sgm_browser.py` after `read_stake_mlb_moneylines`:

```python
def build_stake_mlb_moneyline_review_slip(
    selections: list[dict[str, Any]],
    *,
    execution_timeout_seconds: int | float | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    prepared = _prepare_moneyline_build_selections(selections)
    if prepared["status"] != "ready":
        return _moneyline_build_result(
            status=prepared["status"],
            requested=prepared["selections"],
            added=[],
            already_present=[],
            remaining=[
                {
                    "selection": error.get("selection"),
                    "reason": error.get("reason"),
                }
                for error in prepared["errors"]
            ],
            warnings=[],
            sidebar={"mode": "not_read"},
        )

    from playwright.sync_api import sync_playwright

    deadline = _build_execution_deadline(execution_timeout_seconds)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _find_or_open_mlb_page(browser.contexts[0])
        _expand_mlb_game_list(page, limit=100)
        state = _read_stake_ui_state_from_page(page)
        sidebar = _classify_moneyline_sidebar_state(
            state.get("slip") or {},
            requested=prepared["selections"],
        )
        if sidebar["mode"] == "blocked_mixed_or_unknown":
            return _moneyline_build_result(
                status="blocked_sidebar_not_moneyline_only",
                requested=prepared["selections"],
                added=[],
                already_present=[],
                remaining=[],
                warnings=[],
                sidebar=sidebar,
            )

        already_present = []
        added = []
        remaining = []
        warnings = []
        present_row_ids = set(sidebar.get("alreadyPresentRowIds") or [])
        for selection in prepared["selections"]:
            if _execution_deadline_expired(deadline, reserve_seconds=2.0):
                remaining.append({**selection, "reason": "local_helper_execution_timeout"})
                continue
            if selection["rowId"] in present_row_ids:
                already_present.append({**selection, "reason": "selection_already_present"})
                warnings.append("selection_already_present")
                continue

            click_result = _click_mlb_moneyline_selection_with_retry(
                page,
                selection,
                deadline=deadline,
            )
            if click_result.get("status") == "added":
                added.append(click_result["selection"])
                if click_result["selection"].get("oddsMoved"):
                    warnings.append("odds_moved")
            else:
                remaining.append({**selection, "reason": click_result.get("reason")})

        final_state = _read_stake_ui_state_from_page(page)
        final_sidebar = _classify_moneyline_sidebar_state(
            final_state.get("slip") or {},
            requested=prepared["selections"],
        )
        return _moneyline_build_result(
            status=None,
            requested=prepared["selections"],
            added=added,
            already_present=already_present,
            remaining=remaining,
            warnings=warnings,
            sidebar=final_sidebar,
        )
```

- [ ] **Step 5: Add click helper stubs with bounded behavior**

Add these helpers in `app/stake_sgm_browser.py`; they may be refined while testing against live DOM:

```python
def _click_mlb_moneyline_selection_with_retry(
    page: Any,
    selection: dict[str, Any],
    *,
    deadline: float | None,
) -> dict[str, Any]:
    first = _click_mlb_moneyline_selection_once(page, selection)
    if first.get("status") == "added":
        return first

    _expand_mlb_game_list(page, limit=100)
    second = _click_mlb_moneyline_selection_once(page, selection)
    if second.get("status") == "added":
        return second

    if _execution_deadline_expired(deadline, reserve_seconds=2.0):
        return {"status": "not_added", "reason": "local_helper_execution_timeout"}

    page.goto(MLB_INDEX_URL, wait_until="domcontentloaded", timeout=45_000)
    _expand_mlb_game_list(page, limit=100)
    third = _click_mlb_moneyline_selection_once(page, selection)
    if third.get("status") == "added":
        return third
    return {
        "status": "not_added",
        "reason": third.get("reason") or "visible_moneyline_selection_not_found_after_retry",
    }
```

Add `_click_mlb_moneyline_selection_once` by reusing the existing raw card extraction shape:

```python
def _click_mlb_moneyline_selection_once(page: Any, selection: dict[str, Any]) -> dict[str, Any]:
    raw_cards = _extract_mlb_moneyline_cards(page)
    board = _normalize_mlb_moneyline_cards(raw_cards, limit=100)
    game = next(
        (
            item
            for item in board.get("games") or []
            if item.get("fixtureSlug") == selection.get("fixtureSlug")
        ),
        None,
    )
    if not game:
        return {"status": "not_added", "reason": "fixture_not_visible"}

    current = next(
        (
            item
            for item in game.get("selections") or []
            if item.get("rowId") == selection.get("rowId")
            and _text_key(item.get("team")) == _text_key(selection.get("team"))
        ),
        None,
    )
    if not current:
        return {"status": "not_added", "reason": "visible_moneyline_selection_not_found"}

    clicked = _click_visible_moneyline_outcome_button(page, selection)
    if not clicked.get("clicked"):
        return {"status": "not_added", "reason": clicked.get("reason")}

    state_after = _read_stake_ui_state_from_page(page)
    sidebar = _classify_moneyline_sidebar_state(
        state_after.get("slip") or {},
        requested=[selection],
    )
    if selection["rowId"] not in set(sidebar.get("alreadyPresentRowIds") or []):
        return {"status": "not_added", "reason": "sidebar_not_updated_after_click"}

    clicked_odds = _float_or_none(current.get("odds"))
    researched_odds = _float_or_none(selection.get("researchedOdds"))
    return {
        "status": "added",
        "selection": {
            **selection,
            "clickedOdds": clicked_odds,
            "oddsMoved": (
                researched_odds is not None
                and clicked_odds is not None
                and abs(researched_odds - clicked_odds) > 0.000001
            ),
        },
    }
```

Implement `_click_visible_moneyline_outcome_button` conservatively using text scoping:

```python
def _click_visible_moneyline_outcome_button(page: Any, selection: dict[str, Any]) -> dict[str, Any]:
    team = str(selection.get("team") or "").strip()
    fixture_slug = str(selection.get("fixtureSlug") or "").strip()
    matchup = _fixture_matchup_from_slug(fixture_slug).get("matchup")
    try:
        candidates = page.get_by_text(team, exact=True).all()
    except Exception as exc:
        return {"clicked": False, "reason": f"team_text_lookup_failed:{exc}"}

    for candidate in candidates:
        try:
            candidate.scroll_into_view_if_needed(timeout=2_000)
            box = candidate.bounding_box()
            if not box:
                continue
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=6)
            page.mouse.down()
            page.wait_for_timeout(80)
            page.mouse.up()
            page.wait_for_timeout(300)
            return {
                "clicked": True,
                "team": team,
                "matchup": matchup,
                "clickedBy": "playwright_mouse_center",
            }
        except Exception:
            continue
    return {"clicked": False, "reason": "visible_moneyline_button_not_clicked"}
```

- [ ] **Step 6: Run result-shaping tests and a browser module smoke subset**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_sgm_browser.py::test_moneyline_build_status_reports_already_built tests\test_stake_sgm_browser.py::test_moneyline_build_status_reports_partial_with_remaining_rows tests\test_stake_sgm_browser.py::test_moneyline_row_id_is_stable_and_separate_from_sgm_ids -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app\stake_sgm_browser.py tests\test_stake_sgm_browser.py
git commit -m "Add MLB moneyline review slip builder"
```

---

### Task 4: Local Helper Job Routing

**Files:**
- Modify: `app/local_ui_bridge.py`
- Modify: `app/local_stake_helper.py`
- Test: `tests/test_local_stake_helper.py`

- [ ] **Step 1: Write failing local-helper tests**

Append these tests to `tests/test_local_stake_helper.py`:

```python
def test_process_job_runs_mlb_moneyline_review_slip_builder(monkeypatch):
    def fake_build_stake_mlb_moneyline_review_slip(
        selections: list[dict[str, object]],
        *,
        execution_timeout_seconds,
        cdp_url: str,
    ):
        assert cdp_url == "http://127.0.0.1:9222"
        assert execution_timeout_seconds == 55
        assert selections == [
            {
                "rowId": "mlb_ml_yankees",
                "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                "team": "New York Yankees",
                "odds": 1.72,
            }
        ]
        return {
            "source": "stake_ui_mlb_moneyline_review_slip",
            "status": "built_for_review",
        }

    monkeypatch.setattr(
        local_stake_helper,
        "build_stake_mlb_moneyline_review_slip",
        fake_build_stake_mlb_moneyline_review_slip,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-moneyline-build",
        "jobType": "stake_ui_mlb_moneyline_build_slip",
        "request": {
            "selections": [
                {
                    "rowId": "mlb_ml_yankees",
                    "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                    "team": "New York Yankees",
                    "odds": 1.72,
                }
            ],
            "localExecutionTimeoutSeconds": 55,
        },
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][0] == "job-moneyline-build"
    assert store.completed[0][1]["status"] == "built_for_review"


def test_review_mode_claims_moneyline_review_slip_jobs():
    assert "stake_ui_mlb_moneyline_build_slip" in local_stake_helper._job_types_for_mode("review")
```

- [ ] **Step 2: Run local-helper tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_local_stake_helper.py::test_process_job_runs_mlb_moneyline_review_slip_builder tests\test_local_stake_helper.py::test_review_mode_claims_moneyline_review_slip_jobs -q
```

Expected: FAIL because the job constant and routing do not exist.

- [ ] **Step 3: Add job constant and routing**

In `app/local_ui_bridge.py`, add:

```python
STAKE_MLB_MONEYLINE_BUILD_SLIP_JOB_TYPE = "stake_ui_mlb_moneyline_build_slip"
```

In `app/local_stake_helper.py`, import:

```python
    STAKE_MLB_MONEYLINE_BUILD_SLIP_JOB_TYPE,
```

Also import:

```python
    build_stake_mlb_moneyline_review_slip,
```

Add the job type to `fixture_optional_types` because a moneyline request can contain multiple fixture slugs:

```python
        STAKE_MLB_MONEYLINE_BUILD_SLIP_JOB_TYPE,
```

Add the routing branch before SGM batch build:

```python
        elif job_type == STAKE_MLB_MONEYLINE_BUILD_SLIP_JOB_TYPE:
            result = await asyncio.to_thread(
                build_stake_mlb_moneyline_review_slip,
                list(request.get("selections") or []),
                execution_timeout_seconds=request.get("localExecutionTimeoutSeconds"),
                cdp_url=cdp_url,
            )
```

Add the job type to review, build, and all mode lists:

```python
            STAKE_MLB_MONEYLINE_BUILD_SLIP_JOB_TYPE,
```

Add a label:

```python
    if job_type == STAKE_MLB_MONEYLINE_BUILD_SLIP_JOB_TYPE:
        return "Building Stake MLB moneyline review slip"
```

- [ ] **Step 4: Pass moneyline removal fields through existing removal job**

Update the existing removal call in `app/local_stake_helper.py`:

```python
            result = await asyncio.to_thread(
                remove_stake_sidebar_group,
                cdp_url=cdp_url,
                fixture_slug=fixture_slug or None,
                matchup=str(request.get("matchup") or "").strip() or None,
                row_id=str(request.get("rowId") or "").strip() or None,
                team=str(request.get("team") or "").strip() or None,
            )
```

Update `remove_stake_sidebar_group` signature in `app/stake_sgm_browser.py`:

```python
def remove_stake_sidebar_group(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    fixture_slug: str | None = None,
    matchup: str | None = None,
    row_id: str | None = None,
    team: str | None = None,
) -> dict[str, Any]:
```

Update the missing-target check:

```python
    if not fixture_slug and not matchup and not row_id:
        raise RuntimeError("fixtureSlug, matchup, or rowId is required to remove a sidebar item.")
```

Call `_sidebar_group_target` with `row_id=row_id, team=team`.

- [ ] **Step 5: Run local-helper tests and confirm pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_local_stake_helper.py::test_process_job_runs_mlb_moneyline_review_slip_builder tests\test_local_stake_helper.py::test_review_mode_claims_moneyline_review_slip_jobs tests\test_local_stake_helper.py::test_process_job_runs_sidebar_group_remover -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app\local_ui_bridge.py app\local_stake_helper.py app\stake_sgm_browser.py tests\test_local_stake_helper.py
git commit -m "Route MLB moneyline review slip helper jobs"
```

---

### Task 5: FastAPI Route

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_stake_ui_bridge.py`

- [ ] **Step 1: Write failing route test**

Append this test near `test_stake_ui_mlb_moneylines_route_returns_enriched_read_only_board` in `tests/test_stake_ui_bridge.py`:

```python
def test_stake_ui_mlb_moneyline_review_slip_route_creates_helper_job():
    class FakeMoneylineBuildJobStore(FakeCompletedMlbMoneylinesJobStore):
        async def create_job(self, *, job_type, request, timeout_seconds):
            self.created_jobs.append(
                {
                    "jobId": "job-moneyline-build",
                    "jobType": job_type,
                    "request": request,
                    "timeoutSeconds": timeout_seconds,
                }
            )
            return {"jobId": "job-moneyline-build"}

        async def wait_for_completed_result(self, job_id, *, timeout_seconds):
            return {
                "jobId": job_id,
                "status": "completed",
                "workerId": "helper-1",
                "createdAt": "2026-05-31T12:00:00+00:00",
                "completedAt": "2026-05-31T12:00:01+00:00",
                "result": {
                    "source": "stake_ui_mlb_moneyline_review_slip",
                    "status": "built_for_review",
                    "reviewOnly": True,
                    "requestedSelections": 1,
                    "addedSelections": [
                        {
                            "rowId": "mlb_ml_yankees",
                            "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                            "team": "New York Yankees",
                        }
                    ],
                    "alreadyPresentSelections": [],
                    "remainingSelections": [],
                    "safety": {
                        "enteredStakeAmount": False,
                        "clickedPlaceBet": False,
                    },
                },
            }

    fake_store = FakeMoneylineBuildJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/moneyline-review-slip",
            json={
                "reviewOnly": True,
                "timeoutSeconds": 2,
                "selections": [
                    {
                        "rowId": "mlb_ml_yankees",
                        "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
                        "team": "New York Yankees",
                        "odds": 1.72,
                    }
                ],
            },
        )

    result = response.json()
    created = fake_store.created_jobs[0]

    assert response.status_code == 200
    assert created["jobType"] == "stake_ui_mlb_moneyline_build_slip"
    assert created["request"]["purpose"] == "stake_ui_mlb_moneyline_review_slip"
    assert created["request"]["forbiddenActions"] == ["enter_stake_amount", "click_place_bet"]
    assert result["source"] == "stake_ui_mlb_moneyline_review_slip_via_local_helper"
    assert result["result"]["status"] == "built_for_review"
```

- [ ] **Step 2: Run route test and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_ui_bridge.py::test_stake_ui_mlb_moneyline_review_slip_route_creates_helper_job -q
```

Expected: FAIL with 404 because the route does not exist.

- [ ] **Step 3: Implement route**

Import the new constant in `app/main.py`:

```python
    STAKE_MLB_MONEYLINE_BUILD_SLIP_JOB_TYPE,
```

Add this route near the moneyline read route:

```python
@app.post("/mlb/stake-ui/moneyline-review-slip")
async def mlb_stake_ui_moneyline_review_slip(
    payload: dict[str, Any] = Body(default_factory=dict),
    _: None = Depends(require_gpt_api_key),
    job_store: SupabaseLocalUiJobStore = Depends(get_local_ui_job_store),
) -> Any:
    if not job_store.enabled():
        raise HTTPException(
            status_code=503,
            detail={
                "source": "local_ui_bridge",
                "message": (
                    "Supabase local UI bridge is not configured. Set SUPABASE_URL "
                    "and SUPABASE_SERVICE_ROLE_KEY on Render and the local helper."
                ),
            },
        )

    review_only = _bool_from_body(payload, "reviewOnly", "review_only", True)
    if not review_only:
        raise HTTPException(
            status_code=422,
            detail="reviewOnly must be true. AZP will not place bets or enter stake amounts.",
        )

    selections = payload.get("selections")
    if not isinstance(selections, list) or not selections:
        raise HTTPException(
            status_code=422,
            detail="selections must contain at least one MLB moneyline row.",
        )

    timeout_seconds = _clean_int_from_body(
        payload,
        "timeoutSeconds",
        90,
        minimum=1,
        maximum=180,
    )
    request = {
        "requestedBy": "custom_gpt",
        "purpose": "stake_ui_mlb_moneyline_review_slip",
        "reviewOnly": True,
        "forbiddenActions": ["enter_stake_amount", "click_place_bet"],
        "selections": selections[:30],
        "localExecutionTimeoutSeconds": _local_helper_execution_timeout_seconds(
            timeout_seconds
        ),
    }

    job: dict[str, Any] | None = None
    try:
        job = await job_store.create_job(
            job_type=STAKE_MLB_MONEYLINE_BUILD_SLIP_JOB_TYPE,
            request=request,
            timeout_seconds=timeout_seconds,
        )
        completed = await job_store.wait_for_completed_result(
            job["jobId"],
            timeout_seconds=timeout_seconds,
        )
    except LocalUiBridgeDisabled as exc:
        raise HTTPException(
            status_code=503,
            detail={"source": "local_ui_bridge", "message": str(exc)},
        ) from exc
    except LocalUiBridgeTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail=_local_ui_timeout_detail(
                message=str(exc),
                job=job,
                fixture_slug=None,
                matchup="MLB moneyline review slip",
            ),
        ) from exc
    except LocalUiBridgeError as exc:
        raise HTTPException(
            status_code=502,
            detail={"source": "local_ui_bridge", "message": str(exc)},
        ) from exc

    if completed.get("status") != "completed":
        raise HTTPException(
            status_code=502,
            detail={
                "source": "local_ui_bridge",
                "message": completed.get("error") or "Local helper job did not complete.",
                "status": completed.get("status"),
                "jobId": completed.get("jobId"),
            },
        )

    return {
        "decisionOwner": "custom_gpt",
        "source": "stake_ui_mlb_moneyline_review_slip_via_local_helper",
        "purpose": "stake_ui_review_only_moneyline_builder",
        "bridge": _local_ui_bridge_summary(completed),
        "result": completed.get("result") or {},
    }
```

- [ ] **Step 4: Run route test and confirm pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_ui_bridge.py::test_stake_ui_mlb_moneyline_review_slip_route_creates_helper_job -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app\main.py tests\test_stake_ui_bridge.py
git commit -m "Add MLB moneyline review slip API route"
```

---

### Task 6: OpenAPI Schema and GPT Instructions

**Files:**
- Modify: `app/gpt_action.py`
- Modify: `custom_gpt_action/custom-gpt-instructions.md`
- Test: `tests/test_gpt_action.py`
- Test: `tests/test_stake_ui_bridge.py`

- [ ] **Step 1: Write failing OpenAPI tests**

Add this to `tests/test_stake_ui_bridge.py`:

```python
def test_gpt_schema_exposes_stake_ui_mlb_moneyline_review_slip_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operation = schema["paths"]["/mlb/stake-ui/moneyline-review-slip"]["post"]
    properties = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]

    assert operation["operationId"] == "buildStakeUiMoneylineReviewSlip"
    assert properties["reviewOnly"]["const"] is True
    assert properties["selections"]["items"]["properties"]["rowId"]["type"] == "string"
    assert properties["selections"]["items"]["properties"]["fixtureSlug"]["type"] == "string"
    assert properties["selections"]["items"]["properties"]["team"]["type"] == "string"
```

Update `tests/test_gpt_action.py` operation-count test from 29 to 30:

```python
def test_gpt_action_schema_stays_within_custom_gpt_operation_limit():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")

    operations = [
        operation
        for path_item in schema["paths"].values()
        for operation in path_item.values()
    ]

    assert len(operations) == 30
```

- [ ] **Step 2: Run OpenAPI tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_ui_bridge.py::test_gpt_schema_exposes_stake_ui_mlb_moneyline_review_slip_action tests\test_gpt_action.py::test_gpt_action_schema_stays_within_custom_gpt_operation_limit -q
```

Expected: FAIL because the OpenAPI path does not exist and count is still 29.

- [ ] **Step 3: Add OpenAPI path and request schema**

In `app/gpt_action.py`, add this path near `/mlb/stake-ui/mlb-moneylines`:

```python
            "/mlb/stake-ui/moneyline-review-slip": {
                "post": _operation(
                    "buildStakeUiMoneylineReviewSlip",
                    "Build Stake UI MLB moneyline review slip",
                    (
                        "Adds exact visible pregame MLB Winner (incl. Extra Innings) "
                        "moneyline teams to the review-only Stake sidebar through the "
                        "local helper. This action is moneyline-only, never enters a "
                        "stake amount, and never clicks Place Bet."
                    ),
                    request_body=_stake_ui_mlb_moneyline_review_slip_request_body(),
                )
            },
```

Add this schema helper:

```python
def _stake_ui_mlb_moneyline_review_slip_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "reviewOnly": {"type": "boolean", "const": True},
                        "selections": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 30,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "rowId": {"type": "string"},
                                    "fixtureSlug": {"type": "string"},
                                    "team": {"type": "string"},
                                    "odds": {"type": "number"},
                                },
                                "required": ["rowId", "fixtureSlug", "team", "odds"],
                                "additionalProperties": True,
                            },
                        },
                        "timeoutSeconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 180,
                        },
                    },
                    "required": ["reviewOnly", "selections"],
                    "additionalProperties": True,
                }
            }
        },
    }
```

Extend `_stake_ui_remove_sidebar_group_request_body` properties with:

```python
                        "rowId": {
                            "type": "string",
                            "description": "Optional exact MLB moneyline row id to remove.",
                        },
                        "team": {
                            "type": "string",
                            "description": "Team name for one MLB moneyline sidebar leg.",
                        },
```

Update the remove operation description to mention SGM group or one MLB moneyline leg.

- [ ] **Step 4: Update GPT instructions**

Add a moneyline build section to `custom_gpt_action/custom-gpt-instructions.md`:

```markdown
## MLB Moneyline Review-Slip Workflow

- Use `getStakeUiMlbMoneylines` first for visible pregame `Winner (incl. Extra Innings)` rows and team context.
- Use only returned exact `mlb_ml_` row IDs for `buildStakeUiMoneylineReviewSlip`.
- Moneyline builds are separate from SGM builds. Do not mix SGM/custom-bet groups with ordinary moneyline legs.
- If `buildStakeUiMoneylineReviewSlip` returns `blocked_sidebar_not_moneyline_only`, tell the user what is already in the sidebar and ask before clearing.
- If it returns `partial_review_slip`, report added, already-present, and remaining teams. Ask whether to retry only `remainingSelections`.
- Odds movement on the same team does not block the build. Disclose moved odds when returned.
- Never say stake was entered or Place Bet was clicked. These actions are forbidden.
```

- [ ] **Step 5: Run OpenAPI tests and confirm pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stake_ui_bridge.py::test_gpt_schema_exposes_stake_ui_mlb_moneyline_review_slip_action tests\test_gpt_action.py::test_gpt_action_schema_stays_within_custom_gpt_operation_limit -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app\gpt_action.py custom_gpt_action\custom-gpt-instructions.md tests\test_gpt_action.py tests\test_stake_ui_bridge.py
git commit -m "Expose MLB moneyline review slip action"
```

---

### Task 7: Full Verification and Push

**Files:**
- Verify all changed files.

- [ ] **Step 1: Run compile check**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q app
```

Expected: no output and exit code 0.

- [ ] **Step 2: Run focused moneyline tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_mlb_moneylines.py tests\test_stake_sgm_browser.py tests\test_local_stake_helper.py tests\test_stake_ui_bridge.py tests\test_gpt_action.py -q
```

Expected: PASS. If the monolithic selection crashes under Windows asyncio cleanup, run per-file tests and record each file summary.

- [ ] **Step 3: Run full per-file regression**

Run:

```powershell
$ErrorActionPreference='Stop'; $files = Get-ChildItem tests -Filter 'test_*.py' | Sort-Object Name; foreach ($file in $files) { $output = & .\.venv\Scripts\python.exe -m pytest $file.FullName -q 2>&1; if ($LASTEXITCODE -ne 0) { $output | Write-Host; exit $LASTEXITCODE }; $summary = ($output | Select-String -Pattern 'passed|skipped').Line | Select-Object -Last 1; Write-Host "$($file.Name): $summary" }
```

Expected: every `test_*.py` file passes. A skipped test is acceptable only if it was already skipped by the suite.

- [ ] **Step 4: Verify OpenAPI operation count**

Run:

```powershell
@'
from app.gpt_action import build_gpt_action_openapi_schema
schema = build_gpt_action_openapi_schema("https://azp-test.example")
ops = [(path, method, op["operationId"]) for path, item in schema["paths"].items() for method, op in item.items()]
print(len(ops))
print([op for _, _, op in ops if op == "buildStakeUiMoneylineReviewSlip"])
'@ | .\.venv\Scripts\python.exe -
```

Expected:

```text
30
['buildStakeUiMoneylineReviewSlip']
```

- [ ] **Step 5: Commit final verification fixups if needed**

If any verification fix changes files, run:

```powershell
git add app tests custom_gpt_action
git commit -m "Stabilize MLB moneyline review slip tests"
```

Expected: commit created only if files changed.

- [ ] **Step 6: Push**

Run:

```powershell
git status --short --branch
git push origin main
```

Expected: branch `main` pushes cleanly to `origin/main`.

---

## Self-Review

- Spec coverage: The plan covers the one new action, local-helper job, exact `mlb_ml_` validation, moneyline-only sidebar preservation/blocking, append/resume behavior, bounded retry, odds movement reporting, individual moneyline removal through the existing remove action, GPT instruction update, OpenAPI operation count 30, and deployment verification.
- Placeholder scan: Clean. Each task lists exact files, exact tests, expected failure and pass states, and code shapes.
- Type consistency: The plan uses `rowId`, `fixtureSlug`, `team`, `odds`, `researchedOdds`, `clickedOdds`, `oddsMoved`, `remainingSelections`, and `buildStakeUiMoneylineReviewSlip` consistently across route, helper, OpenAPI, and tests.
