-- Fallback variant for environments without CREATE MATERIALIZED VIEW privileges.
-- It keeps the same relation name and refresh function contract as the MV version.

create index if not exists idx_charging_log_processed_mv_position
    on public.charging_log_processed_mv (position);

create index if not exists idx_charging_log_processed_mv_session_sort
    on public.charging_log_processed_mv (
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        event_ts asc nulls last,
        raw_id asc
    );


do $$
begin
    if to_regclass('public.charging_log_sessions_mv') is null then
        return;
    end if;

    if exists (
        select 1
        from pg_catalog.pg_class c
        join pg_catalog.pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public'
          and c.relname = 'charging_log_sessions_mv'
          and c.relkind = 'm'
    ) then
        execute 'drop materialized view public.charging_log_sessions_mv';
    else
        execute 'drop table public.charging_log_sessions_mv';
    end if;
end;
$$;


create table public.charging_log_sessions_mv as
with source_rows as (
    select
        src.raw_id,
        src.device_name,
        src.source_csv_file,
        src.event_ts,
        src.charger_name,
        src.phone,
        src.position,
        src.project_number,
        src.software_version,
        src.scenario_hint,
        src.fod_object,
        src.card_position,
        src.sample_label,
        src.manual_result,
        src.defect_id,
        src.defect_comment,
        src.dual_charging_label,
        src.dual_charging_flag,
        src.hmi_status,
        src.eff,
        src.rx,
        src.tx,
        src.temperature,
        lower(coalesce(nullif(btrim(src.manual_result), ''), '')) as manual_result_normalized,
        lower(coalesce(nullif(btrim(src.defect_comment), ''), '')) as defect_comment_normalized,
        lower(coalesce(nullif(btrim(src.scenario_hint), ''), '')) as scenario_hint_normalized,
        nullif(btrim(src.defect_id), '') as defect_id_normalized,
        nullif(btrim(src.card_position), '') as card_position_normalized,
        nullif(btrim(src.fod_object), '') as fod_object_normalized,
        case
            when nullif(btrim(src.defect_id), '') is not null then true
            when lower(coalesce(nullif(btrim(src.manual_result), ''), '')) ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)' then true
            when lower(coalesce(nullif(btrim(src.defect_comment), ''), '')) ~ '(?:^|[^a-z])(not ok|no ok|nok|defect|failure|charging inter|no charging|toggling)(?:[^a-z]|$)' then true
            when nullif(btrim(src.hmi_status), '') in ('0', '1', '4', '5', '6', '8', '9', '13', '14', '17') then true
            when nullif(btrim(src.hmi_status), '') = '3' and (
                nullif(btrim(src.card_position), '') is not null
                or nullif(btrim(src.fod_object), '') is not null
                or lower(coalesce(nullif(btrim(src.scenario_hint), ''), '')) like '%rfid%'
                or lower(coalesce(nullif(btrim(src.scenario_hint), ''), '')) like '%fod%'
            ) then true
            when nullif(btrim(src.hmi_status), '') in ('15', '16') and (
                lower(coalesce(nullif(btrim(src.scenario_hint), ''), '')) not like '%rfid%'
                or lower(coalesce(nullif(btrim(src.manual_result), ''), '')) ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)'
                or lower(coalesce(nullif(btrim(src.defect_comment), ''), '')) ~ '(?:^|[^a-z])(not ok|no ok|nok|defect|failure|charging inter|no charging|toggling)(?:[^a-z]|$)'
                or nullif(btrim(src.defect_id), '') is not null
            ) then true
            else false
        end as analysis_candidate
    from public.charging_log_processed_mv src
),
ordered_events as (
    select
        src.*,
        lag(src.event_ts) over session_window as prev_event_ts,
        lag(coalesce(src.software_version, '')) over session_window as prev_software_version,
        lag(coalesce(src.sample_label, '')) over session_window as prev_sample_label,
        lag(src.scenario_hint_normalized) over session_window as prev_scenario_hint,
        lag(coalesce(src.card_position, '')) over session_window as prev_card_position,
        lag(coalesce(src.fod_object, '')) over session_window as prev_fod_object,
        lag(coalesce(src.dual_charging_flag::text, '')) over session_window as prev_dual_charging_flag
    from source_rows src
    where src.event_ts is not null
    window session_window as (
        partition by
            src.phone,
            src.project_number,
            src.device_name,
            src.charger_name,
            src.position
        order by src.event_ts, src.raw_id
    )
),
marked_sessions as (
    select
        ordered_events.*,
        case
            when prev_event_ts is null then 1
            when event_ts - prev_event_ts > interval '20 minutes' then 1
            when coalesce(software_version, '') is distinct from prev_software_version then 1
            when coalesce(sample_label, '') is distinct from prev_sample_label then 1
            when lower(coalesce(nullif(btrim(scenario_hint), ''), '')) is distinct from prev_scenario_hint then 1
            when coalesce(card_position, '') is distinct from prev_card_position then 1
            when coalesce(fod_object, '') is distinct from prev_fod_object then 1
            when coalesce(dual_charging_flag::text, '') is distinct from prev_dual_charging_flag then 1
            else 0
        end as session_boundary
    from ordered_events
),
numbered_sessions as (
    select
        marked_sessions.*,
        sum(session_boundary) over (
            partition by
                phone,
                project_number,
                device_name,
                charger_name,
                position
            order by event_ts, raw_id
            rows between unbounded preceding and current row
        ) as session_seq
    from marked_sessions
),
session_event_metrics as (
    select
        numbered_sessions.*,
        lag(numbered_sessions.eff) over session_window as session_prev_eff,
        lag(numbered_sessions.rx) over session_window as session_prev_rx,
        lag(numbered_sessions.tx) over session_window as session_prev_tx,
        extract(epoch from (
            numbered_sessions.event_ts - lag(numbered_sessions.event_ts) over session_window
        )) as gap_seconds
    from numbered_sessions
    window session_window as (
        partition by
            phone,
            project_number,
            device_name,
            charger_name,
            position,
            session_seq
        order by event_ts, raw_id
    )
),
session_gap_summary as (
    select
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        session_seq,
        percentile_cont(0.5) within group (order by gap_seconds)
            filter (where gap_seconds is not null and gap_seconds > 0) as median_gap_seconds
    from session_event_metrics
    group by
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        session_seq
),
session_events_enriched as (
    select
        metrics.*,
        greatest(300.0, coalesce(gap_summary.median_gap_seconds, 60.0) * 4.0) as interruption_threshold_seconds
    from session_event_metrics metrics
    left join session_gap_summary gap_summary
        on gap_summary.phone is not distinct from metrics.phone
       and gap_summary.project_number is not distinct from metrics.project_number
       and gap_summary.device_name is not distinct from metrics.device_name
       and gap_summary.charger_name is not distinct from metrics.charger_name
       and gap_summary.position is not distinct from metrics.position
       and gap_summary.session_seq = metrics.session_seq
)
select
    md5(
        concat_ws(
            '|',
            coalesce(phone, ''),
            coalesce(project_number, ''),
            coalesce(device_name, ''),
            coalesce(charger_name, ''),
            coalesce(position, ''),
            session_seq::text
        )
    ) as session_key,
    phone,
    project_number,
    device_name,
    charger_name,
    position,
    count(*)::integer as source_row_count,
    max(software_version) as software_version,
    max(source_csv_file) as source_csv_file,
    max(scenario_hint) as scenario_hint,
    max(fod_object) as fod_object,
    max(card_position) as card_position,
    max(sample_label) as sample_label,
    max(manual_result) as manual_result,
    max(defect_id) as defect_id,
    max(defect_comment) as defect_comment,
    max(dual_charging_label) as dual_charging_label,
    min(event_ts) as start_ts,
    max(event_ts) as end_ts,
    avg(eff) as avg_eff,
    avg(rx) as avg_rx,
    avg(tx) as avg_tx,
    max(temperature) as max_temperature,
    array_remove(array_agg(distinct hmi_status), null) as status_codes,
    (array_agg(hmi_status order by event_ts, raw_id) filter (where nullif(btrim(hmi_status), '') is not null))[1] as first_status,
    (array_agg(hmi_status order by event_ts desc, raw_id desc) filter (where nullif(btrim(hmi_status), '') is not null))[1] as last_status,
    count(*) filter (
        where gap_seconds is not null
          and gap_seconds > interruption_threshold_seconds
    )::integer as interruption_count,
    count(*) filter (
        where session_prev_eff is not null
          and eff is not null
          and session_prev_eff > 0
          and (session_prev_eff - eff) >= 15
          and ((session_prev_eff - eff) / session_prev_eff) >= 0.25
    )::integer as eff_drop_count,
    count(*) filter (
        where session_prev_rx is not null
          and rx is not null
          and session_prev_rx > 0
          and (session_prev_rx - rx) > 0
          and ((session_prev_rx - rx) / session_prev_rx) >= 0.20
    )::integer as rx_drop_count,
    count(*) filter (
        where session_prev_tx is not null
          and tx is not null
          and session_prev_tx > 0
          and (session_prev_tx - tx) > 0
          and ((session_prev_tx - tx) / session_prev_tx) >= 0.20
    )::integer as tx_drop_count
