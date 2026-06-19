create or replace function public.charging_log_try_parse_jsonb(raw_value text)
returns jsonb
language plpgsql
immutable
as $$
begin
    if raw_value is null or btrim(raw_value) = '' then
        return null;
    end if;

    return raw_value::jsonb;
exception
    when others then
        return null;
end;
$$;


create or replace function public.charging_log_try_parse_float(raw_value text)
returns double precision
language sql
immutable
as $$
    with normalized as (
        select nullif(replace(btrim(raw_value), ',', '.'), '') as value_text
    ),
    matched as (
        select regexp_match(value_text, '(-?\d+(?:\.\d+)?)') as value_match
        from normalized
    )
    select case
        when value_match is null then null
        else value_match[1]::double precision
    end
    from matched;
$$;


create or replace function public.charging_log_extract_metric(payload jsonb, candidate_keys text[])
returns double precision
language plpgsql
immutable
as $$
declare
    object_key text;
    object_value jsonb;
    array_item jsonb;
    parsed_value double precision;
begin
    if payload is null then
        return null;
    end if;

    if jsonb_typeof(payload) = 'object' then
        for object_key, object_value in
            select key, value
            from jsonb_each(payload)
        loop
            if lower(object_key) = any(candidate_keys) then
                parsed_value := public.charging_log_try_parse_float(trim(both '"' from object_value::text));
                if parsed_value is not null then
                    return parsed_value;
                end if;
            end if;

            parsed_value := public.charging_log_extract_metric(object_value, candidate_keys);
            if parsed_value is not null then
                return parsed_value;
            end if;
        end loop;
    elsif jsonb_typeof(payload) = 'array' then
        for array_item in
            select value
            from jsonb_array_elements(payload)
        loop
            parsed_value := public.charging_log_extract_metric(array_item, candidate_keys);
            if parsed_value is not null then
                return parsed_value;
            end if;
        end loop;
    end if;

    return null;
end;
$$;


create or replace function public.charging_log_extract_text(payload jsonb, candidate_keys text[])
returns text
language plpgsql
immutable
as $$
declare
    object_key text;
    object_value jsonb;
    array_item jsonb;
    parsed_value text;
begin
    if payload is null then
        return null;
    end if;

    if jsonb_typeof(payload) = 'object' then
        for object_key, object_value in
            select key, value
            from jsonb_each(payload)
        loop
            if lower(object_key) = any(candidate_keys) then
                parsed_value := nullif(trim(both '"' from object_value::text), '');
                if parsed_value is not null then
                    return parsed_value;
                end if;
            end if;

            parsed_value := public.charging_log_extract_text(object_value, candidate_keys);
            if parsed_value is not null then
                return parsed_value;
            end if;
        end loop;
    elsif jsonb_typeof(payload) = 'array' then
        for array_item in
            select value
            from jsonb_array_elements(payload)
        loop
            parsed_value := public.charging_log_extract_text(array_item, candidate_keys);
            if parsed_value is not null then
                return parsed_value;
            end if;
        end loop;
    end if;

    return null;
end;
$$;


create or replace function public.charging_log_normalize_status(raw_value text)
returns text
language sql
immutable
as $$
    with normalized as (
        select nullif(btrim(raw_value), '') as value_text
    ),
    matched as (
        select regexp_match(value_text, '(\d+)') as value_match
        from normalized
    )
    select case
        when value_text is null then null
        when value_match is not null then value_match[1]
        else upper(value_text)
    end
    from normalized
    left join matched on true;
$$;


create or replace function public.charging_log_try_parse_bool(raw_value text)
returns boolean
language sql
immutable
as $$
    select case lower(nullif(btrim(raw_value), ''))
        when 'true' then true
        when 'yes' then true
        when 'y' then true
        when 'tak' then true
        when '1' then true
        when 'dual' then true
        when 'enabled' then true
        when 'false' then false
        when 'no' then false
        when 'n' then false
        when 'nie' then false
        when '0' then false
        when 'single' then false
        when 'disabled' then false
        else null
    end;
