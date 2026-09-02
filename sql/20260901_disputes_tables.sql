-- Disputes & Customer Ban: real disputes table + append-only history, replacing
-- the old code's dependency on a nonexistent dispute_logs table (confirmed
-- absent from a real production schema dump this session -- the old
-- app/services/disputes/router.py could return HTTP 200 "success" on every
-- action while writing nothing anywhere).
--
-- dispute_history is distinct from admin_audit_log (generic Python-side admin
-- action log, app/services/audit/router.py) and admin_logs (generic RPC-side
-- log, sql/20260702_admin_log_refresh.sql) -- it's a domain-specific,
-- append-only timeline for one dispute's own lifecycle, giving the dispute
-- detail endpoint a purpose-built history without joining across two unrelated
-- generic-audit tables. Both admin_audit_log and admin_logs are still written
-- to as well, for the generic cross-entity admin-action trail.
--
-- *** MANUAL APPLICATION REQUIRED ***
-- AUTO_MIGRATE_ENABLED=false in production. Run manually in the Supabase SQL editor.

CREATE TABLE IF NOT EXISTS public.disputes (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ride_id               UUID REFERENCES public.rides(id),
    customer_id           UUID REFERENCES public.users(id),
    driver_id             UUID REFERENCES public.driver_profiles(id),
    filed_by_admin_id     UUID NOT NULL REFERENCES public.users(id),
    filed_for             TEXT NOT NULL CHECK (filed_for IN ('customer', 'driver')),
    dispute_type          TEXT NOT NULL,
    priority              TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    status                TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'assigned', 'escalated', 'resolved', 'dismissed', 'reopened')),
    assigned_to_admin_id  UUID REFERENCES public.users(id),
    description           TEXT NOT NULL,
    attachment_urls       TEXT[] NOT NULL DEFAULT '{}',
    resolution_type       TEXT,
    resolution_notes      TEXT,
    resolved_by_admin_id  UUID REFERENCES public.users(id),
    resolved_at           TIMESTAMPTZ,
    disputed_amount_cdf   NUMERIC(12,2),
    resolution_amount_cdf NUMERIC(12,2),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Supports GET /disputes/admin/list default view (status filter + recency order)
CREATE INDEX IF NOT EXISTS disputes_status_created_at_idx ON public.disputes (status, created_at DESC);
-- Supports lookups by ride/customer/driver (detail views, "does this ride
-- already have an open dispute" checks)
CREATE INDEX IF NOT EXISTS disputes_ride_id_idx ON public.disputes (ride_id) WHERE ride_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS disputes_customer_id_idx ON public.disputes (customer_id) WHERE customer_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS disputes_driver_id_idx ON public.disputes (driver_id) WHERE driver_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.dispute_history (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id    UUID NOT NULL REFERENCES public.disputes(id) ON DELETE CASCADE,
    admin_id      UUID REFERENCES public.users(id),
    action        TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT,
    notes         TEXT,
    metadata      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS dispute_history_dispute_id_created_at_idx
    ON public.dispute_history (dispute_id, created_at DESC);

ALTER TABLE public.disputes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all" ON public.disputes;
CREATE POLICY "service_role_all" ON public.disputes FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE public.dispute_history ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all" ON public.dispute_history;
CREATE POLICY "service_role_all" ON public.dispute_history FOR ALL TO service_role USING (true) WITH CHECK (true);

notify pgrst, 'reload schema';
