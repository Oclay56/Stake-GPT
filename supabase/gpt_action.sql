create table if not exists public.gpt_decision_requests (
    decision_id text primary key,
    captured_at timestamptz not null,
    source text not null default 'custom_gpt',
    matchup text,
    slate_date date,
    prompt text,
    request_json jsonb not null,
    response_json jsonb not null,
    validation_json jsonb not null,
    metadata_json jsonb not null default '{}'::jsonb
);

alter table public.gpt_decision_requests
    add column if not exists request_json jsonb;

alter table public.gpt_decision_requests
    add column if not exists response_json jsonb;

alter table public.gpt_decision_requests
    add column if not exists validation_json jsonb;

alter table public.gpt_decision_requests
    add column if not exists metadata_json jsonb not null default '{}'::jsonb;

update public.gpt_decision_requests
set
    request_json = coalesce(request_json, '{}'::jsonb),
    response_json = coalesce(response_json, '{}'::jsonb),
    validation_json = coalesce(validation_json, '{}'::jsonb)
where request_json is null
   or response_json is null
   or validation_json is null;

alter table public.gpt_decision_requests
    alter column request_json set not null,
    alter column response_json set not null,
    alter column validation_json set not null;

create table if not exists public.gpt_decision_legs (
    leg_id text primary key,
    decision_id text not null references public.gpt_decision_requests(decision_id) on delete cascade,
    rank integer not null,
    captured_at timestamptz not null,
    slate_date date,
    matchup text,
    selection_id text,
    prop_id text,
    fixture_slug text,
    player_name text,
    team_name text,
    market_key text,
    market_name text,
    side text,
    line numeric,
    odds numeric,
    playable boolean not null default false,
    status text,
    selection_json jsonb not null,
    decision_profile_json jsonb not null default '{}'::jsonb,
    risk_flags_json jsonb not null default '[]'::jsonb,
    settlement_status text not null default 'unsettled',
    actual_stat numeric,
    settled_at timestamptz,
    settlement_confidence numeric,
    settlement_source text
);

alter table public.gpt_decision_legs
    add column if not exists decision_profile_json jsonb not null default '{}'::jsonb;

alter table public.gpt_decision_legs
    add column if not exists risk_flags_json jsonb not null default '[]'::jsonb;

alter table public.gpt_decision_legs
    add column if not exists settlement_status text not null default 'unsettled';

alter table public.gpt_decision_legs
    add column if not exists actual_stat numeric;

alter table public.gpt_decision_legs
    add column if not exists settled_at timestamptz;

alter table public.gpt_decision_legs
    add column if not exists settlement_confidence numeric;

alter table public.gpt_decision_legs
    add column if not exists settlement_source text;

create table if not exists public.market_mappings (
    sport text not null default 'mlb',
    stake_display_name text not null,
    internal_market_key text not null,
    stat_key text,
    group_name text,
    last_seen_at timestamptz not null,
    active boolean not null default true,
    examples jsonb not null default '[]'::jsonb,
    primary key (sport, stake_display_name, internal_market_key)
);

create index if not exists gpt_decision_requests_slate_date_idx
    on public.gpt_decision_requests (slate_date);

create index if not exists gpt_decision_legs_slate_date_idx
    on public.gpt_decision_legs (slate_date);

create index if not exists gpt_decision_legs_market_idx
    on public.gpt_decision_legs (market_key, side);

create index if not exists market_mappings_active_idx
    on public.market_mappings (sport, active);

create table if not exists public.local_ui_jobs (
    job_id text primary key,
    job_type text not null,
    status text not null default 'pending',
    request_json jsonb not null,
    result_json jsonb,
    error_message text,
    worker_id text,
    created_at timestamptz not null default now(),
    claimed_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz not null default now(),
    expires_at timestamptz
);

alter table public.local_ui_jobs
    add column if not exists result_json jsonb;

alter table public.local_ui_jobs
    add column if not exists error_message text;

alter table public.local_ui_jobs
    add column if not exists worker_id text;

alter table public.local_ui_jobs
    add column if not exists claimed_at timestamptz;

alter table public.local_ui_jobs
    add column if not exists completed_at timestamptz;

alter table public.local_ui_jobs
    add column if not exists updated_at timestamptz not null default now();

alter table public.local_ui_jobs
    add column if not exists expires_at timestamptz;

create index if not exists local_ui_jobs_pending_idx
    on public.local_ui_jobs (job_type, status, created_at);

create index if not exists local_ui_jobs_expires_idx
    on public.local_ui_jobs (expires_at);

create table if not exists public.bet_history_imports (
    import_id text primary key,
    imported_at timestamptz not null,
    source_path text,
    source_format text not null,
    source_fingerprint text,
    fingerprint_version text,
    parser_version text,
    eligibility_version text,
    raw_row_count integer not null default 0,
    parsed_leg_count integer not null default 0,
    needs_review_count integer not null default 0,
    report_json jsonb not null default '{}'::jsonb
);

