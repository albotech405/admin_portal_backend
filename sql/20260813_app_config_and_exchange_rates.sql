-- Create app_config and exchange_rates tables
-- Run this in the Supabase SQL editor (Dashboard → SQL editor)
--
-- Why this is needed:
--
-- 1. app_config — the /config/admin/app-toggles endpoints and the dashboard's
--    stale-request threshold already read and write this table, but it was never
--    created. Reads swallow the error and silently fall back to hardcoded defaults,
--    so the toggles look functional in the UI while never persisting; writes fail.
--
-- 2. exchange_rates — backs GET/PUT /config/admin/exchange-rate. The admin portal
--    calls that endpoint from the Dashboard, Finance and Pricing screens to display
--    and set the USD→CDF rate; before this it returned 404 on every load.

CREATE TABLE IF NOT EXISTS public.app_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key TEXT NOT NULL UNIQUE,
    value TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.app_config ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all" ON public.app_config;
CREATE POLICY "service_role_all" ON public.app_config
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Seed the toggle defaults the backend currently assumes.
INSERT INTO public.app_config (key, value) VALUES
    ('active_request_resume_enabled', 'true'),
    ('driver_offer_update_enabled', 'true'),
    ('stale_request_alert_threshold_minutes', '10')
ON CONFLICT (key) DO NOTHING;


CREATE TABLE IF NOT EXISTS public.exchange_rates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rate_cdf_per_usd NUMERIC(14, 4) NOT NULL CHECK (rate_cdf_per_usd > 0),
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('live', 'manual')),
    effective_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_to TIMESTAMPTZ,
    set_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The current rate is the newest row by effective_from; this index serves that lookup.
CREATE INDEX IF NOT EXISTS exchange_rates_effective_from_idx
    ON public.exchange_rates (effective_from DESC);

ALTER TABLE public.exchange_rates ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all" ON public.exchange_rates;
CREATE POLICY "service_role_all" ON public.exchange_rates
    FOR ALL TO service_role USING (true) WITH CHECK (true);
