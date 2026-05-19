# AZP Custom GPT Action

AZP is now a thin GPT data backend.

The Custom GPT makes the final decision. The Render backend only:

- pulls Stake-backed MLB props and matchups
- normalizes players, teams, markets, lines, sides, and odds
- pulls MLB Stats API context for players and props
- validates GPT-selected props against the current Stake board
- returns decision profiles, market heatmaps, and constrained slip candidates for GPT review
- saves GPT-authored decisions and market mappings when storage is configured
- creates pending local slip jobs for the AZP Local Bridge when the user asks to build a slip for manual Stake review

It does not place bets, log in to Stake, scrape account pages, or run the old AZP analyzer as the final pick engine.

## Local Stake GraphQL Reference

The repo keeps a small local-only helper in `app/stake_graphql_reference.py`.
It was extracted from the useful parts of the unofficial StakeAPI donor folder:

- browser-like GraphQL headers
- cURL token/session extraction
- read-only GraphQL request helper
- balance/profile/history query examples

It is not exposed through the hosted Render GPT action and it does not place bets.
Use it later as reference plumbing for a local slip builder where the visible Stake UI
still confirms the exact player, market, side, line, and odds before anything is clicked.

## Import URL

Use this in the Custom GPT Actions editor:

```text
https://YOUR-RENDER-SERVICE.onrender.com/gpt/openapi.json
```

Authentication can stay `None` unless `AZP_GPT_API_KEY` is set on Render. If that env var is set, configure the action to send `X-AZP-API-Key`.

## Main Actions

- `getMlbMatchups`: list Stake-backed MLB matchups for a date
- `getMlbSchedule`: list official MLB games for a date from MLB Stats API
- `mapMlbScheduleToStake`: map official MLB games to Stake fixtures when available
- `getAvailableMarkets`: discover markets available for a matchup
- `getMatchupPropBoard`: return line-specific Stake selections for a matchup
- `getBoardSummary`: return compact counts, market coverage, context coverage, and warning counts without raw prop dumps
- `getPropPage`: return a filtered/paginated page of compact Stake rows
- `getComparisonBoard`: return compact Stake rows with MLB helper metrics, multi-window evidence, decision profiles, and market heatmap data for comparison, not final picks
- `buildSlipCandidates`: assemble target-odds candidate slip shapes from comparison rows; GPT still owns the final recommendation
- `getPlayerMlbContext`: return MLB season and recent-window context for a player
- `getSpecificPropContext`: enrich one Stake prop selection with MLB context for the exact requested side
- `getPropContextBatch`: enrich up to 20 selected Stake props at once for finalist review
- `getProbablePitchers`: return probable pitchers from MLB Stats API
- `getMarketMap`: map Stake display market names to backend stat keys
- `validateSelections`: confirm GPT-selected props still match Stake, with strict odds/line validation options
- `saveGptDecision`: store the GPT-authored validated decision
- `createSlipJob`: save a validated slip as a pending local bridge job
- `getSlipJobStatus`: check whether the local bridge claimed or processed a slip job

## Required GPT Flow

1. Call `getBoardSummary` first for broad matchup requests.
2. Use `getPropPage` to page through specific markets/sides instead of requesting the full raw board.
3. Use `getComparisonBoard` for compact MLB helper metrics on filtered candidates.
4. Use `getPropContextBatch` or `getSpecificPropContext` for finalists.
5. Make the decision inside the GPT.
6. For target-odds or mega-parlay requests, call `buildSlipCandidates` before choosing finalists.
7. Call `validateSelections` with the exact `selectionId`, side, line, and odds. Use `validationMode: strict` unless you are only doing loose research.
8. If validation passes, call `saveGptDecision`.
9. If the user requested local slip building, call `createSlipJob`.
10. Do not recommend props that fail validation.

Stake availability comes first. MLB context can support or reject a pick, but it cannot create a pick that Stake does not currently offer. Feed validation is not the same as a final Stake bet-slip quote; if a line or price differs in the UI, the UI/quote wins.

The GPT should treat no-pick or fewer-pick outcomes as valid. If clean candidates cannot reach a requested target odds range, it should say that instead of forcing weak filler legs.

## Local AZP Bridge

The local bridge is the PC-side worker that turns a GPT slip job into a local review workflow.

Start it with:

```text
Start-AZP-Local-Bridge.bat
```

Stop it with:

```text
Stop-AZP-Local-Bridge.bat
```

The bridge is intentionally guarded:

- it watches Render/Supabase-backed slip-job endpoints
- it claims pending jobs
- it can audit or click exact visible Stake legs when configured
- it blocks ambiguous or missing UI matches instead of guessing
- it reports the bridge result back to the backend
- it does not enter wager amount
- it does not submit bets

For the AI-to-local handoff to work, the bridge must be running before or after the GPT creates the job. If it is off, pending jobs wait until the bridge starts.