alter table public.bet_history_imports enable row level security;

alter table public.bet_history_imports
    add column if not exists source_fingerprint text;

alter table public.bet_history_imports
    add column if not exists fingerprint_version text;

alter table public.bet_history_imports
    add column if not exists parser_version text;

alter table public.bet_history_imports
    add column if not exists eligibility_version text;

create table if not exists public.bet_history_raw (
    raw_id text primary key,
    import_id text not null references public.bet_history_imports(import_id) on delete cascade,
    source_row_number integer not null,
    source_format text not null,
    raw_text text,
    raw_json jsonb not null,
    parse_status text not null default 'parsed',
    parse_notes_json jsonb not null default '[]'::jsonb
);

alter table public.bet_history_raw enable row level security;

create table if not exists public.bet_history_legs (
    history_leg_id text primary key,
    import_id text not null references public.bet_history_imports(import_id) on delete cascade,
    raw_id text references public.bet_history_raw(raw_id) on delete set null,
    ticket_id text,
    leg_index integer not null,
    bet_date date,
    settled_date date,
    sport text not null default 'mlb',
    league text,
    player_name text,
    team_name text,
    opponent_name text,
    fixture_slug text,
    matchup text,
    market_key text,
    market_name text,
    side text,
    line numeric,
    odds numeric,
    stake_amount numeric,
    payout_amount numeric,
    result_status text,
    actual_stat numeric,
    parse_confidence numeric not null default 0,
    parse_confidence_label text not null default 'low',
    needs_review boolean not null default true,
    training_eligible boolean not null default false,
    parser_version text,
    eligibility_version text,
    parse_notes_json jsonb not null default '[]'::jsonb,
    ignored_fields_json jsonb not null default '[]'::jsonb,
    normalized_json jsonb not null,
    raw_json jsonb not null,
    created_at timestamptz not null
);

alter table public.bet_history_legs enable row level security;

alter table public.bet_history_legs
    add column if not exists training_eligible boolean not null default false;

alter table public.bet_history_legs
    add column if not exists parser_version text;

alter table public.bet_history_legs
    add column if not exists eligibility_version text;

alter table public.bet_history_legs
    add column if not exists ignored_fields_json jsonb not null default '[]'::jsonb;

create index if not exists bet_history_legs_market_idx
    on public.bet_history_legs (market_key, side);

create index if not exists bet_history_legs_date_idx
    on public.bet_history_legs (bet_date);

create index if not exists bet_history_legs_review_idx
    on public.bet_history_legs (needs_review, parse_confidence_label);

create index if not exists bet_history_legs_ticket_idx
    on public.bet_history_legs (ticket_id);

create index if not exists bet_history_legs_player_idx
    on public.bet_history_legs (player_name);

create index if not exists bet_history_imports_fingerprint_idx
    on public.bet_history_imports (source_fingerprint);

create table if not exists public.bet_history_game_snapshots (
    game_pk bigint primary key,
    official_date date,
    game_date timestamptz,
    matchup_key text,
    away_team_name text,
    home_team_name text,
    final_status text,
    venue_json jsonb not null default '{}'::jsonb,
    probable_pitchers_json jsonb not null default '{}'::jsonb,
    pregame_context_json jsonb not null default '{}'::jsonb,
    grading_context_json jsonb not null default '{}'::jsonb,
    raw_context_json jsonb not null default '{}'::jsonb,
    source text not null default 'mlb_stats_api',
    fetched_at timestamptz not null
);

alter table public.bet_history_game_snapshots enable row level security;

create index if not exists bet_history_game_snapshots_date_idx
    on public.bet_history_game_snapshots (official_date);

create table if not exists public.bet_history_leg_enrichments (
    history_leg_id text primary key references public.bet_history_legs(history_leg_id) on delete cascade,
    game_pk bigint not null references public.bet_history_game_snapshots(game_pk),
    player_mlb_id bigint,
    player_team_side text,
    player_team_name text,
    lineup_confirmed boolean not null default false,
    confirmed_starter boolean not null default false,
    batting_order integer,
    bat_side text,
    pitch_hand text,
    position text,
    stat_key text,
    stat_value numeric,
    enriched_result_status text,
    context_quality text not null default 'unknown',
    pregame_context_json jsonb not null default '{}'::jsonb,
    grading_context_json jsonb not null default '{}'::jsonb,
    notes_json jsonb not null default '[]'::jsonb,
    source text not null default 'mlb_stats_api_snapshot',
    enriched_at timestamptz not null
);

alter table public.bet_history_leg_enrichments enable row level security;

create index if not exists bet_history_leg_enrichments_game_idx
    on public.bet_history_leg_enrichments (game_pk);

notify pgrst, 'reload schema';
