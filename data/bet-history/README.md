# Bet History Workspace

Use `imports/` as the local drop folder for exported historical bet files.

Supported source formats:
- CSV
- JSON
- JSONL
- TXT/raw pasted text (including Stake settled UI "prop list + actual stat" blocks and SGM pastes)

Odds should be decimal, matching Stake display format, for example `1.83`,
`2.10`, or `2.90`.

Recommended flow:

```powershell
historic data\bet-history\imports\your-file.csv --dry-run
historic data\bet-history\imports\your-file.csv
historic update
historic report
historic review
historic imports
historic analysis
analysis
analysis tickets
analysis signals
analysis calibration
```

Parsed rows are stored in the `bet_history_*` tables. In normal configured mode, Supabase is
the persistent source of truth and `data/gpt_action.sqlite` is a local cache/backup. In dev
or offline mode, SQLite can still run the full parser, enrichment, and analysis loop by itself.
Files placed in `imports/` are ignored by git so private bet history does not get committed.

The parser only normalizes canonical betting fields. Extra Stake UI text, promo
labels, icons, cashout text, and other layout noise are ignored unless they map
cleanly to a supported field.

Duplicate imports are skipped automatically (via content fingerprint). Use `--force` only when you
intentionally want to save another copy:

```powershell
historic data\bet-history\imports\your-file.csv --force
```

Deleting an import requires an explicit confirmation flag:

```powershell
historic delete-import <import-id> --yes
```

## Historic Analysis

- `historic report` - overall status of everything imported.
- `historic review` - rows that are blocked from automatic training, with reasons and repair policy.
- `historic update` - the normal automation flow: import new files, enrich missing MLB snapshots, then run updated analysis.
- `historic enrich --missing-only` - lower-level enrichment-only command for frozen MLB context snapshots.
- `historic storage` - explicit Supabase <-> SQLite sync. Use `historic storage pull` to hydrate local cache, `historic storage push` to upload local history, or `historic storage sync` for both.
- `historic analysis` / `analysis` / `z` - automated historic analysis flow across training-eligible legs, SGM tickets, signals, calibration, and one final outcome.
- `analysis legs` - leg-level hit rate + stake-aware ROI.
- `analysis tickets` - ticket-level SGM/multi results grouped by `ticketId`.
- `analysis signals` - market, player-market, side, line-bucket, and losing-ticket contributor signals.
- `analysis calibration` - historical hit-rate vs. break-even buckets for later model calibration.
- `historic analysis --import-id <id>` - run analysis against **only one** import/session (see `historic imports` for IDs). Use this to evaluate specific days, straight props vs. SGMs, etc. without mixing everything.
- `historic imports` - list every import with its ID, leg count, and source file.

Global analysis combines all imports (largest sample for training signal). Use the `--import-id` filter when you want isolated, session-specific accuracy.

Analysis filters:

```powershell
analysis --market singles
analysis --player "Max Muncy"
analysis --from-date 2026-06-01
analysis --ticket <ticket-id>
analysis tickets --market batter_walks
analysis calibration --market singles --side under
```

Filter behavior is shared across dashboard, legs, tickets, signals, and calibration views.

The default `z` command runs the full automated flow:

1. Load imported history from Supabase when configured, with SQLite as the local cache/fallback.
2. Run leg-level performance analysis.
3. Run ticket-level SGM/multi performance analysis.
4. Build market/player/side/line signals.
5. Build calibration buckets.
6. Print a final outcome with history quality, ticket sample, strongest/weakest markets, calibration status, model readiness, warnings, and next action.

The default `Historic` TUI action and `historic update` command run the full update flow:

1. Sync new files from `data/bet-history/imports`.
2. Skip duplicate imports and refresh duplicate metadata when needed.
3. Run `historic enrich --missing-only` against the same normalized history cache.
4. Store frozen MLB game snapshots and leg enrichment rows.
5. Push normalized imports/snapshots/enrichments to Supabase when configured.
6. Run updated analysis from the normalized history layer.

Use `historic update --from-date YYYY-MM-DD` to scope a recent slice, or
`historic update --enrich-limit 500` to control how many missing legs are enriched in one run.

