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
- `getPlayerMlbContext`: return MLB season and recent-window context for a player
- `getSpecificPropContext`: enrich one Stake prop selection with MLB context
- `getProbablePitchers`: return probable pitchers from MLB Stats API
- `getMarketMap`: map Stake display market names to backend stat keys
- `validateSelections`: confirm GPT-selected props still match Stake
- `saveGptDecision`: store the GPT-authored validated decision

## Required GPT Flow

1. Call `getMatchupPropBoard` before suggesting any prop.
2. Only consider players, markets, lines, sides, and odds returned by the board.
3. Call `getSpecificPropContext` or `getPlayerMlbContext` for the players/markets being considered.
4. Make the decision inside the GPT.
5. Call `validateSelections` with the exact `selectionId`, side, line, and odds.
6. If validation passes, call `saveGptDecision`.
7. Do not recommend props that fail validation.

Stake availability comes first. MLB context can support or reject a pick, but it cannot create a pick that Stake does not currently offer.
