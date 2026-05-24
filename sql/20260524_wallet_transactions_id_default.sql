-- Fix: wallet_transactions.id was missing a default, causing NOT NULL violations
-- when inserting from RPC functions or Python code without an explicit id.

alter table public.wallet_transactions
  alter column id set default gen_random_uuid();

-- Re-create approve_wallet_topup with explicit id in the insert
create or replace function public.approve_wallet_topup(
  p_request_id uuid,
  p_admin_id uuid
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

-- Re-create deduct_commission with explicit id in the insert
create or replace function public.deduct_commission(
  p_ride_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  ride_row record;
  driver_row record;
  commission_rate numeric(5,4) := 0.15;
  commission_amount numeric(12,2);
  new_balance numeric(12,2);
begin
  select r.id, r.driver_id, r.price, r.status
  into ride_row
  from public.rides r
  where r.id = p_ride_id
  for update;

  if not found then
    raise exception 'ride not found';
  end if;

  if ride_row.status <> 'completed' then
    raise exception 'commission can only be deducted for completed rides';
  end if;

  if exists (
    select 1
    from public.wallet_transactions wt
    where wt.reference_type = 'ride_commission'
      and wt.reference_id = p_ride_id
  ) then
    raise exception 'commission already deducted for ride %', p_ride_id;
  end if;

  if ride_row.driver_id is null then
    raise exception 'completed ride has no assigned driver';
  end if;

  commission_amount := round(coalesce(ride_row.price, 0) * commission_rate, 2);

  if commission_amount <= 0 then
    return jsonb_build_object(
      'ride_id', p_ride_id,
      'commission_amount', 0,
      'deducted', false
    );
  end if;

  select dp.id, dp.user_id, dp.credit_balance
  into driver_row
  from public.driver_profiles dp
  where dp.id = ride_row.driver_id
  for update;

  if not found then
    raise exception 'driver profile not found for completed ride';
  end if;

  if coalesce(driver_row.credit_balance, 0) < commission_amount then
    perform public.block_driver_if_no_balance(ride_row.driver_id, null);
    raise exception 'driver wallet balance is too low to deduct commission';
  end if;

  perform set_config('app.wallet_rpc_context', 'on', true);

  update public.driver_profiles
  set credit_balance = credit_balance - commission_amount,
      updated_at = now()
  where id = ride_row.driver_id
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
    ride_row.driver_id,
    'debit',
    commission_amount,
    new_balance,
    'ride_commission',
    p_ride_id,
    'Ride commission deduction'
  );

  if new_balance <= 5 then
    perform public.create_system_notification(
      driver_row.user_id,
      'Low wallet balance',
      format('Your wallet balance is now %s after commission deduction. Please top up soon to avoid suspension.', new_balance),
      array['wallet', 'system', 'general'],
      array['unread', 'pending', 'sent']
    );
  end if;

  perform public.insert_admin_log(
    'deduct_commission',
    null,
    p_ride_id,
    'rides',
    jsonb_build_object('driver_id', ride_row.driver_id, 'commission_rate', commission_rate, 'commission_amount', commission_amount, 'new_balance', new_balance)
  );

  if new_balance <= 0 then
    perform public.block_driver_if_no_balance(ride_row.driver_id, null);
  end if;

  return jsonb_build_object(
    'ride_id', p_ride_id,
    'driver_id', ride_row.driver_id,
    'commission_rate', commission_rate,
    'commission_amount', commission_amount,
    'new_balance', new_balance,
    'deducted', true
  );
end;
$$;
