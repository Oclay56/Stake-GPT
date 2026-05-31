# MLB Moneyline Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one read-only `getStakeUiMlbMoneylines` GPT action that scans visible pregame Stake MLB main-winner rows, enriches each team with official MLB context, and returns compact research data without changing SGM click behavior.

**Architecture:** Add an isolated moneyline reader beside the existing Stake MLB index reader, route it through a new local-helper job type, and enrich its raw board with a focused `app/mlb_moneylines.py` module. Extend the official MLB Stats client and engine with one standings request and one schedule-range request so a full slate can be researched with bounded official API calls. Keep ranking authority in the Custom GPT and keep all click/build behavior out of this phase.

**Tech Stack:** Python 3, FastAPI, Playwright sync CDP helper, Supabase local UI job bridge, official MLB Stats API, pytest

---

## Scope Guardrails

- [ ] Add exactly one GPT-visible operation: `getStakeUiMlbMoneylines`.
- [ ] Keep the implementation read-only: no odds-button clicks, no Add Bet clicks, no stake entry, and no Place Bet clicks.
- [ ] Support only pregame `Winner (incl. Extra Innings)`.
- [ ] Skip live/in-play fixtures with warnings.
- [ ] Reuse MLB index discovery and Load More expansion, but do not modify SGM selection matching or review-slip builders.
- [ ] Return raw visible Stake rows plus official MLB research. Do not add backend ranking or final-pick logic.
- [ ] Keep the OpenAPI operation count at `29`, leaving one slot for a later isolated moneyline review-slip build action.

## Task 1: Extend Official MLB Data Support for Slate-Level Team Research

**Files:**
- Modify: `app/mlb_data/client.py`
- Modify: `app/mlb_data/engine.py`
- Modify: `tests/test_mlb_data.py`

- [ ] **Step 1: Add failing client path assertions**

Extend `test_mlb_stats_client_uses_official_endpoint_paths` in `tests/test_mlb_data.py`:

```py
await client.get_schedule_range("2026-04-15", "2026-05-08")
await client.get_standings(season=2026)

assert dict(seen_requests[7].url.params) == {
    "sportId": "1",
    "startDate": "2026-04-15",
    "endDate": "2026-05-08",
    "hydrate": "probablePitcher",
}
assert seen_requests[8].url.path == "/api/v1/standings"
assert dict(seen_requests[8].url.params) == {
    "leagueId": "103,104",
    "season": "2026",
    "standingsTypes": "regularSeason",
}
```

- [ ] **Step 2: Add failing engine normalization assertions**

Extend the fake client and `test_mlb_data_engine_normalizes_core_shapes`:

```py
recent = asyncio.run(engine.get_schedule_range("2026-04-15", "2026-05-08"))
standings = asyncio.run(engine.get_standings(season=2026))

assert recent["games"][0]["homeTeam"]["score"] == 4
assert recent["games"][0]["awayTeam"]["isWinner"] is False
assert standings["teamsById"][117] == {
    "mlbId": 117,
    "name": "Houston Astros",
    "key": "houston-astros",
    "wins": 20,
    "losses": 16,
    "pct": ".556",
}
```

- [ ] **Step 3: Run the focused tests and confirm failure**

Run:

```powershell
python -m pytest tests/test_mlb_data.py -q
```

Expected: FAIL because the client and engine do not yet expose schedule-range or standings methods, and normalized game teams do not retain score/winner state.

- [ ] **Step 4: Add the official MLB client methods**

Add to `app/mlb_data/client.py`:

```py
async def get_schedule_range(self, start_date: str, end_date: str) -> dict[str, Any]:
    return await self._get(
        "/schedule",
        params={
            "sportId": 1,
            "startDate": start_date,
            "endDate": end_date,
            "hydrate": "probablePitcher",
        },
    )

async def get_standings(self, season: int) -> dict[str, Any]:
    return await self._get(
        "/standings",
        params={
            "leagueId": "103,104",
            "season": season,
            "standingsTypes": "regularSeason",
        },
    )
```

- [ ] **Step 5: Add engine methods and normalization**

Add to `app/mlb_data/engine.py`:

