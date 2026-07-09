-- Refresh the approve_driver RPC so PostgREST can resolve driver approvals.
-- This is safe to apply even if the function already exists.

create or replace function public.approve_driver(
  p_driver_id uuid,
  p_admin_id uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  driver_row record;
  acting_admin_id uuid;
begin
  if p_admin_id is not null then
    acting_admin_id := public.assert_admin(p_admin_id);
  else
    raise exception 'admin_id is required';
  end if;

  select dp.id, dp.user_id, dp.verification_status
  into driver_row
  from public.driver_profiles dp
  where dp.id = p_driver_id
  for update;

  if not found then
    raise exception 'driver profile not found';
  end if;

  update public.driver_profiles
  set verification_status = 'approved',
      verification_feedback = null,
      activation_date = coalesce(activation_date, now()),
      is_suspended = false,
      updated_at = now()
  where id = p_driver_id;

  perform public.create_system_notification(
    driver_row.user_id,
    'Driver account approved',
    'Your driver account has been approved. You can now go online and accept rides.',
    array['driver_update', 'system', 'general'],
    array['unread', 'pending', 'sent']
  );

  perform public.insert_admin_log(
    'approve_driver',
    acting_admin_id,
    p_driver_id,
    'driver_profiles',
    jsonb_build_object('previous_status', driver_row.verification_status, 'new_status', 'approved')
  );

  return jsonb_build_object(
    'driver_id', p_driver_id,
    'status', 'approved',
    'is_suspended', false
  );
end;
$$;

notify pgrst, 'reload schema';