$$;


create or replace function public.charging_log_extract_efficiency(payload jsonb)
returns double precision
language plpgsql
immutable
as $$
declare
    direct_eff double precision;
    left_eff double precision;
    right_eff double precision;
begin
    direct_eff := public.charging_log_extract_metric(payload, array['eff', 'efficiency']);
    if direct_eff is not null then
        return direct_eff;
    end if;

    left_eff := public.charging_log_extract_metric(payload, array['eff_left_charger']);
    right_eff := public.charging_log_extract_metric(payload, array['eff_right_charger']);

    if left_eff is not null and right_eff is not null then
        return (left_eff + right_eff) / 2.0;
    end if;
    if left_eff is not null then
        return left_eff;
    end if;

    return right_eff;
end;
$$;


drop materialized view if exists public.charging_log_processed_mv;

create materialized view public.charging_log_processed_mv as
select
    raw.raw_id,
    raw.device_name,
    raw.source_csv_file,
    raw.source_seq,
    parsed.event_ts,
    raw.charger_name,
    raw.phone,
    raw.position,
    raw.project_number,
    raw.software_version,
    context.scenario_hint,
    context.fod_object,
    context.card_position,
    context.sample_label,
    context.manual_result,
    context.defect_id,
    context.defect_comment,
    context.dual_charging_label,
    context.dual_charging_flag,
    raw.row_json,
    metrics.hmi_status,
    parsed.inserted_at,
    metrics.eff,
    metrics.rx,
    metrics.tx,
    metrics.rpp,
    metrics.current_a,
    metrics.temperature,
    metrics.battery_level,
    metrics.voltage_v,
    case
        when parsed.event_ts is null or parsed.inserted_at is null then null
        else extract(epoch from (parsed.inserted_at - parsed.event_ts))
    end as ingest_delay_seconds,
    flags.analysis_candidate
