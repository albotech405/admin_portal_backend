-- Admin Heatmap V3: per-cell cancellation count/rate and demand-trend metrics.
-- Run this AFTER sql/20260826_heatmap_v2_viewport_zoom.sql and sql/20260827_service_areas.sql.
--
-- *** MANUAL APPLICATION REQUIRED ***
-- AUTO_MIGRATE_ENABLED=false in production (Render cannot reach Supabase's
-- IPv6-only direct-connection hostname). This file will NOT be auto-applied
-- on deploy. Run manually in the Supabase SQL editor, like the three prior
-- post-incident migrations, until the Session Pooler/IPv6 fix is confirmed
-- and AUTO_MIGRATE_ENABLED is re-enabled.
--
-- Same signature as V2 -- strict no-op superset for every existing caller.
-- cancellation_count: ONLY when p_source = 'rides' (cancelled_at only exists
-- on rides) -- null (not 0) for source='requests'. prev_demand_count (for
-- trend): computed for BOTH sources (only needs created_at). Rate/trend-label
-- derivation happens in Python (_compute_cancellation_rate /
-- _compute_demand_trend), mirroring the existing _compute_imbalance pattern,
-- so that logic is unit-testable without a live database.

create or replace function public.get_admin_heatmap(
  p_admin_id uuid,
  p_days integer default 7,
  p_source text default 'requests',
  p_category text default null,
  p_vehicle_type text default null,
  p_grid_size numeric default 0.01,
  p_minutes integer default null,
  p_north numeric default null,
  p_south numeric default null,
  p_east numeric default null,
  p_west numeric default null
)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_window interval := case
    when p_minutes is not null then make_interval(mins => p_minutes)
    else make_interval(days => p_days)
  end;
  v_cutoff timestamptz := now() - v_window;
  v_prev_cutoff timestamptz := (now() - v_window) - v_window;
  v_cells jsonb;
  v_demand_skipped integer := 0;
  v_demand_total integer := 0;
  v_supply_total integer := 0;
