-- Migration: align admin_sessions to refresh-token-aware schema
-- Adds admin_id, logged_in_at, logged_out_at, user_agent, last_refreshed_at columns
-- and backfills from the existing admin_user_id / created_at columns.
-- Old columns (admin_user_id, admin_email, created_at, expires_at) are kept for
-- backward compatibility and can be dropped in a later migration.

-- 1. Add new columns
ALTER TABLE public.admin_sessions
  ADD COLUMN IF NOT EXISTS admin_id          UUID,
  ADD COLUMN IF NOT EXISTS logged_in_at      TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS logged_out_at     TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS user_agent        TEXT,
  ADD COLUMN IF NOT EXISTS last_refreshed_at TIMESTAMPTZ;

-- 2. Backfill admin_id and logged_in_at from existing rows that have a
--    matching user in the users table (orphaned rows are left as NULL).
UPDATE public.admin_sessions s
SET
  admin_id     = s.admin_user_id::uuid,
  logged_in_at = coalesce(s.created_at, now())
FROM public.users u
WHERE s.admin_id IS NULL
  AND u.id::text = s.admin_user_id::text;

-- 3. Add FK constraint (idempotent via DO block).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'admin_sessions_admin_id_fkey'
      AND conrelid = 'public.admin_sessions'::regclass
  ) THEN
    ALTER TABLE public.admin_sessions
      ADD CONSTRAINT admin_sessions_admin_id_fkey
      FOREIGN KEY (admin_id) REFERENCES public.users(id) ON DELETE CASCADE;
  END IF;
EXCEPTION WHEN OTHERS THEN
  -- Skip FK if any orphaned rows remain; handle manually before re-running.
  RAISE NOTICE 'Skipped FK constraint: %', SQLERRM;
END;
$$;

-- 4. Fast lookup index for active-session queries
CREATE INDEX IF NOT EXISTS admin_sessions_admin_id_is_active_idx
  ON public.admin_sessions (admin_id, is_active);