from public.remote_csv_raw raw
cross join lateral (
    select
        nullif(raw.event_ts::text, '')::timestamp as event_ts,
        nullif(raw.inserted_at::text, '')::timestamp as inserted_at,
        public.charging_log_try_parse_jsonb(raw.row_json::text) as payload
) parsed
cross join lateral (
    select
        public.charging_log_normalize_status(
            public.charging_log_extract_text(parsed.payload, array['hmi_status'])
        ) as hmi_status,
        public.charging_log_extract_efficiency(parsed.payload) as eff,
        public.charging_log_extract_metric(parsed.payload, array['rx']) as rx,
        public.charging_log_extract_metric(parsed.payload, array['tx']) as tx,
        public.charging_log_extract_metric(parsed.payload, array['rpp', 'received_power', 'rx_power', 'power_rx']) as rpp,
        public.charging_log_extract_metric(parsed.payload, array['current (a)', 'current_a', 'coil_current', 'rx_current']) as current_a,
        public.charging_log_extract_metric(parsed.payload, array['temperature', 'temperature_c', 'temp', 'temp_c']) as temperature,
        public.charging_log_extract_metric(parsed.payload, array['battery_level', 'battery_percent', 'battery_pct', 'soc', 'soc_percent']) as battery_level,
        public.charging_log_extract_metric(parsed.payload, array['voltage', 'voltage (v)', 'voltage_v', 'battery_voltage', 'vbat']) as voltage_v
) metrics
cross join lateral (
    select
        public.charging_log_extract_text(parsed.payload, array['scenario', 'test_scenario', 'test scenario', 'sheet_name', 'sheet', 'worksheet', 'tab_name', 'test_type', 'test type']) as scenario_hint,
        public.charging_log_extract_text(parsed.payload, array['fod object', 'fod_object', 'foreign object', 'foreign_object', 'foreign item', 'fod item']) as fod_object,
        public.charging_log_extract_text(parsed.payload, array['card position', 'card_position', 'rfid card position', 'nfc card position', 'card location', 'card_location']) as card_position,
        public.charging_log_extract_text(parsed.payload, array['sample', 'sample id', 'sample_id', 'prototype', 'device sample']) as sample_label,
        public.charging_log_extract_text(parsed.payload, array['result', 'manual result', 'manual_result', 'test result', 'verdict']) as manual_result,
        public.charging_log_extract_text(parsed.payload, array['defect id', 'defect_id', 'defect', 'bug id', 'jira']) as defect_id,
        public.charging_log_extract_text(parsed.payload, array['defect comment', 'defect_comment', 'defect description', 'comment', 'remarks', 'note']) as defect_comment,
        public.charging_log_extract_text(parsed.payload, array['dual charging', 'dual_charging', 'dual charger', 'dual']) as dual_charging_label,
        public.charging_log_try_parse_bool(
            public.charging_log_extract_text(parsed.payload, array['dual charging', 'dual_charging', 'dual charger', 'dual'])
        ) as dual_charging_flag
) context
cross join lateral (
    select
        case
            when nullif(btrim(context.defect_id), '') is not null then true
            when lower(coalesce(context.manual_result, '')) ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)' then true
            when lower(coalesce(context.defect_comment, '')) ~ '(?:^|[^a-z])(not ok|no ok|nok|defect|failure|charging inter|no charging|toggling)(?:[^a-z]|$)' then true
            when metrics.hmi_status in ('0', '1', '4', '5', '6', '8', '9', '13', '14', '17') then true
            when metrics.hmi_status = '3' and (
                nullif(btrim(context.card_position), '') is not null
                or nullif(btrim(context.fod_object), '') is not null
                or lower(coalesce(context.scenario_hint, '')) like '%rfid%'
                or lower(coalesce(context.scenario_hint, '')) like '%fod%'
                or lower(coalesce(context.scenario_hint, '')) like '%foreign object%'
            ) then true
            when metrics.hmi_status in ('15', '16') and (
                nullif(btrim(context.defect_id), '') is not null
                or lower(coalesce(context.manual_result, '')) ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)'
                or lower(coalesce(context.defect_comment, '')) ~ '(?:^|[^a-z])(not ok|no ok|nok|defect|failure|charging inter|no charging|toggling)(?:[^a-z]|$)'
                or nullif(btrim(context.fod_object), '') is not null
                or (
                    lower(coalesce(context.scenario_hint, '')) like '%fod%'
                    or lower(coalesce(context.scenario_hint, '')) like '%foreign object%'
                    or (
                        nullif(btrim(context.card_position), '') is null
                        and lower(coalesce(context.scenario_hint, '')) not like '%rfid%'
                    )
                )
            ) then true
            else false
        end as analysis_candidate
) flags;


create index if not exists idx_charging_log_processed_mv_event_ts
    on public.charging_log_processed_mv (event_ts desc nulls last);

create index if not exists idx_charging_log_processed_mv_inserted_at
    on public.charging_log_processed_mv (inserted_at desc nulls last);

create index if not exists idx_charging_log_processed_mv_phone
    on public.charging_log_processed_mv (phone);

create index if not exists idx_charging_log_processed_mv_project_number
    on public.charging_log_processed_mv (project_number);

create index if not exists idx_charging_log_processed_mv_software_version
    on public.charging_log_processed_mv (software_version);

create index if not exists idx_charging_log_processed_mv_scope_event_ts
    on public.charging_log_processed_mv (
        phone,
        project_number,
        software_version,
        event_ts desc nulls last,
        raw_id desc
    );


create index if not exists idx_charging_log_processed_mv_analysis_scope_event_ts
    on public.charging_log_processed_mv (
        project_number,
        software_version,
        phone,
        position,
        event_ts desc nulls last,
        raw_id desc
    )
    where analysis_candidate;


create index if not exists idx_charging_log_processed_mv_analysis_group_event_ts
    on public.charging_log_processed_mv (
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        software_version,
        source_csv_file,
        event_ts desc nulls last,
        raw_id desc
    )
    where analysis_candidate;


create or replace function public.refresh_charging_log_processed_mv()
returns void
language plpgsql
as $$
begin
    refresh materialized view public.charging_log_processed_mv;
end;
$$;
