-- Disputes & Customer Ban: atomic financial-action RPCs for dispute resolution,
-- mirroring approve_wallet_topup's pattern
-- (sql/20260702_approve_wallet_topup_rpc_refresh.sql): assert_admin ->
-- lock+validate -> mutate balance -> insert ledger row -> update the dispute
-- row -> notify -> audit log, all in one Postgres transaction. Fixes the old
-- (Python-side, non-atomic, zero-audit-trail) charge_driver_dispute code path
-- that directly mutated driver_profiles.credit_balance with no RPC, no lock,
-- and no write_audit_log/insert_admin_log call anywhere.
--
-- Both functions are VOLATILE, not STABLE -- they perform DML (UPDATE/INSERT),
-- which STABLE's "no database-visible side effects" contract forbids. Same
-- rule as sql/20260831_fix_heatmap_rpc_volatility.sql fixed earlier this
-- session (that one was CREATE TEMP TABLE; this one is UPDATE/INSERT) --
-- avoided here from the start rather than fixed after a production failure.
--
-- dispute_charge_driver REJECTS (raises) rather than floors to zero when the
-- charge amount exceeds the driver's balance -- silently under-collecting
-- would hide a real accounting gap; the admin must resolve the shortfall
-- explicitly (a partial charge, or a company_adjustment resolve for the
-- remainder) rather than have it happen invisibly.
--
-- *** MANUAL APPLICATION REQUIRED ***
-- AUTO_MIGRATE_ENABLED=false in production. Run manually in the Supabase SQL editor.
-- Run AFTER sql/20260901_disputes_tables.sql.