```py
async def get_schedule_range(self, start_date: str, end_date: str) -> dict[str, Any]:
    payload = await self._client.get_schedule_range(start_date, end_date)
    games = [
        _normalize_game(game)
        for date_entry in payload.get("dates") or []
        for game in date_entry.get("games") or []
    ]
    return {
        "startDate": start_date,
        "endDate": end_date,
        "gameCount": len(games),
        "games": games,
    }

async def get_standings(self, season: int) -> dict[str, Any]:
    payload = await self._client.get_standings(season)
    teams = [
        _normalize_standing(team)
        for record in payload.get("records") or []
        for team in record.get("teamRecords") or []
    ]
    return {
        "season": season,
        "teamCount": len(teams),
        "teams": teams,
        "teamsById": {
            team["mlbId"]: team
            for team in teams
            if team.get("mlbId") is not None
        },
    }
```

Extend `_normalize_game_team`:

```py
return {
    "mlbId": team.get("id"),
    "name": name,
    "key": slug_key(name),
    "score": raw_side.get("score"),
    "isWinner": raw_side.get("isWinner"),
    "probablePitcher": _normalize_pitcher(pitcher),
}
```

Add:

```py
def _normalize_standing(raw_team: dict[str, Any]) -> dict[str, Any]:
    team = raw_team.get("team") or {}
    name = str(team.get("name") or "")
    return {
        "mlbId": team.get("id"),
        "name": name,
        "key": slug_key(name),
        "wins": raw_team.get("wins"),
        "losses": raw_team.get("losses"),
        "pct": raw_team.get("winningPercentage"),
    }
```

- [ ] **Step 6: Run focused tests**

Run:

```powershell
python -m pytest tests/test_mlb_data.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/mlb_data/client.py app/mlb_data/engine.py tests/test_mlb_data.py
git commit -m "Add official MLB team slate context sources"
```

## Task 2: Add a Pure Moneyline Team-Context Enrichment Module

**Files:**
- Create: `app/mlb_moneylines.py`
- Create: `tests/test_mlb_moneylines.py`

- [ ] **Step 1: Add failing enrichment tests**

Create `tests/test_mlb_moneylines.py` with a fake MLB engine that returns:

- two official teams,
- target-day schedule with probable pitchers,
- standings records,
- at least 15 completed recent games,
- one visible raw moneyline game.

Test the response shape:

```py
def test_enrich_mlb_moneyline_board_maps_team_context():
    result = asyncio.run(
        enrich_stake_ui_moneylines(
            _raw_board(),
            FakeMoneylineMLBEngine(),
            slate_date=date(2026, 5, 31),
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
```

Add focused tests:

- `test_enrich_mlb_moneyline_board_keeps_visible_row_with_partial_warning`: use a fake engine with only three completed games and assert that the visible row remains present with `partial_recent_sample`.
- `test_enrich_mlb_moneyline_board_warns_when_team_identity_is_unmatched`: use a visible Stake team absent from `get_teams`, then assert `teamContext is None` and `team_identity_unmatched`.
- `test_enrich_mlb_moneyline_board_filters_fixture_and_matchup_requests`: use two raw games and assert each supported filter reduces the result to one matching game.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```powershell
python -m pytest tests/test_mlb_moneylines.py -q
```

Expected: FAIL because `app/mlb_moneylines.py` does not exist.

- [ ] **Step 3: Implement bounded slate enrichment**

Create `app/mlb_moneylines.py` with these exports:

```py
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .mlb_props import slug_key


MONEYLINE_MARKET_KEY = "winner_including_extra_innings"
RECENT_LOOKBACK_DAYS = 60


async def enrich_stake_ui_moneylines(
    raw_board: dict[str, Any],
    mlb_engine: Any,
    *,
    slate_date: date,
    fixture_slugs: list[str] | None = None,
    matchups: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]
```

Use four bounded official MLB requests for the entire slate:

```py
season = slate_date.year
teams = await mlb_engine.get_teams(season=season)
schedule = await mlb_engine.get_schedule(slate_date.isoformat())
recent = await mlb_engine.get_schedule_range(
    (slate_date - timedelta(days=RECENT_LOOKBACK_DAYS)).isoformat(),
    (slate_date - timedelta(days=1)).isoformat(),
)
standings = await mlb_engine.get_standings(season=season)
```