from session_events_enriched
group by
    phone,
    project_number,
    device_name,
    charger_name,
    position,
    session_seq
with no data;


create unique index if not exists idx_charging_log_sessions_mv_session_key
    on public.charging_log_sessions_mv (session_key);

create index if not exists idx_charging_log_sessions_mv_start_ts
    on public.charging_log_sessions_mv (start_ts desc nulls last);

create index if not exists idx_charging_log_sessions_mv_project_number
    on public.charging_log_sessions_mv (project_number);

create index if not exists idx_charging_log_sessions_mv_software_version
    on public.charging_log_sessions_mv (software_version);

create index if not exists idx_charging_log_sessions_mv_phone
    on public.charging_log_sessions_mv (phone);

create index if not exists idx_charging_log_sessions_mv_position
    on public.charging_log_sessions_mv (position);

create index if not exists idx_charging_log_sessions_mv_scope_start_ts
    on public.charging_log_sessions_mv (
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        start_ts desc nulls last
    );


create or replace function public.refresh_charging_log_sessions_mv()
returns void
language plpgsql
as $$
begin
    truncate table public.charging_log_sessions_mv;

    insert into public.charging_log_sessions_mv (
        session_key,
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        source_row_count,
        software_version,
        source_csv_file,
        scenario_hint,
        fod_object,
        card_position,
        sample_label,
        manual_result,
        defect_id,
        defect_comment,
        dual_charging_label,
        start_ts,
        end_ts,
        avg_eff,
        avg_rx,
        avg_tx,
        max_temperature,
        status_codes,
        first_status,
        last_status,
        interruption_count,
        eff_drop_count,
        rx_drop_count,
        tx_drop_count
    )
    with source_rows as (
        select
            src.raw_id,
            src.device_name,
            src.source_csv_file,
            src.event_ts,
            src.charger_name,
            src.phone,
            src.position,
            src.project_number,
            src.software_version,
            src.scenario_hint,
            src.fod_object,
            src.card_position,
            src.sample_label,
            src.manual_result,
            src.defect_id,
            src.defect_comment,
            src.dual_charging_label,
            src.dual_charging_flag,
            src.hmi_status,
            src.eff,
            src.rx,
            src.tx,
            src.temperature,
            lower(coalesce(nullif(btrim(src.manual_result), ''), '')) as manual_result_normalized,
            lower(coalesce(nullif(btrim(src.defect_comment), ''), '')) as defect_comment_normalized,
            lower(coalesce(nullif(btrim(src.scenario_hint), ''), '')) as scenario_hint_normalized,
            nullif(btrim(src.defect_id), '') as defect_id_normalized,
            nullif(btrim(src.card_position), '') as card_position_normalized,
            nullif(btrim(src.fod_object), '') as fod_object_normalized,
            case
                when nullif(btrim(src.defect_id), '') is not null then true
                when lower(coalesce(nullif(btrim(src.manual_result), ''), '')) ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)' then true
                when lower(coalesce(nullif(btrim(src.defect_comment), ''), '')) ~ '(?:^|[^a-z])(not ok|no ok|nok|defect|failure|charging inter|no charging|toggling)(?:[^a-z]|$)' then true
                when nullif(btrim(src.hmi_status), '') in ('0', '1', '4', '5', '6', '8', '9', '13', '14', '17') then true
                when nullif(btrim(src.hmi_status), '') = '3' and (
                    nullif(btrim(src.card_position), '') is not null
                    or nullif(btrim(src.fod_object), '') is not null
                    or lower(coalesce(nullif(btrim(src.scenario_hint), ''), '')) like '%rfid%'
                    or lower(coalesce(nullif(btrim(src.scenario_hint), ''), '')) like '%fod%'
                ) then true
                when nullif(btrim(src.hmi_status), '') in ('15', '16') and (
                    lower(coalesce(nullif(btrim(src.scenario_hint), ''), '')) not like '%rfid%'
                    or lower(coalesce(nullif(btrim(src.manual_result), ''), '')) ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)'
                    or lower(coalesce(nullif(btrim(src.defect_comment), ''), '')) ~ '(?:^|[^a-z])(not ok|no ok|nok|defect|failure|charging inter|no charging|toggling)(?:[^a-z]|$)'
                    or nullif(btrim(src.defect_id), '') is not null
                ) then true
                else false
            end as analysis_candidate
        from public.charging_log_processed_mv src
    ),
    ordered_events as (
        select
            src.*,
            lag(src.event_ts) over session_window as prev_event_ts,
            lag(coalesce(src.software_version, '')) over session_window as prev_software_version,
            lag(coalesce(src.sample_label, '')) over session_window as prev_sample_label,
            lag(src.scenario_hint_normalized) over session_window as prev_scenario_hint,
            lag(coalesce(src.card_position, '')) over session_window as prev_card_position,
            lag(coalesce(src.fod_object, '')) over session_window as prev_fod_object,
            lag(coalesce(src.dual_charging_flag::text, '')) over session_window as prev_dual_charging_flag
        from source_rows src
        where src.event_ts is not null
        window session_window as (
            partition by
                src.phone,
                src.project_number,
                src.device_name,
                src.charger_name,
                src.position
            order by src.event_ts, src.raw_id
        )
    ),
    marked_sessions as (
        select
            ordered_events.*,
            case
                when prev_event_ts is null then 1
                when event_ts - prev_event_ts > interval '20 minutes' then 1
                when coalesce(software_version, '') is distinct from prev_software_version then 1
                when coalesce(sample_label, '') is distinct from prev_sample_label then 1
                when lower(coalesce(nullif(btrim(scenario_hint), ''), '')) is distinct from prev_scenario_hint then 1
                when coalesce(card_position, '') is distinct from prev_card_position then 1
                when coalesce(fod_object, '') is distinct from prev_fod_object then 1
                when coalesce(dual_charging_flag::text, '') is distinct from prev_dual_charging_flag then 1
                else 0
            end as session_boundary
        from ordered_events
    ),
    numbered_sessions as (
        select
            marked_sessions.*,
            sum(session_boundary) over (
                partition by
                    phone,
                    project_number,
                    device_name,
                    charger_name,
                    position
                order by event_ts, raw_id
                rows between unbounded preceding and current row
            ) as session_seq
        from marked_sessions
    ),
    session_event_metrics as (
        select
            numbered_sessions.*,
            lag(numbered_sessions.eff) over session_window as session_prev_eff,
            lag(numbered_sessions.rx) over session_window as session_prev_rx,
            lag(numbered_sessions.tx) over session_window as session_prev_tx,
            extract(epoch from (
                numbered_sessions.event_ts - lag(numbered_sessions.event_ts) over session_window
            )) as gap_seconds
        from numbered_sessions
        window session_window as (
            partition by
                phone,
                project_number,
                device_name,
                charger_name,
                position,
                session_seq
            order by event_ts, raw_id
        )
    ),
    session_gap_summary as (
        select
            phone,
            project_number,
            device_name,
            charger_name,
            position,
            session_seq,
            percentile_cont(0.5) within group (order by gap_seconds)
                filter (where gap_seconds is not null and gap_seconds > 0) as median_gap_seconds
        from session_event_metrics
        group by
            phone,
            project_number,
            device_name,
            charger_name,
            position,
            session_seq
    ),
    session_events_enriched as (
        select
            metrics.*,
            greatest(300.0, coalesce(gap_summary.median_gap_seconds, 60.0) * 4.0) as interruption_threshold_seconds
        from session_event_metrics metrics
        left join session_gap_summary gap_summary
            on gap_summary.phone is not distinct from metrics.phone
           and gap_summary.project_number is not distinct from metrics.project_number
           and gap_summary.device_name is not distinct from metrics.device_name
           and gap_summary.charger_name is not distinct from metrics.charger_name
           and gap_summary.position is not distinct from metrics.position
           and gap_summary.session_seq = metrics.session_seq
    )
    select
        md5(
            concat_ws(
                '|',
                coalesce(phone, ''),
                coalesce(project_number, ''),
                coalesce(device_name, ''),
                coalesce(charger_name, ''),
                coalesce(position, ''),
                session_seq::text
            )
        ) as session_key,
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        count(*)::integer as source_row_count,
        max(software_version) as software_version,
        max(source_csv_file) as source_csv_file,
        max(scenario_hint) as scenario_hint,
        max(fod_object) as fod_object,
        max(card_position) as card_position,
        max(sample_label) as sample_label,
        max(manual_result) as manual_result,
        max(defect_id) as defect_id,
        max(defect_comment) as defect_comment,
        max(dual_charging_label) as dual_charging_label,
        min(event_ts) as start_ts,
        max(event_ts) as end_ts,
        avg(eff) as avg_eff,
        avg(rx) as avg_rx,
        avg(tx) as avg_tx,
        max(temperature) as max_temperature,
        array_remove(array_agg(distinct hmi_status), null) as status_codes,
        (array_agg(hmi_status order by event_ts, raw_id) filter (where nullif(btrim(hmi_status), '') is not null))[1] as first_status,
        (array_agg(hmi_status order by event_ts desc, raw_id desc) filter (where nullif(btrim(hmi_status), '') is not null))[1] as last_status,
        count(*) filter (
            where gap_seconds is not null
              and gap_seconds > interruption_threshold_seconds
        )::integer as interruption_count,
        count(*) filter (
            where session_prev_eff is not null
              and eff is not null
              and session_prev_eff > 0
              and (session_prev_eff - eff) >= 15
              and ((session_prev_eff - eff) / session_prev_eff) >= 0.25
        )::integer as eff_drop_count,
        count(*) filter (
            where session_prev_rx is not null
              and rx is not null
              and session_prev_rx > 0
              and (session_prev_rx - rx) > 0
              and ((session_prev_rx - rx) / session_prev_rx) >= 0.20
        )::integer as rx_drop_count,
        count(*) filter (
            where session_prev_tx is not null
              and tx is not null
              and session_prev_tx > 0
              and (session_prev_tx - tx) > 0
              and ((session_prev_tx - tx) / session_prev_tx) >= 0.20
        )::integer as tx_drop_count
    from session_events_enriched
    group by
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        session_seq;
end;
$$;


select public.refresh_charging_log_sessions_mv();
