-- Add DB-side aggregation for customer wallet / welcome-bonus admin metrics.
-- Run this in the Supabase SQL editor (Dashboard → SQL editor).
--
-- Why this is needed:
--
-- GET /customer-wallet/admin/metrics and GET /customer-wallet/admin/welcome-bonus-metrics
-- currently sum customer_wallet_transactions / wallet_transactions client-side in Python,
-- capped at 50,000 rows as a safety net (added 2026-08-24). That cap means the reported
-- totals will silently under-report once transaction volume passes it — correct today,
-- not correct forever. PostgREST has no SUM()/COUNT() aggregation without a Postgres
-- function, so this migration adds one read-only RPC per metrics endpoint. Both are
-- SELECT-only, security definer (so a caller only needs EXECUTE, not table SELECT grants),
-- and admin-gated the same way every other admin RPC in this schema already is
-- (public.assert_admin, see sql/20260702_assert_admin_refresh.sql).
--
-- Response shape matches the existing Pydantic response models exactly
-- (CustomerWalletMetricsResponse / WelcomeBonusMetricsResponse in
-- app/services/customer_wallet/router.py) so the Python side only needs to swap the
-- query source, not the API contract.

create or replace function public.get_customer_wallet_metrics(p_admin_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_total_wallet_balance_cdf numeric;
  v_customers_with_balance integer;
  v_total_credits_issued_cdf numeric;
  v_total_first_ride_bonus_cdf numeric;
  v_total_referral_credit_cdf numeric;
  v_total_change_credit_cdf numeric;
  v_total_promo_credit_cdf numeric;
  v_transaction_count integer;
  v_first_ride_bonus_count integer;
  v_referral_credit_count integer;
  v_change_credit_count integer;
begin
  perform public.assert_admin(p_admin_id);

  select
    coalesce(sum(wallet_balance_cdf), 0),
    count(*)
  into v_total_wallet_balance_cdf, v_customers_with_balance
  from public.users
  where wallet_balance_cdf > 0;

  select
    coalesce(sum(amount_cdf) filter (where type = 'credit'), 0),
    coalesce(sum(amount_cdf) filter (where type = 'credit' and source = 'first_ride'), 0),
    coalesce(sum(amount_cdf) filter (where type = 'credit' and source = 'referral'), 0),
    coalesce(sum(amount_cdf) filter (where type = 'credit' and source = 'change'), 0),
    coalesce(sum(amount_cdf) filter (where type = 'credit' and source = 'promo'), 0),
    count(*),
    count(*) filter (where type = 'credit' and source = 'first_ride'),
    count(*) filter (where type = 'credit' and source = 'referral'),
    count(*) filter (where type = 'credit' and source = 'change')
  into
    v_total_credits_issued_cdf,
    v_total_first_ride_bonus_cdf,
    v_total_referral_credit_cdf,
    v_total_change_credit_cdf,
    v_total_promo_credit_cdf,
    v_transaction_count,
    v_first_ride_bonus_count,
    v_referral_credit_count,
    v_change_credit_count
  from public.customer_wallet_transactions;

  return jsonb_build_object(
    'total_wallet_balance_cdf', v_total_wallet_balance_cdf,
    'customers_with_balance', v_customers_with_balance,
    'total_credits_issued_cdf', v_total_credits_issued_cdf,
    'total_first_ride_bonus_cdf', v_total_first_ride_bonus_cdf,
    'total_referral_credit_cdf', v_total_referral_credit_cdf,
    'total_change_credit_cdf', v_total_change_credit_cdf,
    'total_promo_credit_cdf', v_total_promo_credit_cdf,
    'transaction_count', v_transaction_count,
    'first_ride_bonus_count', v_first_ride_bonus_count,
    'referral_credit_count', v_referral_credit_count,
    'change_credit_count', v_change_credit_count
  );
end;
$$;

create or replace function public.get_welcome_bonus_metrics(p_admin_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_customer_count integer;
  v_customer_total numeric;
  v_driver_count integer;
  v_driver_total numeric;
begin
  perform public.assert_admin(p_admin_id);

  select coalesce(count(*), 0), coalesce(sum(amount_cdf), 0)
  into v_customer_count, v_customer_total
  from public.customer_wallet_transactions
  where source = 'first_ride' and type = 'credit';

  select coalesce(count(*), 0), coalesce(sum(amount), 0)
  into v_driver_count, v_driver_total
  from public.wallet_transactions
  where reference_type = 'driver_welcome_bonus';

  return jsonb_build_object(
    'customer_welcome_bonus_count', v_customer_count,
    'customer_welcome_bonus_total_cdf', v_customer_total,
    'driver_welcome_bonus_count', v_driver_count,
    'driver_welcome_bonus_total_cdf', v_driver_total
  );
end;
$$;

notify pgrst, 'reload schema';
