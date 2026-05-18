begin;

alter table public.wallet_topup_requests
  add column if not exists reference_number text,
  add column if not exists sender_name text;

alter table public.driver_profiles
  add column if not exists is_suspended boolean not null default false;

create table if not exists public.admin_logs (
  id uuid primary key default gen_random_uuid(),
  action text not null,
  admin_id uuid null references public.users(id) on delete set null,
  target_id uuid null,
  target_table text null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists admin_logs_action_created_at_idx
  on public.admin_logs (action, created_at desc);

create or replace function public.is_admin_user(p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists(
    select 1
    from public.users
    where id = p_user_id
      and is_admin = true
  );
$$;

create or replace function public.assert_admin(p_admin_id uuid)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_admin_id is null then
    raise exception 'admin_id is required';
  end if;

  if not public.is_admin_user(p_admin_id) then
    raise exception 'admin access required';
  end if;

  return p_admin_id;
end;
$$;

create or replace function public.pick_notification_type(preferred_labels text[])
returns text
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  selected_label text;
begin
  select enum_values.enumlabel
  into selected_label
  from (
    select e.enumlabel, array_position(preferred_labels, e.enumlabel) as preferred_rank
    from pg_type t
    join pg_enum e on e.enumtypid = t.oid
    where t.typname = 'notificationtype'
  ) enum_values
  order by coalesce(enum_values.preferred_rank, 2147483647), enum_values.enumlabel
  limit 1;

  return selected_label;
end;
$$;

create or replace function public.pick_notification_status(preferred_labels text[])
returns text
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  selected_label text;
begin
  select enum_values.enumlabel
  into selected_label
  from (
    select e.enumlabel, array_position(preferred_labels, e.enumlabel) as preferred_rank
    from pg_type t
    join pg_enum e on e.enumtypid = t.oid
    where t.typname = 'notificationstatus'
  ) enum_values
  order by coalesce(enum_values.preferred_rank, 2147483647), enum_values.enumlabel
  limit 1;

  return selected_label;
end;
$$;

create or replace function public.create_system_notification(
  p_user_id uuid,
  p_title text,
  p_content text,
  p_type_preferences text[] default array['system', 'payment_update', 'wallet', 'general'],
  p_status_preferences text[] default array['unread', 'pending', 'sent']
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  selected_type text;
  selected_status text;
begin
  if p_user_id is null then
    return;
  end if;

  selected_type := public.pick_notification_type(p_type_preferences);
  selected_status := public.pick_notification_status(p_status_preferences);

  if selected_type is null or selected_status is null then
    return;
  end if;

  insert into public.notifications (
    user_id,
    title,
    content,
    notification_type,
    status
  ) values (
    p_user_id,
    left(coalesce(p_title, 'Admin update'), 255),
    coalesce(p_content, ''),
    selected_type::notificationtype,
    selected_status::notificationstatus
  );
end;
$$;

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

create or replace function public.guard_credit_balance_update()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.credit_balance is distinct from old.credit_balance
     and coalesce(current_setting('app.wallet_rpc_context', true), 'off') <> 'on' then
    raise exception 'credit_balance must be updated via approved wallet RPC functions';
  end if;

  return new;
end;
$$;

drop trigger if exists trg_guard_credit_balance_update on public.driver_profiles;
create trigger trg_guard_credit_balance_update
before update of credit_balance on public.driver_profiles
for each row
execute function public.guard_credit_balance_update();

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
    driver_id,
    type,
    amount,
    balance_after,
    reference_type,
    reference_id,
    description
  ) values (
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

create or replace function public.reject_wallet_topup(
  p_request_id uuid,
  p_admin_id uuid,
  p_reason text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  request_row public.wallet_topup_requests%rowtype;
  driver_user_id uuid;
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

  select user_id
  into driver_user_id
  from public.driver_profiles
  where id = request_row.driver_id;

  update public.wallet_topup_requests
  set status = 'rejected',
      reviewed_at = now(),
      reviewed_by = p_admin_id,
      rejection_reason = nullif(trim(coalesce(p_reason, '')), '')
  where id = request_row.id;

  perform public.create_system_notification(
    driver_user_id,
    'Wallet top-up rejected',
    case
      when nullif(trim(coalesce(p_reason, '')), '') is null then
        'Your wallet top-up request was rejected. Please contact support if you need more detail.'
      else
        format('Your wallet top-up request was rejected: %s', trim(p_reason))
    end,
    array['payment_update', 'wallet', 'system', 'general'],
    array['unread', 'pending', 'sent']
  );

  perform public.insert_admin_log(
    'reject_wallet_topup',
    p_admin_id,
    request_row.id,
    'wallet_topup_requests',
    jsonb_build_object('driver_id', request_row.driver_id, 'amount', request_row.amount, 'reason', nullif(trim(coalesce(p_reason, '')), ''))
  );

  return jsonb_build_object(
    'request_id', request_row.id,
    'driver_id', request_row.driver_id,
    'status', 'rejected'
  );
end;
$$;

create or replace function public.block_driver_if_no_balance(
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
  end if;

  select dp.id, dp.user_id, dp.credit_balance, dp.verification_status, dp.is_suspended
  into driver_row
  from public.driver_profiles dp
  where dp.id = p_driver_id
  for update;

  if not found then
    raise exception 'driver profile not found';
  end if;

  if coalesce(driver_row.credit_balance, 0) > 0 then
    return jsonb_build_object(
      'driver_id', p_driver_id,
      'status', driver_row.verification_status,
      'is_suspended', driver_row.is_suspended,
      'balance', coalesce(driver_row.credit_balance, 0),
      'blocked', false
    );
  end if;

  update public.driver_profiles
  set is_suspended = true,
      verification_status = 'suspended',
      updated_at = now()
  where id = p_driver_id;

  perform public.create_system_notification(
    driver_row.user_id,
    'Low wallet balance',
    'Your driver account has been suspended because your wallet balance is zero or below. Please top up your wallet to continue receiving rides.',
    array['wallet', 'system', 'general'],
    array['unread', 'pending', 'sent']
  );

  perform public.insert_admin_log(
    'block_driver_if_no_balance',
    acting_admin_id,
    p_driver_id,
    'driver_profiles',
    jsonb_build_object('balance', coalesce(driver_row.credit_balance, 0))
  );

  return jsonb_build_object(
    'driver_id', p_driver_id,
    'status', 'suspended',
    'is_suspended', true,
    'balance', coalesce(driver_row.credit_balance, 0),
    'blocked', true
  );
end;
$$;

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

create or replace function public.reject_driver(
  p_driver_id uuid,
  p_reason text,
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
  next_status text;
  clean_reason text;
begin
  if p_admin_id is not null then
    acting_admin_id := public.assert_admin(p_admin_id);
  else
    raise exception 'admin_id is required';
  end if;

  clean_reason := nullif(trim(coalesce(p_reason, '')), '');

  select dp.id, dp.user_id, dp.verification_status
  into driver_row
  from public.driver_profiles dp
  where dp.id = p_driver_id
  for update;

  if not found then
    raise exception 'driver profile not found';
  end if;

  if driver_row.verification_status = 'approved' then
    next_status := 'suspended';
  else
    next_status := 'rejected';
  end if;

  update public.driver_profiles
  set verification_status = next_status::verificationstatus,
      verification_feedback = clean_reason,
      is_suspended = (next_status = 'suspended'),
      updated_at = now()
  where id = p_driver_id;

  perform public.create_system_notification(
    driver_row.user_id,
    case when next_status = 'suspended' then 'Driver account suspended' else 'Driver application rejected' end,
    case
      when clean_reason is null and next_status = 'suspended' then
        'Your driver account has been suspended. Please contact support for more detail.'
      when clean_reason is null then
        'Your driver application has been rejected. Please contact support for more detail.'
      when next_status = 'suspended' then
        format('Your driver account has been suspended: %s', clean_reason)
      else
        format('Your driver application has been rejected: %s', clean_reason)
    end,
    array['driver_update', 'system', 'general'],
    array['unread', 'pending', 'sent']
  );

  perform public.insert_admin_log(
    case when next_status = 'suspended' then 'suspend_driver' else 'reject_driver' end,
    acting_admin_id,
    p_driver_id,
    'driver_profiles',
    jsonb_build_object('previous_status', driver_row.verification_status, 'new_status', next_status, 'reason', clean_reason)
  );

  return jsonb_build_object(
    'driver_id', p_driver_id,
    'status', next_status,
    'is_suspended', next_status = 'suspended'
  );
end;
$$;

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
    driver_id,
    type,
    amount,
    balance_after,
    reference_type,
    reference_id,
    description
  ) values (
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

create or replace function public.handle_completed_ride_commission()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.status = 'completed' and (tg_op = 'INSERT' or old.status is distinct from new.status) then
    perform public.deduct_commission(new.id);
  end if;

  return new;
end;
$$;

drop trigger if exists trg_handle_completed_ride_commission on public.rides;
create trigger trg_handle_completed_ride_commission
after insert or update of status on public.rides
for each row
when (new.status = 'completed')
execute function public.handle_completed_ride_commission();

alter table public.driver_profiles enable row level security;
alter table public.wallet_topup_requests enable row level security;
alter table public.wallet_transactions enable row level security;
alter table public.notifications enable row level security;
alter table public.admin_logs enable row level security;

drop policy if exists driver_profiles_admin_all on public.driver_profiles;
create policy driver_profiles_admin_all
on public.driver_profiles
for all
using (public.is_admin_user(auth.uid()))
with check (public.is_admin_user(auth.uid()));

drop policy if exists driver_profiles_driver_self_read on public.driver_profiles;
create policy driver_profiles_driver_self_read
on public.driver_profiles
for select
using (user_id = auth.uid());

drop policy if exists wallet_topup_requests_admin_all on public.wallet_topup_requests;
create policy wallet_topup_requests_admin_all
on public.wallet_topup_requests
for all
using (public.is_admin_user(auth.uid()))
with check (public.is_admin_user(auth.uid()));

drop policy if exists wallet_topup_requests_driver_self_read on public.wallet_topup_requests;
create policy wallet_topup_requests_driver_self_read
on public.wallet_topup_requests
for select
using (
  exists (
    select 1
    from public.driver_profiles dp
    where dp.id = wallet_topup_requests.driver_id
      and dp.user_id = auth.uid()
  )
);

drop policy if exists wallet_transactions_admin_all on public.wallet_transactions;
create policy wallet_transactions_admin_all
on public.wallet_transactions
for all
using (public.is_admin_user(auth.uid()))
with check (public.is_admin_user(auth.uid()));

drop policy if exists wallet_transactions_driver_self_read on public.wallet_transactions;
create policy wallet_transactions_driver_self_read
on public.wallet_transactions
for select
using (
  exists (
    select 1
    from public.driver_profiles dp
    where dp.id = wallet_transactions.driver_id
      and dp.user_id = auth.uid()
  )
);

drop policy if exists notifications_admin_all on public.notifications;
create policy notifications_admin_all
on public.notifications
for all
using (public.is_admin_user(auth.uid()))
with check (public.is_admin_user(auth.uid()));

drop policy if exists notifications_user_self_read on public.notifications;
create policy notifications_user_self_read
on public.notifications
for select
using (user_id = auth.uid());

drop policy if exists admin_logs_admin_all on public.admin_logs;
create policy admin_logs_admin_all
on public.admin_logs
for all
using (public.is_admin_user(auth.uid()))
with check (public.is_admin_user(auth.uid()));

commit;
