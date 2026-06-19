create table if not exists public.charging_log_ranking_snapshot (
    snapshot_date date primary key,
    generated_at timestamp without time zone not null,
    source_relation text not null,
    source_row_count bigint not null,
    session_count integer not null,
    payload jsonb not null
);


create index if not exists idx_charging_log_ranking_snapshot_generated_at
    on public.charging_log_ranking_snapshot (generated_at desc);
