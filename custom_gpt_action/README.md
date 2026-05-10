# AZP Custom GPT Action Pack

This folder is the handoff package for connecting your existing Custom GPT to the AZP backend.

The GPT routes are data and ledger routes only. They can pull Stake-offered MLB props, enrich them with MLB Stats API context, validate GPT-selected props against the current board, and save what the GPT chose. They do not log in to Stake, place bets, scrape the account UI, or control a slip.

## What Codex Added

- `GET /gpt/openapi.json`
- `GET /gpt/health`
- `GET /gpt/mlb/matchup-picks`
- `GET /gpt/mlb/matchup-prop-board`
- `GET /gpt/mlb/player-context`
- `POST /gpt/mlb/validate-selections`
- `POST /gpt/mlb/gpt-decisions`
- `GET /gpt/mlb/settle-recommendations`
- `GET /gpt/mlb/performance-summary`

There are two separate workflows.

### AZP-picked workflow

Use this when you want the deterministic AZP engine to choose the picks:

```text
GET /gpt/mlb/matchup-picks?matchup=Blue%20Jays%20vs%20Angels&date=2026-05-08&markets=hits&side=over&legs=2&mode=sgp
```

It does this flow:

1. Pulls the live MLB player prop board from Stake.
2. Filters to the matchup you requested.
3. Enriches only those Stake-returned players with MLB Stats API history.
4. Scores over/under recommendations from available Stake props only.
5. Saves the exact recommendation response to the recommendation ledger.
6. Returns a candidate parlay with raw product odds and correlation warnings from the current engine.

### GPT-owned workflow

Use this when the Custom GPT should inspect the available board and make its own picks:

```text
GET /gpt/mlb/matchup-prop-board?matchup=Blue%20Jays%20vs%20Angels&date=2026-05-08&markets=hits,runs,strikeouts&side=under&limit=50
GET /gpt/mlb/player-context?matchup=Blue%20Jays%20vs%20Angels&date=2026-05-08&propId=PROP_ID_FROM_BOARD
POST /gpt/mlb/validate-selections
POST /gpt/mlb/gpt-decisions
```

It does this flow:

1. Pulls the current Stake-backed prop board for the matchup.
2. Returns side-level props with player, team, market, side, line, odds, fixture, prop id, and availability flags.
3. Lets the GPT request MLB context for only the players/markets it is considering.
4. Requires the GPT to validate its selected prop ids, sides, lines, and odds before answering.
5. Saves the GPT's final choice separately from AZP's own recommendations.

After games finish, `settle-recommendations` grades saved legs against MLB game logs.
`performance-summary` summarizes what has actually worked or failed by market, side,
confidence, risk flag, context tag, and diversity mode.

## What You Need To Do

### 1. Start AZP locally

From `C:\Users\farne\Desktop\AZP`:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Local test URLs:

```text
http://127.0.0.1:8000/gpt/health
http://127.0.0.1:8000/gpt/openapi.json
```

### 2. Expose it to ChatGPT

ChatGPT cannot call `127.0.0.1` on your PC directly. For local testing, start a temporary HTTPS tunnel.

If Cloudflare Tunnel is installed:

```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8000
```

Use the `https://...trycloudflare.com` URL it prints.

For the hosted Custom GPT setup, use the Render URL instead:

```text
https://azp-gpt-action.onrender.com/gpt/openapi.json
```

### 3. Add the Action in your Custom GPT

In your Custom GPT builder:

1. Open `Configure`.
2. Go to `Actions`.
3. Import from URL:

```text
https://YOUR-TUNNEL-URL/gpt/openapi.json
```

4. Authentication:
   - For easiest local testing, choose no authentication.
   - If you set `AZP_GPT_API_KEY` in your environment, choose API key auth and use header name:

```text
X-AZP-API-Key
```

### 4. Add the GPT Instructions

Copy the contents of:

```text
custom_gpt_action/custom-gpt-instructions.md
```

into your Custom GPT instructions.

The cleaned analyzer-layer guidance lives here:

```text
custom_gpt_action/analyzer-layer-notes.md
```

That file explains which parts of the imported edge/umpire/parlay notes are implemented, corrected, or intentionally deferred.

## Important Reality Check

If you keep this local, your PC must be running both:

- the AZP FastAPI server
- the tunnel

If either stops, the Custom GPT cannot reach AZP.

The current hosted shape is:

- Render runs the FastAPI action endpoint.
- Supabase stores the durable recommendation, GPT decision, and settlement ledgers.
- The Custom GPT imports `https://azp-gpt-action.onrender.com/gpt/openapi.json`.

Render still needs these secret environment variables set in the dashboard:

```text
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
```

Run `supabase/recommendation_ledger.sql` in Supabase SQL Editor before enabling
the Supabase ledger on Render.