Implement focused pure helpers named `_filter_raw_games`, `_team_index`, `_schedule_index`, `_recent_results_by_team`, `_team_result_for_game`, `_window_summary`, `_context_for_selection`, and `_same_matchup`.

Each team result should retain:

```py
{
    "gamePk": 824522,
    "date": "2026-05-30",
    "opponent": "Toronto Blue Jays",
    "isHome": True,
    "won": True,
    "runsScored": 5,
    "runsAllowed": 3,
}
```

Each window summary should return:

```py
{
    "gamesUsed": 5,
    "wins": 3,
    "losses": 2,
    "runsScored": 22,
    "runsAllowed": 17,
    "runDifferential": 5,
    "results": [],
}
```

Use only completed games with numeric scores. Keep visible Stake rows even when official context is partial, and attach warnings:

```py
"team_identity_unmatched"
"partial_recent_sample"
"probable_pitcher_unavailable"
"official_mlb_context_unavailable"
```

For `homeAwaySplit`, summarize recent completed games matching the team's home/away role in the target fixture and include:

```py
{"scope": "recent_completed_games", "gamesUsed": 7, "wins": 4, "losses": 3}
```

Do not claim that this is a season-long split.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m pytest tests/test_mlb_moneylines.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/mlb_moneylines.py tests/test_mlb_moneylines.py
git commit -m "Add read-only MLB moneyline team research"
```

## Task 3: Extract Read-Only Main-Winner Rows from the Stake MLB Index

**Files:**
- Modify: `app/stake_sgm_browser.py`
- Modify: `tests/test_stake_sgm_browser.py`

- [ ] **Step 1: Add failing pure-normalization tests**

Add imports and tests in `tests/test_stake_sgm_browser.py`:

```py
from app.stake_sgm_browser import (
    _normalize_mlb_moneyline_cards,
    make_mlb_moneyline_row_id,
)


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


def test_moneyline_row_id_is_stable_and_separate_from_sgm_ids():
    first = make_mlb_moneyline_row_id("123-yankees-blue-jays", "New York Yankees")
    second = make_mlb_moneyline_row_id("123-yankees-blue-jays", "New York Yankees")
    assert first == second
    assert first.startswith("mlb_ml_")
    assert not first.startswith("sgm_")
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```powershell
python -m pytest tests/test_stake_sgm_browser.py -q
```

Expected: FAIL because the moneyline normalization helpers do not exist.

- [ ] **Step 3: Add isolated normalization helpers**

Add to `app/stake_sgm_browser.py`:

```py
MONEYLINE_MARKET_LABEL = "Winner (incl. Extra Innings)"
MONEYLINE_MARKET_KEY = "winner_including_extra_innings"


def make_mlb_moneyline_row_id(fixture_slug: str, team: str) -> str:
    identity = "|".join([fixture_slug, MONEYLINE_MARKET_KEY, slug_key(team)])
    return f"mlb_ml_{sha1(identity.encode('utf-8')).hexdigest()[:16]}"


def _normalize_mlb_moneyline_cards(
    raw_cards: list[dict[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]
```

Normalization rules:

- Require a valid MLB fixture URL.
- Match the exact normalized label `Winner (incl. Extra Innings)`.
- Require two visible team outcomes with numeric decimal odds.
- Generate `mlb_ml_` row IDs independently from SGM IDs.
- Skip live/in-play cards and add `live_fixture_skipped`.
- Keep processing other cards after malformed cards and add `moneyline_card_not_normalized`.

- [ ] **Step 4: Add the Playwright read-only browser reader**

Add beside `read_stake_mlb_games`:

```py
def read_stake_mlb_moneylines(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    limit: int = 50,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found on the debug port.")

        page = _find_or_open_mlb_page(browser.contexts[0])
        warnings = _check_stake_page_access(page)
        expansion = _expand_mlb_game_list(page, limit=limit)
        raw_cards = _extract_mlb_moneyline_cards(page)
        normalized = _normalize_mlb_moneyline_cards(raw_cards, limit=limit)
        return {
            "source": "stake_ui_mlb_moneylines_raw",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "url": page.url,
            "returnedGames": len(normalized["games"]),
            "games": normalized["games"],
            "expansion": expansion,
            "warnings": warnings + normalized["warnings"],
        }
```

