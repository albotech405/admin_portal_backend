-- Refresh the approve_wallet_topup RPC so PostgREST can resolve wallet top-up approvals.
-- This is safe to apply even if the function already exists.

create or replace function public.approve_wallet_topup(
  p_admin_id uuid,
  p_request_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  request_row public.wallet_topup_requests%rowtype;
  driver_user_id uuid;
  new_balance numeric(12,2);
begin
  perform public.assert_admin(p_admin_id);

  select *
  into request_row
  from public.wallet_topup_requests
  where id = p_request_id
  for update;

  if not found then
    raise exception 'wallet top-up request not found';
  end if;

  if request_row.status <> 'pending' then
    raise exception 'wallet top-up request is already %', request_row.status;
  end if;

  select user_id, credit_balance
  into driver_user_id, new_balance
  from public.driver_profiles
  where id = request_row.driver_id
  for update;

  if driver_user_id is null and new_balance is null then
    raise exception 'driver profile not found for wallet top-up request';
  end if;

  perform set_config('app.wallet_rpc_context', 'on', true);

  update public.driver_profiles
  set credit_balance = credit_balance + request_row.amount,
      updated_at = now()
  where id = request_row.driver_id
  returning credit_balance into new_balance;

  insert into public.wallet_transactions (
    id,
    driver_id,
    type,
    amount,
    balance_after,
    reference_type,
    reference_id,
    description
  ) values (
    gen_random_uuid(),
    request_row.driver_id,
    'credit',
    request_row.amount,
    new_balance,
    'topup',
    request_row.id,
    'Wallet top-up approved by admin'
  );

  update public.wallet_topup_requests
  set status = 'approved',
      reviewed_at = now(),
      reviewed_by = p_admin_id,
      rejection_reason = null
  where id = request_row.id;

  perform public.create_system_notification(
    driver_user_id,
    'Wallet top-up approved',
    format('Your wallet top-up of %s has been approved and credited to your balance.', request_row.amount),
    array['payment_update', 'wallet', 'system', 'general'],
    array['unread', 'pending', 'sent']
  );

  perform public.insert_admin_log(
    'approve_wallet_topup',
    p_admin_id,
    request_row.id,
    'wallet_topup_requests',
    jsonb_build_object('driver_id', request_row.driver_id, 'amount', request_row.amount, 'new_balance', new_balance)
  );

  return jsonb_build_object(
    'request_id', request_row.id,
    'driver_id', request_row.driver_id,
    'status', 'approved',
    'new_balance', new_balance
  );
end;
$$;

notify pgrst, 'reload schema';