begin
  perform public.assert_admin(p_admin_id);

  create temporary table tmp_demand_cells (grid_lat numeric, grid_lng numeric) on commit drop;
  create temporary table tmp_supply_cells (grid_lat numeric, grid_lng numeric) on commit drop;
  create temporary table tmp_cancel_cells (grid_lat numeric, grid_lng numeric) on commit drop;
  create temporary table tmp_prev_demand_cells (grid_lat numeric, grid_lng numeric) on commit drop;

  if p_source = 'rides' then
    insert into tmp_demand_cells (grid_lat, grid_lng)
    select round(v_lat / p_grid_size) * p_grid_size, round(v_lng / p_grid_size) * p_grid_size
    from (
      select
        (coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric as v_lat,
        (coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric as v_lng
      from public.rides
      where created_at >= v_cutoff
        and (p_category is null or category = p_category)
        and (p_vehicle_type is null or vehicle_type = p_vehicle_type)
        and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
        and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null
    ) pts
    where (p_south is null or v_lat >= p_south) and (p_north is null or v_lat <= p_north)
      and (p_west is null or v_lng >= p_west) and (p_east is null or v_lng <= p_east);

    select count(*) into v_demand_total from public.rides
    where created_at >= v_cutoff
      and (p_category is null or category = p_category)
      and (p_vehicle_type is null or vehicle_type = p_vehicle_type);

    insert into tmp_cancel_cells (grid_lat, grid_lng)
    select round(v_lat / p_grid_size) * p_grid_size, round(v_lng / p_grid_size) * p_grid_size
    from (
      select
        (coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric as v_lat,
        (coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric as v_lng
      from public.rides
      where status = 'cancelled'
        and cancelled_at >= v_cutoff
        and (p_category is null or category = p_category)
        and (p_vehicle_type is null or vehicle_type = p_vehicle_type)
        and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
        and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null
    ) pts
    where (p_south is null or v_lat >= p_south) and (p_north is null or v_lat <= p_north)
      and (p_west is null or v_lng >= p_west) and (p_east is null or v_lng <= p_east);

    insert into tmp_prev_demand_cells (grid_lat, grid_lng)
    select round(v_lat / p_grid_size) * p_grid_size, round(v_lng / p_grid_size) * p_grid_size
    from (
      select
        (coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric as v_lat,
        (coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric as v_lng
      from public.rides
      where created_at >= v_prev_cutoff and created_at < v_cutoff
        and (p_category is null or category = p_category)
        and (p_vehicle_type is null or vehicle_type = p_vehicle_type)
        and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
        and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null
    ) pts
    where (p_south is null or v_lat >= p_south) and (p_north is null or v_lat <= p_north)
      and (p_west is null or v_lng >= p_west) and (p_east is null or v_lng <= p_east);
  else
    insert into tmp_demand_cells (grid_lat, grid_lng)
    select round(v_lat / p_grid_size) * p_grid_size, round(v_lng / p_grid_size) * p_grid_size
    from (
      select
        (coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric as v_lat,
        (coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric as v_lng
      from public.ride_requests
      where created_at >= v_cutoff
        and (p_category is null or category = p_category)
        and (p_vehicle_type is null or vehicle_type = p_vehicle_type)
        and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
        and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null
    ) pts
    where (p_south is null or v_lat >= p_south) and (p_north is null or v_lat <= p_north)
      and (p_west is null or v_lng >= p_west) and (p_east is null or v_lng <= p_east);

    select count(*) into v_demand_total from public.ride_requests
    where created_at >= v_cutoff
      and (p_category is null or category = p_category)
      and (p_vehicle_type is null or vehicle_type = p_vehicle_type);

    insert into tmp_prev_demand_cells (grid_lat, grid_lng)
    select round(v_lat / p_grid_size) * p_grid_size, round(v_lng / p_grid_size) * p_grid_size
    from (
      select
        (coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric as v_lat,
        (coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric as v_lng
      from public.ride_requests
      where created_at >= v_prev_cutoff and created_at < v_cutoff
        and (p_category is null or category = p_category)
        and (p_vehicle_type is null or vehicle_type = p_vehicle_type)
        and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
        and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null
    ) pts
    where (p_south is null or v_lat >= p_south) and (p_north is null or v_lat <= p_north)
      and (p_west is null or v_lng >= p_west) and (p_east is null or v_lng <= p_east);
  end if;

  select v_demand_total - count(*) into v_demand_skipped from tmp_demand_cells;

  insert into tmp_supply_cells (grid_lat, grid_lng)
  select round(latitude::numeric / p_grid_size) * p_grid_size, round(longitude::numeric / p_grid_size) * p_grid_size
  from public.driver_profiles
  where is_online = true
    and latitude is not null and longitude is not null
    and (p_category is null or category = p_category)
    and (p_vehicle_type is null or vehicle_type = p_vehicle_type)
    and (p_south is null or latitude >= p_south) and (p_north is null or latitude <= p_north)
    and (p_west is null or longitude >= p_west) and (p_east is null or longitude <= p_east);

  select count(*) into v_supply_total from tmp_supply_cells;

  select coalesce(jsonb_agg(jsonb_build_object(
    'grid_lat', grid_lat, 'grid_lng', grid_lng,
    'demand_count', demand_count, 'supply_count', supply_count,
    'cancellation_count', cancellation_count,
    'prev_demand_count', prev_demand_count
  )), '[]'::jsonb)
  into v_cells
  from (
    select
      coalesce(d.grid_lat, s.grid_lat, c.grid_lat, p.grid_lat) as grid_lat,
      coalesce(d.grid_lng, s.grid_lng, c.grid_lng, p.grid_lng) as grid_lng,
      coalesce(d.demand_count, 0) as demand_count,
      coalesce(s.supply_count, 0) as supply_count,
      c.cancellation_count as cancellation_count,
      coalesce(p.prev_demand_count, 0) as prev_demand_count
    from
      (select grid_lat, grid_lng, count(*) as demand_count from tmp_demand_cells group by grid_lat, grid_lng) d
      full outer join
      (select grid_lat, grid_lng, count(*) as supply_count from tmp_supply_cells group by grid_lat, grid_lng) s
        on d.grid_lat = s.grid_lat and d.grid_lng = s.grid_lng
      full outer join
      (select grid_lat, grid_lng, count(*) as cancellation_count from tmp_cancel_cells group by grid_lat, grid_lng) c
        on coalesce(d.grid_lat, s.grid_lat) = c.grid_lat and coalesce(d.grid_lng, s.grid_lng) = c.grid_lng
      full outer join
      (select grid_lat, grid_lng, count(*) as prev_demand_count from tmp_prev_demand_cells group by grid_lat, grid_lng) p
        on coalesce(d.grid_lat, s.grid_lat, c.grid_lat) = p.grid_lat and coalesce(d.grid_lng, s.grid_lng, c.grid_lng) = p.grid_lng
  ) merged;

  return jsonb_build_object(
    'cells', v_cells,
    'demand_points_included', v_demand_total - v_demand_skipped,
    'demand_points_skipped', v_demand_skipped,
    'supply_points_included', v_supply_total
  );
end;
$$;

CREATE INDEX IF NOT EXISTS rides_cancelled_at_idx
    ON public.rides (cancelled_at)
    WHERE status = 'cancelled';

notify pgrst, 'reload schema';
