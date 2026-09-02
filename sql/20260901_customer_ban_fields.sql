-- Persist ban context on the users row itself (reason/actor/timestamp), while
-- keeping is_active as the sole boolean ban representation. Unban clears all
-- three fields back to null -- there is no product requirement for a ban-history
-- trail on the row itself (admin_audit_log already captures that, unconditionally,
-- on every ban/unban call via write_audit_log in
-- app/services/customers/router.py). The row reflects current state; the audit
-- log reflects history.
--
-- *** MANUAL APPLICATION REQUIRED ***
-- AUTO_MIGRATE_ENABLED=false in production. Run manually in the Supabase SQL editor.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS ban_reason TEXT,
    ADD COLUMN IF NOT EXISTS banned_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS banned_by UUID REFERENCES public.users(id);

CREATE INDEX IF NOT EXISTS users_banned_at_idx ON public.users (banned_at) WHERE banned_at IS NOT NULL;

notify pgrst, 'reload schema';
