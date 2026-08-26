-- Extend get_admin_heatmap with viewport bbox filtering and sub-day time windows.
-- Run this AFTER sql/20260825_heatmap_rpc.sql (already applied).
--
-- All five new parameters (p_minutes, p_north/p_south/p_east/p_west) default to NULL,
-- so this is a strict superset of the existing signature -- when omitted, every new
-- WHERE predicate below becomes a no-op ("... is null or ...") and the function
-- executes identically to the pre-V2 version. This is the backward-compatibility
-- guarantee required by the V2 task (BE-12).
--
-- p_minutes, when provided, overrides p_days for the time cutoff (matches the
-- days-vs-minutes precedence in app/services/analytics/router.py get_heatmap).
--
-- Bbox filtering is applied on the raw rides/ride_requests/driver_profiles rows
-- BEFORE grid bucketing, so it's index-friendly once
-- sql/20260826_heatmap_v2_indexes.sql's indexes exist.

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
  v_cutoff timestamptz := case
    when p_minutes is not null then now() - make_interval(mins => p_minutes)
    else now() - make_interval(days => p_days)
  end;
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
    'demand_count', demand_count, 'supply_count', supply_count
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

-- Seed the default operating-area config used by use_default_area=true. Approximate
-- Kinshasa-metro bounds, confirmed with the user as a starting estimate (2026-08-26)
-- -- NOT a precise administrative boundary. Tighten later with a direct UPDATE on
-- this row as real coverage data becomes available:
--   update public.app_config set value = '{"north": .., "south": .., "east": .., "west": ..}'
--   where key = 'default_operating_area_bbox';
insert into public.app_config (key, value)
values ('default_operating_area_bbox', '{"north": -4.20, "south": -4.50, "east": 15.40, "west": 15.15}')
on conflict (key) do nothing;

notify pgrst, 'reload schema';
