# AZP Custom GPT Action

AZP is now a thin GPT data backend.

The Custom GPT makes the final decision. The Render backend only:

- pulls Stake-backed MLB props and matchups
- pulls official MLB schedule context for game discovery
- normalizes players, teams, markets, lines, sides, and odds
- pulls MLB Stats API context for players and props
- validates GPT-selected props against the current Stake board
- creates local-helper jobs for UI-verified Stake Same Game Multi boards
- returns decision profiles, market heatmaps, and constrained slip candidates for GPT review
- saves GPT-authored decisions and market mappings when storage is configured

It does not place bets, log in to Stake, scrape account pages, or run the old AZP analyzer as the final pick engine.

## Import URL

Use this in the Custom GPT Actions editor:

```text
https://YOUR-RENDER-SERVICE.onrender.com/gpt/openapi.json
```

Authentication can stay `None` unless `AZP_GPT_API_KEY` is set on Render. If that env var is set, configure the action to send `X-AZP-API-Key`.

## Instruction Files

Paste `regular-instruction-tab-8000.md` into the Custom GPT regular instruction tab. It is exactly 8,000 characters and is designed for the text box above the Knowledge files.

Use `custom-gpt-instructions.md` as the primary Custom GPT instruction file. It is intentionally lean and contains the always-on operating rules.

Attach or upload `custom-gpt-operational-reference.md` as the secondary reference file. It keeps the heavier glossary, probability engine, risk flags, playbooks, validation rules, lineup/opponent/game-context usage, and Stake SGM metadata guidance out of the main instruction stream while still preserving the full operating manual.

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
- `getStakeUiSgmBoard`: request the local helper to read the exact Stake Same Game Multi board through the user's Chrome/VPN session; every compact row includes a stable `rowId`, plus SGM metadata such as market catalog, bet factor, balance/push flags, and exact non-playable reasons when available
- `getStakeUiMlbGames`: request the local helper to read visible MLB fixture links from the actual Stake UI
- `readStakeUiState`: optional diagnostic action for failed/unclear UI helper states; reports page, fixture, SGM visibility, login/region/Cloudflare state, and sidebar state
- `clearStakeUiSgmSelections`: optional recovery action for clearing pending SGM working selections before a retry; it does not clear placed sidebar slip legs
- `clearStakeUiSidebar`: optional recovery action for clearing the entire visible right-sidebar review slip; use only when the user asks to wipe the whole slip
- `buildStakeUiReviewSlipBatch`: build multiple exact UI-backed SGM groups into one visible Stake review slip using one shared browser page; prefer passing `rowIds`
- `getPlayerMlbContext`: return MLB season and recent-window context for a player
- `getSpecificPropContext`: enrich one Stake prop selection with MLB context for the exact requested side, including lineup, opponent pitcher/team, player split, venue/weather, and game-status context when available
- `getPropContextBatch`: enrich up to 20 selected Stake props at once for finalist review, including lineup, opponent pitcher/team, player split, venue/weather, and game-status context when available
- `getProbablePitchers`: return probable pitchers from MLB Stats API
- `getMarketMap`: map Stake display market names to backend stat keys
- `validateSelections`: confirm GPT-selected props still match Stake, with strict odds/line validation options
- `saveGptDecision`: store the GPT-authored validated decision

## Required GPT Flow

1. Use `getMlbSchedule`, `mapMlbScheduleToStake`, or `getStakeUiMlbGames` when the user asks what games are available. Prefer `getStakeUiMlbGames` for multi-game Same Game Multi work.
2. For Same Game Multi requests, call `getStakeUiSgmBoard` before selecting finalists. If it is unavailable, do not pretend feed-only lines are final.
3. Call `getBoardSummary` first for broad non-SGM matchup requests.
4. Use `getPropPage` to page through specific markets/sides instead of requesting the full raw board.
5. Use `getComparisonBoard` for compact MLB helper metrics on filtered candidates.
6. Use `getPropContextBatch` or `getSpecificPropContext` for finalists.
7. Make the decision inside the GPT.
8. For target-odds or mega-parlay requests, call `buildSlipCandidates` before choosing finalists.
9. Call `validateSelections` with the exact `selectionId`, side, line, and odds. Use `validationMode: strict` unless you are only doing loose research.
10. If validation passes, call `saveGptDecision`.
11. For Same Game Multi review slips, pass the selected rows' `rowIds` to `buildStakeUiReviewSlip` or `buildStakeUiReviewSlipBatch`. Do not reconstruct the build request from player name, line, and odds when a `rowId` exists.
12. For multi-game Same Game Multi review slips, use `buildStakeUiReviewSlipBatch` once instead of separate one-game slip builds.
13. Use `readStakeUiState` or `clearStakeUiSgmSelections` only after a UI helper failure, unclear status, or stuck pending SGM selection. Use `clearStakeUiSidebar` only when the user explicitly asks to clear the whole visible slip. Do not spend action calls on diagnostics during a successful normal flow.
14. Do not recommend props that fail validation.

Stake availability comes first. MLB context can support or reject a pick, but it cannot create a pick that Stake does not currently offer. Feed validation is not the same as a final Stake bet-slip quote; if a line or price differs in the UI, the UI/quote wins.

The GPT should treat no-pick or fewer-pick outcomes as valid. If clean candidates cannot reach a requested target odds range, it should say that instead of forcing weak filler legs.
