-- Add DB-side grid-bucketed demand/supply aggregation for the admin heatmap.
-- Run this in the Supabase SQL editor (Dashboard -> SQL editor).
--
-- GET /analytics/admin/heatmap needs to group ride_requests/rides pickup points
-- and driver_profiles locations into a coarse lat/lng grid and count rows per
-- cell. PostgREST has no GROUP BY over REST, and picking_point is opaque JSON
-- with inconsistent key names (latitude/lat, longitude/lng — see
-- app/services/live_location/router.py _point_from_anchor), so this RPC does
-- extraction, coordinate parsing, and grid bucketing server-side in one pass.
-- Same pattern as sql/20260825_customer_wallet_metrics_rpc.sql. Until applied,
-- GET /analytics/admin/heatmap falls back to a capped client-side computation
-- in app/services/analytics/router.py.
--
-- Grid bucketing: cell center = round(coord / p_grid_size) * p_grid_size,
-- identical to the _bucket() helper in app/services/analytics/router.py.
--
-- picking_point is read via to_jsonb(picking_point)->>'latitude' so this works
-- whether the column is native jsonb/json or text containing JSON.

create or replace function public.get_admin_heatmap(
  p_admin_id uuid,
  p_days integer default 7,
  p_source text default 'requests',
  p_category text default null,
  p_vehicle_type text default null,
  p_grid_size numeric default 0.01
)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_cutoff timestamptz := now() - make_interval(days => p_days);
  v_cells jsonb;
  v_demand_skipped integer := 0;
  v_demand_total integer := 0;
  v_supply_total integer := 0;
begin
  perform public.assert_admin(p_admin_id);

  create temporary table tmp_demand_cells (grid_lat numeric, grid_lng numeric) on commit drop;
  create temporary table tmp_supply_cells (grid_lat numeric, grid_lng numeric) on commit drop;

  if p_source = 'rides' then
    insert into tmp_demand_cells (grid_lat, grid_lng)
    select
      round((coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric / p_grid_size) * p_grid_size,
      round((coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric / p_grid_size) * p_grid_size
    from public.rides
    where created_at >= v_cutoff
      and (p_category is null or category = p_category)
      and (p_vehicle_type is null or vehicle_type = p_vehicle_type)
      and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
      and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null;

    select count(*) into v_demand_total from public.rides
    where created_at >= v_cutoff
      and (p_category is null or category = p_category)
      and (p_vehicle_type is null or vehicle_type = p_vehicle_type);
  else
    insert into tmp_demand_cells (grid_lat, grid_lng)
    select
      round((coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat'))::numeric / p_grid_size) * p_grid_size,
      round((coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng'))::numeric / p_grid_size) * p_grid_size
    from public.ride_requests
    where created_at >= v_cutoff
      and (p_category is null or category = p_category)
      and (p_vehicle_type is null or vehicle_type = p_vehicle_type)
      and coalesce(to_jsonb(picking_point)->>'latitude', to_jsonb(picking_point)->>'lat') is not null
      and coalesce(to_jsonb(picking_point)->>'longitude', to_jsonb(picking_point)->>'lng') is not null;

    select count(*) into v_demand_total from public.ride_requests
    where created_at >= v_cutoff
      and (p_category is null or category = p_category)
      and (p_vehicle_type is null or vehicle_type = p_vehicle_type);
  end if;

  select v_demand_total - count(*) into v_demand_skipped from tmp_demand_cells;

  insert into tmp_supply_cells (grid_lat, grid_lng)
  select
    round(latitude::numeric / p_grid_size) * p_grid_size,
    round(longitude::numeric / p_grid_size) * p_grid_size
  from public.driver_profiles
  where is_online = true
    and latitude is not null
    and longitude is not null
    and (p_category is null or category = p_category)
    and (p_vehicle_type is null or vehicle_type = p_vehicle_type);

  select count(*) into v_supply_total from tmp_supply_cells;

  select coalesce(jsonb_agg(jsonb_build_object(
    'grid_lat', grid_lat,
    'grid_lng', grid_lng,
    'demand_count', demand_count,
    'supply_count', supply_count
  )), '[]'::jsonb)
  into v_cells
  from (
    select
      coalesce(d.grid_lat, s.grid_lat) as grid_lat,
      coalesce(d.grid_lng, s.grid_lng) as grid_lng,
      coalesce(d.demand_count, 0) as demand_count,
      coalesce(s.supply_count, 0) as supply_count
    from
      (select grid_lat, grid_lng, count(*) as demand_count from tmp_demand_cells group by grid_lat, grid_lng) d
      full outer join
      (select grid_lat, grid_lng, count(*) as supply_count from tmp_supply_cells group by grid_lat, grid_lng) s
      on d.grid_lat = s.grid_lat and d.grid_lng = s.grid_lng
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
