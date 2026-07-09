-- Restore the admin log helper required by approval RPCs.

create or replace function public.insert_admin_log(
  p_action text,
  p_admin_id uuid,
  p_target_id uuid,
  p_target_table text,
  p_metadata jsonb default '{}'::jsonb
)
returns void
language sql
security definer
set search_path = public
as $$
  insert into public.admin_logs (action, admin_id, target_id, target_table, metadata)
  values (p_action, p_admin_id, p_target_id, p_target_table, coalesce(p_metadata, '{}'::jsonb));
$$;

notify pgrst, 'reload schema';