# MLB Moneyline Review Slip Design

## Goal

Add an isolated review-only builder for visible MLB pregame main-winner moneylines. The builder accepts exact `mlb_ml_` row IDs returned by `getStakeUiMlbMoneylines`, preserves already-added requested moneylines, appends missing teams, skips failures after a bounded retry, and reports any remaining selections for an optional targeted retry.

The builder must never enter a stake amount or click Place Bet.

## Scope

Version one supports only:

```text
Winner (incl. Extra Innings)
```

Included:

- Multiple MLB moneyline teams in one review-slip action.
- Exact `mlb_ml_` row IDs from the read-only moneyline board.
- Moneyline-only sidebar validation.
- Preserve-and-append resume behavior.
- Skip already-present requested moneylines.
- One bounded retry per failed selection.
- Odds movement reporting without blocking the same team selection.
- Partial success with explicit remaining selections.
- Reuse of the existing whole-sidebar clear action after explicit user approval.
- Focused removal of one visible moneyline leg.
- One new GPT-visible action.

Excluded:

- SGM rows and SGM Add Bet behavior.
- Player props, run lines, totals, first-five markets, and alternate moneylines.
- Other sports.
- Mixed moneyline plus SGM slips.
- Mixed MLB moneyline plus unknown standard-selection slips.
- Automatic sidebar clearing.
- Infinite retries.
- Stake entry.
- Place Bet clicking.

## Product Boundary

Stake does not allow SGM/custom-bet groups to mix with ordinary single-leg selections. Version one also intentionally avoids classifying or preserving arbitrary standard-selection markets.

The builder owns an MLB-moneyline-only visible slip:

- If the sidebar is empty, proceed.
- If it contains only recognized MLB moneylines, preserve them and append requested missing teams.
- If it contains SGM groups, player props, other standard selections, or unrecognized content, block and explain that the sidebar must be cleared before building a moneyline slip.
- Never clear the sidebar automatically.

The existing `clearStakeUiSidebar` action remains the explicit whole-slip reset path. It must only be called when the user asks to clear or approves a fresh rebuild.

## Architecture

Add one GPT-visible action:

```text
buildStakeUiMoneylineReviewSlip
```

Endpoint:

```text
POST /mlb/stake-ui/moneyline-review-slip
```

Internal flow:

```text
GPT-selected mlb_ml_ rows
  -> Render creates local-helper job
  -> helper opens or reuses Stake MLB index
  -> helper expands Load More controls
  -> helper reads sidebar mode
  -> block if sidebar is not MLB-moneyline-only
  -> skip requested teams already present
  -> click each missing main-winner team
  -> verify sidebar addition
  -> on failure: rescan DOM, then reload MLB index once and retry
  -> continue through remaining teams
  -> return added, existing, skipped, odds movement, and remaining selections
```

Do not route this through SGM board extraction, SGM row matching, SGM working selections, or SGM Add Bet logic.

## API Shape

### Endpoint

```text
POST /mlb/stake-ui/moneyline-review-slip
```

Operation ID:

```text
buildStakeUiMoneylineReviewSlip
```

### Request

```json
{
  "reviewOnly": true,
  "selections": [
    {
      "rowId": "mlb_ml_xxx",
      "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
      "team": "New York Yankees",
      "odds": 1.72
    }
  ],
  "timeoutSeconds": 90
}
```

Rules:

- `reviewOnly` is required and must be `true`.
- `selections` contains between 1 and 30 requested teams.
- Each selection requires exact `rowId`, `fixtureSlug`, `team`, and researched `odds`.
- Only `mlb_ml_` row IDs are accepted.
- Duplicate team/fixture requests are deduplicated.
- The researched odds value is retained for movement reporting, but odds movement does not block the same visible team outcome.

### Response

```json
{
  "source": "stake_ui_mlb_moneyline_review_slip",
  "status": "built_for_review",
  "reviewOnly": true,
  "requestedSelections": 5,
  "addedSelections": [
    {
      "rowId": "mlb_ml_xxx",
      "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
      "team": "New York Yankees",
      "researchedOdds": 1.72,
      "clickedOdds": 1.68,
      "oddsMoved": true
    }
  ],
  "alreadyPresentSelections": [],
  "remainingSelections": [],
  "warnings": ["odds_moved"],
  "safety": {
    "enteredStakeAmount": false,
    "clickedPlaceBet": false
  }
}
```

Partial result:

```json
{
  "status": "partial_review_slip",
  "addedSelections": [],
  "alreadyPresentSelections": [],
  "remainingSelections": [
    {
      "rowId": "mlb_ml_xxx",
      "fixtureSlug": "456-los-angeles-dodgers-san-diego-padres",
      "team": "Los Angeles Dodgers",
      "researchedOdds": 1.81,
      "reason": "visible_moneyline_selection_not_found_after_retry"
    }
  ],
  "safety": {
    "enteredStakeAmount": false,
    "clickedPlaceBet": false
  }
}
```

Blocked result:

```json
{
  "status": "blocked_sidebar_not_moneyline_only",
  "message": "Clear the visible sidebar before building an MLB moneyline-only review slip.",
  "sidebar": {
    "containsSgmGroup": true,
    "containsUnknownSelections": false
  },
  "safety": {
    "enteredStakeAmount": false,
    "clickedPlaceBet": false
  }
}
```

## Stable Row Identity

The read-only moneyline board already returns deterministic row IDs:

```text
fixtureSlug + winner_including_extra_innings + normalized team name
```

Prefix:

```text
mlb_ml_
```

