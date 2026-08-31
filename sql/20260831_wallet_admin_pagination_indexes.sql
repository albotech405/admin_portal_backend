-- Indexes supporting the newly-paginated wallet admin endpoints:
--   GET /wallet/admin/driver/{driver_id}/transactions (BE-6)
--   GET /wallet/admin/topup/requests (BE-5)
--
-- *** MANUAL APPLICATION REQUIRED (as of 2026-08-31) ***
-- The auto-migration runner's code default is AUTO_MIGRATE_ENABLED=true, but the
-- live Render environment variable was set to false after a real IPv6-connectivity
-- boot crash earlier this session (not visible in this repo -- env vars aren't
-- committed). Run this manually in the Supabase SQL editor until that's resolved
-- and the runner is re-enabled.
--
-- wallet_transactions: the endpoint filters .eq("driver_id", driver_id) and orders
-- by created_at desc, now ranged via .range(offset, offset+limit-1) -- a composite
-- index directly matches this WHERE+ORDER BY.
CREATE INDEX IF NOT EXISTS wallet_transactions_driver_id_created_at_idx
    ON public.wallet_transactions (driver_id, created_at DESC);

-- wallet_topup_requests: the endpoint optionally filters .eq("status", status) and
-- orders by submitted_at desc, and status is now queried twice per request (once
-- for the head=True count, once for the ranged data fetch) -- a composite index
-- supports both.
CREATE INDEX IF NOT EXISTS wallet_topup_requests_status_submitted_at_idx
    ON public.wallet_topup_requests (status, submitted_at DESC);

-- Deliberately NOT added: an index for dispute_logs (BE-4's pagination target).
-- dispute_logs has no CREATE TABLE anywhere under sql/ -- its schema is defined
-- entirely outside this repo's migration history. No index migration is proposed
-- for a table this repo doesn't define; check directly in the Supabase dashboard
-- if dispute list performance becomes a real issue.

notify pgrst, 'reload schema';
