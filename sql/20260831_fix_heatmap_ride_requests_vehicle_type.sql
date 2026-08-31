-- Fix: get_admin_heatmap filtered ride_requests by vehicle_type, but that
-- column does not exist on ride_requests -- only on `rides` and
-- `driver_profiles`. Production error:
--   {'message': 'column "vehicle_type" does not exist', 'code': '42703', ...}
--
-- Evidence: every other place in this codebase that reads vehicle_type off a
-- ride_requests row does so defensively (r.get("vehicle_type", "car")),
-- unlike `category`, which is filtered directly (.eq("category", category)
-- in app/services/pricing/router.py) -- confirming category is real on
-- ride_requests but vehicle_type was never actually confirmed there.
--
-- Fix: drop the vehicle_type predicate from the two ride_requests queries
-- (demand + previous-period demand, source='requests' branch only). The
-- rides branch (source='rides') and the driver_profiles supply query are
-- untouched -- vehicle_type is confirmed real on both of those.
-- p_vehicle_type is still accepted as a parameter (so the function signature
-- doesn't change for existing callers); it's simply a no-op filter when
-- p_source='requests'.
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
    -- source='requests': ride_requests has no vehicle_type column (confirmed
    -- absent -- see header comment) -- category is filtered, vehicle_type is
    -- not, unlike the rides branch above.
    insert into tmp_demand_cells (grid_lat, grid_lng)
    select round(v_lat / p_grid_size) * p_grid_size, round(v_lng / p_grid_size) * p_grid_size
    from (
      select
        (coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric as v_lat,
        (coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric as v_lng
      from public.ride_requests
      where created_at >= v_cutoff
        and (p_category is null or category = p_category)
        and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
        and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null
    ) pts
    where (p_south is null or v_lat >= p_south) and (p_north is null or v_lat <= p_north)
      and (p_west is null or v_lng >= p_west) and (p_east is null or v_lng <= p_east);

    select count(*) into v_demand_total from public.ride_requests
    where created_at >= v_cutoff
      and (p_category is null or category = p_category);

    insert into tmp_prev_demand_cells (grid_lat, grid_lng)
    select round(v_lat / p_grid_size) * p_grid_size, round(v_lng / p_grid_size) * p_grid_size
    from (
      select
        (coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric as v_lat,
        (coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric as v_lng
      from public.ride_requests
      where created_at >= v_prev_cutoff and created_at < v_cutoff
        and (p_category is null or category = p_category)
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
