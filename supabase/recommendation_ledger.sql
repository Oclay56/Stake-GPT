create table if not exists public.recommendation_requests (
    request_id text primary key,
    captured_at timestamptz not null,
    source text,
    matchup text,
    slate_date date,
    timezone text,
    diversity_mode text,
    filters jsonb not null default '{}'::jsonb,
    request_params jsonb not null default '{}'::jsonb,
    diagnostics jsonb not null default '{}'::jsonb,
    concentration_tags jsonb not null default '[]'::jsonb,
    matched_fixture_count integer,
    available_prop_count integer,
    matched_prop_count integer,
    unmatched_prop_count integer,
    recommendation_count integer,
    parlay jsonb not null default '{}'::jsonb,
    notes jsonb not null default '[]'::jsonb,
    raw jsonb not null default '{}'::jsonb
);

create index if not exists idx_recommendation_requests_slate_date
    on public.recommendation_requests (slate_date, captured_at desc);

create index if not exists idx_recommendation_requests_diversity
    on public.recommendation_requests (diversity_mode, captured_at desc);

create table if not exists public.recommendation_legs (
    leg_id text primary key,
    request_id text not null references public.recommendation_requests(request_id) on delete cascade,
    captured_at timestamptz not null,
    slate_date date,
    matchup text,
    rank integer not null,
    prop_id text,
    fixture_slug text,
    game text,
    mlb_game_pk integer,
    player_name text,
    player_key text,
    player_mlb_id integer,
    team_name text,
    team_key text,
    team_mlb_id integer,
    market_key text,
    stat_key text,
    line numeric,
    side text,
    lean text,
    odds numeric,
    over_odds numeric,
    under_odds numeric,
    edge numeric,
    score integer,
    confidence text,
    selection text,
    diversity_mode text,
    risk_flags jsonb not null default '[]'::jsonb,
    reasons jsonb not null default '[]'::jsonb,
    contextual_tags jsonb not null default '[]'::jsonb,
    deferred_layers jsonb not null default '[]'::jsonb,
    concentration_tags jsonb not null default '[]'::jsonb,
    raw jsonb not null default '{}'::jsonb
);

create index if not exists idx_recommendation_legs_slate_date
    on public.recommendation_legs (slate_date, captured_at desc);

create index if not exists idx_recommendation_legs_market_side
    on public.recommendation_legs (market_key, side, captured_at desc);

create index if not exists idx_recommendation_legs_request
    on public.recommendation_legs (request_id, rank);

create table if not exists public.recommendation_settlements (
    settlement_id text primary key,
    request_id text not null references public.recommendation_requests(request_id) on delete cascade,
    leg_id text references public.recommendation_legs(leg_id) on delete cascade,
    leg_rank integer not null,
    prop_id text,
    slate_date date,
    market_key text,
    side text,
    actual_value numeric,
    actual_result text,
    over_outcome text,
    decision_outcome text,
    reasons jsonb not null default '[]'::jsonb,
    settled_at timestamptz not null,
    raw jsonb not null default '{}'::jsonb,
    unique (request_id, leg_rank)
);

create index if not exists idx_recommendation_settlements_slate_date
    on public.recommendation_settlements (slate_date, settled_at desc);

create index if not exists idx_recommendation_settlements_decision
    on public.recommendation_settlements (decision_outcome, settled_at desc);
