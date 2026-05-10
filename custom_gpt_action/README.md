# AZP Custom GPT Action

AZP is now a thin GPT data backend.

The Custom GPT makes the final decision. The Render backend only:

- pulls Stake-backed MLB props and matchups
- normalizes players, teams, markets, lines, sides, and odds
- pulls MLB Stats API context for players and props
- validates GPT-selected props against the current Stake board
- saves GPT-authored decisions and market mappings when storage is configured

It does not place bets, log in to Stake, scrape account pages, or run the old AZP analyzer as the final pick engine.

## Import URL

Use this in the Custom GPT Actions editor:

```text
https://YOUR-RENDER-SERVICE.onrender.com/gpt/openapi.json
```

Authentication can stay `None` unless `AZP_GPT_API_KEY` is set on Render. If that env var is set, configure the action to send `X-AZP-API-Key`.

## Main Actions

- `getMlbMatchups`: list Stake-backed MLB matchups for a date
- `getAvailableMarkets`: discover markets available for a matchup
- `getMatchupPropBoard`: return line-specific Stake selections for a matchup
- `getBoardSummary`: return compact counts, market coverage, context coverage, and warning counts without raw prop dumps
- `getPropPage`: return a filtered/paginated page of compact Stake rows
- `getComparisonBoard`: return compact Stake rows with MLB helper metrics for comparison, not final picks
- `getPlayerMlbContext`: return MLB season and recent-window context for a player
- `getSpecificPropContext`: enrich one Stake prop selection with MLB context for the exact requested side
- `getPropContextBatch`: enrich up to 20 selected Stake props at once for finalist review
- `getProbablePitchers`: return probable pitchers from MLB Stats API
- `getMarketMap`: map Stake display market names to backend stat keys
- `validateSelections`: confirm GPT-selected props still match Stake, with strict odds/line validation options
- `saveGptDecision`: store the GPT-authored validated decision

## Required GPT Flow

1. Call `getBoardSummary` first for broad matchup requests.
2. Use `getPropPage` to page through specific markets/sides instead of requesting the full raw board.
3. Use `getComparisonBoard` for compact MLB helper metrics on filtered candidates.
4. Use `getPropContextBatch` or `getSpecificPropContext` for finalists.
5. Make the decision inside the GPT.
6. Call `validateSelections` with the exact `selectionId`, side, line, and odds. Use `validationMode: strict` unless you are only doing loose research.
7. If validation passes, call `saveGptDecision`.
8. Do not recommend props that fail validation.

Stake availability comes first. MLB context can support or reject a pick, but it cannot create a pick that Stake does not currently offer. Feed validation is not the same as a final Stake bet-slip quote; if a line or price differs in the UI, the UI/quote wins.