The builder must accept the exact row ID and verify that it matches the requested fixture and team. It may re-read the current visible odds and click the same team at updated odds.

Odds movement is a diagnostic:

```json
{
  "researchedOdds": 1.72,
  "clickedOdds": 1.68,
  "oddsMoved": true
}
```

It is not a blocker unless the visible outcome no longer matches the requested fixture and team.

## Sidebar State

Add a dedicated moneyline sidebar reader that distinguishes:

```text
empty
moneyline_only
blocked_mixed_or_unknown
```

For recognized MLB moneyline legs, return:

```json
{
  "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
  "team": "New York Yankees",
  "market": "Winner (incl. Extra Innings)",
  "odds": 1.68
}
```

Detection must be conservative:

- Recognized MLB moneyline legs may be preserved.
- SGM/custom-bet groups block the builder.
- Unknown or unsupported standard selections block the builder.
- The helper must not infer that an unknown row is safe to preserve.

## Resume and Retry Behavior

The builder is append-oriented:

1. Inspect the visible sidebar.
2. Block if it is not empty or MLB-moneyline-only.
3. Treat requested teams already present in the sidebar as successful existing legs.
4. For each missing requested selection:
   - Reuse or open the MLB index.
   - Expand visible Load More controls.
   - Find the exact fixture and team in `Winner (incl. Extra Innings)`.
   - Click the visible team outcome.
   - Verify that the sidebar now contains the requested team moneyline.
5. If the selection is not found or does not appear in the sidebar:
   - Rescan the current DOM once.
   - If still missing, reload the MLB index once.
   - Expand Load More again.
   - Retry the exact fixture/team once.
6. If it still fails:
   - Add it to `remainingSelections`.
   - Continue processing later teams.

The helper must not:

- clear successful additions,
- restart the full slip from scratch,
- retry indefinitely,
- silently omit failed teams.

The GPT may offer a targeted retry using only returned `remainingSelections`.

## Individual Removal

Extend individual sidebar removal so a specific visible MLB moneyline can be removed using:

```json
{
  "rowId": "mlb_ml_xxx",
  "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
  "team": "New York Yankees"
}
```

Do not add another GPT-visible operation. Extend the existing `removeStakeUiSidebarGroup` request schema and helper behavior so it can remove either:

- an SGM sidebar group by fixture or matchup, or
- one recognized MLB moneyline leg by `mlb_ml_` row ID plus team identity.

## Local Helper Job

Add one local-helper job type:

```text
stake_ui_mlb_moneyline_build_slip
```

The existing review helper mode must claim it because the action remains review-only.

Flow:

```text
Render API
  -> create Supabase local UI job
  -> local helper builds moneyline-only visible review slip
  -> local helper returns compact result
  -> Render returns result to GPT
```

## OpenAPI Budget

The current schema has 29 operations. Version one adds exactly one:

```text
buildStakeUiMoneylineReviewSlip
```

That reaches the Custom GPT maximum:

```text
30 operations
```

Do not add another GPT-visible operation for targeted retries or moneyline removal. Targeted retries reuse the same builder action. Individual removal extends the existing sidebar-removal action.

## Error Handling

Return clear statuses for:

- `blocked_sidebar_not_moneyline_only`
- `blocked_invalid_row_id`
- `blocked_missing_selection_identity`
- `partial_review_slip`
- `built_for_review`
- `already_built_for_review`

Per-selection failure reasons include:

- `visible_moneyline_selection_not_found`
- `visible_moneyline_selection_not_found_after_retry`
- `sidebar_not_updated_after_click`
- `sidebar_not_updated_after_retry`
- `fixture_not_visible`
- `moneyline_market_not_visible`

Warnings include:

- `odds_moved`
- `selection_already_present`
- `sidebar_preserved`

One failed team must not discard successfully added teams.

## GPT Workflow

When the user asks to build an MLB moneyline review slip:

1. Call `getStakeUiMlbMoneylines`.
2. Compare official MLB team context and choose teams.
3. Call `buildStakeUiMoneylineReviewSlip` once with selected exact `mlb_ml_` row IDs.
4. If the builder returns `partial_review_slip`, report the added legs and failed legs plainly.
5. Ask whether the user wants a targeted retry for `remainingSelections`.
6. If approved, call `buildStakeUiMoneylineReviewSlip` again with only remaining rows.
7. If the sidebar is blocked, explain the conflict and ask whether the user wants to clear it.
8. Only call `clearStakeUiSidebar` after explicit approval.
9. Never imply that the helper entered stake or clicked Place Bet.

## Testing

Add focused tests for:

- Multiple requested moneyline teams.
- Exact `mlb_ml_` row ID validation.
- Moneyline-only sidebar preservation.
- Requested already-present team skip.
- Unknown sidebar selection block.
- SGM sidebar group block.
- Empty sidebar acceptance.
- Odds movement acceptance and reporting.
- Current-DOM rescan retry.
- One MLB-index reload retry.
- Continue-after-failure behavior.
- Partial result with remaining selections.
- Successful result with no remaining selections.
- Existing whole-sidebar clear behavior remains unchanged.
- Individual MLB moneyline removal through the existing sidebar removal action.
- Local-helper routing.
- New API route.
- OpenAPI operation ID.
- OpenAPI operation count equals `30`.
- Existing SGM tests continue passing.

## Deployment

After implementation:

1. Push the backend and helper changes.
2. Restart or redeploy Render.
3. Re-import the Custom GPT OpenAPI schema because one action is added.
4. Replace the Custom GPT knowledge instructions with the updated markdown file.
5. Test with a small two-team moneyline review slip before attempting a larger slate.
