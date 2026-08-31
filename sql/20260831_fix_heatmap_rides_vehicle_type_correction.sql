-- Correction to sql/20260831_fix_heatmap_ride_requests_vehicle_type.sql, which
-- got this backwards. Verified against the actual production schema (supplied
-- directly by the user, not inferred from code patterns this time):
--
--   rides:         NO vehicle_type column at all (only category + vehicle_snapshot jsonb)
--   ride_requests: vehicle_type character varying NOT NULL  -- DOES exist
--   driver_profiles: vehicle_type character varying          -- DOES exist (unaffected either way)
--
-- The prior migration removed the vehicle_type filter from the WRONG branch
-- (ride_requests, where the column is real) and left it on the branch that
-- actually lacks the column (rides). That migration's CREATE OR REPLACE
-- succeeded (DDL definition doesn't fail on this), but calling the RPC with
-- source='rides' and a non-null vehicle_type would still hit
-- 'column "vehicle_type" does not exist' (42703) -- just on the other branch
-- than before.
--
-- Fix: drop the vehicle_type predicate from the source='rides' branch (3
-- occurrences), restore it on the source='requests' branch (2 occurrences,
-- matching the ride_requests table's real vehicle_type NOT NULL column).
-- driver_profiles supply-side filtering is untouched (confirmed correct).
--
-- *** MANUAL APPLICATION REQUIRED ***
-- AUTO_MIGRATE_ENABLED=false in production. Run manually in the Supabase SQL
-- editor.

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
volatile
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
    -- rides has no vehicle_type column -- category-only filter.
    insert into tmp_demand_cells (grid_lat, grid_lng)
    select round(v_lat / p_grid_size) * p_grid_size, round(v_lng / p_grid_size) * p_grid_size
    from (
      select
        (coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric as v_lat,
        (coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric as v_lng
      from public.rides
      where created_at >= v_cutoff
        and (p_category is null or category = p_category)
        and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
        and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null
    ) pts
    where (p_south is null or v_lat >= p_south) and (p_north is null or v_lat <= p_north)
      and (p_west is null or v_lng >= p_west) and (p_east is null or v_lng <= p_east);

    select count(*) into v_demand_total from public.rides
    where created_at >= v_cutoff
      and (p_category is null or category = p_category);

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
        and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
        and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null
    ) pts
    where (p_south is null or v_lat >= p_south) and (p_north is null or v_lat <= p_north)
      and (p_west is null or v_lng >= p_west) and (p_east is null or v_lng <= p_east);
  else
    -- ride_requests.vehicle_type is real (character varying NOT NULL) -- filter it.
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

notify pgrst, 'reload schema';