Add `_extract_mlb_moneyline_cards(page)` as one Playwright `page.evaluate` call. It should read visible MLB index cards and return raw values only:

```js
{
  href,
  text,
  statusText,
  markets: [
    {
      label,
      outcomes: [{ team, oddsText, disabled }]
    }
  ]
}
```

The evaluate script must not call `.click()`.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
python -m pytest tests/test_stake_sgm_browser.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/stake_sgm_browser.py tests/test_stake_sgm_browser.py
git commit -m "Read pregame MLB moneylines from Stake index"
```

## Task 4: Route the New Read-Only Job Through the Local Helper

**Files:**
- Modify: `app/local_ui_bridge.py`
- Modify: `app/local_stake_helper.py`
- Modify: `tests/test_local_stake_helper.py`
- Modify: `tests/test_stake_ui_bridge.py`

- [ ] **Step 1: Add a failing local-helper routing test**

Add to `tests/test_local_stake_helper.py`:

```py
def test_process_job_runs_mlb_moneyline_reader(monkeypatch):
    def fake_read_stake_mlb_moneylines(*, cdp_url: str, limit: int):
        assert cdp_url == "http://127.0.0.1:9222"
        assert limit == 40
        return {
            "source": "stake_ui_mlb_moneylines_raw",
            "games": [{"fixtureSlug": "123-yankees-blue-jays"}],
        }

    monkeypatch.setattr(
        local_stake_helper,
        "read_stake_mlb_moneylines",
        fake_read_stake_mlb_moneylines,
    )
    store = FakeJobStore()
    job = {
        "jobId": "job-moneylines",
        "jobType": "stake_ui_mlb_moneylines",
        "request": {"limit": 40},
    }

    asyncio.run(
        local_stake_helper.process_job(
            store,
            job,
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert not store.failed
    assert store.completed[0][1]["source"] == "stake_ui_mlb_moneylines_raw"
```

Also assert review mode claims the new read-only job:

```py
assert "stake_ui_mlb_moneylines" in local_stake_helper._job_types_for_mode("review")
```

Add a local bridge cache-key lookup test in `tests/test_stake_ui_bridge.py` that monkeypatches `SupabaseLocalUiJobStore._request` to return a recent completed row with:

```py
{
    "job_type": "stake_ui_mlb_moneylines",
    "status": "completed",
    "request_json": {"cacheKey": "mlb-moneylines:2026-05-31:50"},
}
```

Then assert:

```py
cached = asyncio.run(
    store.find_recent_completed_job(
        job_type="stake_ui_mlb_moneylines",
        cache_key="mlb-moneylines:2026-05-31:50",
        max_age_seconds=60,
    )
)
assert cached["jobType"] == "stake_ui_mlb_moneylines"
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```powershell
python -m pytest tests/test_local_stake_helper.py -q
```

Expected: FAIL because the new job constant and reader import do not exist.

- [ ] **Step 3: Add the job type and routing**

Add to `app/local_ui_bridge.py`:

```py
STAKE_MLB_MONEYLINES_JOB_TYPE = "stake_ui_mlb_moneylines"
```

Generalize `SupabaseLocalUiJobStore.find_recent_completed_job` without breaking existing fixture-bound callers:

```py
async def find_recent_completed_job(
    self,
    *,
    job_type: str,
    fixture_slug: str = "",
    cache_key: str = "",
    max_age_seconds: int,
    limit: int = 20,
) -> dict[str, Any] | None:
```

Change its early return and request filtering:

```py
if max_age_seconds <= 0 or not (fixture_slug or cache_key):
    return None

if fixture_slug and str(request.get("fixtureSlug") or "") != fixture_slug:
    continue
if cache_key and str(request.get("cacheKey") or "") != cache_key:
    continue
```

Existing SGM board cache callers continue passing `fixture_slug`. The moneyline route will pass `cache_key`.

Import it and `read_stake_mlb_moneylines` in `app/local_stake_helper.py`.

Add it to the fixture-optional job types and process branch:

```py
if job_type == STAKE_MLB_MONEYLINES_JOB_TYPE:
    limit = _clean_int(request.get("limit"), 50, minimum=1, maximum=100)
    result = await asyncio.to_thread(
        read_stake_mlb_moneylines,
        cdp_url=cdp_url,
        limit=limit,
    )
```

Add `STAKE_MLB_MONEYLINES_JOB_TYPE` to review, build, and all mode job lists. Add a job label:

```py
if job_type == STAKE_MLB_MONEYLINES_JOB_TYPE:
    return "Reading Stake MLB moneylines"
```

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m pytest tests/test_local_stake_helper.py tests/test_stake_ui_bridge.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/local_ui_bridge.py app/local_stake_helper.py tests/test_local_stake_helper.py tests/test_stake_ui_bridge.py
git commit -m "Route read-only MLB moneyline helper jobs"
```

## Task 5: Add the Combined FastAPI Moneyline Research Endpoint

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_stake_ui_bridge.py`

- [ ] **Step 1: Add a failing API route test**

Add a `FakeCompletedMlbMoneylinesJobStore` and `FakeMoneylineMLBEngine` to `tests/test_stake_ui_bridge.py`.

Test:

```py
def test_stake_ui_mlb_moneylines_route_returns_enriched_read_only_board():
    fake_store = FakeCompletedMlbMoneylinesJobStore()
    app.dependency_overrides[get_local_ui_job_store] = lambda: fake_store
    app.dependency_overrides[get_mlb_engine] = lambda: FakeMoneylineMLBEngine()

    with TestClient(app) as client:
        response = client.post(
            "/mlb/stake-ui/mlb-moneylines",
            json={
                "date": "2026-05-31",
                "fixtureSlugs": ["123-new-york-yankees-toronto-blue-jays"],
                "timeoutSeconds": 2,
                "limit": 50,
            },
        )

    result = response.json()
    created = fake_store.created_jobs[0]

    assert response.status_code == 200
    assert created["jobType"] == "stake_ui_mlb_moneylines"
    assert created["request"]["purpose"] == "stake_ui_mlb_moneyline_research"
    assert result["source"] == "stake_ui_mlb_moneylines"
    assert result["decisionOwner"] == "custom_gpt"
    assert result["builderRole"] == "read_only_moneyline_research_not_final_recommendation"
    assert result["games"][0]["selections"][0]["teamContext"]["mlbTeamId"] == 147
```

Add tests that:

- no filters returns the full visible slate,
- `fixtureSlugs` filters visible games,
- `matchups` filters visible games,
- a local helper timeout returns HTTP `504`,
- an MLB API failure returns a clear HTTP `502` detail without implying a pick.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```powershell
python -m pytest tests/test_stake_ui_bridge.py -q
```

Expected: FAIL because the route does not exist.

- [ ] **Step 3: Implement the endpoint**

Import:

```py
from .local_ui_bridge import STAKE_MLB_MONEYLINES_JOB_TYPE
from .mlb_moneylines import enrich_stake_ui_moneylines
```

Add beside `/mlb/stake-ui/mlb-games` with the same local-bridge disabled, timeout, failed-job, and bridge-error handling structure already used by that route:

```py
@app.post("/mlb/stake-ui/mlb-moneylines")
async def mlb_stake_ui_mlb_moneylines(
    payload: dict[str, Any] = Body(default_factory=dict),
    _: None = Depends(require_gpt_api_key),
    job_store: SupabaseLocalUiJobStore = Depends(get_local_ui_job_store),
    mlb_engine: MLBDataEngine = Depends(get_mlb_engine),
) -> Any:
```

Import `datetime` and `ZoneInfo`, then clean inputs with existing helpers:

```py
slate_date = _date_from_body(payload) or datetime.now(ZoneInfo(_timezone_name())).date()
timeout_seconds = _clean_int_from_body(
    payload, "timeoutSeconds", 45, minimum=1, maximum=90
)
limit = _clean_int_from_body(payload, "limit", 50, minimum=1, maximum=100)
max_cache_age_seconds = _clean_int_from_body(
    payload, "maxCacheAgeSeconds", 60, minimum=0, maximum=600
)
fixture_slugs = _string_list_from_body(payload, "fixtureSlugs", "fixture_slugs")
matchups = _string_list_from_body(payload, "matchups")
cache_key = f"mlb-moneylines:{slate_date.isoformat()}:{limit}"
```

Before creating a helper job, reuse a recent full-slate raw UI result when allowed:

```py
cached = await job_store.find_recent_completed_job(
    job_type=STAKE_MLB_MONEYLINES_JOB_TYPE,
    cache_key=cache_key,
    max_age_seconds=max_cache_age_seconds,
)
```

When no cache hit exists, create the helper job:

```py
request = {
    "requestedBy": "custom_gpt",
    "purpose": "stake_ui_mlb_moneyline_research",
    "limit": limit,
    "cacheKey": cache_key,
}
```

After helper completion:

```py
return await enrich_stake_ui_moneylines(
    completed.get("result") or {},
    mlb_engine,
    slate_date=slate_date,
    fixture_slugs=fixture_slugs,
    matchups=matchups,
    limit=limit,
)
```

Attach compact bridge metadata and `cacheHit` to the enriched response. Apply `fixtureSlugs` and `matchups` only during enrichment so the cached raw slate remains reusable for different research filters.

Wrap official MLB failures consistently:

```py
except MLBAPIError as exc:
    raise HTTPException(
        status_code=502,
        detail={
            "source": "mlb_stats_api",
            "message": exc.message,
        },
    ) from exc
```

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m pytest tests/test_stake_ui_bridge.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/main.py tests/test_stake_ui_bridge.py
git commit -m "Expose read-only MLB moneyline research API"
```

## Task 6: Add the GPT Action Without Spending the Future Click Slot

**Files:**
- Modify: `app/gpt_action.py`
- Modify: `tests/test_stake_ui_bridge.py`
- Modify: `tests/test_gpt_action.py`
- Modify: `custom_gpt_action/custom-gpt-instructions.md`

- [ ] **Step 1: Add failing OpenAPI tests**

Add to `tests/test_stake_ui_bridge.py`:

```py
def test_gpt_schema_exposes_stake_ui_mlb_moneylines_action():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")
    operation = schema["paths"]["/mlb/stake-ui/mlb-moneylines"]["post"]

    assert operation["operationId"] == "getStakeUiMlbMoneylines"
    properties = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "fixtureSlugs" in properties
    assert "matchups" in properties
    assert "date" in properties
```

Add to `tests/test_gpt_action.py`:

```py
def test_openapi_stays_under_custom_gpt_operation_cap():
    schema = build_gpt_action_openapi_schema("https://azp-test.example")
    operations = [
        operation
        for methods in schema["paths"].values()
        for operation in methods.values()
    ]
    assert len(operations) == 29
    assert len(operations) <= 30
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```powershell
python -m pytest tests/test_stake_ui_bridge.py tests/test_gpt_action.py -q
```

Expected: FAIL because the OpenAPI path is absent and the operation count remains `28`.

- [ ] **Step 3: Add the OpenAPI action**

Add to `build_gpt_action_openapi_schema` in `app/gpt_action.py`:

```py
"/mlb/stake-ui/mlb-moneylines": {
    "post": _operation(
        "getStakeUiMlbMoneylines",
        "Get Stake UI MLB moneyline research",
        (
            "Reads visible pregame Winner (incl. Extra Innings) rows from the "
            "Stake MLB index through the local helper and enriches each team "
            "with official MLB context. This is read-only support data, not a "
            "recommendation endpoint, and it never clicks Stake selections."
        ),
        request_body=_stake_ui_mlb_moneylines_request_body(),
    )
},
```

Add:

```py
def _stake_ui_mlb_moneylines_request_body() -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "format": "date"},
                        "fixtureSlugs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "matchups": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 90},
                        "maxCacheAgeSeconds": {"type": "integer", "minimum": 0, "maximum": 600},
                    },
                    "additionalProperties": True,
                }
            }
        },
    }
```

Do not add `buildStakeUiMoneylineReviewSlip` in this phase.

- [ ] **Step 4: Update Custom GPT instructions**

Add a concise moneyline workflow section to `custom_gpt_action/custom-gpt-instructions.md`:

```md
When the user asks for MLB moneyline or main-winner research:

1. Call `getStakeUiMlbMoneylines`.
2. Use only returned pregame `Winner (incl. Extra Innings)` rows.
3. Compare the returned official MLB team context: season record, last 5/10/15 completed results, runs scored and allowed, relevant home/away split, opponent, and probable pitcher.
4. Make the ranking yourself. The backend does not choose winners.
5. Disclose partial-data warnings plainly.
6. Do not claim the helper can click moneylines yet. Version one is read-only.
```

Also add a general rule:

```md
- Do not route moneyline requests through SGM actions. Use `getStakeUiMlbMoneylines` for visible pregame MLB main-winner rows.
```

- [ ] **Step 5: Run focused tests**

Run:

```powershell
python -m pytest tests/test_stake_ui_bridge.py tests/test_gpt_action.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/gpt_action.py tests/test_stake_ui_bridge.py tests/test_gpt_action.py custom_gpt_action/custom-gpt-instructions.md
git commit -m "Add GPT action for MLB moneyline research"
```

## Task 7: Run Regression Verification and Perform a Live Read-Only Smoke Test

**Files:**
- Verify only: `app/stake_sgm_browser.py`
- Verify only: `app/main.py`
- Verify only: `app/gpt_action.py`
- Verify only: `custom_gpt_action/custom-gpt-instructions.md`

- [ ] **Step 1: Run the full automated suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass, including existing SGM browser, review-slip, candidate-pool, GUI color, and schedule regressions.

- [ ] **Step 2: Confirm OpenAPI operation count**

Run:

```powershell
@'
from app.gpt_action import build_gpt_action_openapi_schema
schema = build_gpt_action_openapi_schema("https://azp-test.example")
operations = [
    (path, method, op["operationId"])
    for path, methods in schema["paths"].items()
    for method, op in methods.items()
]
print(len(operations))
for item in operations:
    print(item)
'@ | python -
```

Expected first line:

```text
29
```

- [ ] **Step 3: Start the local helper in review mode and run a live read-only request**

Prerequisites:

- Chrome debug session is already open through the existing helper setup.
- Desktop VPN/login state is valid if Stake requires it.
- Supabase local UI bridge environment variables are configured locally and on the API process.

Run the API and helper using the existing project launch flow, then call:

```powershell
$body = @{
  date = "2026-05-31"
  limit = 50
  timeoutSeconds = 45
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/mlb/stake-ui/mlb-moneylines" `
  -ContentType "application/json" `
  -Body $body
```

Verify manually:

- response contains only pregame `Winner (incl. Extra Innings)` games,
- every returned selection has visible Stake odds and an `mlb_ml_` row ID,
- official context appears where mapping succeeds,
- incomplete rows carry warnings instead of invented data,
- no Stake outcome is clicked,
- no SGM tab is opened,
- no sidebar slip is modified.

- [ ] **Step 4: Re-run the existing SGM regression subset**

Run:

```powershell
python -m pytest tests/test_stake_sgm_browser.py tests/test_local_stake_helper.py tests/test_stake_ui_bridge.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit any test-only adjustments if needed**

If the live Stake DOM requires a narrowly scoped extractor adjustment, update only `_extract_mlb_moneyline_cards` and its focused normalization tests:

```powershell
git add app/stake_sgm_browser.py tests/test_stake_sgm_browser.py
git commit -m "Adjust Stake MLB moneyline index extraction"
```

- [ ] **Step 6: Push**

```powershell
git push origin main
```

## Deployment and GPT Update

- [ ] Restart or redeploy Render after the pushed backend commit.
- [ ] Re-import the generated Custom GPT OpenAPI schema because this phase adds `getStakeUiMlbMoneylines`.
- [ ] Replace the Custom GPT knowledge instructions with the updated `custom_gpt_action/custom-gpt-instructions.md`.
- [ ] Keep the helper running in review mode for moneyline scans.
- [ ] Do not expect moneyline clicking until the later isolated `buildStakeUiMoneylineReviewSlip` phase is designed and live-tested.
