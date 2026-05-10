# AZP Custom GPT Action Pack

This folder is the handoff package for connecting your existing Custom GPT to the local AZP backend.

The backend routes added for the GPT are read-only. They can pull Stake-offered MLB props, enrich them with MLB Stats API context, and return recommendations. They do not log in to Stake, place bets, scrape the account UI, or control a slip.

## What Codex Added

- `GET /gpt/openapi.json`
- `GET /gpt/health`
- `GET /gpt/mlb/matchup-picks`
- `GET /gpt/mlb/settle-recommendations`
- `GET /gpt/mlb/performance-summary`

The important action is:

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
- Supabase stores the durable recommendation and settlement ledger.
- The Custom GPT imports `https://azp-gpt-action.onrender.com/gpt/openapi.json`.

Render still needs these secret environment variables set in the dashboard:

```text
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
```

Run `supabase/recommendation_ledger.sql` in Supabase SQL Editor before enabling
the Supabase ledger on Render.
