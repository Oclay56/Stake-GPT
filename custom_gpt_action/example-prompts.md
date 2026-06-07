# Stake MLB Prompt Examples

These prompts are designed for a Custom GPT that can use Stake UI data, Stake odds data, MLB context, and review-slip helper tools.

Default flow: Stake UI/board truth first, MLB and current context second, validation/build third. Only use Stake-available props and markets. Preserve exact `rowId` values when the UI provides them.

## Market Discovery

1. List available markets for one game

```text
What markets and player props does Stake currently offer for Yankees vs Red Sox?
Use the exact Stake board first. Summarize key categories, available lines, odds, and any markets that look thin or unsupported.
```

2. Find one market family

```text
For Blue Jays vs Angels today, show me the Stake-available player props for hits unders.
Include current lines, odds, player/team, and whether each row has enough MLB context to research.
```

## Single-Game Props

3. Best under candidates

```text
For [Game], use the exact Stake board first.
Then compare recent 5/10/15-game context, season stats, probable pitchers, home/away context, and current lineup/weather notes if available.
Rank the best under candidates with odds, line, evidence summary, risk flags, and rowIds when available.
```

4. Merit-based player prop search

```text
For [Game], scan all available Stake player prop markets.
Do not preselect hits, strikeouts, total bases, or any other market unless the data points there.
For each finalist, choose the best player-market-side row based on evidence, implied probability, line value, and risk.
Output only researched Stake-backed candidates.
```

5. Player-specific best market

```text
For [Player] in [Game], use Stake's current board to compare every available prop market and side for that player.
Tell me which market is best supported today, which ones are weak, and why.
Include line, odds, implied probability, MLB context, and rowId or selectionId if available.
```

## SGM / Review Slips

6. Two-leg Same Game Multi

```text
Build a two-leg Same Game Multi for Braves vs Marlins.
Use the Stake UI SGM board first, then pull MLB context for likely finalists.
Run the finalist research gate before choosing.
Validate or disclose UI-only source of truth, then give me only UI-backed picks with rowIds, odds, reasoning, and risk notes.
```

7. Conservative under SGM

```text
Build a two-leg Same Game Multi for [Game], unders only.
Use the Stake UI SGM board first.
Pull MLB context, check probable pitchers, lineup/weather/park notes if current data is needed, and avoid Stake last-5-only reasoning.
Select the strongest researched legs and include rowIds.
```

8. Correlated SGM research

```text
For [Game], research a 2-3 leg SGM built around correlated game script.
Use only Stake UI-backed SGM rows.
Consider pitcher role, opposing offense, team hits/runs context, weather/park factors, and market concentration.
If the correlation is weak or unsupported, say so and suggest better alternatives.
```

## Multi-Game / Batch Slips

9. Multi-game review slip

```text
Use the Stake UI MLB game index.
Build one review slip containing:
- Two under SGM legs from Yankees vs Blue Jays
- Two under SGM legs from Nationals vs Mets

Prioritize exact Stake UI board data, use MLB/current context before choosing, then call the batch review-slip builder once using exact rowIds from each SGM board.
Report any failed, stale, or skipped rows.
```

10. Full-slate SGM scan

```text
Scan today's full MLB slate on the Stake UI.
Find 1-2 top Same Game Multi review-slip candidates focused on merit, not familiar markets.
Use all available markets unless a market is unsupported or unresearchable.
Show the selected legs, rowIds, odds, evidence, risk flags, and why skipped games were skipped.
```

## Value / Implied Probability

11. Value candidates

```text
Identify the highest-edge player props on Stake for today's MLB slate.
Use Stake odds to calculate implied probability, then compare against evidence-based estimated probability from MLB context.
Use live lookup if needed for lineups, scratches, pitcher changes, weather, roof status, or park factors.
Do not call something value if the edge is unknown.
```

12. Target odds with risk control

```text
Build a 2-4 leg researched slip with combined decimal odds around [Target Range].
Use all available markets unless I specify otherwise.
Prioritize evidence quality, implied probability versus estimated probability, row freshness, and low concentration risk.
If the target cannot be reached cleanly, give the best researched alternative instead of filler.
```

## Longshots

13. Best-effort longshot

```text
Build the best researched longshot parlay available for today's MLB slate.
I understand this is lottery-tier and unlikely to hit.
Do not block only because it is low-probability, but do not include stale, unresearched, unsupported, or invalid legs.
Use all available markets, run the finalist research gate, and label borderline legs clearly.
```

14. Aggressive SGM batch

```text
Build an aggressive multi-game SGM review slip for [Games or Slate].
Treat this as a high-variance longshot, not a clean/value slip.
Use exact Stake UI rows, compare all available markets, include only researched legs, and call the batch review-slip builder once if building.
```

## Troubleshooting

15. Missing market

```text
The Stake SGM board is not showing [Market] for [Game].
Check the exact UI board, explain whether the market is unavailable, hidden, unsupported, or not parsed.
Suggest the best researched alternatives from currently available markets.
```

16. Build failed or sidebar conflict

```text
The review-slip build failed or looks wrong.
Read the Stake UI state once, identify whether the issue is stale rowIds, unavailable markets, a sidebar conflict, login/region state, or pending SGM selections.
Do not clear anything unless I explicitly ask or the recovery action is necessary for retrying selected SGM rows.
```

## Quick Templates

```text
For [Game], find the best researched [over/under/any] props using all Stake-available markets. Output legs, odds, implied probability, MLB evidence, risk flags, and rowIds.
```

```text
For [Player] in [Game], compare every available Stake prop market and choose the best market-side row for that player today.
```

```text
Build a [safe/balanced/longshot] [2-4] leg SGM for [Game]. Use Stake UI first, MLB/current context second, finalist research gate third, and rowIds for any build.
```

```text
Scan [today's slate / these games] and build one batch review slip with [N] researched legs per game. Use all markets unless I restrict them.
```

## Notes

- Keep market restrictions request-scoped. A prompt asking for hits or unders should not carry that restriction into later broad requests.
- For current-day slates, ask the GPT to use live lookup when lineups, injuries, pitcher changes, weather, roof status, or park factors may matter.
- Use "evidence strength", "risk tier", or "edge status" instead of overconfident lock-style language.
- Update these examples when Stake adds new markets, cross-game features, or helper actions.
