# MLB Moneyline Research Design

## Goal

Add a read-only MLB moneyline research workflow that scans the visible Stake MLB index, returns pregame main-winner moneyline selections, enriches each team with official MLB context, and lets the Custom GPT choose winners.

This first version does not click Stake selections, enter a stake amount, or click Place Bet.

## Scope

Version one supports only the visible pregame market:

```text
Winner (incl. Extra Innings)
```

Included:

- Full visible MLB slate scan by default.
- Optional filtering by fixture slugs or matchup names.
- Visible Stake team names and decimal odds.
- Stable moneyline row IDs for future review-slip clicking.
- Official MLB team context for GPT research.
- Explicit partial-data warnings.
- One new GPT action.

Excluded:

- Live or in-play games.
- First-five-innings moneylines.
- Alternate moneylines.
- Run lines and totals.
- SGM markets and SGM click logic.
- Ranking or recommendation authority in the backend.
- Review-slip clicking.
- Stake entry and Place Bet clicking.

## Product Boundary

The backend is a read-only research provider. It answers:

```text
Which pregame MLB main-winner moneylines are visibly offered by Stake, and what official MLB team context is available?
```

The Custom GPT remains responsible for comparing the returned evidence and choosing teams. The backend must not return final picks or imply probability certainty.

## Architecture

Add one GPT-visible action:

```text
getStakeUiMlbMoneylines
```

Internally, keep Stake UI extraction separate from MLB research:

```text
Stake MLB index
  -> expand Load More controls
  -> extract visible pregame Winner (incl. Extra Innings) cards
  -> normalize team selections and stable row IDs
  -> map Stake team names to official MLB team IDs
  -> enrich each matchup with official MLB team context
  -> return one compact read-only board
```

Do not route this through `app/stake_sgm_browser.py` SGM selection matching or SGM Add Bet functions. Shared MLB-index discovery helpers may be reused where appropriate, but moneyline parsing must have its own focused functions.

## API Shape

### Endpoint

```text
POST /mlb/stake-ui/mlb-moneylines
```

Operation ID:

```text
getStakeUiMlbMoneylines
```

### Request

```json
{
  "date": "2026-05-31",
  "fixtureSlugs": [],
  "matchups": [],
  "limit": 50,
  "timeoutSeconds": 45,
  "maxCacheAgeSeconds": 60
}
```

Behavior:

- With no fixture or matchup filters, scan the full visible MLB slate.
- If `fixtureSlugs` is present, return only matching visible fixtures.
- If `matchups` is present, normalize team names and return matching visible fixtures.
- Clamp `limit` to the existing MLB UI slate limits.
- Skip live games rather than returning them as selectable.

### Response

```json
{
  "source": "stake_ui_mlb_moneylines",
  "decisionOwner": "custom_gpt",
  "builderRole": "read_only_moneyline_research_not_final_recommendation",
  "market": "winner_including_extra_innings",
  "pregameOnly": true,
  "capturedAt": "2026-05-31T12:00:00Z",
  "games": [
    {
      "fixtureSlug": "123-new-york-yankees-toronto-blue-jays",
      "matchup": "New York Yankees vs Toronto Blue Jays",
      "status": "pregame",
      "marketLabel": "Winner (incl. Extra Innings)",
      "selections": [
        {
          "team": "New York Yankees",
          "odds": 1.72,
          "rowId": "mlb_ml_xxx",
          "teamContext": {
            "mlbTeamId": 147,
            "seasonRecord": {"wins": 0, "losses": 0},
            "last5": {},
            "last10": {},
            "last15": {},
            "runsScored": {},
            "runsAllowed": {},
            "homeAwaySplit": {},
            "probablePitcher": {}
          }
        }
      ],
      "warnings": []
    }
  ],
  "warnings": []
}
```

## Stable Row IDs

Generate deterministic read-only moneyline row IDs from the visible fixture and selected team:

```text
fixtureSlug + normalized market key + normalized team name
```

Use a distinct prefix:

```text
mlb_ml_
```

Do not reuse SGM `rowId` generation. These IDs are future click pointers for the dedicated moneyline review-slip workflow.

## Stake UI Extraction

The extractor must:

1. Open or reuse the Stake MLB index page through the existing Chrome/VPN helper.
2. Expand visible `Load More`, `Show More`, or equivalent controls using the existing MLB index expansion behavior.
3. Read visible MLB fixture cards.
4. Identify the exact main winner market label.
5. Extract both visible team outcome buttons and decimal odds.
6. Skip live or in-play cards.
7. Return explicit warnings for cards that were visible but could not be normalized.

The extractor must not:

- Navigate into SGM tabs.
- Search player markets.
- Click any outcome.
- Reuse SGM Add Bet logic.

## Official MLB Team Context

Add a focused MLB team-context builder backed by official MLB Stats API data.

Return, where available:

- MLB team ID and normalized team identity.
- Current season record.
- Last 5, last 10, and last 15 completed game results.
- Recent runs scored.
- Recent runs allowed.
- Home/away split relevant to the scheduled matchup.
- Opponent identity.
- Probable starting pitcher from the scheduled game.

If data is incomplete:

- Keep the visible Stake moneyline selection.
- Return the available context.
- Add a warning such as `team_identity_unmatched`, `partial_recent_sample`, or `probable_pitcher_unavailable`.
- Never fabricate missing values.

## Local Helper Job

Add one local-helper job type:

```text
stake_ui_mlb_moneylines
```

Flow:

```text
Render API
  -> create Supabase local UI job
  -> local helper reads Stake MLB index through Chrome/VPN session
  -> local helper returns visible moneyline rows
  -> Render enriches rows with official MLB team context
  -> API returns compact board
```

This follows the existing `stake_ui_mlb_games` job pattern.

## OpenAPI Budget

The current schema has 28 operations. Version one adds exactly one:

```text
getStakeUiMlbMoneylines
```

That leaves one operation slot for a later isolated click action:

```text
buildStakeUiMoneylineReviewSlip
```

Do not add a separate GPT-visible `getMlbTeamContext` operation in version one. Keep team enrichment internal to the combined endpoint.

## Error Handling

Return clear warnings or errors for:

- Stake MLB index unavailable.
- Region block, login requirement, or Cloudflare verification.
- Load More expansion incomplete.
- No visible pregame winner market.
- Live game skipped.
- Team identity unmatched.
- Partial recent sample.
- Probable pitcher unavailable.
- Official MLB API failure.

One fixture failure must not discard other valid fixtures from the slate.

## Testing

Add focused tests for:

- Extraction of two visible main-winner team buttons from one MLB fixture card.
- Pregame-only filtering.
- Live fixture exclusion.
- Stable `mlb_ml_` row IDs.
- Full-slate scan as the default.
- Optional fixture-slug filtering.
- Optional matchup filtering.
- Team-name-to-MLB-ID mapping.
- Last 5, last 10, and last 15 completed-game summaries.
- Season record.
- Runs scored and allowed.
- Home/away split.
- Probable pitcher inclusion.
- Partial-data warnings.
- Local-helper job routing.
- New API route.
- OpenAPI operation ID.
- OpenAPI operation count remains at or below 30.
- Regression: existing SGM tests continue passing.

## Future Phase

After the read-only endpoint passes live tests, design a second isolated action:

```text
buildStakeUiMoneylineReviewSlip
```

That action should accept exact `mlb_ml_` row IDs, click visible main-winner buttons, verify sidebar updates, and remain review-only.

Do not add future clicking until read-only extraction and team context are proven against the live Stake UI.

