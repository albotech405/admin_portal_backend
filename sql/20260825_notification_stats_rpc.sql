-- Add DB-side type histogram for GET /admin/notifications/stats.
-- Run this in the Supabase SQL editor (Dashboard → SQL editor).
--
-- Why this is needed:
--
-- GET /admin/notifications/stats currently fetches every row of the notifications
-- table (SELECT notification_type FROM notifications, no filter, no limit) just to
-- build a by-type count in Python. notifications is a per-user, per-event table with
-- no natural upper bound, so this is a full-table scan on every dashboard load.
-- PostgREST has no GROUP BY over REST, so this migration adds one read-only RPC that
-- does the count server-side. Same pattern as sql/20260825_customer_wallet_metrics_rpc.sql
-- (security definer, public.assert_admin gate, notify pgrst on apply).
--
-- Response shape: a JSON object keyed by notification_type -> count, matching the
-- `by_type` dict the Python endpoint already returns, so the API contract is unchanged.

create or replace function public.get_notification_type_counts(p_admin_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_result jsonb;
begin
  perform public.assert_admin(p_admin_id);

  select coalesce(jsonb_object_agg(notification_type, cnt), '{}'::jsonb)
  into v_result
  from (
    select notification_type::text as notification_type, count(*) as cnt
    from public.notifications
    group by notification_type
  ) counts;

  return v_result;
end;
$$;

notify pgrst, 'reload schema';