## Market Coverage

The history parser uses the same MLB prop market normalization as the SGM candidate pool.
Supported player/pitcher prop families include hits, singles, total bases, home runs, RBI,
runs, hits + runs + RBIs, batter walks, batter strikeouts, stolen bases, pitcher strikeouts,
outs recorded, hits allowed, earned runs, and walks allowed.

Ambiguous markets are stored for review instead of guessed. For example, plain "Strikeouts"
without batter/pitcher context is held for review; "Batter Strikeouts" and "Pitcher
Strikeouts" are converted directly.

## Voids and Stale Live Labels

The system tries to be pragmatic instead of overly strict:

**Voids**
- Explicit "Void", "Cancelled", etc. are normalized to `resultStatus: "void"`.
- Voids **are training-eligible** (they are real historical outcomes - the prop simply didn't happen or was cancelled).
- In historic analysis: voids are excluded from graded hit rate (no decision was made) and from unit-ROI.
- When `stakeAmount` is known: voids contribute **0 profit** (stake returned) to realized P/L and `realizedRoi`.
- They appear in the W-L-P-V breakdown and have their own `roiExcludedVoid` counter.
- Bottom line: voids are kept and can inform the model ("this market voided a lot"), but they don't pretend to be wins or losses.

**Stale Live Labels**
- Stake sometimes leaves a "Live" label visible on settled/past bet cards.
- The importer treats "Live", inning text, and similar UI labels as non-authoritative display noise.
- These labels do **not** create review rows, do **not** block `training_eligible`, and do **not** enter the normalized training payload.
- Settlement still comes from explicit result text, void text, or actual-stat math against the line.

Recommendation: only import settled/past bet history. The parser assumes pasted history is not an intentional live-bet sample.

## Ticket Performance

Ticket-level performance groups SGM/multi legs by `ticketId`.

- A ticket loses if any graded leg loses.
- A ticket wins if at least one graded leg wins and no graded leg loses.
- Push/void-only tickets are excluded from ticket hit rate and ROI.
- Winning tickets with push/void legs are counted as wins, but ticket ROI is excluded unless adjusted payout odds are known.
- Losing-leg contributors are grouped by market, player-market, side, and line bucket.

This matters because per-leg results can look decent while correlated SGM tickets still fail.

## Calibration

Calibration is descriptive history, not machine learning.

The report compares historical hit rate against average break-even probability from decimal odds:

```text
historicalEdge = actualHitRate - averageBreakEvenRate
```

The recommended adjustment is capped between `-0.15` and `+0.08`, and buckets below 10 graded rows are marked `low_sample`.

## Builder Signal Integration

The SGM candidate pool dynamically reads imported historic results when scoring rows. In
production, it hydrates the local SQLite cache from Supabase first; in dev/offline mode it reads
the SQLite fallback directly.

- History is a soft signal, never a hard pick/reject rule.
- Low-sample history is shown but cannot move score.
- Usable buckets can add a small capped score adjustment.
- Missing per-leg odds limits the signal to hit-rate-only adjustment.
- Per-leg decimal odds unlock stronger value calibration against break-even probability.

Candidate rows expose:

```text
historicalSignal
historicalSignalStatus
historicalAppliedBucket
historicalHitRate
historicalSampleSize
historicalScoreAdjustment
historicalEnrichmentStatus
historicalEnrichmentCoverage
```

Default behavior is enabled. To debug without local history influence, call the SGM candidate pool with:

```json
{"useBetHistorySignals": false}
```

## Other Recent Improvements

- Stake-aware analysis: when your import has `stakeAmount` (and optionally `payoutAmount`), realized ROI and profit use the actual amounts risked instead of always assuming flat 1 unit.
- SGM / parlay awareness: pastes with "2 Leg Same Game Multi" + multiplier now group the legs under a shared `ticketId` and support ticket-level performance analysis.
- Parser + eligibility versioning + stable fingerprint deduping.
- Clearer analysis splits (hit-rate eligible vs. ROI eligible vs. missing-odds vs. push/void).