create or replace function public.dispute_refund_customer(
  p_admin_id uuid,
  p_dispute_id uuid,
  p_amount_cdf numeric,
  p_notes text default null
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  dispute_row public.disputes%rowtype;
  new_balance numeric(12,2);
begin
  perform public.assert_admin(p_admin_id);

  if p_amount_cdf is null or p_amount_cdf <= 0 then
    raise exception 'refund amount must be positive';
  end if;

  select * into dispute_row from public.disputes where id = p_dispute_id for update;
  if not found then
    raise exception 'dispute not found';
  end if;
  if dispute_row.status not in ('open', 'assigned', 'escalated') then
    raise exception 'dispute is not in a resolvable state (current status: %)', dispute_row.status;
  end if;
  if dispute_row.customer_id is null then
    raise exception 'dispute has no associated customer to refund';
  end if;

  update public.users
  set wallet_balance_cdf = coalesce(wallet_balance_cdf, 0) + p_amount_cdf, updated_at = now()
  where id = dispute_row.customer_id
  returning wallet_balance_cdf into new_balance;
  if not found then
    raise exception 'customer not found for refund';
  end if;

  insert into public.customer_wallet_transactions (
    id, user_id, type, amount_cdf, balance_after_cdf, source, ride_id, reference_id, transaction_metadata
  ) values (
    gen_random_uuid(), dispute_row.customer_id, 'credit', p_amount_cdf, new_balance,
    'dispute_refund', dispute_row.ride_id, dispute_row.id,
    jsonb_build_object('notes', p_notes, 'dispute_id', dispute_row.id)
  );

  update public.disputes
  set status = 'resolved', resolution_type = 'refund', resolution_notes = p_notes,
      resolution_amount_cdf = p_amount_cdf, resolved_by_admin_id = p_admin_id,
      resolved_at = now(), updated_at = now()
  where id = dispute_row.id;

  insert into public.dispute_history (id, dispute_id, admin_id, action, from_status, to_status, notes, metadata)
  values (gen_random_uuid(), dispute_row.id, p_admin_id, 'refund_issued', dispute_row.status, 'resolved', p_notes,
          jsonb_build_object('amount_cdf', p_amount_cdf, 'new_balance', new_balance));

  perform public.create_system_notification(
    dispute_row.customer_id, 'Dispute resolved — refund issued',
    format('A refund of %s CDF has been issued to your wallet for your dispute.', p_amount_cdf),
    array['payment_update', 'wallet', 'system', 'general'], array['unread', 'pending', 'sent']
  );

  perform public.insert_admin_log('dispute_refund_customer', p_admin_id, dispute_row.id, 'disputes',
    jsonb_build_object('customer_id', dispute_row.customer_id, 'amount_cdf', p_amount_cdf, 'new_balance', new_balance));

  return jsonb_build_object('dispute_id', dispute_row.id, 'customer_id', dispute_row.customer_id,
    'status', 'resolved', 'resolution_type', 'refund', 'amount_cdf', p_amount_cdf, 'new_balance', new_balance);
end;
$$;


create or replace function public.dispute_charge_driver(
  p_admin_id uuid,
  p_dispute_id uuid,
  p_amount_cdf numeric,
  p_notes text default null
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  dispute_row public.disputes%rowtype;
  driver_user_id uuid;
  current_balance numeric(12,2);
  new_balance numeric(12,2);
begin
  perform public.assert_admin(p_admin_id);

  if p_amount_cdf is null or p_amount_cdf <= 0 then
    raise exception 'charge amount must be positive';
  end if;

  select * into dispute_row from public.disputes where id = p_dispute_id for update;
  if not found then
    raise exception 'dispute not found';
  end if;
  if dispute_row.status not in ('open', 'assigned', 'escalated') then
    raise exception 'dispute is not in a resolvable state (current status: %)', dispute_row.status;
  end if;
  if dispute_row.driver_id is null then
    raise exception 'dispute has no associated driver to charge';
  end if;

  select user_id, credit_balance into driver_user_id, current_balance
  from public.driver_profiles where id = dispute_row.driver_id for update;
  if not found then
    raise exception 'driver profile not found';
  end if;

  if coalesce(current_balance, 0) < p_amount_cdf then
    raise exception 'driver balance (%) is less than charge amount (%)', coalesce(current_balance, 0), p_amount_cdf;
  end if;

  update public.driver_profiles
  set credit_balance = credit_balance - p_amount_cdf, updated_at = now()
  where id = dispute_row.driver_id
  returning credit_balance into new_balance;

  insert into public.wallet_transactions (id, driver_id, type, amount, balance_after, reference_type, reference_id, description)
  values (gen_random_uuid(), dispute_row.driver_id, 'debit', p_amount_cdf, new_balance,
          'dispute_charge', dispute_row.id, coalesce(p_notes, 'Dispute resolution charge'));

  update public.disputes
  set status = 'resolved', resolution_type = 'driver_charged', resolution_notes = p_notes,
      resolution_amount_cdf = p_amount_cdf, resolved_by_admin_id = p_admin_id,
      resolved_at = now(), updated_at = now()
  where id = dispute_row.id;

  insert into public.dispute_history (id, dispute_id, admin_id, action, from_status, to_status, notes, metadata)
  values (gen_random_uuid(), dispute_row.id, p_admin_id, 'driver_charged', dispute_row.status, 'resolved', p_notes,
          jsonb_build_object('amount_cdf', p_amount_cdf, 'new_balance', new_balance));

  if driver_user_id is not null then
    perform public.create_system_notification(
      driver_user_id, 'Dispute resolution — charge applied',
      format('%s CDF has been deducted from your wallet as a dispute resolution charge.', p_amount_cdf),
      array['payment_update', 'wallet', 'system', 'general'], array['unread', 'pending', 'sent']
    );
  end if;

  perform public.insert_admin_log('dispute_charge_driver', p_admin_id, dispute_row.id, 'disputes',
    jsonb_build_object('driver_id', dispute_row.driver_id, 'amount_cdf', p_amount_cdf, 'new_balance', new_balance));

  return jsonb_build_object('dispute_id', dispute_row.id, 'driver_id', dispute_row.driver_id,
    'status', 'resolved', 'resolution_type', 'driver_charged', 'amount_cdf', p_amount_cdf, 'new_balance', new_balance);
end;
$$;

notify pgrst, 'reload schema';
