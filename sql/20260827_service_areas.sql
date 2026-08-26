-- Admin Operating-Area Discovery: service_areas table.
-- Backs GET /api/v1/config/admin/service-areas and the optional
-- service_area_id param on GET /api/v1/analytics/admin/heatmap.
--
-- Flat single-table design (country_code/country_name/city/area_name as plain
-- columns, not FKs to separate countries/cities tables) -- no requirement exists
-- to manage countries/cities as independent entities, so a normalized hierarchy
-- would be unused complexity. bbox columns match Heatmap V2's own bbox
-- representation exactly -- no PostGIS, matching this repo's existing standard
-- of zero PostGIS usage.
--
-- Picked up automatically by the auto-migration runner (app/core/migrations.py)
-- on the next deploy -- no manual Supabase SQL editor step required.

CREATE TABLE IF NOT EXISTS public.service_areas (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    country_code  TEXT NOT NULL,
    country_name  TEXT NOT NULL,
    city          TEXT NOT NULL,
    area_name     TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT true,
    north         DOUBLE PRECISION NOT NULL,
    south         DOUBLE PRECISION NOT NULL,
    east          DOUBLE PRECISION NOT NULL,
    west          DOUBLE PRECISION NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT service_areas_bbox_valid CHECK (north > south AND east > west)
);

CREATE INDEX IF NOT EXISTS service_areas_is_active_idx
    ON public.service_areas (is_active);

ALTER TABLE public.service_areas ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all" ON public.service_areas;
CREATE POLICY "service_role_all" ON public.service_areas
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Seed the ONE real geographic value that exists anywhere in this system: the
-- DRC/Kinshasa-metro approximate bbox, identical to the existing app_config
-- 'default_operating_area_bbox' seed (deliberately not reconciled into one
-- source of truth in this task -- see app/services/analytics/router.py). Not a
-- precise administrative boundary -- same caveat as that existing seed. No
-- other cities are seeded; none exist anywhere in this codebase's data.
INSERT INTO public.service_areas (country_code, country_name, city, area_name, is_active, north, south, east, west)
VALUES ('CD', 'Democratic Republic of the Congo', 'Kinshasa', 'Kinshasa Metro', true, -4.20, -4.50, 15.40, 15.15)
ON CONFLICT DO NOTHING;

notify pgrst, 'reload schema';
