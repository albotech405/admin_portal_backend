-- Indexes supporting GET /analytics/admin/heatmap. Confirmed via code-usage audit
-- that zero indexes currently exist on driver_profiles, rides, or ride_requests in
-- any prior migration in this repo. Safe to run independently of
-- sql/20260826_heatmap_v2_viewport_zoom.sql (DDL only, no function changes).
--
-- CREATE INDEX (not CONCURRENTLY): the Supabase SQL editor runs statements in an
-- implicit transaction and CONCURRENTLY cannot run inside one. If these tables are
-- large/high-traffic in production, consider running the CONCURRENTLY equivalents
-- individually outside the editor instead.

-- Supply queries always filter is_online = true first (both V1 and V2) -- a partial
-- index avoids indexing offline drivers, which are never read by this endpoint.
CREATE INDEX IF NOT EXISTS driver_profiles_online_latlng_idx
    ON public.driver_profiles (latitude, longitude)
    WHERE is_online = true;

-- Demand time-window filter -- now more selective with sub-day (15m/30m/60m) windows
-- than the existing 7-day default, so a sequential scan is proportionally more
-- wasteful than before.
CREATE INDEX IF NOT EXISTS rides_created_at_idx
    ON public.rides (created_at);

CREATE INDEX IF NOT EXISTS ride_requests_created_at_idx
    ON public.ride_requests (created_at);

-- Deliberately NOT added: indexes on category/vehicle_type/status. These are
-- low-cardinality columns and created_at already narrows the row set first; V1/V2
-- don't filter demand by status at all, so no index is needed for a filter that
-- doesn't exist. Revisit with EXPLAIN ANALYZE once live DB access is available.
